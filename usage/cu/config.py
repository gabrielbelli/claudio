"""Layered key=value configuration, in claudio's idiom.

One `key=value` per line, `#` comments and blanks ignored.  Last value wins.
Read line-wise; never parsed into structures.  Environment beats file.
"""

import os

# claudio's own version, and the ONLY copy of it on the Python side.
#
# The number lives in the `VERSION=` line of the `claudio` script, which is
# POSIX sh and cannot be imported; `test.sh` already reads it with `sed` in two
# places and CI reads it in a third, so a value is derived from that line rather
# than owned here.  Deriving it a fourth time AT RUNTIME was the alternative and
# is worse: `claudio` sits at three different places relative to this file
# (checkout, `$PREFIX/libexec/claudio/`, and `$PATH`) -- `_tool_path` exists to
# resolve exactly that, downhill -- and a reverse resolver that missed would
# degrade the User-Agent to "unknown" silently, which is precisely the WAF rule
# this string exists to be matched by.  A constant that a test compares against
# the script fails loudly at the one moment it can be fixed, which is the same
# trade every doc guard in this project already makes.
CLAUDIO_VERSION = "0.1.0"

# Every key here is read by something.  `idle_stop_minutes` and `retain_days`
# used to sit in this table and were read by nothing: there is no idle timer
# and no retention pass, so they promised behaviour that did not exist.
#
# `record` was the third.  It gated `recorder/cu-record`, a second
# implementation of the stream-B recorder that claudio's status-line shim
# superseded; nothing in Python ever read it.  Two keys for one decision cost a
# real bug before either was deleted -- `doctor` read `record=` and reported
# "recording is OFF" while samples were arriving through the shim.  There is
# now exactly one switch for recording and it lives where the recorder does:
# `logging=local` in ~/.claudio/claudio.conf.
# `collector_image`, `container_name`, `grpc_port` and `metrics_port` were the
# fourth, fifth, sixth and seventh.  The receiver is a process in this repo
# now, not a container: there is no image to pin, no container to name, no gRPC
# port because nothing here speaks HTTP/2 framing, and the counters are read
# off the receiver itself rather than scraped over a published 8888.
DEFAULTS = {
    # Where Claude Code must be told to export.  This is OTLP/HTTP JSON, so it
    # is 4318 and not 4317: the receiver is stdlib Python and gRPC is HTTP/2
    # framing plus protobuf, which `http.server` cannot be talked into.
    # claudio defaults to the same two values, so `logging=local` alone works
    # end to end; `doctor` still compares them against claudio's actual export,
    # because an export aimed at a port nothing listens on fails quietly by
    # design, which is this project's cardinal sin.
    "endpoint": "http://localhost:4318",
    "http_port": "4318",
    # Idle exit: no records for this long AND no live session. Both, always --
    # a user idle mid-task must not be orphaned by a timer.
    "idle_minutes": "30",
    # Rotation of the raw capture, kept at the figures the Docker collector's
    # lumberjack config used, because the volume it is sized for has not
    # changed.  It matters more now: the receiver writes every log record
    # verbatim, where the collector wrote only the filtered, allow-listed ones.
    "raw_max_megabytes": "32",
    "raw_max_backups": "16",
}

# query_source values that are Claude Code's own overhead rather than user work.
#
# Deny-list, not an allow-list: an unknown future source counts as real work,
# so a new kind of request is visibly over-counted rather than invisibly lost.
#
# **Every name here was read off a captured payload.**  The list used to read
# session_title / compaction / summarization / summarize / quota_check /
# topic_detection -- six plausible names, not one of which Claude Code has ever
# emitted.  The four values that actually appear across the eight captured
# records are `generate_session_title`, `repl_main_thread`, `prompt_suggestion`
# and `sdk`, so the deny-list matched nothing at all: Claude Code's own titling
# requests were billed to the user as work, and `--only-overhead` printed
# "(nothing attributable in this window)" against a ledger holding three
# titling rows.  Guessing the strings was worse than having no list, because it
# read as coverage.
#
#   generate_session_title  Claude Code names the session for its own UI.  It
#                           fires ~1.4s into a turn, before the user's request
#                           is answered, on a haiku model, at ~$0.0006.  The
#                           user never asked for a title.
#   prompt_suggestion       Fires 2.18s *after* the main-thread response of
#                           turn 5f150da0, carrying that same turn's prompt.id
#                           (tests/fixtures/real-payloads/04-*.json).  The user
#                           sent one message in that turn and it had already
#                           been answered, so this is work Claude Code started
#                           on its own initiative -- the same test as titling,
#                           applied to the captured bytes rather than to the
#                           plausibility of the name.
#
# `repl_main_thread` and `sdk` are the user's own requests and must never
# appear here.  Anything not listed is counted as user work *and named* by
# `show`, so the next real overhead source announces itself the first time it
# is seen instead of waiting for someone to guess its name correctly.
#
# This list survived the strip to a pure recorder because "whose request was
# this" is a property of the single request -- it is read off `query_source` on
# the record itself and needs nothing about the rest of the account.  What was
# downstream of it did need the account: the report filtered overhead out
# *before* weighting, so that excluding it reweighted the survivors' share of
# plan movement.  That share is gone; `show` only counts and names.
OVERHEAD_SOURCES = frozenset({
    "generate_session_title",
    "prompt_suggestion",
})

