#!/usr/bin/env python3
"""The container's preflight: check the store, then become the door.

SHARED BY EVERY IMAGE THAT TOUCHES THE STORE.  The Linux and the FreeBSD build
differ in their base, in how the interpreter got there and in nothing else that
reaches this file; the ingest service and the api service differ in which
DIRECTION they need the store to work in, and that is the one branch below.

Python rather than sh because the one question worth asking -- "is this store
going to survive `docker rm`" -- needs `st_dev`, and `stat(1)` spells that
`-c %d` on GNU and `-f %d` on BSD.  The bases agree on a Python; they do not
agree on a stat.

WHAT IT REFUSES, AND WHAT IT ONLY SAYS OUT LOUD

  A store this process cannot write is a REFUSAL (exit 5), because the
  alternative is `lock_root`'s `os.makedirs` raising `PermissionError` out of
  the top of `serve.py` -- a traceback whose first line is about `door.lock`,
  for a fault that is about `-v` and `chown`.  The remedy is printed with the
  numeric uid and gid this process actually has, because that is the number the
  operator has to type and it is not guessable from the image name.

  An EPHEMERAL store is a warning, never a refusal.  A door with no volume
  works perfectly and loses everything on `docker rm`, which is the silent kind
  of loss this project calls its cardinal sin -- so it is said, loudly, on every
  start.  It is not refused because a throwaway door over a throwaway store is
  a legitimate five-minute experiment, and refusing it would make the honest
  case unreachable.

WHAT IT DELIBERATELY DOES NOT CHECK

  Reader auth and the bind address.  `serve.py` already refuses an
  unauthenticated read API on a non-loopback bind, by name, with the path of the
  file to write, and returns 4.  Repeating that decision here would be two
  implementations of one rule -- the shape this project has apologised for
  more than once -- and the copy would be the one that drifts, because it is the
  one nobody runs the suite against.  This file must never grow a
  `--no-api`, a `--host 127.0.0.1` fallback or any other paper over that exit.

  DuckDB.  `serve.py` prints `engine duckdb present` or `MISSING -- stream A
  routes will refuse by name` on every start.  One statement, one place.

  Whether `accounts/` exists.  `serve.py` refuses that by name for a reader
  (exit 9), with the three things it can mean.  Same rule: one place.

THE READER IS THE OTHER DIRECTION, AND IT IS DECIDED BY `--no-ship`

  There is no second flag for it.  `--no-ship` is already in the argv this file
  passes through, it already means "this process writes nothing", and inventing
  a `--read-only` beside it would be two spellings of one fact that can
  disagree -- a preflight in write mode in front of a reader would refuse a
  correctly mounted read-only store (exit 5) with a remedy telling the operator
  to make the record of truth writable by the one service that must never write
  to it.

  For a reader the probe is INVERTED and so is its severity.  It must prove it
  can LIST the store, because `accounts_in` and `Paths.accounts` both swallow
  `OSError` and return `[]` -- deliberately, on the write path -- and a reader
  that cannot read answers `no-data` about every account in a store that is
  full.  And a store it CAN write is a warning, not a refusal: the mount was
  meant to be `:ro` and is not, which is a real mistake worth naming, but the
  service works and refusing would be this file breaking a stack over a
  belt-and-braces property `serve.py` enforces in code anyway.
"""

import os
import sys

# The default store root, and it is the same string in `entrypoint.sh`, in both
# Containerfiles and in `server/README.md`; a test pins the four together.
#
# `claudio-usage`, not `claudio-server`, because `server/compose.yml` already
# used it and one directory with two names is worse than a name that could have
# been better.  It is NOT the client's `usage_dir`: that is the directory the
# status-line shim writes samples into on a workstation, and this is the store
# a door writes on a receiving host.  They are never the same machine in any
# deployment this image is for.
DEFAULT_STORE = "/var/lib/claudio-usage"

PROBE = ".preflight-probe"


def say(msg):
    sys.stderr.write("[preflight] %s\n" % msg)


def refuse(msg, remedy):
    say("REFUSING TO START -- %s" % msg)
    for line in remedy:
        say("  %s" % line)
    return 5


def store_is_ephemeral(store):
    """True if `store` lives in the image's own writable layer.

    Same device as `/` means nothing is mounted there, which on any runtime is
    the container's own layer: an image is one filesystem, so a path inside it
    cannot be on a second device unless something was mounted over it.  That
    direction is exact.

    The other direction is where the honesty is.  A different device means
    "something is mounted", which is what we want to conclude -- but a runtime
    whose mount type reports the *underlying* device (FreeBSD nullfs is the one
    to worry about, and it has not been measured) would read a real mount as
    same-device and produce a warning about a store that is in fact durable.
    That is the safe direction to be wrong in: a false warning costs a sentence,
    a false silence costs the records.  Never the other way round.
    """
    try:
        return os.stat(store).st_dev == os.stat("/").st_dev
    except OSError:
        # Cannot tell.  Say nothing here; the writability probe below is about
        # to speak about this same path with a real error in its hands.
        return False


