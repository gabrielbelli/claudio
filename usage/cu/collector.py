"""Receiver process lifecycle, plus the uptime ledger of when it was up.

THE FILENAME IS STALE ON PURPOSE.  There is no collector any more -- this
module drives `../recv/otlp-recv` -- and renaming it to `receiver.py` would be
a rename across `claudio`, `test.sh`, three READMEs and every importer here for
no behavioural gain.  So the name stays and this sentence carries the
correction, which is the trade this project makes everywhere else it cannot
delete a misleading label cheaply.

Not a daemon, and no longer a container either.  The receiver is
`../recv/otlp-recv`: a stdlib Python process, spawned on demand by `claudio
run` before it `exec`s claude, kept alive by the status-line shim, exiting by
itself when the work stops.  This module is the library side of that -- the
pidfile, the lock, the session directory, the uptime ledger and the counters --
so that claudio (POSIX sh, which can import none of it) has exactly one set of
paths to hardcode, and they are declared in `config.py`.

**Why not a service.**  Three arguments, all settled.  Its flush argument was
measured false -- see below.  Its coverage argument was near-empty: `_build_env`
writes the OTLP variables only into the environment of the `exec`ed claude, so
stream A does not exist outside claudio-launched sessions and an always-on
listener would be attached to an unplugged wire.  And `launchctl`/`systemctl`
cannot be stubbed in a hermetic suite, while everything here is pinned by one.

**Why not otelcol-contrib.**  Idle self-exit is in the design and the collector
cannot do it without a second watcher process -- the daemon returning by the
side door.  93-107MB, no Homebrew formula, and untestable here.  It regains the
argument when remote mTLS and a durable retry queue matter; that swap is a
later decision on its own evidence.

`uptime.jsonl` records *facts*: when recording started, when it stopped, and
whether the stop was graceful.  It carries no judgement.  A local
`downtime_checker` used to turn those spans into a verdict about which plan
movement "we were not listening" for; that verdict was one half of an
attribution split, and attribution needs every machine on the account.  The
spans are still worth recording — the server needs to know which machines were
listening over a window it is closing — so the facts stay and the reasoning
goes.

**A graceful stop is a labelling concern, not a data one.**  This module used
to claim the file exporter buffers ~4KB pages, does not flush on idle, and
loses its tail to a `docker kill`.  That is false, and it was reproduced wrong
twice.  What actually happens, measured twice on 0.158.0: 5 OTLP records were
POSTed and a host-side `wc -c` on the bind-mounted file read **0 bytes** —
then `docker kill` (SIGKILL, no grace period) and `docker rm -f`, and the file
held all 5 records, 3355 bytes.  The zero was macOS VirtioFS bind-mount
coherence lag: the host could not see bytes that were already durable.  With
`rotation:` configured the exporter writes through lumberjack, which is
unbuffered per write.

So no shutdown path is load-bearing for **data integrity**; there is no
buffered tail to lose.  The native receiver keeps that property by
construction: every payload is one `write(2)` on an `O_APPEND` file before the
200 goes back.  What `stop()` still buys is narrower and must be stated as
such: `clean` in `uptime.jsonl` is only earned by an *observed* graceful stop --
here, by the receiver living long enough to write its own stop record -- and
that label is what a server is later told about which machines were listening.
A killed receiver costs the label, not the records — a **reporting** loss,
never a data one.

The transferable lesson, which is the only part of the old note worth
keeping: never conclude anything about durability from what the host can see
through a bind mount.  Running the receiver natively removes the bind mount,
and with it the artefact.
"""

import errno
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
import urllib.request

from . import config

# The receiver this module starts and stops.  Resolved, never a constant:
# `receiver=` in claudio's global conf outranks the copy that ships beside
# `cu/`, and `config.receiver()` is the single implementation of that rule --
# shared with claudio's own `_recv_cmd` in spirit and pinned against it by
# test.  It is a function call at every use site rather than a module constant
# because the conf can be written after this module is imported, and a value
# frozen at import time would report the previous machine's answer.
def receiver():
    return config.receiver()


