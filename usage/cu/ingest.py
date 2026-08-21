"""Raw OTLP capture -> ledger rows.  The step that used to happen never.

This is `claudio usage`'s ingest, MOVED here rather than copied, for the same
reason `tail.py` exists: a second reader needed it.  That reader is the
receiver's own watchdog, and until it had one, **nothing on any automatic path
ingested at all**.

That was not a gap in tidiness.  `do_ingest` was reached from exactly two
places, both of them in `claudio usage main()` -- the `ingest` verb and
`collect stop` -- so on a fully configured `logging=remote` machine the
receiver wrote `raw/api_request.jsonl`, the shipper read `ledger.jsonl`, and
nothing ever turned one into the other.  `ledger.jsonl` therefore never
existed, `ship._ship_stream` answered "no such file yet; nothing to say", and
`run_pass` recorded no failure and cleared `ship.err`.  Reproduced with real
processes: 7 real OTLP payloads captured durably, several shipping ticks, the
door accepted **zero** stream-A batches, and both `ship.err` and `ship.warn`
were absent.  Stream B shipped fine the whole time -- so the machine delivered
plan percentages and no request rows, which on the server is indistinguishable
from a machine that made no requests.  That is the exact failure `logging=
remote` was added to close.

The lock is what makes an automatic ingest safe beside a manual one: it
refuses rather than waits, so a tick that collides with someone's
`claudio usage ingest` is a skipped tick and never a doubled ledger.
"""

import contextlib
import fcntl
import os

from . import ledger, otlp, tail

# Re-exported under the names `claudio usage` has always used, so every
# existing caller and every existing test is unchanged.
file_identity = tail.file_identity
start_offset = tail.start_offset


def offsets_path(cfg):
    return tail.offsets_path(cfg)


def load_offsets(cfg):
    return tail.load_offsets(cfg)


def save_offsets(cfg, offsets):
    return tail.save_offsets(cfg, offsets)


def lock_path(cfg):
    return os.path.join(cfg.state_dir, "ingest.lock")


