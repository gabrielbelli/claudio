"""The door: the only way a record gets from a machine into the store.

Everything else in `srv/` is pure -- `now` is an argument, nothing opens a file
and a static test asserts it.  This module is the deliberate exception, and it
is named in that test rather than hidden from it: something has to hold the
socket and the bytes, and keeping that in one file is what keeps the other six
free of the storage decision.

It does five things and refuses to do a sixth.

**It never reads an identity out of the payload.**  The bearer token names the
tenant.  A record whose `account_uuid` disagrees with the token's fails the
WHOLE batch with 409 and nothing is written -- partial acceptance is how one
account's rows end up in another account's directory, and a batch is one byte
range from one file on one machine, so a disagreeing record means the shipper
picked the wrong destination and the agreeing half of that file is no more
trustworthy than the rest.  The rule is `ingest.Batch`'s rule, spelled the same
way here because the door has to refuse before the bytes land, not after.

**Durable before the ack.**  Records appended and fsynced, then the manifest
appended and fsynced, then the offset persisted by temp+rename with the
directory fsynced -- and only then the 200.  A client that does not get a 200
still holds the bytes and its own offset is untouched, so resending the same
range is always safe.  Nothing is ever partially acknowledged.

**It computes nothing.**  It parses each line once, because the identity check
demands it, and then appends the line's own bytes.  Not the re-serialised
parse: `json.dumps(json.loads(line))` reorders keys, reformats floats and
re-escapes non-ASCII, which is a third schema invented in transit -- the same
reason `cu.ship.stamp` splices its one key textually.  Every interpretation --
windows, coverage, attribution, the residual -- happens in `reconcile.py`, over
the whole store, re-derived from byte zero every time.  That is what lets a
week-late shipment retroactively correct a window that closed days ago.

**The manifest is kept, and that is not bookkeeping.**  `wire.sample_key`
hashes a stream-B record together with the SHIPPING HOST, because the v1 sample
shape has no `host` column of its own -- 65 real records on disk are in that
shape -- and `ingest.add_batch` reads that host off `manifest["host"]`.  A door
that stored the records and dropped the envelope would make those 65 records
unattributable to any machine, permanently and silently.  So each accepted
batch appends one line to `<account>/manifests.jsonl`: the manifest's own bytes
with a `"recv"` object spliced in naming the byte range those records occupy in
the stream file.  `read_batches` below turns the pair straight back into the
`ingest.Batch` the core consumes, which is the whole loop closed with nothing
invented in the middle.

**The ack carries the two numbers that go wrong invisibly.**  The offset the
server durably holds for that client file, so a client can see it agree with
its own; and the free bytes on the store's filesystem, in every response
including the refusals, with a 507 once a batch would take it under the
reserve.  A disk filling up is one of exactly two failures this system can have
without anyone noticing, and a number that only appears once it is too late is
not a warning.

What it deliberately does not do: TLS, mTLS, a CA, token expiry, rate limiting.
The network is trusted -- that is an explicit scope decision, not an oversight
-- and the token is a ROUTING LABEL, not a security boundary.  Bind it on a
trusted network only.  `server/README.md` says so once, plainly, and the
process says it again on stderr whenever it binds a non-loopback address.

**And it reads the store back out, as an API.**  `View` + `store.DuckStore`
-> `srv/api.py` -> `GET /api/v1/*`.  There is no bundled page: **omini is the
front end**, a separate project, and this file serves it data.  A page held
here as a string was one renderer with one opinion, shipped inside the server
that produced its numbers, and every honesty property it carried is now stated
as a property of the PAYLOAD instead -- which is the only form in which a front
end nobody in this repository controls can be held to it.

The contract itself lives in `api.py` rather than here, because it is a
statement about answers and this file is a statement about a socket.  What
stays here is the wiring, and three properties that are refusals like the five
above:

  * **It re-derives on every request and caches nothing.**  A window's state
    is a function of `now`, so a stored report announces as OPEN a window that
    closed an hour ago, and the caveat that would make that honest is one
    nobody reads on a figure that looks live.  The measured cost travels in
    every payload instead.
  * **A refusal reaches the client whole**, with its reason, detail and remedy,
    and carrying no `result` key at all -- so a client looping over
    `payload.result.rows` raises rather than rendering a tidy empty table.
    Flattening a question this data cannot answer into an empty list is how it
    becomes a confident nothing.
  * **It asks for no token, and the process says so.**  The API serves every
    record in the store, email addresses included; on loopback that is the
    intended shape, binding elsewhere prints a second warning naming exactly
    that, and `--no-api` removes the routes.
"""

import argparse
import errno
import fcntl
import gzip
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:                                    # imported as `srv.serve`
    from . import api, ingest, reconcile, store as duckstore, wire
except ImportError:                     # run directly: python3 server/srv/serve.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from srv import (api, ingest, reconcile,      # noqa: F401
                     store as duckstore, wire)


DOOR_VERSION = 1

PATH_SHIP = "/v1/ship"
PATH_HEALTH = "/healthz"
PATH_API = "/api/"

# The versioned API.  `api.PATH_V1` is the same string; it is compared in the
# suite rather than duplicated by hand.
PATH_API_V1 = "/api/v1/"

# Spelled to match `cu.ship.CONTENT_TYPE`, and a test compares the two rather
# than trusting this line.
CONTENT_TYPE = "application/x-ndjson"

# One body, on the wire.  `cu.ship.MAX_LINES_PER_BATCH` is 500 lines and the
# largest real record measured is 751 bytes, so a legitimate batch is ~375 KB;
# this is two orders of magnitude of headroom and still bounds memory.
MAX_BODY = 32 * 1024 * 1024

# And after gunzip.  A compressed body is checked before it is expanded,
# because "the network is trusted" is a statement about eavesdroppers, not a
# reason to let a 200-byte request allocate a gigabyte.
MAX_DECOMPRESSED = 128 * 1024 * 1024

# Refuse a batch that would take the filesystem under this.  A store that fills
# silently loses every record shipped afterwards; a store that refuses loudly
# loses none, because an unacked batch stays on the machine that made it.
DISK_RESERVE = 256 * 1024 * 1024

# The stream name comes off the manifest and `wire.manifest_check` has already
# constrained it to `wire.STREAMS`, but it is mapped through a fixed table
# rather than formatted into a filename: a name that reaches `os.path.join` is
# a path component, and the difference between a closed set and a validated
# string is one refactor.
STREAM_FILES = {
    wire.STREAM_LEDGER: "ledger.jsonl",
    wire.STREAM_SAMPLES: "samples.jsonl",
    wire.STREAM_ATTEST: "attestations.jsonl",
}

MANIFESTS_FILE = "manifests.jsonl"