def receiver_missing():
    """Why there is no receiver, naming where this looked.

    "receiver missing at <path>" is a true sentence that sends the reader to
    the wrong place when `receiver=` is what named the path: they check the
    tree, find the file exactly where it ships, and have no way to reach the
    line that overrode it.  So the override is named when it is what decided.
    """
    r = receiver()
    if r != config.BUNDLED_RECEIVER:
        return ("receiver not found at %s -- named by receiver= in %s"
                % (r, config.conf_path()))
    return "receiver not found at %s" % r

# How long `stop()` waits for SIGTERM to be taken before escalating.  The
# receiver's own shutdown is a `serve_forever` return plus two small file
# writes, so this is slack, not a budget.
STOP_TIMEOUT = 10

# How long `start()` waits for the pidfile to appear.  `docker run -d` returned
# on CREATE and `start` reported success over a container that had already
# exited on a config error; every session after that exported into nothing,
# silently.  A spawn has the same shape, so a start that cannot say the
# receiver is still there is a start that failed.
START_TIMEOUT = 5.0
START_TICK = 0.05

# A `recv.lock` older than this is assumed abandoned.  The lock is held across
# the spawn window only -- tens of milliseconds -- so a minute is three orders
# of magnitude of slack.  It has to expire at all because a spawner killed
# between `mkdir` and exec would otherwise stop every future spawn for ever.
LOCK_STALE = 60

# The crash-loop cap, shared with the receiver, which enforces the backstop
# half of it.  Five starts in a minute that never reached a bind is a loop.
MAX_STARTS = 5
START_WINDOW = 60


def available():
    """Can the receiver be run at all."""
    return os.path.exists(receiver())


def host_slug():
    """claudio's `host=` tag value, computed identically.

    Must match `_host_slug` in ../claudio byte for byte or the uptime spans
    will not join the request rows they are meant to explain: CLAUDIO_HOST or
    `uname -n`, domain dropped, lowercased, runs of anything else collapsed to
    a dash, dashes trimmed off both ends.
    """
    h = os.environ.get("CLAUDIO_USAGE_HOST") or os.environ.get("CLAUDIO_HOST") \
        or platform.node() or ""
    h = h.split(".")[0].lower()
    return re.sub(r"[^a-z0-9_-]+", "-", h).strip("-")


# --------------------------------------------------------------------------
# pidfile and liveness
# --------------------------------------------------------------------------

def write_pidfile(cfg, pid, token, port, started):
    """Four lines -- pid, token, port, started-at -- written temp+rename.

    Atomic because the reader is the status-line shim, running several times a
    second: a pidfile read halfway through a `write` would report a receiver
    that is not there, or nothing at all, and either answer respawns over a
    live process.

    One field per line rather than a tab-separated record, for claudio's own
    reason: a tab is IFS whitespace, so a run of tabs collapses and an empty
    field disappears, silently shifting every later field.  `{ read pid; read
    token; } < recv.pid` is two builtins and no fork, which is what the shim
    can afford.
    """
    tmp = cfg.recv_pid + ".%d.tmp" % pid
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("%d\n%s\n%d\n%.3f\n" % (pid, token, port, started))
    os.replace(tmp, cfg.recv_pid)


