#!/bin/sh
# The container's entrypoint, shared by the Linux and the FreeBSD image.
#
# WHY THIS IS SH AND NOT THE PYTHON NEXT TO IT
#
#   `/bin/sh` is the one interpreter both bases certainly have at a known path.
#   Python is in both images too, but not at the same path and not under the
#   same name: Debian's slim image gives `/usr/local/bin/python3`, and a
#   FreeBSD image staged from `pkg` gives `/usr/local/bin/python3.12` with the
#   unversioned symlink living in a separate `python3` metapackage that this
#   build has no reason to stage.  Resolving that is this file's first job and
#   the whole reason it exists; everything else it does is turn environment
#   into flags.
#
# HOW CONFIGURATION COMPOSES, AND WHY THERE IS ONLY ONE WAY TO DO IT
#
#   The environment supplies the defaults.  Anything you pass to `docker run`
#   after the image name is appended to the door's argv verbatim, and argparse
#   takes the LAST occurrence of a repeated option -- so
#
#       docker run ... claudio-server --port 9000
#
#   overrides the port and leaves the other four flags alone.  That is why the
#   defaults are env vars rather than a `CMD` carrying the full flag list: a
#   `CMD` is replaced wholesale, so changing one flag would mean retyping all
#   five, and the fifth one people forget to retype is `--readers`.
#
#   A first argument that does not begin with `-` is exec'd as a command
#   instead, which is the conventional escape hatch (`docker run ... sh`).
#
# THE ROLE, AND WHY IT IS AN ENVIRONMENT VARIABLE AND NOT A `CMD`
#
#   One source, two services.  `CLAUDIO_SERVER_ROLE` says which:
#
#       ingest   adds --no-ship's opposite, `--no-api`: POST /v1/ship only.
#                The only writer. Holds the store lock and the store
#                read-write.
#       api      adds `--no-ship`: GET /api/v1/* only. Takes no lock, wants the
#                store read-only, and is the half DuckDB is for.
#       door     adds nothing. The whole door, which is still the default so
#                that nothing which ran before the split runs differently.
#
#   A `CMD ["--no-ship"]` would have said the same thing in fewer lines and
#   would have broken the contract two paragraphs up: a CMD is replaced
#   WHOLESALE, so `docker run IMAGE --port 9000` would silently drop the flag
#   that makes the image what it is and start a WHOLE DOOR -- taking the store
#   lock on a read-only mount, or becoming a second writer on a read-write one.
#   Loud, both times, but loud about the wrong thing.  As an environment
#   variable the role survives every argv override, is visible in `docker
#   inspect`, and can be changed with `-e` by somebody who means to.
#
#   The role flag is placed BEFORE `"$@"`, so an operator's own flags are still
#   last and argparse still takes the last occurrence of a repeated option.
#
#   An unrecognised value is REFUSED BY NAME (exit 11) rather than ignored.
#   Ignoring it would start a whole door -- both halves, the store lock, the
#   query engine -- for somebody who typed `reader` instead of `api`, and the
#   only evidence would be a banner nobody reads twice.
#
# WHAT IT MUST NEVER DO
#
#   Add `--no-api` OF ITS OWN ACCORD, or fall back to `--host 127.0.0.1`.  The
#   door refuses to serve an unauthenticated read API on a non-loopback bind
#   and returns 4; inside a container the bind IS non-loopback (see the
#   Containerfile), so that refusal is the ordinary first experience of this
#   image when no `readers.json` has been mounted.  It is meant to be.  An
#   entrypoint that quietly turned the API off, or quietly bound loopback,
#   would convert a refusal that names its own remedy into either a mystery 404
#   or a port that answers nothing -- and, in the first case, into a running
#   door that serves every record in the store to anyone who can reach it.
#
#   The role above is not that.  It is DECLARED -- by the image, in an
#   environment variable an operator can read and change -- and it is printed
#   on every start.  "Quietly" is the whole of the difference.

set -eu