# The tenant is a directory name.  It arrives from the operator's own tokens
# file rather than from any payload, but a hand-edited `"../../etc"` would
# still be a directory name, so it is validated once at load and the mapping is
# refused BY NAME if it is not usable.  Same rejections as claudio's
# `_validate_name` and `cu.ship._UUID_OK`, plus the traversal pair.
TENANT_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def valid_tenant(name):
    return bool(name) and bool(TENANT_OK.match(name)) and ".." not in name


# --------------------------------------------------------------------------
# durability primitives
# --------------------------------------------------------------------------

def _fsync(fd):
    """Flush this file's bytes as far as the platform can be made to flush them.

    `os.fsync` on Darwin returns once the data reaches the drive's write cache,
    not the platter; `F_FULLFSYNC` is the call that waits for the platter, and
    it is what "durable before the ack" has to mean if the promise is to
    survive the power going off rather than only the process dying.  It is
    absent on Linux, where `fsync` already carries that meaning.  A refusal
    (some filesystems return ENOTTY/EINVAL) falls back rather than failing the
    write.
    """
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def _fsync_dir(path):
    """A rename is durable when the DIRECTORY is synced, not the file."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def splice(raw, key, value):
    """`raw`'s own bytes with `,"key":<value>` before the closing brace.

    Textual, for `cu.ship.stamp`'s reason one hop further along: strip the
    suffix and the client's manifest line is back byte for byte, so a stored
    envelope is diffable against a captured request body.  `value` is already
    JSON.  None if `raw` is not an object -- callers have parsed it first, so
    that is a corruption check rather than a branch anyone reaches.
    """
    s = raw.strip()
    if not s.endswith(b"}"):
        return None
    add = b'"' + key.encode("ascii") + b'":' + value
    if s == b"{}":
        return b"{" + add + b"}"
    return s[:-1] + b"," + add + b"}"


# --------------------------------------------------------------------------
# who a token is
# --------------------------------------------------------------------------

class Tenants(object):
    """token -> account UUID.  The whole of the server's idea of identity.

    A JSON object, `{"<token>": "<account_uuid>"}`, because a token can be any
    string an operator pastes and claudio's `key=value` idiom would have to
    guess where the key ends.  Several tokens may name one tenant -- that is
    rotation and several machines -- and a token that names nothing is simply
    unknown.

    Token VALUES are never logged, never echoed in a response and never
    written to the store.  A refused mapping is reported by its position and by
    the tenant it named, so a typo is fixable without the file's secrets
    appearing in a terminal someone screenshots.
    """

    def __init__(self, path=None, mapping=None):
        self.path = path
        self.map = {}
        self.refused = []
        if mapping is not None:
            self._absorb(mapping)
        elif path:
            self.load()

    def _absorb(self, doc):
        if not isinstance(doc, dict):
            self.refused.append(("(file)", "not a JSON object of token -> "
                                           "account_uuid"))
            return
        for i, (token, tenant) in enumerate(doc.items()):
            where = "entry %d" % (i + 1)
            if not isinstance(token, str) or not token:
                self.refused.append((where, "the token is not a non-empty string"))
                continue
            if not isinstance(tenant, str) or not valid_tenant(tenant):
                self.refused.append(
                    (where, "%r is not a usable account UUID" % (tenant,)))
                continue
            self.map[token] = tenant

    def load(self):
        self.map, self.refused = {}, []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except OSError as exc:
            self.refused.append(("(file)", "cannot read %s: %s"
                                 % (self.path, exc)))
            return self
        except ValueError as exc:
            self.refused.append(("(file)", "%s is not valid JSON: %s"
                                 % (self.path, exc)))
            return self
        self._absorb(doc)
        return self

    def resolve(self, token):
        return self.map.get(token) if token else None

    def tenants(self):
        return sorted(set(self.map.values()))


ALL_ACCOUNTS = "*"


class Readers(object):
    """token -> the accounts a READER may see.  The other half of identity.

    The write side (`Tenants`) answers "whose data is this batch".  This
    answers "whose data may this caller read", and they are deliberately
    separate files: a machine that ships is not thereby entitled to read, and a
    reader is usually a person or a front end that ships nothing.

    A JSON object, `{"<token>": "*"}` for every account in the store, or
    `{"<token>": ["<uuid>", ...]}` for a subset.

    **`"*"` is the ordinary case, and a front end is meant to hold it.**  omini
    reads everything, exactly as Graylog holds every message and Kibana every
    index; isolation between people belongs in the front end, as streams and
    index patterns are, because that is the only layer that knows who is logged
    in and the only one that can hold one permission model across several back
    ends.  A per-user token per back end does not compose and does not survive
    a second back end.

    That is not the confused deputy it resembles.  A confused deputy has broad
    access and NO authorisation model of its own -- a bare proxy.  A front end
    with users, roles and streams is the authority its users authenticate
    against; this token says which store it may reach, not which human asked.

    The list form is for narrower consumers: a reporting script, a per-team
    reader, someone who should see one account and no others.  It exists so
    that "give this person a subset" never requires a second server.

    **Scope is applied as a refusal, never as a filter.**  Asking for an
    account outside the token's scope is answered by name -- not with an empty
    result, which would be indistinguishable from an account that has shipped
    nothing.  That distinction is the whole reason this project spells
    `no-data`, `filtered-to-nothing` and `unanswerable` as three different
    words on the wire.

    Token VALUES are never logged, echoed or stored, exactly as `Tenants` does
    it: a refusal names the entry's position, never its secret.
    """

    def __init__(self, path=None, mapping=None):
        self.path = path
        self.map = {}
        self.refused = []
        self.configured = False
        if mapping is not None:
            self.configured = True
            self._absorb(mapping)
        elif path:
            self.load()

    def _absorb(self, doc):
        if not isinstance(doc, dict):
            self.refused.append(("(file)", "not a JSON object of token -> "
                                           'account list or "*"'))
            return
        for i, (token, scope) in enumerate(doc.items()):
            where = "entry %d" % (i + 1)
            if not isinstance(token, str) or not token:
                self.refused.append((where, "the token is not a non-empty string"))
                continue
            if scope == ALL_ACCOUNTS:
                self.map[token] = ALL_ACCOUNTS
                continue
            if not isinstance(scope, list) or not scope:
                self.refused.append(
                    (where, 'the scope is neither "*" nor a non-empty list of '
                            "account UUIDs"))
                continue
            bad = [a for a in scope
                   if not isinstance(a, str) or not valid_tenant(a)]
            if bad:
                self.refused.append(
                    (where, "%r is not a usable account UUID" % (bad[0],)))
                continue
            self.map[token] = sorted(set(scope))

    def load(self):
        self.map, self.refused, self.configured = {}, [], False
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            # Absent is a state, not an error: on loopback it means "no reader
            # auth", which `serve()` warns about and refuses to pair with a
            # non-loopback bind.
            return self
        except OSError as exc:
            self.configured = True
            self.refused.append(("(file)", "cannot read %s: %s"
                                 % (self.path, exc)))
            return self
        except ValueError as exc:
            self.configured = True
            self.refused.append(("(file)", "%s is not valid JSON: %s"
                                 % (self.path, exc)))
            return self
        self.configured = True
        self._absorb(doc)
        return self

    def scope(self, token):
        """The account list for a token, ALL_ACCOUNTS, or None if unknown."""
        return self.map.get(token) if token else None

    def permits(self, scope, account):
        """May this scope read this account?  An unscoped read is always fine;
        it is the ROUTE that decides whether omitting an account is allowed,
        and `store` already refuses to total across accounts."""
        if scope == ALL_ACCOUNTS or account is None:
            return True
        return account in scope


def bearer(header):
    """The token out of an `Authorization: Bearer <token>` header, or None."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