def read_pidfile(cfg):
    """{"pid","token","port","started"} or None.  Tolerates a truncated file."""
    try:
        with open(cfg.recv_pid, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    out = {"pid": pid, "token": lines[1].strip(), "port": None, "started": None}
    if len(lines) > 2:
        try:
            out["port"] = int(lines[2].strip())
        except ValueError:
            pass
    if len(lines) > 3:
        try:
            out["started"] = float(lines[3].strip())
        except ValueError:
            pass
    return out


def clear_pidfile(cfg, token):
    """Remove the pidfile only if it still carries this token.

    This is what the token is for, and it is not theoretical: a receiver
    shutting down while its replacement is already binding would otherwise
    delete the new one's pidfile on the way out, and the supervisor would
    respawn over a live receiver, which cannot bind, which writes `recv.err`,
    which the user reads as a fault.  A pid alone cannot tell the two apart.
    """
    got = read_pidfile(cfg)
    if got and got.get("token") == token:
        try:
            os.unlink(cfg.recv_pid)
        except OSError:
            pass


def pid_alive(pid):
    """kill -0, with "alive but not mine" counted as alive.

    Cheap and not exact: a recycled pid answers yes.  Nothing portable is both
    cheap and exact -- there is no /proc on macOS and a start time costs a fork
    -- so the exactness lives elsewhere.  The receiver removes its own pidfile
    on every graceful exit, so a stale one survives only a SIGKILL; and
    `verify_running` asks the process itself, over the port, for the token.
    The supervisor is expected to use `pid_alive` on every render and
    `verify_running` rarely.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _reap(pid):
    """Clear the pid if it is a zombie child of *this* process.

    An exited process that nobody has waited for still answers `kill -0`, so a
    caller that spawned the receiver itself -- `start()` here, and the test
    suite -- would watch a corpse for the whole grace period and then "escalate
    to SIGKILL", recording a perfectly graceful stop as unclean.  Harmless when
    the pid is not our child: `waitpid` simply says so.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def running(cfg):
    """The pidfile of a live receiver, or None.  Cheap: no socket."""
    got = read_pidfile(cfg)
    if got and pid_alive(got["pid"]):
        return got
    return None


def health(cfg, timeout=2):
    """`GET /healthz` off the running receiver, or None.

    The counters live here while the process does.  A scrape failure costs a
    reconciliation line, never a stop: this is diagnostics about the data path,
    not the data path.
    """
    got = read_pidfile(cfg)
    if not got or not got.get("port"):
        return None
    url = "http://127.0.0.1:%d/healthz" % got["port"]
    try:
        with urllib.request.urlopen(url, timeout=timeout) as fh:
            return json.loads(fh.read().decode("utf-8", "replace"))
    except Exception:
        return None


def verify_running(cfg):
    """True/False/None: is the process answering with *our* token.

    The exact answer `pid_alive` cannot give, at the cost of a socket.  None
    means the question could not be asked (no pidfile, or nothing answered),
    which is not the same as False and must never be reported as one.
    """
    got = read_pidfile(cfg)
    if not got:
        return None
    body = health(cfg)
    if body is None:
        return None
    return body.get("token") == got.get("token")


# --------------------------------------------------------------------------
# lock, error file, start accounting
# --------------------------------------------------------------------------

def claim_lock(cfg):
    """`mkdir` the spawn lock.  True if this caller may spawn.

    A directory because `mkdir` is atomic in POSIX and needs no helper, which
    matters because the other caller of this contract is a shell script.  The
    loser of a race walks away and does not spawn -- it does not wait, because
    whoever won is about to bind the one port there is.

    A lock older than `LOCK_STALE` is broken rather than obeyed: a spawner
    killed between the `mkdir` and the exec would otherwise stop every future
    spawn for ever, and a permanent refusal to record is worse than a rare
    double spawn (of which the loser simply fails to bind and exits).
    """
    cfg.ensure_dirs()
    try:
        os.mkdir(cfg.recv_lock, 0o700)
        return True
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            return False
    try:
        age = time.time() - os.stat(cfg.recv_lock).st_mtime
    except OSError:
        return False
    if age < LOCK_STALE:
        return False
    release_lock(cfg)
    try:
        os.mkdir(cfg.recv_lock, 0o700)
        return True
    except OSError:
        return False


def release_lock(cfg):
    try:
        os.rmdir(cfg.recv_lock)
    except OSError:
        pass


def _note(path, text):
    """One timestamped line, overwritten rather than appended.

    It is a current state and not a log: a file that grows is a file nobody
    prints.
    """
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                   " ".join(str(text).split())))
    except OSError:
        pass


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _clear(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def note_error(cfg, text):
    """Write `recv.err`: why the last START failed, for the next run.

    **Start faults only.**  claudio's supervisor reads the existence of this
    file as "already reported, do not chase it", so anything a *live* receiver
    writes here silently disarms the respawn for the rest of that session and
    every later one.  Runtime faults belong in `note_warning`.
    """
    try:
        os.makedirs(cfg.dir, mode=0o700, exist_ok=True)
    except OSError:
        pass
    _note(cfg.recv_err, text)


def read_error(cfg):
    return _read(cfg.recv_err)


def clear_error(cfg):
    _clear(cfg.recv_err)


def note_warning(cfg, text):
    """Write `recv.warn`: something went wrong while the receiver was running.

    Same format, same lifetime, same readers -- and deliberately NOT the file
    the supervisor gates on.  An undecodable body is a real loss and has to be
    said; it is not a reason to stop respawning a receiver that later dies.
    """
    try:
        os.makedirs(cfg.dir, mode=0o700, exist_ok=True)
    except OSError:
        pass
    _note(cfg.recv_warn, text)


def read_warning(cfg):
    return _read(cfg.recv_warn)


def clear_warning(cfg):
    _clear(cfg.recv_warn)


def note_ship_error(cfg, text):
    """Write `ship.err`: nothing was delivered, and why.

    Kept here beside `recv.err` rather than in `ship.py` because every file
    that claudio (POSIX sh) reads is written from this module -- the shape of
    those files is a fact spelled in two languages, and a second writer is how
    a format drifts on one side of the seam without the other noticing.
    """
    try:
        os.makedirs(cfg.dir, mode=0o700, exist_ok=True)
    except OSError:
        pass
    _note(cfg.ship_err, text)


def read_ship_error(cfg):
    return _read(cfg.ship_err)


def clear_ship_error(cfg):
    _clear(cfg.ship_err)


def note_ship_warning(cfg, text):
    """Write `ship.warn`: delivery worked, but something will never leave.

    A row with no account identity, or a line that would not parse.  Not a
    delivery failure, so it must not be `ship.err` -- and not nothing, because
    a record that can never be shipped is exactly what has to be said out loud.
    """
    try:
        os.makedirs(cfg.dir, mode=0o700, exist_ok=True)
    except OSError:
        pass
    _note(cfg.ship_warn, text)


def read_ship_warning(cfg):
    return _read(cfg.ship_warn)


def clear_ship_warning(cfg):
    _clear(cfg.ship_warn)


def machine_id(cfg):
    """A stable opaque id for this machine, created on first use.

    Random, and written temp+rename so two receivers starting at once cannot
    read a half-written one.  Losing the file costs a new id -- the server
    sees a new machine, which understates nothing and overstates nothing that
    coverage does not already carry.
    """
    try:
        with open(cfg.machine_id, "r", encoding="utf-8") as fh:
            got = fh.read().strip()
        if got:
            return got
    except OSError:
        pass
    got = os.urandom(16).hex()
    try:
        os.makedirs(cfg.dir, mode=0o700, exist_ok=True)
        tmp = cfg.machine_id + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(got + "\n")
        os.replace(tmp, cfg.machine_id)
    except OSError:
        pass
    return got


def note_start_attempt(cfg, now=None):
    """Record this start; return how many are in the window.

    The file holds only starts that never reached a successful bind -- a bind
    truncates it -- so the count is a crash-loop measure and not a usage
    measure.  A receiver that cannot bind dies instantly, and a supervisor
    firing several times a second would fork-bomb; the supervisor carries the
    cheap half of the cap, and this is the backstop that holds when the
    supervisor is wrong, and the half that can explain the refusal in
    `recv.err`.
    """
    now = time.time() if now is None else now
    keep = []
    try:
        with open(cfg.recv_starts, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    t = float(line.strip())
                except ValueError:
                    continue
                if now - t < START_WINDOW:
                    keep.append(t)
    except OSError:
        pass
    keep.append(now)
    try:
        os.makedirs(cfg.dir, mode=0o700, exist_ok=True)
        with open(cfg.recv_starts, "w", encoding="utf-8") as fh:
            fh.write("".join("%.3f\n" % t for t in keep))
    except OSError:
        pass
    return len(keep)


def clear_start_attempts(cfg):
    try:
        os.unlink(cfg.recv_starts)
    except OSError:
        pass


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def record_session(cfg, pid=None):
    """Register a session pid.  `claudio run` does this in sh, before exec.

    Kept here so the format has one definition and the tests can write one the
    same way claudio does.  `exec` preserves the pid, so `$$` recorded before
    `exec claude` really is the claude process.
    """
    cfg.ensure_dirs()
    pid = os.getpid() if pid is None else pid
    path = os.path.join(cfg.sessions_dir, str(pid))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("%.3f\n" % time.time())
    return path


def sweep_sessions(cfg):
    """Live session count; dead entries are unlinked.

    Nobody else will remove them, and one left behind holds the receiver up for
    ever -- which is exactly what makes an on-demand process indistinguishable
    from a daemon.  A recycled session pid reads as live and delays the exit,
    which is the safe direction: recording longer than necessary costs a
    process, exiting early costs records.
    """
    live = 0
    try:
        names = os.listdir(cfg.sessions_dir)
    except OSError:
        return 0
    for name in names:
        if not name.isdigit():
            # Not ours to interpret and not ours to delete.  It cannot be
            # checked, so it must not vote either: an unreadable entry counting
            # as live would be a permanent hold.
            continue
        if pid_alive(int(name)):
            live += 1
        else:
            try:
                os.unlink(os.path.join(cfg.sessions_dir, name))
            except OSError:
                pass
    return live


# --------------------------------------------------------------------------
# uptime ledger
# --------------------------------------------------------------------------

def log_event(cfg, event, clean=True, extra=None):
    rec = {"event": event, "ts": time.time(), "clean": bool(clean),
           "pid": os.getpid(), "host": host_slug()}
    if extra:
        rec.update(extra)
    os.makedirs(cfg.dir, mode=0o700, exist_ok=True)
    with open(cfg.uptime, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return rec


def read_uptime(cfg):
    out = []
    try:
        fh = open(cfg.uptime, "r", encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def up_spans(cfg, now=None):
    """Clean [start, stop] spans during which the receiver was recording.

    A start with no matching stop is treated as *unclean*: the span is closed
    at `now`, but marked, so `doctor` can say the shutdown was not observed to
    be graceful.  Unclean is a statement about the **label**, not about the
    data: nothing is buffered, so no record is missing because of it.  What is
    missing is the knowledge of when this machine actually stopped listening,
    and that is what a server closing a window needs.

    This file has no idea whether the receiver is running *right now*, so a
    live receiver's span has exactly the shape of an abandoned one: last, open,
    marked unclean.  A caller that knows the process state must exclude the
    trailing span while it is running -- `doctor` does -- or it reports the
    receiver it just started as a failed shutdown, for the whole time it is up.
    """
    if now is None:
        now = time.time()
    spans, open_start = [], None
    for rec in sorted(read_uptime(cfg), key=lambda r: r.get("ts", 0)):
        if rec.get("event") == "start":
            if open_start is not None:
                # Previous start never saw a stop.
                spans.append((open_start, rec["ts"], False))
            open_start = rec.get("ts")
        elif rec.get("event") == "stop" and open_start is not None:
            spans.append((open_start, rec.get("ts", now), bool(rec.get("clean", True))))
            open_start = None
    if open_start is not None:
        spans.append((open_start, now, False))
    return spans


# --------------------------------------------------------------------------
# counters
# --------------------------------------------------------------------------

def read_counters(cfg):
    """The receiver's counters: live off `/healthz`, else the last snapshot.

    Both halves are required and neither is optional.  Live is the only
    accurate answer while the process is up; the snapshot is the only answer at
    all once it has exited, which -- for a receiver that leaves after half an
    idle hour -- is most of the time anyone runs `doctor`.  Counters that died
    with the process would report "nothing arrived" for every session already
    finished, which is the precise ambiguity they exist to remove.
    """
    body = health(cfg)
    if body is not None:
        return body
    try:
        with open(cfg.recv_counters, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def reconcile_text(counters):
    """One line saying whether what arrived is what landed, and what it was.

    The receiver keeps everything, so `accepted` and `written` differ only when
    a write failed -- and that is worth its own alarm, because it is the one
    path on which a record was accepted and is not on disk.

    The event breakdown is printed as a **fact**, never as a loss.  Every real
    session sends `user_prompt` and `assistant_response` beside `api_request`,
    and the ledger stores only the last of those by design.  But this is also
    where a renamed `event.name` goes, and naming the counts is what turns that
    from "an idle machine" into something a reader can see: no `api_request` at
    all, while other events arrive, is drift and is called out.  The old
    collector could not do this -- its filter had already run by the time
    anything was countable -- which is how a wrong event name dropped every
    record for an evening with no symptom but a zero.
    """
    if not counters:
        return "records: no counters (the receiver has not run here)"
    acc = int(counters.get("accepted") or 0)
    wrote = int(counters.get("written") or 0)
    events = counters.get("events") or {}
    txt = "records: %d accepted -> %d written" % (acc, wrote)
    named = ", ".join("%s %d" % (k, v) for k, v in
                      sorted(events.items(), key=lambda kv: (-kv[1], str(kv[0])))
                      if k)
    if named:
        txt += "; events: %s" % named
    dropped = counters.get("dropped") or {}
    n_drop = sum(int(v or 0) for v in dropped.values())
    if n_drop:
        txt += "; %d metrics/traces payload(s) accepted and discarded" % n_drop
    if counters.get("undecodable"):
        txt += "; %d undecodable body(ies)" % counters["undecodable"]
    if counters.get("write_failed"):
        txt += ("\n  <-- %d payload(s) were accepted and could NOT be written; "
                "the client was told to retry" % counters["write_failed"])
    if acc and not wrote:
        txt += "\n  <-- nothing reached disk; see recv.warn"
    elif acc and not events.get("api_request"):
        txt += ("\n  <-- no api_request records arrived, so the ledger will "
                "store nothing; Claude Code may have renamed the event")
    unknown = counters.get("unknown_attributes") or {}
    if unknown:
        txt += ("\n  note: %d attribute(s) arrived that the ledger has no "
                "column for: %s" % (len(unknown), ", ".join(sorted(unknown))))
    return txt


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def start(cfg):
    """Idempotent start.  Returns (started, message).

    This is the *human* start -- `claudio usage collect start` -- so it waits
    for the pidfile and reports what actually happened.  claudio's spawn on the
    `run` path deliberately does not wait: Claude Code's export batches over
    seconds, so the receiver has claude's own startup to bind in, and blocking
    a launch on a listener is a worse failure than a late bind.
    """
    if not available():
        return False, receiver_missing()

    live = running(cfg)
    if live:
        return False, ("receiver already running (pid %d, port %s)"
                       % (live["pid"], live["port"]))

    if not claim_lock(cfg):
        return False, ("another start is already in progress (%s); if that is "
                       "wrong, remove it" % cfg.recv_lock)

    cfg.ensure_dirs()
    # `start_new_session` is setsid: closing the terminal must not take the
    # receiver with it.  stdin from /dev/null and both outputs discarded, for
    # the reason claudio's `_update_refresh` learned the hard way -- a
    # background child holding the parent's stdout keeps a `$( )` capture open
    # until it exits.
    try:
        with open(os.devnull, "r+b") as null:
            proc = subprocess.Popen(
                [sys.executable, receiver(), "serve", "--quiet"],
                stdin=null, stdout=null, stderr=null,
                start_new_session=True, close_fds=True)
    except OSError as exc:
        release_lock(cfg)
        return False, "could not spawn %s: %s" % (receiver(), exc)

    # A spawn that dies on a bind conflict looks exactly like one that has not
    # written its pidfile yet, for the first few milliseconds.  Waiting for the
    # pidfile *or* the child's exit distinguishes them without a sleep-and-hope.
    deadline = time.time() + START_TIMEOUT
    got = None
    while time.time() < deadline:
        got = running(cfg)
        if got and got["pid"] == proc.pid:
            break
        got = None
        if proc.poll() is not None:
            break
        time.sleep(START_TICK)

    if not got:
        release_lock(cfg)
        why = read_error(cfg)
        rc = proc.poll()
        return False, ("receiver did not come up"
                       + (" (exit %s)" % rc if rc is not None else
                          " within %.0fs" % START_TIMEOUT)
                       + (":\n  %s" % why if why else ""))

    return True, ("receiver started on 127.0.0.1:%d (pid %d), writing %s"
                  % (got["port"], got["pid"], cfg.raw_file))


def stop(cfg):
    """Graceful stop.  `clean` is OBSERVED, never assumed.

    The receiver writes its own `stop` record on the way out, so `clean` is
    earned by it actually getting there.  That inverts the old bug rather than
    re-implementing its fix: `docker stop -t 10` returns 0 *after* escalating
    to SIGKILL (measured: rc 0, exit code 137) and the old code wrote
    clean:true over it.  Here, nothing this function does can write a clean
    span -- only the receiver can, by living long enough.

    Two dirty paths, both recorded here because both are *observed*:
    the receiver was already gone (killed, OOM, host sleep) and it did not take
    SIGTERM inside the grace period.  Either way the records are on disk;
    what is lost is the knowledge of when this machine stopped listening.

    Still returns True on the dirty paths.  The caller gates `do_ingest` on it,
    and salvaging whatever did reach disk matters most on exactly these paths.
    """
    got = read_pidfile(cfg)
    if not got:
        return False, "receiver not running"

    if not pid_alive(got["pid"]):
        log_event(cfg, "stop", clean=False,
                  extra={"reason": "already_gone", "pid": got["pid"]})
        clear_pidfile(cfg, got.get("token"))
        return True, ("receiver was already gone -- its stop was never "
                      "observed, so that span is marked unclean. The records it "
                      "wrote are on disk (nothing is buffered); what is unknown "
                      "is when this machine stopped listening.")

    # Scrape before the signal: the endpoint dies with the process.  The
    # receiver also snapshots its counters as it exits, so this is belt to that
    # brace -- and the brace is what covers a kill.
    counters = read_counters(cfg)

    try:
        os.kill(got["pid"], signal.SIGTERM)
    except OSError as exc:
        return False, "could not signal pid %d: %s" % (got["pid"], exc)

    deadline = time.time() + STOP_TIMEOUT
    while time.time() < deadline and pid_alive(got["pid"]):
        _reap(got["pid"])
        time.sleep(0.05)

    escalated = False
    if pid_alive(got["pid"]):
        escalated = True
        try:
            os.kill(got["pid"], signal.SIGKILL)
        except OSError:
            pass
        deadline = time.time() + 2
        while time.time() < deadline and pid_alive(got["pid"]):
            _reap(got["pid"])
            time.sleep(0.05)
        log_event(cfg, "stop", clean=False,
                  extra={"reason": "sigkill_escalation", "pid": got["pid"]})
        clear_pidfile(cfg, got.get("token"))

    if escalated:
        msg = ("receiver did not exit on SIGTERM and was killed, so that span "
               "is marked unclean. Its records are on disk; what is unknown is "
               "exactly when it stopped listening.")
    else:
        msg = "receiver stopped"
    fresh = read_counters(cfg) or counters
    if fresh:
        msg += "\n  %s" % reconcile_text(fresh)
    return True, msg


def status(cfg):
    got = running(cfg)
    size = os.path.getsize(cfg.raw_file) if os.path.exists(cfg.raw_file) else 0
    return {
        "receiver": receiver(),
        "state": "running" if got else "absent",
        "pid": got["pid"] if got else None,
        "port": got["port"] if got else None,
        "sessions": sweep_sessions(cfg),
        "raw_bytes": size,
        "endpoint": "http://localhost:%s" % (
            got["port"] if got and got["port"] else cfg["http_port"]),
        "spans": len(up_spans(cfg)),
    }
