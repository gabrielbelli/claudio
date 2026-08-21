"""The wire format: what a shipper may send, and what each field means.

Three streams cross the wire and each record carries its own schema version,
because **a field added later must be distinguishable from a field that was
absent**.  That is not a hypothetical here.  There are two real stream-B
shapes on disk in this repository right now:

  usage/tests/fixtures/replay-real.jsonl   15 keys, and NO `schema` key at all
  usage/tests/fixtures/real-samples.jsonl  22 keys, `schema: 2`

The 15 keys are a strict subset of the 22 (asserted by a test against both
files), so a v1 record is not a v2 record with seven nulls — it is a record
written before those seven columns existed, and `agent: null` in a v2 record
is a genuine reading of nothing.  Collapsing the two is unrecoverable: once a
reader has written `None` for both, no later pass can tell them apart.  So
every read of a field that a schema might not have goes through `get`, which
returns `ABSENT`, and `ABSENT is not None`.

The one place an absent value is given a default is the stream-B `schema` key
itself: a stream-B record with no `schema` key is schema 1.  It is defaulted
rather than refused because 65 real records on disk are in exactly that shape
and the alternative is dropping all of them.  Nothing else defaults.

Nothing in this module does I/O.  It classifies, validates and refuses; it
never repairs, because a repaired record is a value written from an assumption
about a payload, which is this project's cardinal sin.
"""

import hashlib
import json
import math

# --------------------------------------------------------------------------
# stream names and schema versions
# --------------------------------------------------------------------------

STREAM_LEDGER = "ledger"          # stream A -- one row per API request
STREAM_SAMPLES = "samples"        # stream B -- one row per plan observation
STREAM_ATTEST = "attestations"    # stream C -- one row per closed window/host

STREAMS = (STREAM_LEDGER, STREAM_SAMPLES, STREAM_ATTEST)

# Stream A's version is `cu/otlp.SCHEMA`; stream B's is claudio's `_u_record`.
# A test compares both against the real fixtures rather than trusting these.
SCHEMA_LEDGER = 1
SCHEMA_SAMPLES = 2
SCHEMA_SAMPLES_V1 = 1             # the shape with no `schema` key
# Stream C is AUTHORED, not captured.  Nothing has ever emitted an
# attestation: there is no attestation anywhere in this repository's
# fixtures, no capture of one on any machine, and no code outside this
# directory that writes one.  It is designed here, and the suite's provenance
# ledger records every attestation it builds as AUTHORED -- a test asserts that
# the authored set is exactly {manifest, attestation}, so a third invented
# shape cannot slip in beside them.
SCHEMA_ATTEST = 1

SHIP_VERSION = 1                  # the batch manifest's own version


class _Absent(object):
    """Distinct from None: 'this record has no such key' vs 'the key is null'."""
    __slots__ = ()

    def __repr__(self):
        return "ABSENT"

    def __bool__(self):
        return False


ABSENT = _Absent()


def get(rec, key):
    """The value, or ABSENT if the record does not carry the key at all."""
    return rec[key] if key in rec else ABSENT


# --------------------------------------------------------------------------
# the columns, as they are on disk
# --------------------------------------------------------------------------
#
# These lists are pinned by tests against the real fixtures -- `LEDGER_FIELDS`
# against the actual output of `cu.otlp.rows_from_payload` over
# `real-api-request.jsonl`, and the two sample lists against the two real
# sample files.  A hand-maintained copy that nothing checks is the fourth
# place to forget, which is how six fields were dropped in silence.

# 30 columns from cu/otlp.py, plus `account_uuid`, stamped by the shipper.
# D9: stream A carries no account UUID of its own and its `account` is a label
# `--tag account=` can overwrite, so the shipper stamps the UUID on the
# machine, where `.claude.json` is readable.  A row that arrives without it
# cannot be placed against an account and is REFUSED -- never joined by email
# or by label, because that guess is what "never sum across accounts" exists
# to prevent.
LEDGER_STAMP = "account_uuid"

LEDGER_FIELDS = (
    "request_id", "schema", "ts", "ts_ns", "client_version", "profile",
    "account", "host", "email", "tags", "session_id", "prompt_id", "model",
    "model_raw", "query_source", "agent_name", "skill_name", "plugin_name",
    "mcp_server", "mcp_tool", "event_sequence", "terminal_type",
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "duration_ms", "cost_usd_reported", "source",
    "ingested_at",
)