# --------------------------------------------------------------------------
# counters
# --------------------------------------------------------------------------

class Counters(object):
    """What arrived, what landed, and what was refused -- by name.

    `otlp-recv`'s counters exist because that is the only artefact able to tell
    *nothing arrived* apart from *everything was dropped*.  Same here, one hop
    further along: `batches_refused` is keyed by reason, so a client that has
    been 409ing for a week is a number with a name on it rather than an absence
    of rows that reads exactly like an idle machine.
    """

    def __init__(self, now=None):
        self.lock = threading.Lock()
        self.started_at = time.time() if now is None else now
        self.last_accept_at = None
        self.batches_accepted = 0
        self.batches_skipped_as_held = 0   # held already: written nowhere
        self.batches_empty = 0
        self.batches_refused = {}          # reason -> count
        self.records_written = 0
        self.bytes_written = 0
        self.manifests_written = 0
        self.ranges_overlapping = 0
        self.torn_lines_terminated = 0
        self.write_failures = 0
        self.accounts = {}                 # account_uuid -> batches accepted

    def refused(self, reason):
        with self.lock:
            self.batches_refused[reason] = self.batches_refused.get(reason, 0) + 1

    def snapshot(self):
        with self.lock:
            return {
                "door": DOOR_VERSION,
                "started_at": self.started_at,
                "last_accept_at": self.last_accept_at,
                "batches_accepted": self.batches_accepted,
                "batches_skipped_as_held": self.batches_skipped_as_held,
                "batches_empty": self.batches_empty,
                "batches_refused": dict(self.batches_refused),
                "records_written": self.records_written,
                "bytes_written": self.bytes_written,
                "manifests_written": self.manifests_written,
                "ranges_overlapping": self.ranges_overlapping,
                "torn_lines_terminated": self.torn_lines_terminated,
                "write_failures": self.write_failures,
                "accounts": dict(self.accounts),
            }


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

