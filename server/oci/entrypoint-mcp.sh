#!/bin/sh
# The mcp image's entrypoint, shared by its Linux and FreeBSD builds.
#
# WHY IT IS A SECOND FILE AND NOT A MODE OF `entrypoint.sh`
#
#   `entrypoint.sh` execs `preflight.py`, which checks the STORE -- a directory
#   this component does not have and must never be given.  Sharing one script
#   would mean a store path, a tokens path and a readers path in the argv of a
#   process whose whole authority is meant to be one bearer token it was handed
#   per request.  Two files, and neither knows the other's paths.
#
#   What IS shared is the interpreter search, and it is shared by being written
#   the same way rather than by being sourced: Debian's slim image gives
#   `/usr/local/bin/python3` and a FreeBSD image staged from `pkg` gives
#   `/usr/local/bin/python3.12`, with the unversioned symlink in a separate
#   `python3` metapackage this build has no reason to stage.
#
# WHY IT EXECS A SCRIPT AND NOT `-m srv.mcp`
#
#   `python -m srv.mcp` imports `srv/__init__.py` first, which imports
#   `attribute, coverage, ingest, reconcile, window, wire`.  Those are
#   stdlib-only and would not drag DuckDB in, but they would put six modules of
#   the reconciler in an image whose stated contents are "the MCP surface and
#   nothing else" -- and `reconcile` re-exports from `ingest`, so the set only
#   grows.  The module is copied flat to `/opt/claudio-mcp/mcp.py` and run as a
#   script, which it supports: it imports json, os, sys and urllib and nothing
#   else, and no sibling module at all.
#
# THE FLAG SPELLING IS A CONTRACT, AND IT IS WRITTEN DOWN ONCE
#
#   HTTP:   mcp.py --http --host <h> --port <p> --api <url>
#   stdio:  mcp.py --api <url>
#
#   Both work.  `srv/mcp.py` holds one `handle()` and two front ends around it,
#   so a tool cannot exist on one transport and not the other, and a fix to a
#   refusal cannot land on one only.  A test drives the same JSON-RPC messages
#   through both and compares the replies.
#
# WHAT IT MUST NEVER DO
#
#   Serve HTTP with a token in the environment.  See the refusal below.

set -eu

API="${CLAUDIO_API:-http://api:8787}"
HOST="${CLAUDIO_MCP_HOST:-0.0.0.0}"
PORT="${CLAUDIO_MCP_PORT:-8788}"

# shellcheck disable=SC1007
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

MODE=http
if [ "$#" -gt 0 ]; then
    case "$1" in
        # `stdio` is a MODE of this image, not a command that happens to be on
        # PATH, so it is named before the escape hatch could swallow it.  It is
        # what `docker run -i --rm ... stdio` speaks, and it is the shape a
        # desktop client launches directly -- the token stays in the
        # container's environment instead of in a config file that gets pasted
        # into issues.
        stdio) MODE=stdio; shift ;;
        http)  MODE=http;  shift ;;
        -*)    : ;;
        *)     exec "$@" ;;
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
    echo "[mcp] no python3 in this image, at any of the paths this" >&2
    echo "[mcp]   entrypoint knows or on PATH. The image is broken;" >&2
    echo "[mcp]   this is not a configuration you can correct." >&2
    exit 6
fi

if [ "$MODE" = stdio ]; then
    # Here the launcher IS the caller, so an ambient token is exactly right:
    # one process, one client, one identity.
    exec "$PY" "${HERE}/mcp.py" --api "$API" "$@"
fi

# ---------------------------------------------------------------------------
# SERVED OVER HTTP, A TOKEN IN THE ENVIRONMENT IS A PRIVILEGE ESCALATION.
# ---------------------------------------------------------------------------
#
# In HTTP mode every caller presents their own `Authorization` header and the
# door decides what that reader may see -- the same `readers.json`, the same
# scope, so a served MCP shows exactly what a person holding that token would
# get.  An ambient `CLAUDIO_API_TOKEN` breaks that in the one direction that
# matters: a caller who sends NO credential inherits the container's, and the
# MCP surface starts answering questions the caller was never entitled to ask.
#
# Refused here rather than ignored, because the two are not the same for the
# operator.  Ignoring it leaves a token sitting in `docker inspect` and in the
# compose file, doing nothing, waiting for someone to "fix" the code that
# ignores it.  Refusing says what the variable means and which mode it belongs
# to.  Exit 7: `serve.py` owns 1, 3, 4, 8 and 9, `preflight.py` owns 5,
# every entrypoint owns 6 for a missing interpreter, the proxy's owns 10
# and `entrypoint.sh` owns 11 for an unknown CLAUDIO_SERVER_ROLE.
if [ -n "${CLAUDIO_API_TOKEN:-}" ]; then
    echo "[mcp] CLAUDIO_API_TOKEN is set and this is the HTTP transport." >&2
    echo "[mcp]   Served over HTTP, every caller presents their own bearer" >&2
    echo "[mcp]   token and the door applies that reader's scope. An ambient" >&2
    echo "[mcp]   token would be inherited by a caller who sent none, so a" >&2
    echo "[mcp]   served MCP would see more than the person calling it." >&2
    echo "[mcp]   Remove CLAUDIO_API_TOKEN from this service's environment." >&2
    echo "[mcp]   It belongs to the stdio transport, where the launcher is" >&2
    echo "[mcp]   the caller: docker run -i --rm -e CLAUDIO_API_TOKEN=... IMAGE stdio" >&2
    exit 7
fi

exec "$PY" "${HERE}/mcp.py" --http --host "$HOST" --port "$PORT" --api "$API" "$@"
