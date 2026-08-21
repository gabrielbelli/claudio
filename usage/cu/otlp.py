"""Stream A normaliser: collector OTLP/JSON files -> canonical ledger rows.

The on-disk shape below was verified empirically against
`otel/opentelemetry-collector-contrib:0.158.0` rather than recalled:

    {"resourceLogs":[{"resource":{"attributes":[{"key":k,"value":{...}}]},
                      "scopeLogs":[{"scope":{},"logRecords":[
                          {"timeUnixNano":"...", "body":{...},
                           "attributes":[...]}]}]}]}

Two observations from that run drive the code:

  * `intValue` arrives as a JSON **string** (`"8421"`), while `doubleValue` is
    a JSON number.  Coercing is not optional.
  * A line of a live file may be a partial write — the exporter is writing
    while ingest reads.  (This used to say the exporter buffers ~4KB pages and
    does not flush on idle; that was false, and `cu/collector.py` records how
    it was disproved.  A record reaches disk when it is written; what is not
    atomic is the *host's view of one write in progress*.)  `read_file` never
    consumes past the final newline, which makes a partial write a no-op
    rather than a parse error, and makes ingest safe to run while the
    collector is up.
"""

import json
import os
import time

from . import ledger, models


def _attr_value(v):
    """Unwrap one OTLP AnyValue."""
    if not isinstance(v, dict):
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        # Arrives as a JSON string on the wire. Verified on 0.158.0.
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in v:
        try:
            return float(v["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "boolValue" in v:
        return bool(v["boolValue"])
    return None


def attrs_to_dict(attrs):
    out = {}
    for a in attrs or []:
        k = a.get("key")
        if k:
            out[k] = _attr_value(a.get("value"))
    return out


def _as_int(v, default=0):
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tokens(v):
    """(column, hash) for one token class.

    `column` is None when the attribute is absent or uncoercible; `hash` is
    the 0-coerced value `ledger.request_id` has always been given.  Keeping
    the two apart is the only way to stop recording an unstated figure as a
    confident zero without re-identifying every row already written.
    """
    n = _as_int(v, None)
    return n, (0 if n is None else n)


# Resource attributes that are already named columns, or Claude Code's own
# service metadata, or claudio's synthetic marker.  Everything else is a label
# the *user* invented with `claudio --tag` and is carried through verbatim.
_RESOURCE_NAMED = frozenset({
    "profile", "account", "host", "email",
    "service.name", "service.version", "synthetic",
})


def user_tags(res):
    """The user's own `--tag` labels: every resource attribute we do not name.

    A catch-all, not an allow-list, and that is the whole point.  Three
    allow-lists in a row have now deleted something Claude Code really sends:
    the collector's event-name filter (wrong name), the collector's
    `keep_keys` on resource attributes (every user tag), and
    `rows_from_payload`'s own hardcoded `a.get(...)` list (`prompt.id`).  The
    collector's was fixed by making its resource rule a deny-list; this one is
    the same fix one layer further in, and it was found by running a real
    session with `claudio run --tag verify=pipeline`: the tag reached
    raw/api_request.jsonl and was dropped here, silently, exactly as
    `prompt.id` had been.

    An allow-list over these keys can only ever delete the labels the user
    invented, which is the entire reason for tagging in the first place.
    """
    return {k: v for k, v in (res or {}).items()
            if k not in _RESOURCE_NAMED and not k.startswith("telemetry.sdk.")}


# The event name Claude Code sends on a per-request log record, and the only
# one this normaliser turns into a row.  It is a constant rather than an
# inlined literal so that the count of what was skipped can name it beside the
# names actually seen -- the collector already shipped once with a filter
# naming an event Claude Code does not send, which dropped every record, and
# the symptom was indistinguishable from an idle machine.
EVENT_NAME = "api_request"

# Every log-record attribute this normaliser reads.  It is **not** a filter --
# nothing consults it on the ingest path, and `rows_from_payload`'s `a.get(...)`
# calls remain the only thing that decides what a row carries.  It exists so
# that the receiver can count, at the door, the attributes that arrive and that
# nothing keeps.
#
# That counter is the fix for a structural blind spot: `raw/api_request.jsonl`
# used to be the collector's own *output*, so a field deleted upstream was
# invisible in the only file anyone read, and `prompt.id`, every user `--tag`
# and `agent.name` were each lost that way.  A list that could drift from the
# code would recreate the blind spot one level up, so a test derives the
# `a.get("…")` calls out of this module's source and compares the two sets.
STORED_ATTRS = frozenset({
    "event.name",
    "session.id", "prompt.id", "model", "cost_usd", "duration_ms",
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "query_source", "agent.name", "skill.name",
    "plugin.name", "mcp_server.name", "mcp_tool.name", "event.sequence",
    "terminal.type",
})

# Row schema version, stamped on every row.  1 is the first version to carry
# it; a row without the key predates it.  Bump only when a column changes
# meaning -- adding one does not, since absent and null are already the same
# answer to a reader that checks.
SCHEMA = 1


def _bump(stats, key, n=1):
    if stats is not None:
        stats[key] = stats.get(key, 0) + n


def rows_from_payload(payload, source="otlp", stats=None):
    """Turn one parsed OTLP/JSON line into zero or more ledger rows.

    Pass `stats` (a dict) to learn what was *not* turned into a row.  A record
    whose `event.name` is not `EVENT_NAME` is skipped and the byte offset
    advances past it regardless, so `--from-zero` does not bring it back
    either: if Claude Code renames the event, every record is dropped for ever
    and the only difference from a quiet machine is this counter.  Keys:
    `records` seen, `skipped`, and `events` -- {name: count} for every distinct
    event.name seen, so the drift can be *named* rather than guessed at.
    """
    rows = []
    now = time.time()
    for rl in payload.get("resourceLogs") or []:
        res = attrs_to_dict((rl.get("resource") or {}).get("attributes"))
        # A payload that declares itself fabricated is labelled as such, for
        # the whole of its life in the ledger.  `source` used to be the
        # constant "otlp" for everything, so two rows from `recv/synth-otlp`
        # -- $0.4871, model claude-opus-4-6, tags `profile: soc` and
        # `profile: probe` -- sat in the live ledger byte-identical in kind to
        # real ones and became the *top consumer* of a real account's report,
        # 96% of its attributed plan movement.  The generator's own README
        # forbids using it to prove a shape is handled; nothing stopped it
        # proving a consumption figure.  Only a self-declaration is honoured:
        # guessing which rows look synthetic would be the same class of
        # mistake as guessing query_source names.
        source_of = source
        marker = res.get("synthetic")
        if marker is not None and str(marker).strip().lower() not in ("", "0",
                                                                     "false"):
            source_of = "synthetic"
        for sl in rl.get("scopeLogs") or []:
            for rec in sl.get("logRecords") or []:
                a = attrs_to_dict(rec.get("attributes"))
                name = a.get("event.name")
                _bump(stats, "records")
                if stats is not None:
                    ev = stats.setdefault("events", {})
                    ev[name] = ev.get(name, 0) + 1
                if name != EVENT_NAME:
                    _bump(stats, "skipped")
                    continue
                try:
                    ts_ns = int(rec.get("timeUnixNano") or 0)
                except (TypeError, ValueError):
                    ts_ns = 0
                ts = ts_ns / 1e9

                # Two values per token class, and the split is load-bearing.
                # The *column* is None when the attribute is absent or
                # uncoercible, because a confident 0 is a claim that the
                # request used no cache when the truth is that nobody said.
                # The *hash* still sees the 0-coerced value: request_id has
                # always been computed that way, so changing it would give
                # every historic row a new id and the first re-ingest would
                # double the ledger.
                inp, h_inp = _tokens(a.get("input_tokens"))
                out, h_out = _tokens(a.get("output_tokens"))
                cread, h_cread = _tokens(a.get("cache_read_tokens"))
                ccreate, h_ccreate = _tokens(a.get("cache_creation_tokens"))

                model_raw = a.get("model") or ""
                model = models.normalise(model_raw)
                reported = _as_float(a.get("cost_usd"))

                email = res.get("email")
                row = {
                    # Hash inputs are the 0-coerced values, always: the id is
                    # an identity, not a reading, and re-identifying a row is
                    # how a ledger doubles.
                    "request_id": ledger.request_id(
                        a.get("session.id"), ts_ns, h_inp, h_out, h_cread,
                        h_ccreate, model_raw),
                    # The row's own schema version, so a later reader can tell
                    # a row written before a column existed from one whose
                    # value was genuinely absent.  Without it the two are
                    # identical on disk and no amount of care later recovers
                    # the difference.
                    "schema": SCHEMA,
                    "ts": ts,
                    # The nanosecond integer the payload actually carried.
                    # `ts` is a float of it and does NOT round-trip -- 2**63
                    # nanoseconds needs 19 significant digits and a double
                    # holds 15-17 -- so a row could not recompute its own
                    # request_id from its own columns.  Unrecoverable if left
                    # out: the raw file is rotated away and the integer is
                    # gone for good.
                    "ts_ns": ts_ns,
                    # Which Claude Code wrote the row.  Field names and units
                    # are the client's to change, and every drift this project
                    # has hit was invisible because nothing recorded which
                    # version produced the bytes.
                    "client_version": res.get("service.version"),
                    "profile": res.get("profile"),
                    "account": res.get("account"),
                    "host": res.get("host"),
                    "email": email.lower() if isinstance(email, str) else None,
                    "tags": user_tags(res),
                    "session_id": a.get("session.id"),
                    # The one documented correlation id, and the only field
                    # that ties a request to the turn the user actually asked
                    # for.  Every captured record carries it, the collector's
                    # `keep_keys` kept it deliberately -- and it was discarded
                    # here anyway, by a second, *implicit* allow-list: the
                    # hardcoded a.get() list below.  That duplication is why
                    # the receiver now keeps nothing back: this list is the
                    # only allow-list left, and what it drops is counted at the
                    # door.  Deliberately NOT part of request_id:
                    # rows written before this column existed must still dedupe
                    # onto the same id, or a re-ingest doubles the ledger.
                    "prompt_id": a.get("prompt.id"),
                    "model": model,
                    "model_raw": model_raw,
                    "query_source": a.get("query_source"),
                    # agent.name is the only field that names a subagent;
                    # query_source carries the type but no instance id.  It was
                    # documented by Claude Code and deleted by the collector's
                    # allow-list -- the third time this exact class of bug has
                    # dropped a field that was really being sent.
                    "agent_name": a.get("agent.name"),
                    "skill_name": a.get("skill.name"),
                    "plugin_name": a.get("plugin.name"),
                    "mcp_server": a.get("mcp_server.name"),
                    "mcp_tool": a.get("mcp_tool.name"),
                    "event_sequence": a.get("event.sequence"),
                    "terminal_type": a.get("terminal.type"),
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_read_tokens": cread,
                    "cache_creation_tokens": ccreate,
                    "duration_ms": _as_int(a.get("duration_ms"), None)
                    if a.get("duration_ms") is not None else None,
                    # Claude Code's own figure, recorded verbatim and never
                    # recomputed (`cu/pricing.py` is gone).  It is no longer
                    # turned into a `weight` column either: the weight existed
                    # only to split plan movement between requests, which
                    # needs the whole account and is the server's job.
                    "cost_usd_reported": reported,
                    "source": source_of,
                    "ingested_at": now,
                }
                rows.append(row)
    return rows


# The first bytes of every payload the collector writes.  Used to find the
# start of a complete record inside a line that begins with the tail of a dead
# one -- see `read_file`.
_PAYLOAD_HEAD = b'{"resourceLogs"'


def read_file(path, offset=0, stats=None):
    """Read complete lines from `offset`.

    Returns (rows, new_offset, malformed_count).  The offset only ever advances
    to the last newline seen, so a truncated trailing line -- the normal state
    of a file the collector is still writing -- is left for the next pass
    instead of being reported as corruption.

    `stats`, if given, is the dict `rows_from_payload` fills, plus `recovered`
    -- lines that only parsed after a torn fragment was cut off the front.
    """
    rows, malformed = [], 0
    try:
        size = os.path.getsize(path)
    except OSError:
        return rows, offset, malformed

    # A shrunken file means rotation reused the name; re-read from the start.
    # request_id dedupe makes that safe.
    if size < offset:
        offset = 0
    if size == offset:
        return rows, offset, malformed

    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read(size - offset)

    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return rows, offset, malformed          # nothing complete yet
    complete = chunk[: last_nl + 1]
    new_offset = offset + last_nl + 1

    for line in complete.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            # A process killed mid-write leaves a fragment with no newline
            # after it, so the NEXT payload is appended to that fragment and
            # the two arrive as one line.  Parsed naively, `<frag><record>`
            # fails and a complete, perfectly recoverable record dies with the
            # fragment -- silently, counted only as "1 malformed line".
            #
            # Cut back to the last payload start and retry.  `rfind`, not
            # `find`: the fragment is itself the head of a payload, so
            # searching forwards lands on the fragment's own opening brace and
            # changes nothing.
            #
            # NOT a skip-to-the-first-newline variant, which is the obvious
            # alternative and is wrong for a reason worth stating: there is no
            # newline inside `<frag><record>` at all, so that variant discards
            # the whole line and turns a reported loss into a silent one.
            cut = line.rfind(_PAYLOAD_HEAD)
            payload = None
            if cut > 0:
                try:
                    payload = json.loads(line[cut:].decode("utf-8", errors="replace"))
                except ValueError:
                    payload = None
            if payload is None:
                malformed += 1
                continue
            _bump(stats, "recovered")
        rows.extend(rows_from_payload(payload, stats=stats))
    return rows, new_offset, malformed


def raw_files(raw_dir):
    """The live file plus any rotated siblings, oldest first."""
    try:
        names = sorted(os.listdir(raw_dir))
    except OSError:
        return []
    out = [os.path.join(raw_dir, n) for n in names
           if n.startswith("api_request") and n.endswith(".jsonl")]
    # The live file last, so rotated history ingests in order.
    live = os.path.join(raw_dir, "api_request.jsonl")
    if live in out:
        out.remove(live)
        out.append(live)
    return out