STORE="${CLAUDIO_SERVER_STORE:-/var/lib/claudio-usage}"
HOST="${CLAUDIO_SERVER_HOST:-0.0.0.0}"
PORT="${CLAUDIO_SERVER_PORT:-8787}"
TOKENS="${CLAUDIO_SERVER_TOKENS:-/etc/claudio/tokens.json}"
READERS="${CLAUDIO_SERVER_READERS:-/etc/claudio/readers.json}"
ROLE="${CLAUDIO_SERVER_ROLE:-door}"

ROLE_FLAG=""
case "$ROLE" in
    ingest) ROLE_FLAG="--no-api" ;;
    api)    ROLE_FLAG="--no-ship" ;;
    door)   ROLE_FLAG="" ;;
    *)      echo "[entrypoint] CLAUDIO_SERVER_ROLE='${ROLE}' is not a role" >&2
            echo "[entrypoint]   this image knows. Expected one of:" >&2
            echo "[entrypoint]     ingest  POST /v1/ship only -- the writer" >&2
            echo "[entrypoint]     api     GET /api/v1/* only -- the reader" >&2
            echo "[entrypoint]     door    both, which is the default" >&2
            echo "[entrypoint]   Refused rather than ignored: ignoring it" >&2
            echo "[entrypoint]   would start a WHOLE door -- both halves, the" >&2
            echo "[entrypoint]   store lock and the query engine -- for" >&2
            echo "[entrypoint]   somebody who typed one word wrong." >&2
            exit 11 ;;
esac

# `CDPATH= cd` is one command with CDPATH emptied for its duration, not an
# assignment; shellcheck reads it as the latter.
# shellcheck disable=SC1007
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# Escape hatch first, so it cannot be broken by anything below.
#
# THERE IS NO `mcp` MODE HERE ANY MORE, AND ITS REMOVAL IS THE POINT.
#
#   This entrypoint used to take `mcp` as a first argument and exec the module
#   in this image.  The MCP surface is now its own component with its own image
#   and its own entrypoint, and `srv/mcp.py` is not in this image's build
#   CONTEXT at all -- so the mode could only ever have failed, and it would have
#   failed as a `No module named` traceback rather than as anything an operator
#   could act on.  It is deleted rather than left to rot into that.
if [ "$#" -gt 0 ]; then
    case "$1" in
        -*)  : ;;
        *)   exec "$@" ;;
    esac
fi

# The interpreter, named candidates first and `command -v` last.  An absolute
# path that exists is preferred to a PATH lookup because PATH inside a
# container is whatever the base image happened to set, and a wrong `python3`
# earlier on it would be found in silence.
PY=""
for c in /usr/local/bin/python3 /usr/local/bin/python3.12 /usr/bin/python3; do
    if [ -x "$c" ]; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    PY="$(command -v python3 2>/dev/null || :)"
fi
if [ -z "$PY" ]; then
    # Named, never a `command not found` from two lines further on: this is an
    # image build fault and the message has to say so, because the operator
    # cannot fix it and needs to know that.
    echo "[entrypoint] no python3 in this image, at any of the paths this" >&2
    echo "[entrypoint]   entrypoint knows or on PATH. The image is broken;" >&2
    echo "[entrypoint]   this is not a configuration you can correct." >&2
    exit 6
fi

echo "[entrypoint] role ${ROLE}${ROLE_FLAG:+ (${ROLE_FLAG})}" >&2

# UNQUOTED ON PURPOSE, AND IT IS SAFE FOR EXACTLY ONE REASON: `$ROLE_FLAG` is
# one of three literals set by the `case` above, never anything the caller
# typed.  Quoted it would expand to an empty argument in the `door` case, which
# argparse reports as `unrecognized arguments: ` -- a refusal with nothing in it
# to read.
# shellcheck disable=SC2086
exec "$PY" "${HERE}/preflight.py" \
    --root "$STORE" \
    --host "$HOST" \
    --port "$PORT" \
    --tokens "$TOKENS" \
    --readers "$READERS" \
    $ROLE_FLAG \
    "$@"
