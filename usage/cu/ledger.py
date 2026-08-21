"""The canonical per-request ledger: stream A, normalised.

One JSON object per line, append-only, deduped on `request_id`.  Every row
carries all four token classes as separate required columns — never folded
together, never omitted — because cache reads were ~89% of this user's tokens,
so a figure summing only input+output is wrong by an order of magnitude rather
than by a rounding error.

`cu/otlp.rows_from_payload` is the one place a row is built, and therefore the
definition of the columns; there is no separate field list here to drift from
it.
"""

import hashlib
import json
import os

from . import tail


def request_id(session_id, ts_ns, inp, out, cread, ccreate, model):
    """Stable identity for a request, so ingest is idempotent.

    Derived from content rather than assigned, because the collector may be
    re-read from byte zero after a rotation and the same record must collapse
    onto the same id both times.

    **`prompt_id` is deliberately absent from this hash**, even though it is a
    column now.  Rows written before that column existed must collapse onto
    the same id as the same record re-read today, or the first re-ingest after
    the upgrade silently doubles every historic request.  Everything hashed
    here has been present since the first row was written.
    """
    raw = "|".join(str(x) for x in
                   (session_id or "", ts_ns, inp, out, cread, ccreate, model or ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def fingerprint(row):
    """The fields that distinguish two requests sharing a `request_id`.

    Every one of these is deliberately absent from the hash, so two genuinely
    different requests that collide differ here and nowhere else.
    """
    return (row.get("prompt_id"), row.get("agent_name"), row.get("duration_ms"),
            row.get("cost_usd_reported"), row.get("query_source"))


def read(path, stats=None):
    """Yield ledger rows, at most one per `request_id`.

    Deduping on READ, not only on write, is what repairs a ledger that two
    concurrent ingests have already doubled: a lock added today cannot undo
    yesterday's duplicate rows, and every total drawn from this file was wrong
    until it did.  A malformed line is skipped, never fatal.

    Pass `stats` (a dict) to learn how much was suppressed; it is filled once
    the generator is exhausted, so a partial consumer sees nothing.  Two
    counters, because they are two different events wearing one face:

      duplicates  same id, same fingerprint -- the same request written twice,
                  which is a doubled ledger and is fully repaired by dropping
                  the copy.
      collisions  same id, DIFFERENT fingerprint -- two genuinely different
                  requests that hashed alike, so suppressing the second one
                  loses a real record.  It cannot be repaired here, only
                  reported, which is exactly why it is counted separately
                  rather than folded into `duplicates` where it would look
                  like housekeeping.
    """
    counts = {"duplicates": 0, "collisions": 0, "malformed": 0, "recovered": 0}
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        if stats is not None:
            stats.update(counts)
        return
    seen = {}
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                # The line already on disk from before `append` grew its
                # newline guard: a fragment of an interrupted write with a
                # complete record glued onto the back of it.  `otlp.read_file`
                # recovers exactly this shape by cutting back to the last
                # record head, and the good half is a real request that is
                # otherwise lost from every total for ever.  The fragment is
                # still counted as malformed -- something WAS torn -- so a
                # recovery never reads as a clean file.
                rec = None
                cut = line.rfind('{"request_id"')
                if cut > 0:
                    try:
                        rec = json.loads(line[cut:])
                    except ValueError:
                        rec = None
                counts["malformed"] += 1
                if not isinstance(rec, dict):
                    continue
                counts["recovered"] += 1
            rid = rec.get("request_id")
            if rid:
                fp = fingerprint(rec)
                if rid in seen:
                    counts["duplicates" if seen[rid] == fp else "collisions"] += 1
                    continue
                seen[rid] = fp
            yield rec
    if stats is not None:
        stats.update(counts)


def existing_ids(path):
    return {r.get("request_id") for r in read(path) if r.get("request_id")}


def existing_fingerprints(path):
    """{request_id: fingerprint} -- so an id collision can be told apart from
    an idempotent re-read, which are byte-identical without it."""
    return {r["request_id"]: fingerprint(r)
            for r in read(path) if r.get("request_id")}


def append(path, rows, stats=None):
    """Append rows as JSONL.  Returns the number written.

    Two properties, and each one is a record this file used to lose.

    **A torn last line is terminated before anything is written after it.**  A
    process killed mid-append -- or an ENOSPC -- leaves a fragment with no
    trailing newline, and the next append used to write straight onto it: one
    torn fragment plus one perfectly good row became one unparseable line, so
    `read` reported `malformed: 1` and the good row was gone.  The raw offset
    had already advanced, so a normal `ingest` never regenerated it, and
    `ship._select` consumed the same bytes and advanced its offset past it too
    -- unshippable as well as unreadable.  `serve.Store._append` has had this
    guard from the start, with a comment saying it exists "instead of gluing a
    fragment onto the front of a good record and costing two"; this is the
    same fix on the other end of the same wire.  Pass `stats` (a dict) to
    learn that it fired: a torn line is still a damaged record and stays
    visible rather than being quietly stitched over.

    **The data is fsynced before the caller records an offset for it.**  See
    `tail.save_offsets`: the offsets file is a rename, which is journalled,
    while these bytes are page cache, which is not.
    """
    if stats is not None:
        stats.setdefault("torn_lines_terminated", 0)
    if not rows:
        return 0
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    blob = b"".join(
        (json.dumps(r, separators=(",", ":"), sort_keys=False) + "\n").encode("utf-8")
        for r in rows)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        if size and os.pread(fd, 1, size - 1) != b"\n":
            blob = b"\n" + blob
            if stats is not None:
                stats["torn_lines_terminated"] += 1
        n = 0
        while n < len(blob):
            n += os.write(fd, blob[n:])
        tail.fsync(fd)
    finally:
        os.close(fd)
    return len(rows)