# REMOVED: `agent_summary`.  It was carried here with a comment claiming
# measurements -- "~30s progress heartbeat while a subagent runs, measured gaps
# 32.6/32.9/32.5s, outputs of 11-16 tokens, cost 14% of one turn" -- and there
# is no payload behind any of it in this repository: the string appears in no
# fixture, no test and no capture, and it arrived with the import commit that
# brought this reader in-tree, so whatever was measured was measured somewhere
# these bytes did not follow.
#
# That is the exact shape of the six names apologised for above.  An unverified
# name in a deny-list is not inert: it costs nothing when it matches nothing,
# and it reads as coverage to the next person -- which is how six of them
# survived long enough to bill Claude Code's own titling requests to the user.
#
# Deleting it is also the fail-safe direction, which is the whole argument for
# this being a deny-list: an unlisted source is counted as the user's work AND
# NAMED in `show`'s "counted as your work" line, so if `agent_summary` is real
# it announces itself the first time it is seen, in the output, to the person
# holding the machine that produced it.
#
# What brings it back: one captured payload carrying
# `query_source: "agent_summary"` committed to tests/fixtures/, scrubbed like
# the rest, and cited on the line that re-adds the name.  Not a recollection of
# having seen it.


def data_dir():
    d = os.environ.get("CLAUDIO_USAGE_DIR")
    if not d:
        d = os.path.join(os.path.expanduser("~"), ".claudio-usage")
    return os.path.abspath(d)


def conf_path():
    """claudio's GLOBAL conf, the machine-level layer.

    `CLAUDIO_CONF` overrides it, exactly as it does on claudio's side, which is
    also what keeps the suites hermetic.
    """
    return os.environ.get("CLAUDIO_CONF") or os.path.join(
        os.path.expanduser("~"), ".claudio", "claudio.conf")


def _read_file(path):
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def conf_display():
    """`conf_path()` as a user should read it back, with `~` where it is true.

    The messages that tell somebody to add a line to the global conf used to
    spell `~/.claudio/claudio.conf` literally.  That is right on most machines
    and false on any machine that sets `CLAUDIO_CONF` -- and a remedy naming a
    file the tool does not read sends the reader to edit something that cannot
    possibly work, and then to edit it again.  claudio's own
    `_telemetry_lookup` names the file that decided for exactly this reason.

    `~` is restored only when the path really is under `$HOME`, so the short
    form is never a guess about where the file is.
    """
    p = conf_path()
    home = os.path.expanduser("~")
    if p == home or p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def read_conf(path=None):
    """claudio's `key=value` idiom over one file: comments and blanks skipped,
    last value wins.

    This is `_read_file` under the name the shipper reads the global conf by.
    It lives HERE and not in `ship.py` because `collector.py` needs it too, and
    `ship.py` already imports `collector` -- so a reader kept there could only
    reach the other one by a circular import or by a second copy of five lines
    that both halves have to keep agreeing about.  That is the same move
    `tail.file_identity` got, for the same reason and with the same test: the
    two names are asserted to be one function object, not two that agree today.
    """
    return _read_file(path or conf_path())


def receiver():
    """The receiver THIS MACHINE runs, resolved the way claudio resolves it.

    `receiver=` in the global conf outranks the bundled copy, and it is read
    from the global conf ONLY -- the same rule, and the same reason, as the
    shipping keys: a path in a `claudio.conf` inside a repository someone else
    wrote must never be able to name the program this machine executes.

    This exists because the two halves disagreed.  `claudio` has read
    `receiver=` since the spawn work landed (`_recv_cmd`), and nothing under
    `usage/` read it at all, so on a machine with a working `receiver=` set,
    `claudio run` started that receiver while `claudio usage doctor` reported
    one "missing" at a path the user had deliberately overridden -- a WARN, and
    every WARN sets a non-zero exit -- and `claudio usage collect start` would
    have launched the other one.  Two answers to "which receiver is this
    machine's" is one answer too many.
    """
    return read_conf().get("receiver") or BUNDLED_RECEIVER


# The receiver as it ships: beside `cu/`, in the tree this file is part of.
# A path and not a `which`, because a same-named program further up someone's
# `$PATH` is not the thing whose contract these modules describe.  `receiver()`
# above is what callers should use; this is only the default it falls back to.
BUNDLED_RECEIVER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "recv", "otlp-recv")