class Store(object):
    """Per-account append-only JSONL, plus the offsets and the manifests.

        <root>/accounts/<account_uuid>/ledger.jsonl
                                       samples.jsonl
                                       attestations.jsonl
                                       manifests.jsonl
        <root>/offsets/<account_uuid>.json
        <root>/door.lock

    One lock covers the whole accept, so the size probe, both appends and the
    offset write are one transaction against one root.  It is a *thread* lock;
    two server processes on one root are refused at startup by `lock_root`
    rather than left to interleave offsets files.
    """

    def __init__(self, root, reserve=DISK_RESERVE, counters=None):
        self.root = os.path.abspath(root)
        self.reserve = reserve
        self.counters = counters if counters is not None else Counters()
        self.lock = threading.Lock()
        # Overridable so the disk-full path is testable without filling a disk.
        # A hook, not a global: two Stores in one process must be able to
        # disagree about how much room they have.
        self.statvfs = os.statvfs
        os.makedirs(os.path.join(self.root, "accounts"), mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(self.root, "offsets"), mode=0o700, exist_ok=True)

    # -- paths -----------------------------------------------------------

    def account_dir(self, tenant):
        if not valid_tenant(tenant):
            raise ValueError("unusable account uuid: %r" % (tenant,))
        return os.path.join(self.root, "accounts", tenant)

    def stream_path(self, tenant, stream):
        return os.path.join(self.account_dir(tenant), STREAM_FILES[stream])

    def manifests_path(self, tenant):
        return os.path.join(self.account_dir(tenant), MANIFESTS_FILE)

    def offsets_path(self, tenant):
        if not valid_tenant(tenant):
            raise ValueError("unusable account uuid: %r" % (tenant,))
        return os.path.join(self.root, "offsets", tenant + ".json")

    # -- disk ------------------------------------------------------------

    def free_bytes(self):
        """Bytes available to this (non-root) user, or None if unaskable."""
        try:
            st = self.statvfs(self.root)
        except OSError:
            return None
        return int(st.f_bavail) * int(st.f_frsize)

    def room_for(self, n):
        """(ok, free).  Unaskable free space is not a refusal: a store that
        stops accepting because `statvfs` failed loses records to protect a
        disk nobody has shown to be full."""
        free = self.free_bytes()
        if free is None:
            return True, None
        return (free - n) >= self.reserve, free

    # -- offsets ---------------------------------------------------------

    def load_offsets(self, tenant):
        try:
            with open(self.offsets_path(tenant), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def save_offsets(self, tenant, offsets):
        """temp + rename + fsync, both the file and its directory.

        `tail.save_offsets`' shape, with the two fsyncs added: this side is the
        one whose answer a client is told, so an offsets file that survives the
        ack by luck would let the server report an offset it no longer holds.
        """
        path = self.offsets_path(tenant)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            data = json.dumps(offsets, sort_keys=True).encode("utf-8")
            n = 0
            while n < len(data):
                n += os.write(fd, data[n:])
            _fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        _fsync_dir(os.path.dirname(path))

    @staticmethod
    def offset_key(manifest):
        """Which client file this batch is a range of.

        The machine, the stream and the file's basename -- never the file's
        *identity*, which changes on every rotation and would leave a dead key
        behind for each one.  The identity is stored as the VALUE, so a
        rotation resets this key's offset instead of orphaning it.
        """
        return "%s|%s|%s" % (manifest.get("machine_id"), manifest.get("stream"),
                             manifest.get("file"))

    def held(self, tenant, manifest):
        """(offset, matches) -- what this door durably holds for that file.

        `matches` is whether the stored file identity is the one the client is
        describing now.  A mismatch is a rotation and the offset means nothing
        any more, exactly as `tail.start_offset` treats it from the other end.

        It is **None** when there is no entry at all, and that third answer is
        the point: a first shipment would otherwise be told `false`, which reads
        as "your file is not the one I have" rather than "I have nothing".  Same
        distinction `wire.ABSENT` exists for one package over -- "nobody said"
        is not "no".
        """
        entry = self.load_offsets(tenant).get(self.offset_key(manifest))
        if not isinstance(entry, dict):
            return 0, None
        same = entry.get("ident") == manifest.get("ident")
        return int(entry.get("offset") or 0), bool(same)

    # -- the transaction -------------------------------------------------

    def accept(self, tenant, manifest, manifest_raw, records, now):
        """Append, fsync, persist the offset.  Everything before the ack.

        Returns a dict describing what happened.  Raises OSError, which the
        door answers 503 with: a failed write must never be acknowledged, and
        the client still holds every byte of it.
        """
        stream = manifest["stream"]
        with self.lock:
            offsets = self.load_offsets(tenant)
            key = self.offset_key(manifest)
            entry = offsets.get(key)
            entry = entry if isinstance(entry, dict) else None
            same_file = bool(entry) and entry.get("ident") == manifest.get("ident")
            prev = int(entry.get("offset") or 0) if entry else 0
            to, frm = int(manifest["to"]), int(manifest["from"])

            # A range this door already holds, from a file it can prove is the
            # same file.  The client only advances its own offset on an ack, so
            # this is the ack that was lost -- write nothing, answer with the
            # offset, and let it move on.
            #
            # What would have to be true for the skip to be wrong: the client's
            # file rotated to a new file with the same st_dev, the same st_ino
            # and the same first 256 bytes, and different content in
            # [from, to).  Both streams are append-only, so that cannot happen
            # while nothing rewrites the file -- but something can: an in-place
            # rewrite (an operator's own pruning, which `server/README.md` makes
            # the operator's job) keeps dev and ino, and one surviving head
            # record keeps the hash.  `cu.ship._ship_stream` DETECTS that from
            # the other end -- `start > size`, resume at zero -- and now says so
            # on the manifest.  When it does, the skip is refused: append, and
            # let `request_id`/`sample_key` dedupe, which is the direction every
            # other decision in this system takes.  The old comment claimed this
            # was "the same trust boundary" as `tail.start_offset`; it was not.
            # On the client the same evidence errs toward re-reading and costs a
            # parse; here it errs toward discarding and costs the records.
            if same_file and to <= prev and not wire.manifest_reset(manifest):
                with self.counters.lock:
                    # Its own name, not `batches_duplicate`: a discard and a
                    # duplicate are the same event only when the door is right,
                    # and an operator reading /healthz has to be able to tell
                    # "held already" from "written" without being told a
                    # housekeeping word for both.
                    self.counters.batches_skipped_as_held += 1
                return {"written": 0, "duplicate": True, "offset": prev,
                        "ident_matches": True, "torn": 0, "overlapped": False}

            # A range that starts under what we hold and ends above it.  The
            # whole batch is appended: trimming would mean deciding which
            # LINES fall in the overlapping byte range, which needs the stored
            # file parsed, which is the interpretation this door does not do.
            # The duplicate half costs one dedupe in `ingest` -- `request_id`
            # for stream A, `wire.sample_key` for stream B -- and is counted
            # here so it is a number rather than a mystery in a file size.
            #
            # Note what is NOT flagged: `from` ABOVE what we hold.  A shipper
            # advances its offset over a byte range that held only other
            # accounts' rows without posting anything (`cu.ship._ship_stream`),
            # so a forward jump is the normal state of a multi-account ledger
            # and calling it a gap would be an alarm written from an
            # assumption.
            overlapped = bool(same_file and frm < prev < to)
            if overlapped:
                with self.counters.lock:
                    self.counters.ranges_overlapping += 1

            written = torn = 0
            byte_from = byte_to = None
            if records:
                path = self.stream_path(tenant, stream)
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                blob = b"".join(r + b"\n" for r in records)
                byte_from, byte_to, torn = self._append(path, blob)
                written = len(records)

                # The envelope, and only after the records it describes are
                # durable.  A crash between the two leaves record bytes no
                # manifest claims -- which is not a loss, because no ack went
                # back and the client resends the identical range under a
                # manifest of its own; the orphaned copy is a duplicate that
                # dedupes away.  The other order would leave a manifest naming
                # bytes that are not there, which reads as loss and is not.
                man_raw = splice(manifest_raw, "recv", json.dumps(
                    {"at": now, "byte_from": byte_from, "byte_to": byte_to,
                     "tenant": tenant, "door": DOOR_VERSION},
                    sort_keys=True, separators=(",", ":")).encode("utf-8"))
                if man_raw is not None:
                    _a, _b, torn2 = self._append(self.manifests_path(tenant),
                                                 man_raw + b"\n")
                    torn += torn2
                    with self.counters.lock:
                        self.counters.manifests_written += 1

            # None, not False, when there was nothing on record: see `held`.
            reported = same_file if entry else None
            offsets[key] = {"offset": max(prev, to) if same_file else to,
                            "ident": manifest.get("ident"),
                            "host": manifest.get("host"),
                            "machine_id": manifest.get("machine_id"),
                            "updated_at": now}
            self.save_offsets(tenant, offsets)

            with self.counters.lock:
                self.counters.batches_accepted += 1
                if not records:
                    self.counters.batches_empty += 1
                self.counters.records_written += written
                self.counters.bytes_written += (byte_to - byte_from) if written else 0
                self.counters.torn_lines_terminated += torn
                self.counters.last_accept_at = now
                self.counters.accounts[tenant] = \
                    self.counters.accounts.get(tenant, 0) + 1
            return {"written": written, "duplicate": False,
                    "offset": offsets[key]["offset"], "ident_matches": reported,
                    "torn": torn, "overlapped": overlapped}

    def _append(self, path, blob):
        """Append `blob` durably.  Returns (byte_from, byte_to, torn).

        A file whose last byte is not a newline is a batch that was interrupted
        mid-write -- the only way to get one, since every append here is
        followed by an fsync before anything is acknowledged.  It is terminated
        with a newline BEFORE the new bytes go on, so the damage stays inside
        the one torn line instead of gluing a fragment onto the front of a
        good record and costing two.  The torn line's own records are not lost:
        that batch was never acked, so the client still holds the range and
        resends it.
        """
        torn = 0
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            if size and os.pread(fd, 1, size - 1) != b"\n":
                blob = b"\n" + blob
                torn = 1
            n = 0
            while n < len(blob):
                n += os.write(fd, blob[n:])
            end = os.lseek(fd, 0, os.SEEK_END)
            _fsync(fd)
        finally:
            os.close(fd)
        return end - len(blob) + torn, end, torn


def lock_root(root):
    """An exclusive lock on the store, or None if another door holds it.

    Two processes on one root would not corrupt the record files -- every
    append is `O_APPEND` -- but they would interleave read-modify-write on the
    offsets, and an offset is what the ack promises.  It fails rather than
    waits, for `claudio usage ingest`'s reason: a silent wait hides the
    concurrency from the only person who can do anything about it.  `flock`
    dies with the process, so a crash cannot strand it.
    """
    os.makedirs(root, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(root, "door.lock"),
                 os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return fd


# --------------------------------------------------------------------------
# the door
# --------------------------------------------------------------------------

class TooBig(Exception):
    """A gzip body that expands past the cap.  Raised, never repaired."""


class Door(object):
    """Policy.  Bytes and a bearer token in, a status and an ack out.

    Separate from the HTTP handler so that every refusal is testable as a
    function call, and so the handler stays the thin adapter it should be.  The
    reason strings are the API -- `wire`'s refusal vocabulary, extended with
    the ones only a door can produce.
    """

    def __init__(self, store, tenants, max_body=MAX_BODY,
                 max_decompressed=MAX_DECOMPRESSED, counters=None):
        self.readers = None
        self.store = store
        self.tenants = tenants
        self.max_body = max_body
        self.max_decompressed = max_decompressed
        self.counters = counters if counters is not None else store.counters

    # -- helpers ---------------------------------------------------------

    def _refuse(self, status, reason, detail=None, offset=None):
        self.counters.refused(reason)
        out = {"ok": False, "door": DOOR_VERSION, "reason": reason,
               "detail": detail, "disk_free_bytes": self.store.free_bytes(),
               "disk_reserve_bytes": self.store.reserve}
        if offset is not None:
            out["offset"] = offset
        return status, out

    def _gunzip(self, body):
        """Expanded with a cap, never `gzip.decompress`.

        A 32 MB body at gzip's ceiling ratio is ~32 GB of `bytes`.  "The
        network is trusted" is a statement about who is listening, not a reason
        to let one request allocate the machine.
        """
        out = gzip.GzipFile(fileobj=io.BytesIO(body)).read(self.max_decompressed + 1)
        if len(out) > self.max_decompressed:
            raise TooBig()
        return out

    # -- the one entry point ---------------------------------------------

    def ship(self, token, content_type, encoding, body, now=None):
        """POST /v1/ship.  Returns (http status, ack dict)."""
        now = time.time() if now is None else now

        tenant = self.tenants.resolve(token)
        if tenant is None:
            # 401 for both "no token" and "unknown token", but named apart in
            # the body: an operator debugging a rollout needs to know which,
            # and the client is not a browser being probed.
            return self._refuse(401, "no-bearer-token" if not token
                                else "unknown-token")

        if (content_type or "").split(";")[0].strip().lower() != CONTENT_TYPE:
            return self._refuse(415, "unsupported-media-type",
                                detail="expected %s" % CONTENT_TYPE)

        enc = (encoding or "").strip().lower()
        if enc == "gzip":
            try:
                body = self._gunzip(body)
            except TooBig:
                return self._refuse(413, "body-too-large",
                                    detail="decompressed body exceeds %d bytes"
                                           % self.max_decompressed)
            except (OSError, EOFError, ValueError) as exc:
                return self._refuse(400, "gzip-undecodable", detail=str(exc))
        elif enc not in ("", "identity"):
            return self._refuse(415, "unsupported-content-encoding",
                                detail="expected gzip or identity")

        if len(body) > self.max_body:
            return self._refuse(413, "body-too-large",
                                detail="%d bytes" % len(body))
        if not body.strip():
            return self._refuse(400, "body-empty")

        lines = body.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()                      # the final newline, not a record
        try:
            man = json.loads(lines[0])
        except ValueError as exc:
            return self._refuse(400, "manifest-unparseable", detail=str(exc))
        ok, why = wire.manifest_check(man)
        if not ok:
            return self._refuse(400, why)

        raw_records = lines[1:]
        for i, raw in enumerate(raw_records):
            if not raw.strip():
                return self._refuse(400, "body-blank-line", detail="line %d" % (i + 2))
        if man["count"] != len(raw_records):
            return self._refuse(400, "manifest-count-mismatch",
                                detail="manifest says %r, body carries %d"
                                       % (man["count"], len(raw_records)))

        # Parsed ONCE, for the identity check, and then thrown away: what gets
        # appended is `raw`, never `json.dumps(rec)`.
        held, _same = self.store.held(tenant, man)
        for i, raw in enumerate(raw_records):
            try:
                rec = json.loads(raw)
            except ValueError as exc:
                # Refused whole: a line that is not a record cannot be
                # identity-checked, and appending it anyway would put bytes in
                # an account's file that no reader can ever place.
                #
                # `cu.ship._select` drops unparseable lines and advances past
                # them before anything is posted, so one arriving here is
                # corruption between that filter and this door -- which a
                # retry of the same range normally fixes by itself, since the
                # client's copy is intact.  If it does not, the offset stays
                # put and the reason is named in `ship.err` on the machine
                # that can do something about it.  Advancing over it instead
                # would be this project's cardinal sin with a 200 on top.
                return self._refuse(400, "record-unparseable",
                                    detail="line %d: %s" % (i + 2, exc),
                                    offset=held)
            if not isinstance(rec, dict):
                return self._refuse(400, "record-not-an-object",
                                    detail="line %d" % (i + 2), offset=held)
            # D7, and deliberately `ingest.add_batch`'s exact rule: a record
            # that names a DIFFERENT account fails the batch, while one that
            # names none is accepted here and refused by name downstream
            # (`wire.check` -> `no-account-uuid`), counted in the report rather
            # than jamming a client whose shipper forgot to stamp.  A door
            # stricter than the core would refuse batches the core accepts.
            got = rec.get("account_uuid")
            if got is not None and got != tenant:
                return self._refuse(
                    409, "identity",
                    detail="line %d names another account; the token names "
                           "this one, and nothing from a batch that disagrees "
                           "is written" % (i + 2),
                    offset=held)

        ok, free = self.store.room_for(len(body))
        if not ok:
            return self._refuse(507, "disk-nearly-full",
                                detail="%d bytes free, reserve is %d"
                                       % (free, self.store.reserve),
                                offset=held)

        try:
            res = self.store.accept(tenant, man, lines[0], raw_records, now)
        except OSError as exc:
            with self.counters.lock:
                self.counters.write_failures += 1
            # 503, never 200: the client keeps the batch and its offset, so a
            # failed write costs a retry and never a record.
            return self._refuse(503, "write-failed", detail=str(exc), offset=held)

        return 200, {"ok": True, "door": DOOR_VERSION, "ship": wire.SHIP_VERSION,
                     "stream": man["stream"], "tenant": tenant,
                     "accepted": res["written"], "duplicate": res["duplicate"],
                     "overlapped": res["overlapped"],
                     "torn_line_terminated": bool(res["torn"]),
                     "offset": res["offset"],
                     "ident_matches": res["ident_matches"],
                     "disk_free_bytes": self.store.free_bytes(),
                     "disk_reserve_bytes": self.store.reserve}

    def health(self):
        out = self.counters.snapshot()
        out["root"] = self.store.root
        out["disk_free_bytes"] = self.store.free_bytes()
        out["disk_reserve_bytes"] = self.store.reserve
        out["tenants_configured"] = len(self.tenants.tenants())
        out["tokens_refused"] = [{"where": w, "why": y}
                                 for w, y in self.tenants.refused]
        return out


# --------------------------------------------------------------------------
# reading the store back
# --------------------------------------------------------------------------

def read_batches(root, tenant):
    """([ingest.Batch], [(reason, manifest)]) for one account, off the disk.

    The other half of the loop.  The door stores the records and the envelope
    separately -- the envelope carries the shipping host, which stream B's v1
    shape has no column for -- so this is where they are put back together, and
    it produces exactly the `ingest.Batch` the pure core consumes.  Written
    here, once, so nobody writes a second reader that forgets the host and
    quietly hashes 65 real records under the wrong key.

    Problems are returned, never skipped: a manifest whose byte range is not
    fully on disk is an interrupted batch (its client never got an ack and
    resent it, so its records arrive again under a later manifest), and a
    reader that dropped it silently would be indistinguishable from one that
    never saw it.
    """
    store_dir = os.path.join(root, "accounts", tenant)
    problems, batches, seen = [], [], {}
    try:
        with open(os.path.join(store_dir, MANIFESTS_FILE), "rb") as fh:
            raw_lines = fh.read().split(b"\n")
    except OSError:
        return [], []
    for raw in raw_lines:
        if not raw.strip():
            continue
        try:
            man = json.loads(raw)
        except ValueError as exc:
            problems.append(("manifest-unparseable", str(exc)))
            continue
        recv = man.get("recv") or {}
        stream = man.get("stream")
        if stream not in STREAM_FILES:
            problems.append(("unknown-stream", man))
            continue
        a, b = recv.get("byte_from"), recv.get("byte_to")
        if not isinstance(a, int) or not isinstance(b, int) or b < a:
            problems.append(("recv-range-unreadable", man))
            continue
        # Accounted for HERE, not after the read succeeds: an interrupted
        # batch is a range a manifest claims and a client still holds, so its
        # bytes are not the unaccounted tail below -- they are already named,
        # once, as `interrupted-batch`.
        seen[stream] = max(seen.get(stream, 0), b)
        path = os.path.join(store_dir, STREAM_FILES[stream])
        try:
            with open(path, "rb") as fh:
                fh.seek(a)
                blob = fh.read(b - a)
        except OSError as exc:
            problems.append(("stream-unreadable: %s" % exc, man))
            continue
        if len(blob) != b - a:
            problems.append(("interrupted-batch", man))
            continue
        recs, bad = [], False
        for line in blob.split(b"\n"):
            if not line.strip():
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                bad = True
        if bad:
            problems.append(("record-unparseable", man))
        batches.append(ingest.Batch(man, recs, tenant))

    problems.extend(_unaccounted_tail(store_dir, seen))
    return batches, problems


def _unaccounted_tail(store_dir, seen):
    """Unparseable lines PAST the last byte any manifest accounts for.

    The reconciler reads byte ranges the door acknowledged, so a line written
    by anything else is not in any batch and is invisible here -- which meant
    a torn `ledger.jsonl` was named on `/search` (DuckDB reads the whole file)
    and NOT on `/accounts` or `/windows`, i.e. the anti-silence surface was
    silent in exactly the reader-only case it exists for.

    The TAIL only, and that boundary is not laziness.  A fragment BEFORE the
    door's first accepted range is the interrupted write `Store._append`
    terminates on purpose, and it costs nothing: that batch was never acked,
    so the client still holds the range and resends it -- `read_batches`
    reports no problem for it today and must not start to.  Bytes after the
    last acknowledged range are a different statement: nothing is coming back
    for them.

    Records with no manifest ARE legitimate here -- a crash between the record
    append and the manifest write leaves some, and the docstring above says so
    -- and those lines PARSE, so this counts nothing for them.  Only a line
    this reader cannot parse at all is named.
    """
    out = []
    for stream, fname in sorted(STREAM_FILES.items()):
        path = os.path.join(store_dir, fname)
        start = seen.get(stream, 0)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= start:
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                tail = fh.read(size - start)
        except OSError:
            continue
        n = 0
        for line in tail.split(b"\n"):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except ValueError:
                n += 1
        if n:
            out.append(("unparseable-lines",
                        {"file": path, "count": n,
                         "detail": "%d line(s) past the last byte any "
                                   "manifest accounts for could not be parsed "
                                   "as JSON. No client is going to resend "
                                   "them: nothing acknowledged them."
                                   % n}))
    return out


def accounts_in(root):
    try:
        return sorted(d for d in os.listdir(os.path.join(root, "accounts"))
                      if os.path.isdir(os.path.join(root, "accounts", d)))
    except OSError:
        return []


# --------------------------------------------------------------------------
# the view: the reconciler's side of the read API
# --------------------------------------------------------------------------
#
# `srv/api.py` is the contract; this is the handle it reads stream B, the
# windows and the coverage through.  Stream A is read by `srv/store.py` over
# the same files with DuckDB, and the split is forced rather than preferred:
# a `SELECT` over `samples.jsonl` in place reads neither the shipping host --
# which lives on the manifest, because the v1 sample shape has no host column
# -- nor the dedupe, which is the core's.
#
# **Nothing is cached, and that is the honest choice rather than the lazy
# one.**  A window's state -- open, closed, settled -- is a function of `now`,
# so a report derived at T and served at T+2h reports as open a window that
# closed an hour ago.  Caching it would need the answer to say "as of T",
# which is a caveat nobody reads on a number that looks live.  So every
# request re-derives from byte zero and the measured cost travels in every
# payload (`derived_ms`, with the record counts beside it), so that a slow
# store is a number on the screen rather than a mystery.


class Snapshot(object):
    """One re-derivation of the whole store, and what it cost."""

    __slots__ = ("report", "ing", "problems", "batches_n", "accounts",
                 "derived_ms", "now")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def rows(self, uuid):
        return self.ing.rows_for(uuid)

    def samples(self, uuid):
        return self.ing.samples_for(uuid)


class View(object):
    """The read side of the store.  Impure by necessity, thin by design."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.derivations = 0
        self.lock = threading.Lock()

    def load(self, now=None):
        now = time.time() if now is None else now
        t0 = time.time()
        batches, problems = [], []
        for tenant in accounts_in(self.root):
            bs, probs = read_batches(self.root, tenant)
            batches.extend(bs)
            for reason, detail in probs:
                entry = {"account_uuid": tenant, "reason": reason,
                         "detail": detail if isinstance(detail, str) else None}
                if isinstance(detail, dict):
                    # A problem about a FILE rather than about a manifest.
                    # `file` and `count` are what `api._merge_problems` dedupes
                    # on, so one tear seen by both readers is one entry.
                    entry.update({k: detail[k] for k in
                                  ("file", "count", "detail") if k in detail})
                problems.append(entry)
        ing = ingest.ingest(batches, now)
        rep = reconcile.reconcile_ingest(ing, now)
        with self.lock:
            self.derivations += 1
        return Snapshot(report=rep, ing=ing, problems=problems,
                        batches_n=len(batches), accounts=sorted(rep.accounts),
                        derived_ms=(time.time() - t0) * 1000.0, now=now)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class ShipHandler(BaseHTTPRequestHandler):
    """The adapter.  Everything it decides, it decides about HTTP."""

    protocol_version = "HTTP/1.1"
    server_version = "claudio-door/%d" % DOOR_VERSION
    sys_version = ""
    door = None
    api = None
    quiet = False

    def version_string(self):
        # BaseHTTPRequestHandler joins server_version and sys_version with a
        # space unconditionally, so an empty sys_version -- which is what
        # suppresses the "Python/3.x" half -- leaves `Server: claudio-door/1 `
        # with a trailing space on every response this door has ever sent.
        # Harmless on the wire and pinned in every capture, scrollback and bug
        # report that quotes it. Return the one field.
        return self.server_version

    def log_message(self, fmt, *args):        # no default access log
        pass

    def _respond(self, code, obj, close=False):
        body = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _reader_refusal(self, params):
        """(status, body) when this caller may not read, else None.

        Absent `readers.json` means reader auth is not configured. On loopback
        that is the development default and `serve()` has already warned about
        it; a non-loopback bind with no readers file is refused at startup, so
        this branch cannot be how an exposed door ends up open.
        """
        readers = self.door.readers
        if readers is None or not readers.configured:
            return None
        token = bearer(self.headers.get("Authorization"))
        scope = readers.scope(token)
        if scope is None:
            return api._refusal(
                "unauthorised",
                "no usable reader token on this request",
                "send `Authorization: Bearer <token>` with a token from the "
                "server's readers file; ask whoever runs the door for one",
                status=401)
        asked = params.get("account")
        if asked and not readers.permits(scope, asked):
            return api._refusal(
                "account-not-permitted",
                "this token may not read account %s" % asked,
                "ask for an account this token covers, or ask the door's "
                "operator to widen the token's scope",
                status=403)
        return None

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        # Unversioned, unenveloped, and its shape is pinned: operators and
        # scripts read this one, so it is deliberately not behind the API
        # envelope and deliberately not versioned with it.
        if path == PATH_HEALTH:
            self._respond(200, self.door.health())
            return
        if path.startswith(PATH_API):
            if self.api is None:
                status, obj = api._refusal(
                    "no-view", "this door was started with --no-api",
                    "restart the door without --no-api to serve the read API; "
                    "the store itself is unchanged and POST %s still works"
                    % PATH_SHIP, status=503)
                self._respond(status, obj)
                return
            multi = urllib.parse.parse_qs(qs, keep_blank_values=True)
            params = {k: v[-1] for k, v in multi.items()}
            # Reader auth, before the question is even parsed. The scope is
            # applied as a REFUSAL rather than as a filter: an account outside
            # it is named, because an empty result would be indistinguishable
            # from an account that has shipped nothing, and telling those two
            # apart is the whole point of this API's vocabulary.
            denied = self._reader_refusal(params)
            if denied is not None:
                self._respond(denied[0], denied[1])
                return
            try:
                status, obj = self.api.handle(path, params, multi)
            except duckstore.DuckDBMissing as exc:
                # LOUD and specific, never an empty result: "0 requests" is a
                # perfectly plausible answer for an account that has not
                # shipped yet, so a layer degrading into one is
                # indistinguishable from a working one over a quiet store.
                status, obj = api._refusal("duckdb-missing", str(exc),
                                           duckstore.INSTALL_HINT, status=503)
                self._respond(status, obj)
                return
            except Exception as exc:              # noqa: BLE001
                # Named, never a blank 500: a reader that fails silently is
                # indistinguishable from a store with nothing in it.
                #
                # The TYPE goes on the wire and the message does not.  Several
                # routes take caller-supplied identifiers, and an engine
                # exception over one of those carries the generated SQL and
                # DuckDB's candidate-binding list -- served to a caller this
                # API asks for no token from, over a question the layer should
                # have refused by name.  The operator still gets all of it, on
                # the process's own stderr, which is where a fault in the
                # reader belongs.
                traceback.print_exc()
                status, obj = api._refusal(
                    "reader-failed", type(exc).__name__,
                    "the store is unchanged and every record is still on "
                    "disk; this is a fault in the reader, and its full text "
                    "is on the server's stderr", status=500)
                self._respond(status, obj)
                return
            self._respond(status, obj)
            return
        self._respond(404, {"ok": False, "reason": "not-found",
                            "detail": "the door is POST %s; the read API is "
                                      "%s" % (PATH_SHIP, PATH_API_V1)})

    def do_POST(self):
        if self.path.split("?")[0] != PATH_SHIP:
            self.door.counters.refused("not-found")
            self._respond(404, {"ok": False, "reason": "not-found",
                                "detail": "the door is POST %s" % PATH_SHIP})
            return

        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            # `http.server` does not de-chunk, and a body read as-is would be
            # the chunk framing stored as records.  Refused rather than
            # guessed; `urllib` sends a Content-Length for a bytes body, so no
            # shipper in this repository can reach it.
            self.door.counters.refused("no-content-length")
            self._respond(411, {"ok": False, "reason": "no-content-length",
                                "detail": "chunked bodies are not accepted"},
                          close=True)
            return
        try:
            length = int(self.headers.get("Content-Length"))
        except (TypeError, ValueError):
            self.door.counters.refused("no-content-length")
            self._respond(411, {"ok": False, "reason": "no-content-length"},
                          close=True)
            return
        if length < 0 or length > self.door.max_body:
            # Refused WITHOUT reading it: the point of a cap is not to allocate
            # the thing it is capping.  The connection closes, because there is
            # an unread body still on it.
            self.door.counters.refused("body-too-large")
            self._respond(413, {"ok": False, "reason": "body-too-large",
                                "detail": "%d bytes; the cap is %d"
                                          % (length, self.door.max_body)},
                          close=True)
            return

        body = b""
        while len(body) < length:
            chunk = self.rfile.read(length - len(body))
            if not chunk:
                break
            body += chunk
        if len(body) != length:
            self.door.counters.refused("body-truncated")
            self._respond(400, {"ok": False, "reason": "body-truncated"},
                          close=True)
            return

        status, ack = self.door.ship(bearer(self.headers.get("Authorization")),
                                     self.headers.get("Content-Type"),
                                     self.headers.get("Content-Encoding"),
                                     body)
        self._respond(status, ack)
        if not self.quiet:
            sys.stderr.write(
                "[door] %s %s %s\n"
                % (status, ack.get("stream") or ack.get("reason") or "-",
                   ("%d record(s) -> %s" % (ack["accepted"], ack["tenant"]))
                   if ack.get("ok") else (ack.get("detail") or "")))
            sys.stderr.flush()


# --------------------------------------------------------------------------
# process
# --------------------------------------------------------------------------

LOOPBACK = ("127.0.0.1", "::1", "localhost")

TRUST_NOTE = (
    "this door has no transport security of any kind: no TLS, no mTLS, no "
    "token expiry and no rate limiting. The bearer token is a ROUTING LABEL, "
    "not a credential. Bind it on a trusted network and never on one that "
    "faces the internet.")


def _is_loopback(host):
    """Is this bind address reachable only from this machine?

    Compared as text rather than resolved: a name that resolves to 127.0.0.1
    today can resolve elsewhere tomorrow, and this decides whether an
    unauthenticated read API is allowed to exist. Anything not obviously
    loopback is treated as exposed, which is the safe direction.
    """
    h = (host or "").strip().lower()
    if h in ("127.0.0.1", "::1", "localhost", "ip6-localhost"):
        return True
    return h.startswith("127.")


def serve(host, port, root, tokens_path, reserve=DISK_RESERVE, quiet=False,
          ready=None, api_enabled=True, ui_enabled=None, readers_path=None):
    """Bind and serve until interrupted.  Returns a process exit status.

    `ui_enabled` is the old keyword and still works; there is no UI to enable
    any more, so it is only ever the API it switches.
    """
    if ui_enabled is not None:
        api_enabled = ui_enabled
    fd = lock_root(root)
    if fd is None:
        sys.stderr.write("[door] another door already holds %s (door.lock)\n"
                         % root)
        return 3
    counters = Counters()
    store = Store(root, reserve=reserve, counters=counters)
    tenants = Tenants(tokens_path)
    for where, why in tenants.refused:
        sys.stderr.write("[door] tokens: %s refused -- %s\n" % (where, why))
    if not tenants.map:
        # Not fatal: a door with no tokens answers 401 to everything, which is
        # a running server saying "I do not know you" rather than a silence
        # someone spends an afternoon on.
        sys.stderr.write("[door] WARNING no usable token -> account mapping in "
                         "%s; every batch will be refused 401\n" % tokens_path)

    readers = Readers(readers_path) if readers_path else None
    if readers is not None:
        for where, why in readers.refused:
            sys.stderr.write("[door] readers: %s refused -- %s\n" % (where, why))
    loopback = _is_loopback(host)
    if api_enabled and (readers is None or not readers.configured):
        if not loopback:
            # The one hard refusal. An unauthenticated read API serves every
            # record in the store, email addresses included; on loopback that
            # is a development convenience, and off it that is a breach. The
            # exposure and the auth are decided together or not at all.
            sys.stderr.write(
                "[door] refusing to serve the read API on %s with no reader "
                "auth.\n"
                "[door]   The read API returns every record in the store, "
                "email addresses included.\n"
                "[door]   Write %s, or start with --no-api.\n"
                % (host, readers_path or os.path.join(root, "readers.json")))
            os.close(fd)
            return 4
        sys.stderr.write(
            "[door] WARNING the read API asks for no token (no %s). "
            "Loopback only.\n" % (readers_path or "readers.json"))

    ShipHandler.door = Door(store, tenants, counters=counters)
    ShipHandler.door.readers = readers
    # Two readers, because there are two questions: the reconciler for stream
    # B, the windows and the coverage, and DuckDB over the same JSONL for the
    # ~30-column request rows.  The DuckDB handle is constructed unconditionally
    # and opens nothing until a query needs it, so a door on a machine with no
    # duckdb still starts, still takes shipments, and refuses the stream-A
    # routes BY NAME with the install command.
    ShipHandler.api = (api.Api(View(root), duck_store=duckstore.DuckStore(root),
                               counters=counters)
                       if api_enabled else None)
    ShipHandler.quiet = quiet
    try:
        httpd = ThreadingHTTPServer((host, port), ShipHandler)
    except OSError as exc:
        sys.stderr.write("[door] cannot bind %s:%d: %s\n" % (host, port, exc))
        return 1
    httpd.daemon_threads = True
    bound = httpd.server_address[1]
    sys.stderr.write("[door] listening on http://%s:%d%s\n"
                     "[door] store   %s\n"
                     "[door] tenants %d, reserve %d bytes, free %s\n"
                     % (host, bound, PATH_SHIP, store.root,
                        len(tenants.tenants()), reserve, store.free_bytes()))
    if api_enabled:
        sys.stderr.write(
            "[door] api     http://%s:%d%s  (read-only, no token)\n"
            "[door] engine  duckdb %s\n"
            % (host, bound, PATH_API_V1,
               "present" if duckstore.available()
               else "MISSING -- stream A routes will refuse by name; "
                    "python3 -m pip install duckdb"))
    if host not in LOOPBACK:
        sys.stderr.write("[door] NOTE %s\n" % TRUST_NOTE)
        if api_enabled:
            sys.stderr.write(
                "[door] NOTE the API at %s serves every record in this "
                "store, including email addresses, to anyone who can reach "
                "this port, and it asks for no token. --no-api turns it "
                "off.\n" % PATH_API_V1)
    sys.stderr.flush()
    if ready is not None:
        ready(bound)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        os.close(fd)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="serve.py", description="the claudio store's door")
    p.add_argument("--root", required=True,
                   help="store directory (accounts/, offsets/ live under it)")
    p.add_argument("--tokens", default=None,
                   help="JSON {token: account_uuid} (default <root>/tokens.json)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; %s" % TRUST_NOTE)
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--disk-reserve-bytes", type=int, default=DISK_RESERVE,
                   help="answer 507 rather than take the filesystem under this")
    p.add_argument("--readers",
                   help="JSON {token: [account_uuid, ...] | \"*\"} granting "
                        "READ access to the API (default <root>/readers.json). "
                        "Absent means the read API asks for no token, which is "
                        "allowed on loopback and refused on any other bind.")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--no-api", "--no-ui", dest="api", action="store_false",
                   default=True,
                   help="do not serve the read-only API at /api/v1/*; it is "
                        "unauthenticated and shows everything in the store. "
                        "`--no-ui` is the old spelling and still works -- "
                        "there is no bundled UI to disable any more, omini is "
                        "the front end.")
    args = p.parse_args(argv)
    return serve(args.host, args.port, args.root,
                 args.tokens or os.path.join(args.root, "tokens.json"),
                 reserve=args.disk_reserve_bytes, quiet=args.quiet,
                 api_enabled=args.api,
                 readers_path=(args.readers
                               or os.path.join(args.root, "readers.json")))


if __name__ == "__main__":
    sys.exit(main())
