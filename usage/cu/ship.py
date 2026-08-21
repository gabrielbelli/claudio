"""The shipper: what leaves this machine, to whom, and what never does.

It runs as a tick inside `recv/otlp-recv` -- the process that is already alive
exactly when data exists -- plus one unconditional final pass before the
receiver's idle exit.  There is no cron, no launchd and no systemd unit,
because claudio has none of those and is not gaining one: the supervision
argument was settled when the receiver stopped being a container.

Five properties carry the design, and every one of them is a refusal.

**It ships the file's own bytes.**  The wire format IS the file format.  A
stream-B line is POSTed byte for byte as it sits in `samples.jsonl`, and a
stream-A line is its bytes with ONE key appended textually (see `stamp`), so a
captured request body is `diff`-able against the file that produced it.
Nothing is re-serialised: a `json.loads`/`json.dumps` round trip reorders keys,
reformats floats (`1.0` and `1`), re-escapes non-ASCII and would silently drop
any key a future Claude Code adds before this reader learns about it -- a third
schema, invented in transit, with nothing to compare it against.

**It never reads `raw/`.**  That file is the OTLP payload verbatim, it is
larger, and it can contain prompt and response bodies (`OTEL_LOG_USER_PROMPTS`
is the user's own opt-in and the receiver does not redact it).  The shipper
touches `ledger.jsonl` and `samples.jsonl` and nothing else, so a prompt body
cannot leave this machine through it.

**It fails closed on the destination.**  There is no default URL and no
"ship everything" mode.  An account with no `account.<name>.ship_url` in the
GLOBAL conf ships nothing at all, and claudio says why on the next run.  This
is the load-bearing one: `usage/tests/fixtures/real-api-request.jsonl`
normalises to eight rows carrying THREE different accounts interleaved in one
file, so a shipper that sent a ledger unfiltered to a work destination would
publish two personal accounts to an employer.

**One account per destination, decided by identity and not by a label.**  See
`place_row`: stream B carries `account_uuid` and is matched on it exactly;
stream A carries no UUID at all, so it is matched on the `email` tag against
the address in that account's own `.claude.json`, and the UUID is stamped on
here -- on the machine, which is the only place `.claude.json` is readable.
`account` is a label `--tag account=` overwrites and is never matched on.

**The offset advances only on an acknowledged write.**  A failed POST leaves
the stored offset exactly where it was, so the next tick resends the same byte
range; that is safe because both streams are deduped server-side (stream A on
`request_id`, stream B on `wire.sample_key`).  Resending costs a parse.  The
alternative -- advancing and hoping -- is silent non-delivery, which is this
project's cardinal sin.

The wire constants below are spelled here rather than imported from
`server/srv/wire.py`, for that module's own stated reason: `server/` stands
alone with no path games, and drift between the two is caught as a test
failure rather than discovered.  `usage/tests/test_all.py` imports `srv.wire`,
compares every constant, and pushes a real shipped batch through
`srv.ingest.Ingest.add_batch` -- so "the door accepts what the shipper emits"
is asserted against the door itself and not against a description of it.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import collector, config, tail

# --------------------------------------------------------------------------
# the wire, as `server/srv/wire.py` defines it (pinned by test, not by trust)
# --------------------------------------------------------------------------

SHIP_VERSION = 1
STREAM_LEDGER = "ledger"
STREAM_SAMPLES = "samples"
LEDGER_STAMP = "account_uuid"

AGENT = "claudio-ship/1"
CONTENT_TYPE = "application/x-ndjson"

# One POST carries at most this many lines of the file.  A pass loops until the
# file is drained, so this bounds memory and body size without bounding what
# gets shipped.  Chosen against the measured rate: a heavy day is ~14 000
# stream-A rows across three accounts, so a fully idle machine catches up in
# tens of POSTs and a 15-second tick never carries more than a handful.
MAX_LINES_PER_BATCH = 500

# Loop guard.  Not a limit anyone should reach -- 500 * 200 lines is two orders
# of magnitude above a day's worst case -- it exists so a bug that stops
# advancing the offset cannot spin inside a watchdog tick.
MAX_BATCHES_PER_PASS = 200

TIMEOUT = 10.0

# The stamp is spliced into a JSON line as text, so the value must not be able
# to carry a quote, a backslash or a control character into it.  A UUID cannot;
# a corrupted or hand-edited `.claude.json` could.  Refused by name rather than
# escaped: an account whose UUID does not look like one is not an account this
# shipper can place a row against.
_UUID_OK = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


# --------------------------------------------------------------------------
# claudio's config, read where claudio keeps it
# --------------------------------------------------------------------------
#
# The destination keys are `account.<name>.ship_url` and
# `account.<name>.ship_token`, and they are read from the GLOBAL conf ONLY.
# That is `_telemetry_conf`'s account inversion taken to its conclusion: there
# is no plain `ship_url` key at any layer, because a `claudio.conf` inside a
# repository someone else wrote must never be able to name where this
# machine's telemetry goes.  claudio implements the same rule on its own side
# (`_ship_conf`) and `test.sh` mirrors the `account.<x>.otel` precedence cases
# onto it, case for case.
#
# The mode is read the same way and from the same file: `account.<name>.logging`
# if that account names one, else the plain global `logging`.  The project and
# profile layers are deliberately NOT read here, and the reason is that they
# cannot be: this process has no working directory and no profile -- it is a
# machine-wide receiver spawned by whichever session got there first.  It costs
# nothing in practice, because a project or profile that switched logging OFF
# never produced the rows in the first place; what it does cost is a project
# that switches logging *up* to `remote` while the machine says `local`, and
# claudio names that case on stderr rather than letting it read as shipping.


# Imported, not reimplemented, and re-exported under the names this module has
# always used them by.  Both were defined here until `collector` needed them
# too; `ship` already imports `collector`, so the reader had to move DOWN to a
# module neither imports rather than be copied.  `tail.file_identity` is the
# precedent and the test is the same one: asserted to be the same function
# object, because two that merely agree today is what put 57% of a machine's
# captured spend permanently out of reach.
conf_path = config.conf_path
read_conf = config.read_conf


def accounts_dir():
    return os.environ.get("CLAUDIO_ACCOUNTS") or os.path.join(
        os.path.expanduser("~"), ".claudio", "accounts")


def account_identity(path):
    """(account_uuid, email) out of an account directory's `.claude.json`.

    `json.load`, not the `sed` claudio uses for the same field: a missing
    optional dependency must never cost a command, and here there is none --
    this side is Python already.  Either value may be None; the file survives a
    logout, so a directory that is no longer logged in still answers.  That is
    the right direction: a row already on disk belongs to the identity that
    produced it, whether or not the login is still live.
    """
    try:
        with open(os.path.join(path, ".claude.json"), "r", encoding="utf-8",
                  errors="replace") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None, None
    acct = doc.get("oauthAccount") if isinstance(doc, dict) else None
    if not isinstance(acct, dict):
        return None, None
    uuid = acct.get("accountUuid")
    email = acct.get("emailAddress")
    return (uuid if isinstance(uuid, str) and uuid else None,
            email if isinstance(email, str) and email else None)


class Destination(object):
    """One account, one URL.  There is no other shape a destination can take."""

    __slots__ = ("name", "url", "token", "account_uuid", "account_email")

    def __init__(self, name, url, token, account_uuid, account_email):
        self.name = name
        self.url = url
        self.token = token
        self.account_uuid = account_uuid
        self.account_email = account_email

    def __repr__(self):
        return "Destination(%s -> %s)" % (self.name, self.url)


def destinations(conf=None, accounts=None):
    """([Destination], [(account, reason)]) -- and the refusals are the point.

    Every account that names a `ship_url` and is not shipped comes back in the
    second list with a reason, because "configured a destination and nothing
    arrives" is precisely the state this whole file exists to make impossible
    to reach quietly.
    """
    conf = read_conf() if conf is None else conf
    accounts = accounts_dir() if accounts is None else accounts
    out, refused = [], []
    global_mode = conf.get("logging", "")

    # Parsed from the ENDS, never by splitting on every dot.  claudio's
    # `_validate_name` allows dots INSIDE an account name -- its own comment
    # says so, and says claudio never parses `account.…` back apart because it
    # only ever builds the key from a resolved name.  This is the one reader
    # that does parse it, so a `len(parts) == 3` guard would silently skip
    # `account.my.acct.ship_url` and ship nothing for that account with no
    # refusal named: the exact shape of loss this file exists to prevent.  The
    # suffix is fixed, so stripping both ends is unambiguous at any depth.
    names = sorted({k[len("account."):-len(".ship_url")] for k in conf
                    if k.startswith("account.") and k.endswith(".ship_url")})
    for name in names:
        # The name becomes a path component.  claudio validates it on the way
        # in; nothing validated the copy sitting in a config file, and a
        # hand-edited `account.../../x.ship_url` would otherwise read another
        # directory's login.  Same rejections as `_validate_name`.
        if (not name or name.startswith(".") or "/" in name or "\\" in name
                or name.strip() != name or " " in name or "\t" in name):
            refused.append((name or "(empty)", "not a usable account name"))
            continue
        url = conf.get("account.%s.ship_url" % name, "").strip()
        if not url:
            refused.append((name, "ship_url is empty"))
            continue
        if not (url.startswith("http://") or url.startswith("https://")):
            refused.append((name, "ship_url is not an http(s) URL: %s" % url))
            continue
        mode = conf.get("account.%s.logging" % name, global_mode).strip()
        if mode != "remote":
            # Both halves are named.  A destination with `logging=local` is a
            # configuration that reads as shipping and does not ship, and
            # guessing the other way -- shipping because a URL is present --
            # would make `logging=local` upload records, which is the same sin
            # pointed the other way.
            refused.append((name, "logging is %r, not remote"
                                  % (mode or "unset")))
            continue
        uuid, email = account_identity(os.path.join(accounts, name))
        if not uuid:
            refused.append((name, "no account UUID in %s/.claude.json "
                                  "(never logged in?)"
                                  % os.path.join(accounts, name)))
            continue
        if not _UUID_OK.match(uuid):
            refused.append((name, "account UUID is not a plain identifier"))
            continue
        out.append(Destination(name, url,
                               conf.get("account.%s.ship_token" % name, "").strip(),
                               uuid, email))
    return out, refused


# --------------------------------------------------------------------------
# placement and stamping
# --------------------------------------------------------------------------

def place_row(rec, dest):
    """Does this stream-A row belong to `dest`?  -- True / False / None.

    None means "this row names no account at all", which is a third answer and
    not a no: it is a row claudio produced with no account resolved, or one
    whose `email=` tag a config file suppressed (both documented, both the
    user's own choice).  It can never be placed against any destination, so it
    is counted and named in `ship.warn` rather than quietly passed over.

    **Matched on `email`, never on `account`.**  `account` is a label the user
    picked and `--tag account=` overwrites; `email` is read by claudio out of
    the resolved account's own `.claude.json`, which is the same file this
    shipper reads the UUID from, so the two sides of the match come from one
    source.  Measured on the only real multi-account capture there is: the
    email match places 8 of 8 rows into the right one of three accounts
    (`real-api-request.jsonl`; the alternative join, `session_id` against
    stream B, places 7 of 8 and leaves the `sdk` session unplaceable for ever,
    because a session that never moved the plan percentage produces no sample).

    The honest limit, stated rather than buried: `email` is a resource
    attribute, so a `tag=email=` line in a config file inside a repository
    could name another of this user's addresses and misplace a row.  The
    destination cannot be redirected that way -- `ship_url` is global-conf-only
    -- but the selection can.  Closing it needs an identity Claude Code emits
    itself, which it does not today.
    """
    email = rec.get("email")
    if not isinstance(email, str) or not email:
        return None
    return email == dest.account_email


def stamp(raw, account_uuid):
    """The line's own bytes with `"account_uuid":"..."` appended.  Or None.

    Textual, not a re-serialisation, and that is the whole point of requirement
    2: strip the suffix this function adds and you have the file's bytes back,
    byte for byte, so a captured request body is diffable against
    `ledger.jsonl`.  `json.dumps(json.loads(line))` would also produce valid
    JSON and would quietly reorder keys, reformat every float and re-escape
    every non-ASCII character -- a third schema, invented in transit.

    The server needs the key IN the record: `wire.check` refuses a stream-A row
    without it (`no-account-uuid`) and `ingest` places by it and by nothing
    else.  So the stamp is not decoration; it is what makes the row placeable
    at all, and D9 says it happens here because `.claude.json` is readable here
    and nowhere downstream.
    """
    if not _UUID_OK.match(account_uuid or ""):
        return None
    s = raw.rstrip(b"\r\n").rstrip()
    if not s.endswith(b"}"):
        return None
    add = b'"%s":"%s"}' % (LEDGER_STAMP.encode("ascii"),
                           account_uuid.encode("ascii"))
    if s == b"{}":
        return b"{" + add
    return s[:-1] + b"," + add


# --------------------------------------------------------------------------
# the batch
# --------------------------------------------------------------------------

def manifest(stream, path, ident, start, end, count, host, machine, now,
             reset=False):
    """`wire.MANIFEST_FIELDS`, all twelve, in the order that module lists them.

    `reset` is the signal this side already computes and used to throw away.
    When a file is truncated or replaced under a live offset this shipper
    resumes at byte 0 -- correctly, because both streams dedupe server-side --
    but the file *identity* is unchanged by an in-place rewrite that keeps the
    first 256 bytes, so the door recognised the range as one it already held
    and skipped the whole batch with a 200.  Same evidence, opposite failure
    direction: on this side re-reading costs a parse, on that side discarding
    costs the records.  Saying "I restarted this file" is what lets the door
    err the way everything else in this system errs -- append, and let
    `request_id`/`sample_key` dedupe.
    """
    return {"ship": SHIP_VERSION, "stream": stream,
            "file": os.path.basename(path), "ident": ident,
            "from": int(start), "to": int(end), "count": int(count),
            "sent_at": int(now), "agent": AGENT,
            "host": host, "machine_id": machine, "reset": bool(reset)}


def read_ack(raw):
    """The door's response body as a dict, or None.  Never raises.

    Defensive because it is a *reader of somebody else's output*: a proxy's
    HTML error page, a truncated body and a 200 with no body at all must each
    cost the extra detail and nothing else.
    """
    if not raw:
        return None
    try:
        doc = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _ack_detail(code, ack):
    """`HTTP 507: disk-nearly-full -- 12 bytes free, reserve is 20`.

    The door writes `reason` and `detail` into every refusal and its docstring
    calls them "the two numbers that go wrong invisibly".  They were never
    read: a 409 `identity` -- the shipper aimed a file at the wrong
    destination, which jams that stream for ever because the offset never
    advances -- reached the user as the four characters `409`.
    """
    out = "HTTP %s" % code
    if not ack:
        return out
    reason = ack.get("reason") or ack.get("refused")
    if isinstance(reason, str) and reason:
        out += ": %s" % reason
    detail = ack.get("detail")
    if isinstance(detail, str) and detail:
        out += " -- %s" % detail
    free = ack.get("disk_free_bytes")
    if isinstance(free, int) and not isinstance(free, bool):
        out += " (store has %d byte(s) free)" % free
    return out


def post_batch(url, token, man, records, timeout=TIMEOUT):
    """POST one batch as NDJSON: the manifest, then the record lines.

    (ok, detail, ack).  NDJSON because the files are NDJSON -- a JSON array
    would mean re-serialising or hand-splicing commas between raw lines, and
    the first of those is the drift this shipper exists to avoid.

    **The body is read on both paths.**  It used to be read on neither, so
    every early warning the door was built to send arrived nowhere: the offset
    it durably holds (the one cheap cross-check that catches a discarded
    batch), `disk_free_bytes` on every response including the refusals, and the
    `reason`/`detail` of a refusal.  Reading it costs one `read()` on a body
    the door has already sent.

    The bearer token is a TENANT LABEL for routing and nothing more.  It is not
    engineered as a security boundary here: the network is treated as already
    trusted, there is no mTLS, no CA and no rotation, and if that assumption is
    wrong the fix is at the network layer and not in this function.
    """
    body = json.dumps(man, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8") + b"\n"
    for rec in records:
        body += rec + b"\n"
    headers = {"Content-Type": CONTENT_TYPE}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            code = fh.status
            try:
                ack = read_ack(fh.read())
            except OSError:
                ack = None
            if 200 <= code < 300:
                return True, None, ack
            return False, _ack_detail(code, ack), ack
    except urllib.error.HTTPError as exc:
        try:
            ack = read_ack(exc.read())
        except Exception:                    # noqa: BLE001 -- a body, not a fact
            ack = None
        return False, _ack_detail(exc.code, ack), ack
    except urllib.error.URLError as exc:
        return False, "%s" % (exc.reason,), None
    except Exception as exc:                 # a shipper fault is never fatal
        return False, "%s: %s" % (type(exc).__name__, exc), None


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------

class Report(object):
    """What one pass did.  Read by the tick, by `doctor`, and by the tests."""

    __slots__ = ("shipped", "scanned", "unidentified", "malformed",
                 "failures", "refusals", "partial", "destinations", "batches",
                 "unplaced", "unplaced_addresses", "held", "disagreements",
                 "_placed_ids", "_unplaced_ids")

    def __init__(self):
        self.shipped = {}          # (dest, stream) -> records delivered
        self.scanned = {}          # (dest, stream) -> bytes consumed
        self.unidentified = 0      # stream-A rows naming no account at all
        self.malformed = 0         # lines that would not parse
        self.failures = []         # (dest, stream, detail)
        self.refusals = []         # (account, reason) -- configured, not shipping
        self.partial = []          # accounts shipping stream B only
        self.destinations = 0
        self.batches = 0
        # THE THIRD ANSWER, which used to be counted nowhere.  `place_row`
        # returns True (ship it), None (the row names no account -- counted as
        # `unidentified`) and False (the row names an address no destination
        # has).  False fell through `if not placed: continue` with no counter
        # anywhere while the offset advanced past it, so a machine whose
        # account had been re-logged under a new address, or whose rows carried
        # `tag=email=redacted`, consumed its whole ledger and shipped nothing
        # with `destinations: 1`, `unidentified: 0`, no failure, no refusal and
        # neither ship.err nor ship.warn on disk.
        self.unplaced = 0          # rows NO destination in this pass claimed
        self.unplaced_addresses = {}       # address -> rows, for the message
        self.held = []             # (dest, bytes) -- offset deliberately kept
        self.disagreements = []    # (dest, stream, detail) -- ack vs our offset
        # Per-pass, and deduped on `request_id` so a row scanned by three
        # destinations is one row.  A row is unplaced only if EVERY destination
        # refused it, which is why this is decided once at the end of the pass
        # and not inside a per-destination scan.
        self._placed_ids = set()
        self._unplaced_ids = {}

    @property
    def delivered(self):
        return sum(self.shipped.values())


def _select(fh, dest, stream, limit, report):
    """Read up to `limit` complete lines; return (records, bytes-consumed).

    A final line with no newline is a write in progress and is NOT consumed:
    the offset stops in front of it and the next pass reads it whole.  That is
    the same rule `otlp.read_file` applies to the raw capture, and it is why a
    torn tail costs a tick rather than a record.
    """
    recs, consumed, n = [], 0, 0
    while n < limit:
        raw = fh.readline()
        if not raw:
            break
        if not raw.endswith(b"\n"):
            break                        # partial write; leave it for next time
        consumed += len(raw)
        n += 1
        line = raw[:-1]
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            report.malformed += 1
            continue
        if not isinstance(rec, dict):
            report.malformed += 1
            continue
        if stream == STREAM_SAMPLES:
            # Exact, on the identity the record carries itself.  No fallback to
            # `account` or `account_email`: the UUID is in every real sample
            # shape, v1 and v2 alike.
            if rec.get("account_uuid") == dest.account_uuid:
                recs.append(line)
            continue
        placed = place_row(rec, dest)
        if placed is None:
            report.unidentified += 1
            continue
        rid = rec.get("request_id")
        rid = rid if isinstance(rid, str) and rid else line
        if not placed:
            # Not this destination's row.  That is the ordinary state of a
            # multi-account ledger and is NOT an alarm -- but a row no
            # destination at all claims is a row that never leaves, and until
            # this line existed nothing counted it.
            report._unplaced_ids.setdefault(rid, rec.get("email"))
            continue
        report._placed_ids.add(rid)
        out = stamp(line, dest.account_uuid)
        if out is None:
            report.malformed += 1
            continue
        recs.append(out)
    return recs, consumed


def _ack_parts(got):
    """(ok, detail, ack) from a `post` returning either two values or three.

    The injected `post` in a test may be either shape, and a shipper that
    crashed on the older one would be a test harness deciding the production
    code path.
    """
    if not isinstance(got, tuple):
        return bool(got), None, None
    if len(got) >= 3:
        return got[0], got[1], got[2]
    if len(got) == 2:
        return got[0], got[1], None
    return bool(got and got[0]), None, None


def _note_ack(report, dest, stream, ack, end):
    """The one cheap cross-check the ack was built to make possible.

    The door answers with the offset it *durably holds* for this file.  In the
    steady state that is exactly the range just delivered, because the door
    stores `max(prev, to)` and `to` is `end`.  Anything else means the two
    sides disagree about what is on the far end, and the case that matters is
    the door holding MORE: this machine has re-read a file from zero (a
    truncation, or a lost offsets file) and the door may have taken the batch
    as one it already held.
    """
    if not isinstance(ack, dict):
        return
    got = ack.get("offset")
    if not isinstance(got, int) or isinstance(got, bool) or got == int(end):
        return
    report.disagreements.append((
        dest.name, stream,
        "the door holds offset %d for this file where this machine has just "
        "delivered up to %d. If the file was rewritten in place, the records "
        "in that range may not have been stored" % (got, int(end))))


def _ship_stream(cfg, dest, stream, path, offsets, key, report, now, post,
                 host, machine):
    """Drain one file for one destination.  Returns True if nothing failed."""
    try:
        ident = tail.file_identity(path)
    except OSError as exc:
        # A file that EXISTS and cannot be read.  This used to be the same
        # answer as "no such file yet" -- success -- which contributed to no
        # failure list, so `run_pass` then cleared the `ship.err` the previous
        # pass had written.  One `chmod 000` ended shipping for good with every
        # diagnostic surface clean.
        report.failures.append(
            (dest.name, stream, "cannot read %s: %s" % (path, exc)))
        return False
    if ident is None:
        return True                      # no such file yet; nothing to say
    stored = offsets.get(key)
    start, why = tail.start_offset(stored, ident, path)
    # The door is told when this machine restarted a file.  `why` is a rotation
    # or an unverifiable legacy offset; the truncation branch below is the
    # other way in, and it is the one the door cannot detect for itself.
    reset = bool(why)
    # Has this destination ever placed a stream-A row from this file?  Until it
    # has, the offset is not advanced durably: an address that matches nothing
    # -- a re-login, or a `tag=email=` line -- would otherwise consume the whole
    # backlog into an offset, and fixing the address afterwards would not bring
    # it back.  This is the `partial` branch's rule, applied to the case that
    # takes the same damage.
    placed_ever = bool(stored.get("placed")) if isinstance(stored, dict) else False
    hold = (stream == STREAM_LEDGER) and not placed_ever
    for _ in range(MAX_BATCHES_PER_PASS):
        try:
            with open(path, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                if start > size:
                    # Truncated under us.  Re-read from zero: safe because both
                    # streams dedupe server-side, and the alternative is
                    # sitting past EOF for ever.  The door is TOLD, because an
                    # in-place rewrite that keeps the first 256 bytes leaves the
                    # file identity unchanged and the door would otherwise
                    # recognise the range as one it already holds.
                    start = 0
                    reset = True
                fh.seek(start)
                recs, consumed = _select(fh, dest, stream,
                                         MAX_LINES_PER_BATCH, report)
        except OSError as exc:
            report.failures.append((dest.name, stream, "read: %s" % exc))
            return False
        if not consumed:
            break
        end = start + consumed
        if recs:
            man = manifest(stream, path, ident, start, end, len(recs),
                           host, machine, now, reset=reset)
            ok, detail, ack = _ack_parts(post(dest.url, dest.token, man, recs))
            report.batches += 1
            if not ok:
                # THE OFFSET IS NOT TOUCHED.  The next tick resends this exact
                # byte range, which is safe because both streams are deduped
                # server-side -- and it is the only behaviour under which a
                # failure costs a retry instead of a record.
                report.failures.append((dest.name, stream, detail))
                return False
            _note_ack(report, dest, stream, ack, end)
            reset = False            # the restart has been announced once
            hold = False
            placed_ever = True
            report.shipped[(dest.name, stream)] = \
                report.shipped.get((dest.name, stream), 0) + len(recs)
        # Advanced past a range that either was delivered or held nothing for
        # this destination.  A range holding only other accounts' rows is a
        # final decision, not a deferred one: those rows will never become this
        # account's, so holding the offset for them would jam the stream.
        start = end
        report.scanned[(dest.name, stream)] = \
            report.scanned.get((dest.name, stream), 0) + consumed
        if hold:
            # Scanned, not consumed: nothing durable is written, so the next
            # pass reads this range again.  `offsets` is the whole file's
            # mapping and is saved by the other streams of this same pass, so
            # the entry is not touched at all rather than written and skipped.
            continue
        entry = {"offset": start}
        # See `tail.anchor`: dev+ino+head cannot tell a replaced file from an
        # appended one on Linux, where the inode comes straight back.
        _anchor = tail.anchor(path, start)
        if _anchor:
            entry["anchor"] = _anchor
        if stream == STREAM_LEDGER:
            # Only stream A can be unplaceable: stream B carries the account
            # UUID itself.  The key is written for the stream whose hold rule
            # reads it and for no other.
            entry["placed"] = placed_ever
        entry.update(ident)
        offsets[key] = entry
        try:
            tail.save_offsets(cfg, offsets, tail.SHIP_OFFSETS)
        except OSError as exc:
            report.failures.append((dest.name, stream,
                                    "cannot record the offset: %s" % exc))
            return False
    if hold and report.scanned.get((dest.name, stream)):
        report.held.append((dest.name, report.scanned[(dest.name, stream)]))
    return True


def run_pass(cfg, now=None, post=None, conf=None, accounts=None):
    """One shipping pass over both streams, for every configured destination.

    Every failure it *expects* -- an unreadable file, a refused POST, an
    offset it cannot record -- lands in the returned `Report` and in
    `ship.err`/`ship.warn` rather than propagating.  Containment of the ones it
    does not expect belongs to `tick`, deliberately: this function is also
    called directly by the tests, where a swallowed traceback would be a
    test that passes by hiding the bug it was written for.
    """
    now = time.time() if now is None else now
    post = post_batch if post is None else post
    report = Report()
    dests, report.refusals = destinations(conf, accounts)
    report.destinations = len(dests)

    # Nothing is created on a machine that never ships -- no offsets file, no
    # machine-id.  A machine with no destination configured is the normal case
    # and must cost nothing at all, not even a file.
    if dests:
        offsets = tail.load_offsets(cfg, tail.SHIP_OFFSETS)
        host = collector.host_slug()
        machine = collector.machine_id(cfg)
        streams = ((STREAM_LEDGER, cfg.ledger), (STREAM_SAMPLES, cfg.samples))
        for dest in dests:
            for stream, path in streams:
                if stream == STREAM_LEDGER and not dest.account_email:
                    # An account whose `.claude.json` carries a UUID but no
                    # address: stream B still ships (it carries the UUID
                    # itself), and stream A cannot be placed at all.  It is
                    # NOT scanned, deliberately -- scanning would advance the
                    # offset past rows that become placeable the moment the
                    # login is restored, and it would report every one of them
                    # as another account's.  Named below instead.
                    report.partial.append(dest.name)
                    continue
                _ship_stream(cfg, dest, stream, path, offsets,
                             "%s|%s" % (dest.name, stream), report, now, post,
                             host, machine)

    # A row is unplaced only if EVERY destination refused it, so it is decided
    # here rather than inside a per-destination scan, and deduped on
    # `request_id` so a row three destinations looked at is one row.
    for rid, email in report._unplaced_ids.items():
        if rid in report._placed_ids:
            continue
        report.unplaced += 1
        who = email if isinstance(email, str) and email else "(no address)"
        report.unplaced_addresses[who] = report.unplaced_addresses.get(who, 0) + 1

    # One file, two things that can be wrong with it, and both are named: a
    # delivery that failed (the offset is unchanged, so this is a retry and not
    # a loss) and an account that asked to ship and is not (which is a loss for
    # as long as nobody reads this).  Reporting only the first would let the
    # second one -- the silent one -- hide behind it.
    parts = []
    if report.failures:
        parts.append(
            "%d shipping failure(s); nothing was acknowledged for them and the "
            "offsets are unchanged, so the next pass resends: %s"
            % (len(report.failures),
               "; ".join("%s/%s: %s" % f for f in report.failures)))
    if report.refusals:
        parts.append("not shipping: " + "; ".join(
            "account %s: %s" % (n, why) for n, why in report.refusals))
    if report.partial:
        parts.append(
            "plan samples only for %s: no address in that account's "
            ".claude.json, so no request row can be placed against it. Its "
            "ledger is not read and nothing is skipped past -- log the account "
            "in and the backlog ships." % ", ".join(sorted(set(report.partial))))
    # A destination that read bytes of the ledger and placed NOTHING.  The two
    # documented ways to reach it are a `tag=email=` line -- which every doc
    # offers as the way to suppress the PII tag -- and an account re-logged
    # under a different address with older rows still in the ledger.  In both,
    # `destinations()` reports the account as perfectly healthy.  The offset is
    # not advanced (see `_ship_stream`), so this states a backlog that is still
    # there rather than one that has been consumed.
    for name, n in sorted(set(report.held)):
        dest = next((d for d in dests if d.name == name), None)
        seen = ", ".join(sorted(report.unplaced_addresses)[:4]) or "(none)"
        parts.append(
            "account %s: scanned %d byte(s) of %s and placed 0 request row(s). "
            "Its address in .claude.json is %s; the rows scanned carry %s. "
            "Nothing of stream A has shipped for it. The ledger offset is NOT "
            "advanced, so the backlog ships the moment the two agree -- check "
            "for a re-login under a new address, or a tag=email= line "
            "suppressing the one claudio emits."
            % (name, n, os.path.basename(cfg.ledger),
               (dest.account_email if dest else None) or "(none)", seen))
    if report.disagreements:
        parts.append("the door disagrees about what it holds: " + "; ".join(
            "%s/%s: %s" % d for d in report.disagreements))
    if parts:
        collector.note_ship_error(cfg, " | ".join(parts))
    else:
        collector.clear_ship_error(cfg)

    _warn_unplaceable(cfg, report)
    return report


# --------------------------------------------------------------------------
# the unplaceable ledger, which is why `ship.warn` outlives one tick
# --------------------------------------------------------------------------
#
# `run_pass` used to write `ship.warn` from THIS pass's counters and clear it
# otherwise.  Those counters describe only the bytes this pass scanned, and the
# offset advances past the offending lines, so the very next pass counted zero
# and unlinked the file: "these rows will never be shipped" lived one tick,
# about fifteen seconds.  Both readers -- `claudio run` at session start and
# `claudio usage doctor` on demand -- are outside that window in practice.
#
# `ship.err` never had the problem because a failed POST leaves the offset
# alone and so re-reports itself every pass.  The warn file is the one whose
# condition is by construction NON-REPEATING, which is exactly why it is the
# one that had to persist.  So the counts are accumulated in a small ledger of
# their own and the message is re-derived from the running totals every pass.
# It clears when the totals are zero, which means when somebody has deleted the
# file -- an acknowledgement, rather than a fifteen-second timer.

UNPLACEABLE = "ship-unplaceable.json"


def _unplaceable_path(cfg):
    return os.path.join(cfg.state_dir, UNPLACEABLE)


def _unplaceable_read(cfg):
    try:
        with open(_unplaceable_path(cfg), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _unplaceable_write(cfg, doc):
    cfg.ensure_dirs()
    path = _unplaceable_path(cfg)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, sort_keys=True)
    os.replace(tmp, path)


def _warn_unplaceable(cfg, report, now=None):
    """Add this pass's unshippable lines to the totals, and re-state them."""
    def _n(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0

    doc = _unplaceable_read(cfg)
    totals = {"unidentified": _n(doc.get("unidentified")) + report.unidentified,
              "malformed": _n(doc.get("malformed")) + report.malformed}
    if not (totals["unidentified"] or totals["malformed"]):
        collector.clear_ship_warning(cfg)
        return totals
    doc.update(totals)
    doc["version"] = 1
    doc["last_at"] = int(time.time() if now is None else now)
    doc.setdefault("first_at", doc["last_at"])
    try:
        _unplaceable_write(cfg, doc)
    except OSError:
        # The totals could not be recorded, so this pass's counts are all there
        # is.  Reporting them is still better than reporting nothing; what is
        # lost is the memory, not the message.
        pass
    collector.note_ship_warning(
        cfg, "%d ledger row(s) carry no account identity and %d line(s) would "
             "not parse; they were not shipped and never will be. A row has no "
             "identity when claudio ran with no account, or when a tag=email= "
             "line suppressed it. These are RUNNING TOTALS since %s -- the "
             "rows themselves are behind the shipping offset now, so this is "
             "the only record of them. Delete %s to acknowledge and clear it."
             % (totals["unidentified"], totals["malformed"],
                doc.get("first_at"), _unplaceable_path(cfg)))
    return totals


def tick(cfg, now=None, post=None):
    """The call the receiver makes.  Swallows everything, records everything.

    The receiver's own job -- being on the far end of a socket that is durable
    before it answers 200 -- outranks shipping absolutely.  A shipper traceback
    escaping into the watchdog thread would end the tick loop, and with it the
    idle exit and the session sweep, so the receiver would run for ever and
    nobody would know why.
    """
    try:
        return run_pass(cfg, now=now, post=post)
    except Exception as exc:                 # noqa: BLE001 -- see the docstring
        try:
            collector.note_ship_error(
                cfg, "shipping pass failed: %s: %s" % (type(exc).__name__, exc))
        except Exception:                    # noqa: BLE001
            pass
        return None