@contextlib.contextmanager
def ingest_lock(cfg, on_busy=None):
    """Exclusive for the whole of ingest, and it fails rather than waits.

    `do_ingest` reads the existing ids, reads the raw files, appends and saves
    the offsets.  Two of those interleaved append the same rows twice: each
    read its ids before the other appended.  This is not a rare race --
    `collect stop` runs an ingest itself, so it is one keystroke away from any
    manual `ingest`, and it was reproduced: both runs printed "ingested 8 new
    requests", the ledger held 16 rows for 8 distinct requests, every `show
    --by account` bucket doubled, and `doctor` reported health throughout.

    flock, so the lock dies with the process and a crash cannot strand it.
    Non-blocking and loud: a silent wait would hide the concurrency from the
    person who could fix it, and this project's whole failure mode is silence.
    The read-side dedupe in `ledger.read` is the other half -- a lock stops
    the ledger doubling tomorrow, only the dedupe repairs one doubled
    yesterday.

    `on_busy` is how the receiver's tick asks the same question without the
    exit: a refused acquire there is a skipped tick, not an error, because the
    ingest already running is doing the work this one would have done.  A
    caller that passes nothing still gets `SystemExit`, which is what a person
    at a terminal needs.
    """
    cfg.ensure_dirs()
    fh = open(lock_path(cfg), "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if on_busy is not None:
                on_busy()
                return
            raise SystemExit(
                "another ingest is already running (lock: %s).\n"
                "Refusing to run a second one: two concurrent ingests append "
                "the same rows twice.\n"
                "`collect stop` ingests by itself -- wait for it to finish and "
                "run this again." % lock_path(cfg))
        yield
    finally:
        fh.close()          # closing releases the flock


def do_ingest(cfg, quiet=False, from_zero=False):
    """Read new collector output into the ledger.  Idempotent."""
    cfg.ensure_dirs()
    with ingest_lock(cfg):
        return _ingest_locked(cfg, quiet, from_zero)


def tick(cfg):
    """The receiver's automatic ingest: quiet, non-blocking, never fatal.

    Returns the number of rows written, or None when it did not run.  It
    swallows everything for `ship.tick`'s reason, one file over: an exception
    escaping into the watchdog thread would end the tick loop, and with it the
    shipping pass, the idle exit and the session sweep.
    """
    state = {"ran": True, "written": None}

    def busy():
        state["ran"] = False

    try:
        cfg.ensure_dirs()
        with ingest_lock(cfg, on_busy=busy):
            if state["ran"]:
                state["written"] = _ingest_locked(cfg, quiet=True,
                                                  from_zero=False)
    except Exception:                        # noqa: BLE001 -- see the docstring
        return None
    return state["written"]


def _ingest_locked(cfg, quiet, from_zero):
    offsets = load_offsets(cfg)
    # Fingerprints, not just ids: `request_id` hashes session, timestamp, the
    # four token counts and the model, so every field that could break a tie
    # is excluded from it and a genuine collision is byte-indistinguishable
    # from an idempotent re-read.  The hash is deliberately NOT changed -- that
    # would re-identify every historic row and double the ledger on the next
    # ingest -- so the collision is *reported* instead.
    seen = ledger.existing_fingerprints(cfg.ledger)
    new_rows, malformed, rescans, collisions = [], 0, [], 0
    stats = {}

    for path in otlp.raw_files(cfg.raw_dir):
        # `file_identity_quiet`: an unreadable raw file is handed to
        # `read_file` anyway, which reports its own read failure, so raising
        # here would replace a counted skip with a traceback out of a command.
        ident = tail.file_identity_quiet(path)
        start, why = start_offset(offsets.get(path), ident, path, from_zero)
        if why:
            rescans.append((path, why))
        rows, new_off, bad = otlp.read_file(path, start, stats)
        entry = {"offset": new_off}
        # The anchor is read AFTER the read, because it is a hash of the bytes
        # this pass just consumed -- see `tail.anchor` for the Linux inode reuse
        # that makes dev+ino+head insufficient on its own.
        _anchor = tail.anchor(path, new_off)
        if _anchor:
            entry["anchor"] = _anchor
        if ident:
            entry.update(ident)
        offsets[path] = entry
        malformed += bad
        for r in rows:
            fp = ledger.fingerprint(r)
            rid = r["request_id"]
            if rid in seen:
                if seen[rid] != fp:
                    collisions += 1
                continue
            seen[rid] = fp
            new_rows.append(r)

    new_rows.sort(key=lambda r: r["ts"])
    # `append` fsyncs before this returns, and only then is an offset written
    # that accounts for those rows: a rename is journalled metadata while the
    # appended data is not, so the other order can leave offsets describing
    # rows that did not survive a power loss -- and the raw file they came
    # from is rotated away, so `--from-zero` cannot rebuild them.
    written = ledger.append(cfg.ledger, new_rows, stats)
    save_offsets(cfg, offsets)
    if not quiet:
        _report_ingest(stats, written, malformed, rescans, collisions)
    return written


def _report_ingest(stats, written, malformed, rescans, collisions):
    for path, why in rescans:
        print("rescanned %s from byte 0: %s" % (path, why))

    # Records dropped for their event name, named.  Claude Code renaming the
    # event drops every record for ever -- the offset advances past them, so
    # `--from-zero` does not bring them back either -- and until this line
    # existed the only symptom was a count of zero, which is exactly what an
    # idle machine looks like.  It is a report, not a failure: the exit stays
    # 0 and the offset is not frozen, because a wrong guess about which name
    # is "right" must not be able to stop ingest.
    skipped = stats.get("skipped", 0)
    if skipped:
        drift = sorted(str(n) for n in (stats.get("events") or {})
                       if n != otlp.EVENT_NAME)
        # `kept`, not `wrote`.  This figure is records that PARSED, which is
        # not what reached the ledger: a re-read, a `--from-zero` or the
        # automatic rescan after a rotation all parse rows the ledger already
        # holds, and the line then read "wrote 1" directly above "ingested 0
        # new requests" -- two adjacent statements, one of them false, on
        # exactly the recovery paths a user runs when they already suspect
        # data is missing.  The "ingested N" line below is the only claim
        # about what was written, and it stays the only one.
        print("read %d log records, kept %d, skipped %d (event.name: %s)"
              % (stats.get("records", 0), stats.get("records", 0) - skipped,
                 skipped, ", ".join(drift)))

    msg = "ingested %d new request%s" % (written, "" if written == 1 else "s")
    if malformed:
        msg += " (%d malformed line%s skipped)" % (
            malformed, "" if malformed == 1 else "s")
    print(msg)

    recovered = stats.get("recovered", 0)
    if recovered:
        print("recovered %d record%s from a torn line (a fragment of an "
              "interrupted write preceded it)"
              % (recovered, "" if recovered == 1 else "s"))

    if stats.get("torn_lines_terminated"):
        print("%d torn line(s) in the ledger were terminated before this "
              "append, so the fragment cost itself and not the record behind "
              "it" % stats["torn_lines_terminated"])

    if collisions:
        print("%d record(s) skipped as duplicates of a DIFFERENT request "
              "-- same request_id, different content. The id hashes only "
              "session, timestamp, tokens and model, so this is a real hash "
              "collision and those records are NOT in the ledger."
              % collisions)
