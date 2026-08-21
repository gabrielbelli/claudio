"""Resume a byte-offset read across a rotation, once, for every reader.

This is `claudio usage`'s ingest resume logic, MOVED here rather than copied.
It was moved the moment a second reader needed it -- the shipper -- because a
second implementation of exactly this is what cost 57% of a machine's captured
spend the first time, and the scar is written into `start_offset`'s docstring
below.  `claudio usage` imports these names and re-exports them, so the ingest
side is byte-for-byte the same code path it always was, and a test asserts the
two are the *same function object* rather than two that happen to agree today.

Nothing here knows what a record is.  It answers one question -- "where may I
resume reading this file, and why" -- and the answer is always safe to get
wrong in the direction of 0, because both readers downstream dedupe on
content.
"""

import fcntl
import hashlib
import json
import os


def file_identity(path):
    """(st_dev, st_ino) plus a hash of the first 256 bytes, or None.

    What any tail-follow stores next to a byte offset, and for the same
    reason.  dev+ino catches the ordinary rotation, where a new file takes the
    old name; the head hash catches the case where the inode is reused or the
    file is restored from a copy, which dev+ino alone would call unchanged.

    **None means ENOENT and nothing else.**  It used to mean any `OSError` at
    all, so "the file does not exist yet" and "the file exists and I cannot
    read it" were one answer -- and every caller reads that answer as *nothing
    to do*.  `cu.ship._ship_stream` returned True on it, which contributes to
    no failure list, so the next pass then cleared the `ship.err` the previous
    one had written: one `chmod 000` on `ledger.jsonl` ended shipping for good
    with every diagnostic surface clean.  EACCES, EIO and a stale mount are
    faults about a file that is there, so they are raised and named by whoever
    asked.
    """
    try:
        st = os.stat(path)
        with open(path, "rb") as fh:
            head = fh.read(256)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        # A path component that is not a directory: the file cannot exist,
        # which is the same answer as ENOENT and not a fault to report.
        return None
    return {"dev": st.st_dev, "ino": st.st_ino,
            "head": hashlib.sha256(head).hexdigest()[:16]}


def file_identity_quiet(path):
    """`file_identity`, with an unreadable file answering None as well.

    For the callers whose next step copes on its own -- `claudio usage`'s
    ingest hands the path to `otlp.read_file`, which reports its own read
    failure -- so that raising here would replace a counted skip with a
    traceback out of a command.  Anything that must not be quiet about an
    unreadable file calls `file_identity` and catches.
    """
    try:
        return file_identity(path)
    except OSError:
        return None


def anchor(path, offset):
    """A hash of the bytes immediately BEFORE `offset`, or None.

    dev+ino+head answers "is this the same file".  On Linux it can answer that
    wrong, and it answers wrong in the only direction that loses records.
    Inode numbers are reused immediately there, so a file unlinked and rewritten
    at the same path takes the inode back -- and the first 256 bytes of an
    append-only file rebuilt from its own start are unchanged by construction,
    so all three fields match a file that is NOT the one the offset describes.
    The resume then lands in the middle of somebody else's bytes and everything
    before it is skipped in silence: byte for byte the 57%-of-spend failure this
    module exists to prevent, on the platform a shipping host actually runs.

    Measured in six lines, same code both sides: macOS returned a new `ino` for
    the replacement, Debian returned the identical (dev, ino, head) triple.

    So the offset carries its own evidence.  This is what the reader had just
    consumed when it stopped; if those bytes are not there any more, the file
    under the offset is a different file whatever its inode says.  256 bytes,
    the same window as the head, one read per file per pass.

    None means "cannot vouch for it" -- offset 0, an unreadable file, or a file
    now shorter than the offset -- and `start_offset` reads that as a mismatch
    rather than as a pass, because the whole point is to stop trusting an
    offset nothing can confirm.
    """
    off = int(offset or 0)
    if off <= 0:
        return None
    n = min(256, off)
    try:
        with open(path, "rb") as fh:
            fh.seek(off - n)
            buf = fh.read(n)
    except OSError:
        return None
    if len(buf) != n:
        return None
    return hashlib.sha256(buf).hexdigest()[:16]