SAMPLE_FIELDS_V1 = (
    "ts", "account_uuid", "account_email", "organization_uuid",
    "rate_limit_tier", "five_hour_pct", "five_hour_resets_at",
    "seven_day_pct", "seven_day_resets_at", "session_id", "model",
    "session_cost_usd", "profile", "client", "project",
)

SAMPLE_FIELDS_V2 = SAMPLE_FIELDS_V1 + (
    "schema", "reason", "host", "account", "prompt_id", "agent", "tags",
)

# Written only when the 1023-byte line cap fired, so its absence is normal and
# is not a schema difference.
SAMPLE_FIELDS_CONDITIONAL = ("truncated",)

ATTEST_FIELDS = (
    "schema", "kind", "account_uuid", "window", "resets_at", "host",
    "machine_id", "up_spans", "shipped", "recv_events",
    "recv_unknown_attributes", "ledger_offset", "samples_offset",
    "emitted_at",
)

MANIFEST_FIELDS = (
    "ship", "stream", "file", "ident", "from", "to", "count", "sent_at",
    "agent", "host", "machine_id", "reset",
)

# `reset` is the client saying "I restarted this file from byte zero".  It is
# OPTIONAL and defaults to False, because a shipper written before this key
# existed sends manifests without it and refusing those would jam every one of
# its streams over a field that only ever makes the door *less* likely to
# discard.  It exists because the door cannot infer it: `cu.ship._ship_stream`
# detects `start > size` and resumes at 0, but an in-place rewrite that keeps
# the first 256 bytes leaves `ident` -- dev, ino and the head hash -- unchanged,
# so the door recognised the re-sent range as one it already held and dropped
# the batch with a 200.  The client holds the evidence; this carries it.
MANIFEST_RESET = "reset"

# `host` and `machine_id` are required in the manifest and are NOT in the
# decided example line, deliberately: stream B's v1 shape has no `host` key of
# its own (proven -- `replay-real.jsonl` carries 15 keys and `host` is not one
# of them), and the agreed stream-B idempotency key is a hash of the record
# plus the host.  Without a host on the envelope that key cannot be formed for
# the 65 records that actually exist.


def sample_fields(schema):
    return SAMPLE_FIELDS_V1 if schema == SCHEMA_SAMPLES_V1 else SAMPLE_FIELDS_V2


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def classify(rec):
    """Which stream a record belongs to, by shape -- or None.

    By shape and not by schema number, because stream A and stream B v1 both
    read as schema 1.  A manifest names the stream and is believed when
    present; this is the fallback for a record that arrives without one, and
    for the assertion that the two agree.
    """
    if not isinstance(rec, dict):
        return None
    if "request_id" in rec:
        return STREAM_LEDGER
    if "kind" in rec and "resets_at" in rec:
        return STREAM_ATTEST
    if "five_hour_resets_at" in rec or "five_hour_pct" in rec:
        return STREAM_SAMPLES
    return None


def schema_of(rec, stream):
    """The record's own schema version, or None if it does not state one.

    Stream B is the single exception, documented at the top of this file: no
    `schema` key means schema 1, because 65 real records are in that shape.
    """
    v = get(rec, "schema")
    if v is ABSENT:
        return SCHEMA_SAMPLES_V1 if stream == STREAM_SAMPLES else None
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def absent_fields(rec, stream, schema):
    """Fields this stream's CURRENT schema defines that the record lacks.

    This is the answer to "was the field added later, or was it absent?" --
    carried into the report so a window built from old records says so rather
    than reporting seven nulls that look like readings.
    """
    if stream == STREAM_LEDGER:
        want = LEDGER_FIELDS + (LEDGER_STAMP,)
    elif stream == STREAM_SAMPLES:
        want = SAMPLE_FIELDS_V2
    elif stream == STREAM_ATTEST:
        want = ATTEST_FIELDS
    else:
        return ()
    return tuple(f for f in want if f not in rec)


# --------------------------------------------------------------------------
# value guards
# --------------------------------------------------------------------------
#
# Every guard below mirrors one claudio already applies to the same value on
# the way in.  CLAUDE.md's rule for the recorder -- "marks read back out of
# the state file get exactly the validation an incoming one gets" -- is the
# same rule one hop further along the wire: a shared usage_dir, a hand edit or
# a machine with a different clock produces a record that passed the
# recorder's guard on a different machine, or bypassed it entirely.

