# The server's DuckDB dependency, given a reproducible home.
#
# WHAT THIS IMAGE IS FOR
#
#   DuckDB is the first and only third-party dependency in this project, and it
#   is permitted in ONE place: `server/`.  Without it, `server/tests/test_all.py`
#   self-skips its whole storage half -- measured on this tree, 1147 passed /
#   44 skipped without duckdb against 2065 passed / 0 skipped with it, i.e. 918
#   assertions that did not run.  They are announced rather than silent, so it
#   is not the cardinal sin, but a green run without the engine is not evidence
#   about the engine, and `mutate.py` refuses such a tree outright.  This image
#   exists so that "run the suite properly" is one command that does not depend
#   on what happens to be installed on the machine.
#
# WHAT THIS IMAGE IS NOT FOR
#
#   It is not a dependency of `claudio` and not a dependency of anything under
#   `usage/`.  `claudio` is POSIX sh and needs nothing at all; `claudio usage`
#   reads the same JSONL with the standard library, offline, with no server, no
#   daemon and no container -- a test in `usage/tests/test_all.py` blocks the
#   name `duckdb` at the import system and runs the real CLI to prove it.  A
#   developer with no container runtime must still be able to run `claudio`,
#   `claudio usage` and both of their suites exactly as before, and can: nothing
#   here is on their path.
#
#   It is also not a production deployment.  It publishes a door that asks for
#   no token on its read API and serves every record in the store, email
#   addresses included.  `server/README.md` says once, plainly, that the door is
#   for a trusted network and must never face the internet; putting it in a
#   container does not change that, and a container is not a security boundary
#   for a service that authenticates nobody.
#
#   It carries NO source code (see the `COPY`-free body below).  The repository
#   is bind-mounted at run time, so a developer edits on the host and runs in
#   the container, and nobody rebuilds an image to run a test.

# The base is pinned to a Debian point release rather than `python:3.12-slim`,
# because `slim` alone floats onto the next Debian and quietly changes the libc
# the DuckDB wheel is linked against.
#
# It is a TAG, not a digest, and that is stated rather than implied: a tag can
# be repointed by its publisher, so this is reproducible in the sense of "the
# same Python and the same DuckDB", not in the cryptographic sense.  Pinning a
# digest here would mean writing down a hash that has not been verified against
# a registry on this machine, which is a value written from an assumption --
# exactly the failure the rest of this project spends its comments avoiding.
#
# Python 3.12 rather than the host's 3.14: every module under `server/` parses
# under `ast.parse(..., feature_version=(3, 9))`, so 3.12 is comfortably above
# the floor, and 3.12 has had DuckDB wheels for far longer than 3.14 has.
FROM python:3.12-slim-bookworm

# 1.5.5 is pinned, and the pin is load-bearing rather than tidiness.
#
# `srv/duck.py` does not use DuckDB, it REFUSES five specific things DuckDB does
# silently, and each refusal is written against a behaviour measured on 1.5.5:
#
#   - `read_json_auto` returning 29 columns instead of 30 because a column was
#     absent from the first 25 000 of 30 000 real rows, inside the default
#     `sample_size` -- which is why the schema is pinned from `wire.LEDGER_FIELDS`
#     and why there is no code path that omits `columns=`.
#   - `ignore_errors=true` emitting a row of NULLs for a malformed line rather
#     than skipping it (1000 good records plus one torn fragment gives
#     `count(*) = 1001`), and, undocumented, nulling a single VALUE that does not
#     fit a pinned type while keeping the row.
#   - a pinned BIGINT COERCING rather than refusing: `1.5` arrives as `2`, `"7"`
#     as `7`, `true` as `1`, no warning on any of the three.
#   - what binds and what does not: the path, a list of paths, the `columns=`
#     map, a value inside `FILTER (WHERE ...)`, a row comparison,
#     `LIKE ... ESCAPE`, a JSON path and `LIMIT` all bind; an IDENTIFIER does
#     not, and `SELECT $c` with `c="model"` silently returns the constant string
#     `'model'` once per row.
#   - NaN ordering ABOVE infinity in DuckDB's total float order while Python's
#     `ts <= nan` is False for every row -- the two engines disagreeing on a
#     bound, which is what `wire`/`compile_query` now refuse outright.
#
# A floating version does not merely risk breaking a test.  It invalidates the
# measurements those comments cite while leaving the sentences in place, so the
# source would go on asserting a behaviour nobody has observed on the engine
# actually running -- a claim with no evidence behind it, which this project
# treats as the same defect as an untested assertion.  Bump the pin and re-run
# `server/tests/test_duck.py`, which is where those five behaviours are asserted
# against the live engine, and correct the prose in the same commit.
ARG DUCKDB_VERSION=1.5.5

# The build asserts what it installed.  `pip install duckdb==X` can succeed and
# leave a different version importable -- a pre-existing wheel in the base, a
# resolver falling back -- and an image whose engine does not match its own pin
# is a reproducible home for the wrong thing.  A build that cannot honour the
# pin fails here rather than at the first query.
RUN set -eu; \
    pip install --no-cache-dir "duckdb==${DUCKDB_VERSION}"; \
    got="$(python -c 'import duckdb; print(duckdb.__version__)')"; \
    if [ "$got" != "${DUCKDB_VERSION}" ]; then \
        echo "duckdb ${DUCKDB_VERSION} was pinned but $got is importable" >&2; \
        exit 1; \
    fi

# The repository is mounted here at run time; nothing is copied in.  A bind
# mount is the whole point -- an image that carried the source would have to be
# rebuilt for every edit, and a developer who forgets would be testing the
# previous commit while reading the current one.
WORKDIR /repo

# The mount is the developer's own working tree.  Without this, every import
# through it drops a root-owned `__pycache__` into their checkout on Linux,
# where the container's uid is not theirs.  The suite services also mount the
# tree READ-ONLY (see compose.yml), which makes the same guarantee structurally;
# this is the half that still holds if somebody mounts it writable.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# No default command, deliberately.  This image has two unrelated jobs -- run
# the door, and run the suite -- and a default that quietly picks one is how a
# `docker run` with no arguments ends up serving a port somebody meant to test.
# `compose.yml` names both, one service each.