def start_offset(stored, ident, path, from_zero=False):
    """Where to resume reading, and why -- (offset, reason-or-None).

    The offset used to be keyed on path alone, with no way to tell that the
    file at that path was no longer the file the offset described.  When the
    raw file was replaced under a live offset the next ingest resumed at the
    stale byte position and skipped everything before it, in silence: on this
    machine that put five of eight real records permanently out of reach,
    57% of the captured spend, while `ingest` printed "ingested 0 new
    requests" and the offsets file read exactly EOF.  `otlp.read_file`'s
    `size < offset` guard is not a substitute -- it only fires if you happen
    to look while the replacement is still shorter than the old offset, and
    ingest normally runs on `collect stop`, after a whole session of writes.

    Resuming is therefore only allowed when the file proves it is the same
    file.  Every other answer is 0, which is safe at any time because
    `ledger.request_id` dedupes on content: a re-read costs one parse, never a
    duplicate row.  That is also why a *legacy* integer offset -- written
    before identity was recorded, and so unverifiable -- rescans once rather
    than being trusted.  The dedupe was always documented as what made a
    re-read safe; nothing had ever made a re-read possible.

    The shipper leans on the same property from the other end: a resend is
    safe because both streams are deduped server-side (`request_id` for
    stream A, `wire.sample_key` for stream B), so a rotation it cannot verify
    costs one repeated POST and never a lost record.
    """
    if from_zero:
        return 0, "--from-zero"
    if stored is None:
        return 0, None                       # never read; not a rescan
    if not isinstance(stored, dict):
        return 0, "no file identity recorded (offsets predate this check)"
    if ident is None:
        return int(stored.get("offset") or 0), None     # unreadable; read_file copes
    for k in ("dev", "ino", "head"):
        if stored.get(k) != ident.get(k):
            return 0, "file replaced or rotated since the last ingest"
    off = int(stored.get("offset") or 0)
    if off:
        # `path` is required rather than defaulted, and that is deliberate: an
        # optional path would let a caller drop the anchor check by omission and
        # get a plausible offset back, which is the shape of every silent loss
        # in this project.  A caller that cannot name the file fails loudly.
        if not stored.get("anchor"):
            # Written before the anchor existed, so identity alone decided it
            # -- unverifiable, exactly like the legacy integer above, and given
            # the same answer: rescan once, then every later pass has one.  A
            # re-read costs one parse (`ledger.request_id` dedupes) or one
            # repeated POST (both streams dedupe server-side).
            return 0, "no anchor recorded (offsets predate this check)"
        if anchor(path, off) != stored["anchor"]:
            return 0, ("the bytes before the stored offset are not the ones "
                       "that were read (the file was replaced under it)")
    return off, None


# The two offset ledgers, named here so neither side spells the other's file.
# They are separate on purpose: ingest's offset says how much of `raw/` has
# become ledger rows, and the shipper's says how much of the ledger has left
# the machine.  One file for both would make an ingest advance the shipper
# past records it never sent.
INGEST_OFFSETS = "ingest-offsets.json"
SHIP_OFFSETS = "ship-offsets.json"


def offsets_path(cfg, name=INGEST_OFFSETS):
    return os.path.join(cfg.state_dir, name)


def load_offsets(cfg, name=INGEST_OFFSETS):
    try:
        with open(offsets_path(cfg, name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def fsync(fd):
    """`F_FULLFSYNC` where the platform has it, else `fsync`.

    `serve._fsync`'s shape, and here for its reason: on Darwin `fsync` returns
    once the data has reached the drive's write cache, so "durable" then means
    "survives the process", not "survives the power".
    """
    try:
        if hasattr(fcntl, "F_FULLFSYNC"):
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
    except OSError:
        pass                                  # fall through to plain fsync
    os.fsync(fd)


def fsync_dir(path):
    """Make a rename durable.  A rename is metadata; the directory holds it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def save_offsets(cfg, offsets, name=INGEST_OFFSETS):
    """temp + rename, with both fsyncs -- the file and then its directory.

    The order in the *code* was always right (`ledger.append` then this), and
    the order on the *disk* was not guaranteed: a rename is a journalled
    metadata operation while appended data is unjournalled page cache, so a
    power loss could leave an offsets file that accounts for rows the ledger
    never got.  Ingest would then resume past raw bytes it never turned into
    rows, and the raw file is rotated away, so `--from-zero` could not recover
    them either.  `serve.Store.save_offsets` is the shape this copies; one
    fsync per ingest run is not a cost worth trading a silent gap for.
    """
    cfg.ensure_dirs()
    path = offsets_path(cfg, name)
    tmp = path + ".%d.tmp" % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        data = json.dumps(offsets).encode("utf-8")
        n = 0
        while n < len(data):
            n += os.write(fd, data[n:])
        fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    fsync_dir(os.path.dirname(path))