EPOCH_FLOOR = 1000000000          # `replay-real.jsonl` row 16: resets_at == 1
HORIZON_S = 30 * 86400            # a millisecond epoch is ~55 000 years out
MAX_PCT_DIGITS = 18               # claudio: round|tostring|test("^[0-9]{1,18}$")


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def valid_pct(v):
    """(ok, reason).  A percentage of a plan window.

    The 18-digit rule is claudio's own, and it exists because a quantised
    `used_percentage: 1e30` becomes a high-water mark nothing can ever beat --
    the window freezes for good, with no self-healing path.

    `> 100` is refused and counted rather than kept, and the direction is a
    judgement worth stating: a peak of 105 would inflate that window's
    movement and therefore its residual for ever, and a residual is the one
    number here that is read as evidence of an unwatched machine.  No fixture
    contains one; if a real payload ever does, this refusal is loud on the
    first report rather than quiet for ever.
    """
    if not _is_number(v):
        return False, "pct-not-a-number"
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return False, "pct-not-finite"
    if v < 0:
        return False, "pct-negative"
    if len(str(int(round(v)))) > MAX_PCT_DIGITS:
        return False, "pct-too-large"
    if v > 100:
        return False, "pct-out-of-range"
    return True, None


def valid_up_spans(v):
    """(ok, reason).  An attestation's uptime spans, as `[start, stop, clean]`.

    Refused as a WHOLE RECORD, by name, rather than parsed element by element.
    `coverage._clip` used to drop a span it could not read with a bare
    `continue` and no counter, and `check` never looked at `up_spans` at all,
    so a malformed-span attestation was accepted cleanly and counted as
    `attestations_accepted` -- and the consequence was not an understated
    number, it was an inverted verdict.  Reproduced with the real alpha data
    and one attestation whose span endpoints were strings: fraction 0.0,
    `hosts_reporting` empty, `hosts_dark` {'darwin'}, refused `no-coverage`,
    and the attribution printing "no machine was observed listening during
    this window".  The machine said the opposite; the parser could not read it.

    ABSENT or `[]` is not an error: that is a host saying it was up for none of
    the window, which is the `dark` state and a real thing to say.  The
    distinction this restores is exactly "named no spans" against "the spans
    were unreadable".
    """
    if v is ABSENT or v is None:
        return True, None
    if not isinstance(v, (list, tuple)):
        return False, "attestation-unreadable-spans"
    for s in v:
        if not isinstance(s, (list, tuple)) or len(s) < 2:
            return False, "attestation-unreadable-spans"
        if not _is_number(s[0]) or not _is_number(s[1]):
            return False, "attestation-unreadable-spans"
        if len(s) >= 3 and not isinstance(s[2], bool):
            return False, "attestation-unreadable-spans"
    return True, None


def valid_epoch(v, now):
    """(ok, reason).  A `resets_at`, or an attestation's span endpoint."""
    if not _is_number(v):
        return False, "epoch-not-a-number"
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return False, "epoch-not-finite"
    if v < EPOCH_FLOOR:
        return False, "epoch-below-floor"
    if v > now + HORIZON_S:
        return False, "epoch-beyond-horizon"
    return True, None


# --------------------------------------------------------------------------
# record-level checks
# --------------------------------------------------------------------------