def check_store_readonly(store):
    """The reader's half.  Prove we can LIST it; warn if we can also write."""
    if not os.path.isdir(store):
        return refuse(
            "the store root %s is not a directory" % store,
            ["This service reads the store and never writes to it, so it "
             "cannot create one.",
             "Mount the same directory the ingest service writes, READ ONLY:",
             "  ... -v <host dir or volume>:%s:ro ..." % store,
             "A reader with the wrong path answers `no-data` about every "
             "account, which is indistinguishable from a store nobody has "
             "shipped to."])
    try:
        os.listdir(store)
    except OSError as exc:
        return refuse(
            "the store root %s is not readable by uid %d:%d: %s"
            % (store, os.getuid(), os.getgid(), exc),
            ["chown or chmod the directory you mounted there so uid %d:%d can "
             "read it," % (os.getuid(), os.getgid()),
             "or run the container as a user that can:",
             "  ... --user $(id -u):$(id -g) ...",
             "Unreadable, every question is answered `no-data`, which reads as "
             "an empty store rather than as a broken mount."])

    probe = os.path.join(store, PROBE)
    writable = False
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        os.unlink(probe)
        writable = True
    except OSError:
        pass
    if writable:
        say("WARNING the store %s is WRITABLE by this service, and this "
            "service is the reader." % store)
        say("  It takes no store lock and writes nothing -- that is enforced "
            "in `serve.py`, not here -- so this is not a fault today.")
        say("  It is a mount that was meant to say `:ro` and does not, and the "
            "day somebody starts this image without --no-ship it becomes a "
            "second writer.")
        say("  Mount it read-only:  -v <host dir or volume>:%s:ro" % store)
    return 0


def check_store(store):
    """Create the store root if need be and prove we can write in it."""
    try:
        os.makedirs(store, mode=0o700, exist_ok=True)
    except OSError as exc:
        return refuse(
            "cannot create the store root %s: %s" % (store, exc),
            ["mount a directory there and make it writable by uid %d:%d, e.g."
             % (os.getuid(), os.getgid()),
             "  mkdir -p /srv/claudio-store && chown %d:%d /srv/claudio-store"
             % (os.getuid(), os.getgid()),
             "  ... -v /srv/claudio-store:%s ..." % store,
             "or run the container as the directory's owner with --user."])

    probe = os.path.join(store, PROBE)
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        # A real write, not `os.access`: `access(2)` answers for the real uid
        # rather than the effective one and knows nothing about read-only
        # mounts or ACLs, so it can say yes to a directory the very next
        # `open` refuses.  The whole value of a preflight is that it fails
        # where the fault is.
        return refuse(
            "the store root %s is not writable by uid %d:%d: %s"
            % (store, os.getuid(), os.getgid(), exc),
            ["chown %d:%d the directory you mounted there, or run the "
             "container as its owner:" % (os.getuid(), os.getgid()),
             "  chown -R %d:%d <the host directory>" % (os.getuid(),
                                                        os.getgid()),
             "  ... --user $(id -u):$(id -g) ...",
             "The store is append-only JSONL and is the record of truth; the "
             "door writes nothing anywhere else."])

    if store_is_ephemeral(store):
        say("WARNING the store %s is the container's own writable layer -- "
            "NOTHING IS MOUNTED THERE." % store)
        say("  Every record this door accepts and acknowledges as durable is "
            "lost when the container is removed.")
        say("  Mount a volume:  -v <host dir or volume>:%s" % store)
    return 0


def main(argv):
    store = os.environ.get("CLAUDIO_SERVER_STORE") or DEFAULT_STORE
    # Read out of the argv this file is about to hand on, rather than out of a
    # flag of its own.  See the module docstring: two spellings of one fact can
    # disagree, and the disagreement would be a preflight refusing a correctly
    # mounted read-only store.
    reader = "--no-ship" in argv

    if os.getuid() == 0:
        # A warning and not a refusal: `--user 0` is the shortest way past a
        # bind mount owned by root, and an image that forbade it would send
        # people to `chmod 777` instead, which is worse.  The images set a
        # non-root user themselves; reaching this line means somebody asked.
        say("WARNING running as uid 0. The images default to 65534:65534 and "
            "no component here needs any privilege at all -- they bind 8787 "
            "and one of them appends to one directory.")

    rc = check_store_readonly(store) if reader else check_store(store)
    if rc:
        return rc

    serve = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "srv", "serve.py")
    if not os.path.exists(serve):
        return refuse(
            "the door is not where this image says it is: %s" % serve,
            ["This is an image build fault, not a configuration one.",
             "The Containerfile must COPY server/srv/ next to this file."])

    # `exec`, not `subprocess`: the door becomes pid 1's process, so `docker
    # stop` delivers SIGTERM to the server itself rather than to a wrapper that
    # would have to forward it.  Nothing is lost by dying on the signal --
    # every accept is fsynced before its 200 and `flock` dies with the process,
    # so there is no shutdown path that carries data.  That is the receiver's
    # lesson, one machine over: a graceful stop is a REPORTING property, never
    # an integrity one.
    os.execv(sys.executable, [sys.executable, serve] + argv)
    return 127                                    # unreachable; execv or raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