class Config(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        self.dir = data_dir()
        self.path = os.path.join(self.dir, "config")
        self.update(_read_file(self.path))
        # Environment overrides, for tests and for one-off runs.
        for k in DEFAULTS:
            env = os.environ.get("CLAUDIO_USAGE_" + k.upper())
            if env is not None and env != "":
                self[k] = env

    # -- derived paths ---------------------------------------------------
    @property
    def samples(self):
        return os.path.join(self.dir, "samples.jsonl")

    @property
    def ledger(self):
        return os.path.join(self.dir, "ledger.jsonl")

    @property
    def raw_dir(self):
        return os.path.join(self.dir, "raw")

    @property
    def state_dir(self):
        return os.path.join(self.dir, "state")

    @property
    def uptime(self):
        return os.path.join(self.dir, "uptime.jsonl")

    # -- receiver contract -----------------------------------------------
    #
    # These eleven paths are the entire interface between claudio (POSIX sh)
    # and the receiver (Python), and they are defined **here, once**, because
    # the shell side cannot import them and will therefore hardcode them:
    # anything spelled in two languages has to be spelled in one place first,
    # or the next rename is a silent no-op on one side of the seam.  Every one
    # of them is line-oriented and needs no `jq` -- the status-line shim reads
    # `recv.pid` on every render and must not fork.

    @property
    def raw_file(self):
        """The live capture.  `otlp.raw_files` finds the rotated siblings."""
        return os.path.join(self.raw_dir, "api_request.jsonl")

    @property
    def recv_pid(self):
        """Four lines: pid, token, port, started-at.

        One field per line rather than a tab-separated record, for claudio's
        reason: a tab is IFS whitespace, so a run of tabs collapses and an
        empty field disappears, shifting every later field.  `{ read pid;
        read token; } < recv.pid` is two builtins and no fork.
        """
        return os.path.join(self.dir, "recv.pid")

    @property
    def recv_lock(self):
        """A *directory*, claimed with `mkdir` -- POSIX-atomic, no helper."""
        return os.path.join(self.dir, "recv.lock")

    @property
    def recv_err(self):
        """Why the last start failed.  The next `claudio run` prints it.

        **Start faults only, and that is load-bearing rather than tidy.**  The
        supervisor's spawn gate is `receiver dead AND no recv.err`, on the
        reasoning that a fault already reported must not be chased several
        times a second; the file is cleared by a successful bind.  A *running*
        receiver writing here therefore disarms the supervisor for every later
        session -- one undecodable body, then a SIGKILL an hour later, and
        nothing ever spawns again without a `claudio run`.  Runtime faults go
        to `recv_warn` for exactly that reason.
        """
        return os.path.join(self.dir, "recv.err")

    @property
    def recv_warn(self):
        """What went wrong while the receiver was *running*.

        An undecodable body, a protobuf body this receiver cannot speak, a
        write that failed: each is a real loss with a user-side cause, so none
        of them may be a silent 200 and a counter nobody reads.  But none of
        them is a reason not to spawn, which is the only thing `recv.err`
        means to the supervisor.  Printed by `claudio run` and by `doctor`
        alongside `recv.err`, and cleared by a successful bind the same way.
        """
        return os.path.join(self.dir, "recv.warn")

    @property
    def recv_starts(self):
        """One epoch per line: the crash-loop accounting behind the cap."""
        return os.path.join(self.dir, "recv.starts")

    @property
    def recv_counters(self):
        """Last counter snapshot, so `doctor` can read them once it is gone."""
        return os.path.join(self.dir, "recv-counters.json")

    @property
    def ship_err(self):
        """Why the last shipping pass did not deliver.  `claudio run` prints it.

        The same two-file split as `recv_err`/`recv_warn`, for the same
        reason: this one means *nothing was delivered* -- no destination, no
        readable login, a refused POST -- and the stored offset was therefore
        left exactly where it was, so the next tick resends.  A shipping
        failure that nobody is told about is silent non-delivery, which is
        this project's cardinal sin wearing a new hat.  Cleared by a pass that
        delivers everything it selected.
        """
        return os.path.join(self.dir, "ship.err")

    @property
    def ship_warn(self):
        """What a *delivering* pass could not place or could not read.

        A row carrying no account identity at all (claudio ran with no
        account, or `tag=email=` suppressed it) cannot be attributed to any
        destination, and a malformed ledger line cannot be parsed.  Neither
        stops delivery of everything else, so neither may write `ship.err` --
        but both are records that will never leave this machine, and the one
        thing that may never happen to those is silence.
        """
        return os.path.join(self.dir, "ship.warn")

    @property
    def machine_id(self):
        """An opaque per-machine id, created once, for the batch manifest.

        `wire.manifest_check` requires one and there was nothing on this
        machine to serve as one: `host_slug()` is a hostname, which is neither
        stable (laptops get renamed) nor unique across a fleet.  Random bytes,
        so it says nothing about the machine beyond "the same one as last
        time" -- which is the entire job.
        """
        return os.path.join(self.dir, "machine-id")

    @property
    def sessions_dir(self):
        """One file per claudio-launched session, named for its pid.

        `claudio run` writes `$$` here before `exec claude`, and `exec`
        preserves the pid, so the name really is the claude process.
        """
        return os.path.join(self.dir, "sessions")

    def ensure_dirs(self):
        for d in (self.dir, self.raw_dir, self.state_dir, self.sessions_dir):
            os.makedirs(d, mode=0o700, exist_ok=True)