def check(rec, stream, now):
    """(ok, reason) for a whole record.

    Only what makes the record *placeable* is checked here: its stream, its
    schema, and its account.  Per-window values are checked in `window.py`,
    where a bad `five_hour_resets_at` costs the 5-hour reading and leaves the
    7-day one standing -- `replay-real.jsonl` row 16 is exactly that record
    and dropping it whole would lose nothing, but dropping a record whose
    other window is fine would.
    """
    if not isinstance(rec, dict):
        return False, "not-an-object"
    shape = classify(rec)
    if shape is None:
        return False, "unclassifiable"
    if shape != stream:
        return False, "stream-mismatch"
    schema = schema_of(rec, stream)
    if schema is None:
        return False, "no-schema"
    if stream == STREAM_LEDGER and schema != SCHEMA_LEDGER:
        return False, "schema-unsupported"
    if stream == STREAM_SAMPLES and schema not in (SCHEMA_SAMPLES_V1,
                                                   SCHEMA_SAMPLES):
        return False, "schema-unsupported"
    if stream == STREAM_ATTEST and schema != SCHEMA_ATTEST:
        return False, "schema-unsupported"
    uuid = get(rec, "account_uuid")
    if uuid is ABSENT:
        # Stream A: the shipper did not stamp (D9).  Stream B: impossible in
        # both real shapes, so this is a hand-written or corrupted line.
        return False, "no-account-uuid"
    if not isinstance(uuid, str) or not uuid:
        return False, "account-uuid-not-a-string"
    if stream == STREAM_LEDGER:
        rid = get(rec, "request_id")
        if not isinstance(rid, str) or not rid:
            return False, "no-request-id"
    if stream == STREAM_ATTEST:
        ok, why = valid_epoch(get(rec, "resets_at"), now)
        if not ok:
            return False, why
        if get(rec, "window") not in ("5h", "7d"):
            return False, "attestation-unknown-window"
        if not isinstance(get(rec, "machine_id"), str):
            return False, "attestation-no-machine-id"
        # `emitted_at` is load-bearing now: `ingest._add_attestation` keeps the
        # later-EMITTED record, because batches arrive out of order routinely
        # and last-arrived silently flipped a window from attributed to "no
        # machine was observed listening".  A field a decision rests on has to
        # be validated like one.
        ok, why = valid_epoch(get(rec, "emitted_at"), now)
        if not ok:
            return False, "attestation-" + why
        ok, why = valid_up_spans(get(rec, "up_spans"))
        if not ok:
            return False, why
    return True, None


def manifest_check(m):
    """(ok, reason) for a batch manifest -- the only record a shipper authors."""
    if not isinstance(m, dict):
        return False, "not-an-object"
    if m.get("ship") != SHIP_VERSION:
        return False, "ship-version-unsupported"
    if m.get("stream") not in STREAMS:
        return False, "unknown-stream"
    for f in ("from", "to", "count"):
        if not isinstance(m.get(f), int) or isinstance(m.get(f), bool):
            return False, "manifest-%s-not-an-integer" % f
    if m["to"] < m["from"]:
        return False, "manifest-range-inverted"
    if not isinstance(m.get("host"), str) or not m["host"]:
        return False, "manifest-no-host"
    if not isinstance(m.get("machine_id"), str) or not m["machine_id"]:
        return False, "manifest-no-machine-id"
    if MANIFEST_RESET in m and not isinstance(m[MANIFEST_RESET], bool):
        # Present and not a boolean is a manifest this door cannot read, and
        # the value decides whether a batch is written or dropped.  Absent is
        # fine (see MANIFEST_RESET): a truthy string would otherwise be read as
        # a reset by anything that only asks `if m.get("reset")`.
        return False, "manifest-reset-not-a-boolean"
    return True, None


def manifest_reset(m):
    """Did the client say it restarted this file?  Absent means no."""
    return isinstance(m, dict) and m.get(MANIFEST_RESET) is True


# --------------------------------------------------------------------------
# idempotency keys
# --------------------------------------------------------------------------

def canonical(rec):
    """The record as one deterministic string.  Keys sorted, no whitespace."""
    return json.dumps(rec, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def sample_key(rec, host):
    """sha256 over the whole record plus the shipping host.

    The whole record, not `(account, ts, five_hour_pct, seven_day_pct)`: that
    proposal is falsified by the fixture.  `replay-real.jsonl` rows 0 and 1
    both carry ts 1786381901 with five_hour_pct 5 and 4, from two different
    sessions -- under the tuple key one of the two real records disappears.

    Plus the host because stream B's v1 shape has no host column, so two
    machines observing the identical snapshot produce byte-identical lines,
    and collapsing them would erase the evidence that two machines were up.
    Deduping stream B is housekeeping either way: the only operator applied to
    it is `max`, which is idempotent.
    """
    return hashlib.sha256(
        (canonical(rec) + "\x00" + (host or "")).encode("utf-8")).hexdigest()


def attest_key(rec):
    """(account_uuid, kind, resets_at, machine_id).  Last write wins."""
    return (rec.get("account_uuid"), rec.get("kind"), rec.get("resets_at"),
            rec.get("machine_id"))


def ledger_fingerprint(row):
    """The fields that distinguish two requests sharing a `request_id`.

    Deliberately identical to `cu.ledger.fingerprint`, and a test imports that
    function and compares the two over the real capture.  It is repeated here
    rather than imported so that `server/` stands alone with no path games,
    and the drift is a test failure rather than a discovery.
    """
    return (row.get("prompt_id"), row.get("agent_name"), row.get("duration_ms"),
            row.get("cost_usd_reported"), row.get("query_source"))
