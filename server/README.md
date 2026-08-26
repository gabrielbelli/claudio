# `server/` — the reconciliation core, and the door in front of it

The half of the claudio usage server that interprets, plus the one file that
holds the socket and the bytes.

The core is **pure**: no file, no socket, no database, no clock. `now` is an
argument. A static test walks every module and asserts that none of them
imports anything that touches a file, a socket or a clock — with `srv/serve.py`
named as the single exception, and the exception set asserted rather than
subtracted, so a second impure module has to be admitted in the suite before it
can exist. A second test asserts `srv/__init__.py` never imports the door, so
`import srv` in a pure consumer does not quietly pull in `http.server`.

That is not tidiness. It is what keeps the storage decision open — the record of
truth may end up JSONL, SQLite or something else, and nothing below this line
learns about it — and it is what makes a week-late shipment retroactively
correct a window that closed days ago, because every report is re-derived from
every record, every time.

```bash
# the one command: the suite, with the engine, in a container that pins it
docker compose -f server/compose.yml run --rm suite

# without a container runtime, exactly as before — a venv is enough
python3 -m venv env && env/bin/pip install duckdb
env/bin/python3 server/tests/test_all.py
env/bin/python3 server/tests/mutate.py    # break each behaviour, require a failure

# with no engine at all the suite REFUSES rather than reporting a partial pass
python3 server/tests/test_all.py --allow-skips   # ...if you want it anyway
```

**Run both under an interpreter that has DuckDB, and read the skip count.**
The suite reports `N passed, N failed, N skipped`, and without `duckdb` the
skip count is not small — every route test self-skips. Measured on this tree,
same source, two interpreters:

| interpreter | suite says |
|---|---|
| no duckdb | **1379 passed, 0 failed, 45 skipped** |
| duckdb | 2298 passed, 0 failed, 0 skipped |

> Both totals drift upwards every time an assertion is added, and they have:
> they were first written as 1129 and 2047, were 18 out by the time the
> container work was finished, were 1154 and 2072 mid-packaging, 1164 and 2082
> at the packaging run that made the release ready, and are 1379 and 2298 as of
> the image work.
> **The figure that carries the argument is the gap, not either total**, and
> this instruction has now earned itself: the gap was 918 at three consecutive
> measurements and **is 919 at this one**, with the skipped test functions up
> from 44 to 45. That is not drift, it is one more test function that needs the
> engine, and it is the thing the sentence above asks a re-measurer to look
> for. If you re-measure and the gap has moved again, find out which test
> moved it before you edit this table.

**919 assertions — 40% of the suite — did not run in the first case.** They are
announced rather than silent, so this is not the cardinal sin; but `1379
passed, 0 failed` is the sentence a reader acts on, and 45 `SKIP` lines scroll
off the top of a terminal while a summary line does not. So a skipping run
prints a banner **after** the summary — the last thing on screen — naming the
interpreter that could not import the engine, saying outright that the run is
not evidence about it, and giving both remedies.

**And a skipping run exits 1.** That is a correction, and the argument for the
old default did not survive contact with `make install`: `make check` runs this
file, `make check` is what this project documents as the release gate, and with
a banner over exit 0 it reported success on a machine where 918 assertions had
never run — a banner on stdout being nothing that CI, `&&` chains or Make read.
It is `claudio usage doctor`'s ruling one repository half over: every WARN it
prints sets a non-zero exit, because it used to print `WARN ledger rows: 0` and
exit 0. A developer without the engine is still entitled to the 1379 assertions
that do not need it — `--allow-skips` is one word, it says out loud what it
accepted, and `make check SERVER_SUITE_ARGS=--allow-skips` passes it through.
`--require-duckdb` still works and is now redundant; `compose.yml`'s `suite`
service goes on passing it, because a container built specifically to supply
DuckDB saying so out loud is documentation rather than ceremony.

**A run with skips is not evidence about the storage engine.** Neither is a
green `mutate.py` over one — which is why it refuses outright: `mutate.py` now
**refuses** a skipping baseline exactly as it refuses a red one, because a
survivor over a skipping tree is not "nothing asserts this", it is "this never
ran", and the harness printed the first sentence for the second condition:

| interpreter | matrix said | truth |
|---|---|---|
| no duckdb | 118 caught, **42 SURVIVED**, 1 BAD PATCH | all 42 false |
| duckdb | 172 caught, 0 survived, 0 bad patches | — |

> The two rows are not from one run, and the arithmetic says so: 118 + 42 + 1
> is 161, and the matrix is 172 rows now. The no-duckdb row is the measurement
> that **bought** the refusal, taken over the matrix as it then stood; it
> cannot be re-taken, because `mutate.py` now refuses that tree by design. The
> duckdb row is current. Leaving the first to drift into agreement with the
> second would delete the evidence for the refusal while keeping the sentence
> that cites it.

The 42 were the subject of the review that found them: `store-crosses-accounts`,
`api-cross-account-total`, `api-histogram-crosses-accounts`,
`store-empty-answers-merged`, `api-two-empties-become-one`,
`api-coverage-stripped`, `api-remedy-keeps-the-module-spelling` — every
guarantee the DuckDB swap put at risk, each reported as unguarded when it was
merely unrun. That is this project's cardinal sin sitting inside the one tool
whose whole job is to prove the suite is not decoration, and it is worse than a
silent skip because it emits confident wrong output a reader acts on by
"fixing" controls that already work. Three `have_duck()` guards were also a
bare `return` — one of them the first statement of its test, so that test ran
**zero assertions and printed nothing** — and a static test now walks this file
and requires every such guard to announce itself through the counted mechanism.

```bash
python3 -m venv env && env/bin/pip install duckdb   # what the refusal tells you
```


## Reader authentication

The read API is gated by `readers.json`, the mirror of the write side's
`tokens.json` and deliberately a separate file: a machine that ships is not
thereby entitled to read, and a reader is usually a person or a front end that
ships nothing.

```json
{
  "omini":       "*",
  "team-report": ["f1f7000d-0000-4000-8000-00000000000d"]
}
```

`"*"` is the whole store. A list scopes the token to those accounts.

**A front end is a universal reader, and that is the intended shape.** omini
holds `"*"`, exactly as Graylog holds every message and Kibana every index.
Isolation between people belongs *in the front end*, as streams or index
patterns are, because that is the only layer that knows who is logged in and is
the only one that can hold one permission model across several back ends -- this
store, an OpenSearch cluster, an incident tracker. Minting a per-user token per
back end does not compose and does not survive a second back end.

That is not the confused-deputy shape it resembles. A confused deputy is a
component with broad access and *no authorisation model of its own*, which is a
bare proxy. A front end with users, roles and streams is the authority its
users authenticate against; the token here says which store it may reach, not
which human is asking.

Scoped tokens are for the narrower consumers: a reporting script, a per-team
reader, a friend who should see one account and no others. They are the
exception rather than the rule, and they exist so that "give someone a subset"
never requires a second server.

Every `/api/v1/` route requires `Authorization: Bearer <token>`; `/healthz` does
not, because operators and health checks read it.

**Scope is applied as a refusal, never as a filter.** Asking for an account
outside a token's scope answers `403 account-not-permitted` by name. An empty
result would be byte-indistinguishable from an account that has shipped
nothing, and telling those two apart is why this API spells `no-data`,
`filtered-to-nothing` and `unanswerable` as three different words. Both reasons
are served in `/api/v1/capabilities`, so a client holds no copy of the
vocabulary.

**And scope is decided by the token, not by the `account` parameter.** That
sentence is a correction. The gate read `params.get("account")`, and
`Readers.permits` returned `True` whenever the caller named no account at all,
on a docstring that delegated the omitted case to "the route ... and `store`
already refuses to total across accounts". `store` refuses to **total** across
accounts; it returns **rows** across them happily. Measured against the running
stack with `reader-alpha`, scoped to one account:

```
GET /api/v1/search?text=beta@example.com          200 ok   beta's rows
GET /api/v1/lookup?field=session_id&value=<beta>  200 ok   beta's rows
GET /api/v1/accounts                              200 ok   both identities
GET /api/v1/diagnostics                           200 ok   both
```

and identically through the MCP surface, which forwards the caller's token:
`tools/call search {"text":"beta@example.com"}` with the alpha token returned
`isError: false` and beta's rows to the agent. The scope held only against a
caller that cooperated by naming the account it was not allowed to read, and
every existing test asked with `?account=`, so the hole was unguarded.

A scoped token that names no account is now refused by name --
**`403 account-required-for-this-token`**, with the covered UUIDs in the
remedy -- on every route that can return rows or identities. Three routes are
the exception because none of them reads a record: `openapi`, `capabilities`
and `health`. `aggregate/per-account` gets its own sentence, because it fans
out over every account by design and *refuses* `?account=`, so the generic
remedy would be a confidently wrong instruction; it names
`GET /api/v1/aggregate?account=<uuid>&by=<column>` instead.

**`meta.accounts` was the leak that survived every per-route repair.** `_meta`
attached identity, email address, `organization_uuid` and `account_uuid` for
every account in the store, on every payload, built from the snapshot and never
from the caller -- so a scoped `/api/v1/search` returned its own rows and named
the other account beside them, with `meta.root`, the store's absolute path, in
the same object. It is attached *after* a route has correctly answered about
the account it was allowed to answer about, so it is closed once, on the way
out, rather than in fourteen routes. A scoped token now gets only the accounts
it covers, `root: null`, a `meta.scope`, and a `meta.accounts_are` saying
outright that the list is a statement about the token and not about the store
-- because an unqualified short list is a second wrong answer, and "this store
holds one account" is a claim a scoped reader cannot check.

`"*"` tokens are byte-for-byte unchanged, note and all.

**Absent `readers.json` means no reader auth.** That is allowed on loopback,
with a warning at startup, and it is the development default. On any other bind
the door **refuses to start**: the read API returns every record in the store,
email addresses included, so how exposed it is and who may read it are decided
together or the exposure wins by default. `--no-api` is the other way out.

`_is_loopback` compares the bind address as text rather than resolving it. A
name that resolves to 127.0.0.1 today can resolve elsewhere tomorrow, and this
is the check that decides whether an unauthenticated read API may exist.

## MCP: letting a model ask

`srv/mcp.py` is an MCP server over the read API, so a model can search usage
and reason about it.

**Two transports, one handler.** `mcp.handle()` takes one JSON-RPC message and
returns one reply; the stdio loop and the HTTP front end call it and nothing
else, so a tool cannot exist on one and not the other and a fix to a refusal
cannot land on one transport only. A test drives the same five messages through
both against a live API and requires the replies to differ in **nothing** but
the clock and the derivation counter — enumerated by JSON path rather than
blanked, so a genuine divergence anywhere else is a named failure.

**Stdio** is what desktop clients speak and what composes with `docker run -i`.
There the launcher *is* the caller, so an ambient token is exactly right: one
process, one client, one identity.

```jsonc
// in your MCP client's config
{ "claudio-usage": {
    "command": "docker",
    "args": ["run", "-i", "--rm",
             "-e", "CLAUDIO_API=http://host.docker.internal:8787",
             "-e", "CLAUDIO_API_TOKEN=<a reader token>",
             "ghcr.io/gabrielbelli/claudio-mcp:testing", "stdio"] } }
```

**Streamable HTTP** (`POST /mcp`) is what runs in the stack, behind the proxy,
beside the API. There every caller presents their **own** bearer token, which
is forwarded to the read API untouched and scoped by the same `readers.json`
that gates `/api/v1/*`.

```bash
curl -sk https://localhost:8443/mcp \
  -H 'Authorization: Bearer <a reader token>' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

An ambient `CLAUDIO_API_TOKEN` in HTTP mode is **refused** (exit 7), in the
process as well as in the entrypoint. A caller who sent no credential would
inherit it, and the surface would answer questions the caller was never
entitled to ask. Refused rather than ignored, because ignoring it leaves a
token in `docker inspect` doing nothing, waiting for somebody to "fix" the code
that ignores it.

**It is a client of `/api/v1/`, and that is the whole of its authority.** It
holds a reader token and makes HTTP calls: it cannot reach the store's files,
cannot see an account the token's scope excludes, and cannot answer a question
the API refuses. Handing it the DuckDB handle would have been fewer moving
parts and a privilege escalation, because an agent with a database handle is
bounded by nothing the operator configured.

**Its tools are generated from `/api/v1/openapi`**, so it holds no copy of the
route list, the parameter names or their types. A hardcoded list describes an
endpoint the server may not have, and a model that calls it cannot tell a stale
client from a broken server.

They are fetched **per call and not cached**, and the reason is the token: over
HTTP the credential arrives on the request, so a list fetched once at startup
would have to be fetched with somebody else's or with none. That matters more
than it sounds. The line this replaces was

```python
tools_from_spec((api.get("/api/v1/openapi") or {}).get("result") or {})
```

which turns a 401, a 503, an unreachable host and a renamed `/api/` prefix
alike into `{}` — and `{}` into an **empty tool list**, served with
`"jsonrpc": "2.0"` and no error on it. A model reading that concludes the
server has no tools. `tools_for()` now returns the tools **or a named refusal**
(`no-spec`, or `no-tools` for prefix drift), and `handle()` turns a refusal into
a JSON-RPC error carrying the API's own remedy — never an empty array, and
never a tool result either, because the tool did not run.

**A refusal reaches the model as an error, not an empty result.** `no-data`,
`filtered-to-nothing` and `unanswerable` mean "nothing was ever recorded",
"data exists but this query excluded all of it" and "that question has no
answer here" -- three different next actions. An agent that reads a refusal as
an empty list concludes there is no data when there is, which is this project's
cardinal sin with a model in the loop instead of a person. So `isError` is set
on refusals only, the remedy is passed through, and every tool description
repeats the warning -- not once in a server description a model may never read.

Operator routes (`health`, `diagnostics`) are deliberately not exposed: they
are questions for a person, and every extra tool is context a model spends
before it has read anything.


## The container, and the boundary around it

**Verified.** The image was built and all four services were exercised on
Docker 29.6.2 (Darwin, arm64). This paragraph replaces an `UNVERIFIED` one
that said the daemon had never been running on the machine these files were
written on, and it is worth keeping the shape of that correction: everything
below the line "the image builds" had been reasoned about statically and was
right, but *reasoned about statically and right* is a different claim from
*run*, and only one of them is evidence.

| verified | how |
|---|---|
| the image builds, and the base tag resolves to a wheel-bearing 3.12 | `docker compose -f server/compose.yml build suite`; `python 3.12.14` reported from inside |
| the build's version assertion holds on the engine it installed | `duckdb 1.5.5` imported inside the built image, matching `ARG DUCKDB_VERSION` |
| the suite runs **with** the engine and self-skips nothing | `run --rm suite` → `2298 passed, 0 failed, 0 skipped`, exit 0 — the same count as a host venv, so the container is not a different tree (re-run at every pass since: 2065 when the image was built, 2072 mid-packaging, 2082 at packaging, 2298 at the image work, matching the host venv of that day each time) |
| a skipping run refuses, and `--allow-skips` is the only way past it | exit 1 on an interpreter with no engine, naming the count and the flag; exit 0 with the flag, and exit 0 here where nothing skipped — the `suite` service still passes `--require-duckdb`, which is now redundant and still accepted |
| the `:ro` mount is genuinely read-only | `touch /repo/…` inside → `Read-only file system`, exit 1; the host checkout was unchanged afterwards |
| the mutation matrix runs through the same image | `run --rm mutate` with a one-row filter → `baseline: 2065 passed, 0 failed, 0 skipped`, row caught. That figure is the container run as taken; the **full** matrix has since been re-run on a host venv, most recently at baseline 2448 — 172 mutations, 172 caught, 0 survived, 0 bad patches. One of those re-runs reported a **BAD PATCH** rather than a verdict, and that is the harness working: a `coverage` refusal given the same shape as the `requests` one beside it made an existing mutation's search string match twice, and a row whose patch is ambiguous is reported, never counted as caught |
| the door starts and answers **through the published mapping** | `/healthz` and `/api/v1/capabilities` (13 routes) fetched from the host |
| the door prints its two trust warnings on every start, as this file claims | both observed on the container's stderr, verbatim |
| `compose.yml` is valid YAML, four services, the commands and mounts as written | parsed with `yaml.safe_load` |
| the `Dockerfile`'s instructions are all real, and its `RUN` body is valid POSIX `sh` | parsed; body checked with `sh -n` **and** `dash -n` |
| every module under `server/` parses on Python 3.9, so the 3.12 base is above the floor | `ast.parse(..., feature_version=(3, 9))` |
| the suite writes **nothing** into the repository, so the `:ro` mount is safe on a writable mount too | full run under `PYTHONDONTWRITEBYTECODE=1`, then `find -newer` |
| every claim this README makes about the container *files* | pinned by `test_the_container_pins_the_engine_the_measurements_were_written_against`, each guard mutated once to confirm it fails |

One operational note, met while verifying and not a fault: the publish mapping
is pinned to `127.0.0.1:8787`, so if you already have a door listening on 8787
— a bare `serve.py` from an earlier session, say — `up door` fails with
`address already in use` rather than starting beside it. That is the mapping
doing its job. Stop the other one, or publish elsewhere for a one-off:

```bash
docker compose -f server/compose.yml run --rm -p 127.0.0.1:8797:8787 door
```

```bash
docker compose -f server/compose.yml run --rm suite    # the suite, with the engine
docker compose -f server/compose.yml run --rm mutate   # the mutation matrix
docker compose -f server/compose.yml up door           # the door, on 127.0.0.1:8787
```

### What it is for

One thing: making `server/`'s suite stop self-skipping its storage half on a
machine that has no DuckDB. The image is a pinned Python and a pinned DuckDB
and **nothing else** — it contains no source at all, and the repository is
bind-mounted at run time, so a developer edits on the host and runs in the
container and **nobody rebuilds an image to run a test**. `--build` is needed
only when the pinned version changes.

The pin is `duckdb==1.5.5`, and it is load-bearing rather than tidiness.
`srv/duck.py` does not so much use DuckDB as **refuse five specific things it
does silently**, and every one of those refusals cites a behaviour *measured on
1.5.5* — a column dropped by inference at the default `sample_size`,
`ignore_errors` emitting a row of NULLs instead of skipping a line, a pinned
`BIGINT` coercing `"7"` and `true` rather than refusing them, exactly what binds
and the fact that an identifier does not, and NaN ordering above infinity. A
floating version would leave those sentences in place while removing the
evidence for them, so the source would go on asserting a behaviour nobody has
observed on the engine actually running. The build **asserts what it
installed** and fails if the two disagree, and a test derives the version from
`duck.py`'s own comments and requires the Dockerfile and the image tag to name
the same one — a hardcoded copy would be a fourth place to forget. Bump the pin,
re-run `server/tests/test_duck.py` (where those five behaviours are asserted
against the live engine), and correct the prose in the same commit.

The suite and mutate services mount the tree **read-only**. Both write only
under `tempfile.mkdtemp()`, so it costs nothing and makes "the container cannot
touch your checkout" structural rather than a habit.

### What it is not for

It is **not** a dependency of `claudio` and **not** a dependency of anything
under `usage/`. `claudio` is a POSIX `sh` script plus the standard-library
Python tree it drives (`bin/claudio` and `libexec/claudio/`), and between them
they have **no third-party dependency at all** — which is precisely why DuckDB
has to stay inside `server/`: adding it anywhere else would hand a build
dependency to every user of a tool that has none, for a server almost none of
them run. `claudio usage` reads the same JSONL with the standard library,
offline, with no server, no daemon and no container — `claudio usage show` on a plane is most of
why the tool is pleasant. A developer with no container runtime runs both, and
both of their suites, exactly as before. Nothing here is on their path.

That boundary is asserted, not merely intended, in three ways: a static walk of
`usage/` for any `import duckdb` by any spelling; a behavioural run of the real
`claudio usage show` with the name **blocked at the import system**; and a
directory listing asserting no container file or dependency manifest exists
anywhere under `usage/` or at the repository root, that `claudio` never names
`duckdb`, and — positively, so the test fails if the container is deleted as
well as if it spreads — that the Dockerfile and compose file are in `server/`.

It is also **not a production deployment, and not a security boundary.** With
no `readers.json` the door's read API asks for no token and serves every record
in the store, email addresses included — which is why it *refuses to start at
all* in that state on any bind that is not loopback, and why the `door` service
mounts one. `--host 0.0.0.0` in the `door` service is not a
relaxation of that: inside a container it is the container's own network
namespace, and what decides who can reach the port is the publish mapping,
which is pinned to `127.0.0.1:8787:8787` on the host. The consequence is stated
rather than hidden — because the process sees a non-loopback bind it prints its
two trust warnings on every start, and they are true of the container's
network. Publishing `8787:8787` instead is exactly how this would end up facing
the internet.

**The door is for a trusted network and must never face the internet.** There
is no TLS, no mTLS, no CA, no token expiry and no rate limiting; the bearer
token is a routing label for `POST /v1/ship` and is not engineered as a
security boundary. A container does not change any of that.

## The four-way split

The door used to be one process: it took shipments, held the store lock, held
the store read-write, ran DuckDB over it and served an MCP surface out of the
same port. It is four services now, each carrying only what it needs.

| | serves | image holds | store | lock |
|---|---|---|---|---|
| **claudio-ingest** | `POST /v1/ship` | CPython, `srv/`. **No DuckDB.** | read-**write** | **yes** |
| **claudio-api** | `GET /api/v1/*` | the same, plus DuckDB | read-**only** | no |
| **claudio-mcp** | `POST /mcp` | `srv/mcp.py` alone. Standard library. | **none** | n/a |
| **claudio-proxy** | 8080 / 8443 | nginx. No Python, no source. | none | n/a |

```
                        the ONLY published port
                                  |
     https://host/sender  ->  [ proxy ]  ->  ingest   POST /v1/ship
     https://host/api/    ->      |      ->  api      GET  /api/v1/*
     https://host/mcp     ->      |      ->  mcp      POST /mcp
                                              |
                                              +----> api (an HTTP client of it,
                                                     forwarding the caller's
                                                     bearer token)
```

### How the shipper identifies itself to a proxy

Every `POST /v1/ship` claudio sends carries:

```
User-Agent: claudio-ship/<claudio version>      e.g. claudio-ship/0.1.0
```

That is what a WAF rule matches on. It is a copy of the `VERSION=` line in the
`claudio` script and a test compares the two, so it tracks releases rather than
being set once and forgotten; the same string is repeated in the batch
manifest's `agent` field, from one constant, so the header and the body can
never give two answers about what is calling.

It deliberately carries nothing else -- no hostname, no platform, no
interpreter version. `otlp-recv` and this door both had `Server: BaseHTTP/0.6
Python/3.14.6` replaced for publishing a machine's exact patch level from a
listening socket, and a request crossing somebody's network is that same
disclosure pointed outward.

> Rules keyed on the version need widening at each release. Match the prefix
> `claudio-ship/` when what you mean is "claudio", and pin the full string only
> when you mean one release.

The door reads none of it. It authorises on the bearer token alone, so a rule
here is a network control in front of the door and never a replacement for one:
a request reaching `/v1/ship` with a valid token is accepted whatever its
`User-Agent` says, and any client can send any header it likes.

```bash
echo '["<account-uuid>.<secret>"]' > server/tokens.json    # who may SHIP
echo '{"<reader-token>": "*"}'     > server/readers.json   # who may READ
docker compose -f server/oci/compose.yml up --build
```

That is the whole of the setup. TLS is served on 8443 from a **self-signed
certificate the proxy generates on first start** and announces, at length, on
every start after it — so the stack comes up over HTTPS on a fresh clone with
no certificate anywhere, and the sentence telling you to replace it is in front
of you rather than in this file. Real certificates are two lines in a `.env`:

```
CLAUDIO_DOMAIN=claudio.example.com
CLAUDIO_TLS_DIR=/etc/letsencrypt/live/claudio.example.com
```

Nothing in the nginx configuration changes: the certificate paths are fixed,
the TLS server matches any host, and the domain exists only as the CN of a
certificate that has to be generated. One place, two variables.

### It is not primarily a size win, and saying so is the point

Built and measured on the machine this was written on:

| image | size | of which base |
|---|---|---|
| claudio-ingest | 215 MB | ~76 % |
| claudio-api | 302 MB | ~50 % |
| claudio-mcp | 213 MB | ~99 % |
| claudio-proxy | 94.2 MB | ~99 % |

So the engine is 87 MB of the api, and the ingest and mcp images are mostly
base image. The saving is real and modest. **What the split buys is authority.**
The process that holds the write lock and the store read-write has no query
engine in it. The process that answers arbitrary caller-shaped questions cannot
write a byte. The process an agent talks to has neither, and no store volume at
all — not read-only, none.

That two of the four need no DuckDB was **measured, not assumed**. `import
duckdb` appears exactly once in the whole package, inside `store.require()`,
called from `DuckStore.con()` on the first query; nothing imports it at module
level. On an interpreter with no duckdb installed, `import srv.serve` succeeds,
`duckdb` is not in `sys.modules`, and `store.available()` is `False` — which is
the case `serve.py` already prints `engine duckdb MISSING -- stream A routes
will refuse by name` for. It is asserted in the suite the same way
`usage/tests/test_all.py` asserts that nothing under `usage/` imports duckdb.

And it is asserted **in the built artefacts**, which is the only place the
claim actually has to hold -- a Containerfile that does not install duckdb and
an image that nonetheless contains it would satisfy every static check in this
repository:

```
$ docker run --rm claudio-<c>:local python3 -c \
    "import importlib.util as u; print(u.find_spec('duckdb') is not None)"
ingest   duckdb importable: False        duckdb paths on disk: 0
api      duckdb importable: True         duckdb paths on disk: 2
mcp      duckdb importable: False        duckdb paths on disk: 0
proxy    no python at all (nginx only)

$ docker compose exec api python3 -c "import duckdb; print(duckdb.__version__)"
1.5.5
```

### The lock became the writer's, and that is the one edit that could lose data

`lock_root` ran unconditionally in `serve()` and refused a **second process of
any kind** on one root. Its docstring says why, and read closely the reason is
narrower than the lock was:

> Two processes on one root would not corrupt the record files — every append
> is `O_APPEND` — but they would interleave read-modify-write on the offsets,
> and an offset is what the ack promises.

It does not say "two processes on one root". It says two processes interleaving
the **offsets**, and the offsets are written in exactly one place: the
read-modify-write inside `Store.accept`, from `load_offsets` to `save_offsets`,
held under `Store.lock`. A reader never opens that file. So it is the
**writer's** lock, `serve()` takes it under `ship_enabled`, and the reader-only
api never reaches it. The docstring was extended rather than replaced, so the
next person can see this was narrowed deliberately and not forgotten.

**Two writers are still refused, loudly.** Both would be ship-enabled, both
take the lock, the second gets `None` and exits 3 with the message it always
printed. Three shapes, all exit 3: ingest beside ingest, ingest beside a whole
door, whole door beside whole door.

**What was given up, stated rather than discovered.** The lock incidentally
prevented a *reader* too. That refusal was never load-bearing: the reader
inside the whole-door process has always read the JSONL with no lock at all —
`View.load` takes nothing, `DuckStore` takes nothing — so the split moves
concurrency that was already happening between two threads across a process
boundary. What covers it is what covered it before, and none of it mentions a
lock:

- a torn final line is a write in progress, and `Store._append` **terminates**
  it rather than gluing a good record onto the front of it;
- a manifest whose byte range is not fully on disk is **reported** by
  `read_batches`, never skipped;
- `request_id IS NULL` is exactly the malformed set `ignore_errors=true`
  produces, and `duck.py` excludes it from every count and every page and
  **reports** it as a store problem.

The measurement in [Concurrency](#concurrency-the-door-appends-while-a-query-reads)
is a reader and an `O_APPEND` writer on one file, which is precisely this, and
it was taken before the split for a different reason.

**And what this lock has never covered.** `flock` is advisory and is not
dependable across NFS or SMB. A store on a network filesystem with two writers
on two machines is refused by nothing here and never was.

### Two flags, and the empty intersection between them

```
(neither)   the whole door -- ships in, reads out.  Still the default, so
            nothing that ran before the split runs differently.
--no-api    the INGEST service.  POST /v1/ship.  Takes the lock.
--no-ship   the API service.     GET /api/v1/*.   Takes none, wants :ro.
both        refused by name, exit 8.
```

`--no-ship` decides three things at once and that is deliberate — one spelling,
no second name to disagree with it:

1. `serve()` takes no `door.lock`;
2. `Store` is built with `create=False`, so a read-only mount does not raise
   `EROFS` out of `os.makedirs` on a process that was never going to write;
3. `preflight.py` reads the same flag out of the argv it is handed and runs its
   **read** probe instead of its write one — it must prove it can *list* the
   store, and it *warns* if it can also write, because a mount that was meant
   to say `:ro` and does not is the day somebody starts that image without the
   role and gets a second writer.

The images declare their role in `CLAUDIO_SERVER_ROLE` rather than in a `CMD`.
A `CMD` is replaced **wholesale**, so `docker run IMAGE --port 9000` would
silently drop the flag that makes the image what it is and start a whole door.
As an environment variable the role survives every argv override, is visible in
`docker inspect`, is printed on every start, and an unrecognised value is
refused (exit 11) rather than falling back to the whole door.

### The refusals, and why each is a refusal

| exit | who | when |
|---|---|---|
| 1 | `serve.py` | cannot bind |
| 3 | `serve.py` | another **writer** holds `door.lock` |
| 4 | `serve.py` | an unauthenticated read API on a non-loopback bind |
| 5 | `preflight.py` | the store is unusable in the direction this role needs |
| 6 | either entrypoint | no interpreter in the image |
| 7 | mcp | an ambient `CLAUDIO_API_TOKEN` in HTTP mode |
| 8 | `serve.py` | `--no-api` **and** `--no-ship`: a port that answers `/healthz` and nothing else |
| 9 | `serve.py` | reader-only, and `<root>/accounts` is not there |
| 10 | proxy entrypoint | no certificate and no way to make one |
| 11 | `entrypoint.sh` | `CLAUDIO_SERVER_ROLE` names a role it does not know |

**Exit 9 is new damage the split creates, and it is refused rather than
discovered.** `accounts_in` swallows `OSError` and returns `[]`, and
`Paths.accounts` does the same — both deliberately, on the write path, where an
account directory that does not exist yet is not an error. On the *read* path
with a mistyped `--root`, or a bind mount that did not land, those two zeros
compose into a server answering `no-data` about every account in a store it is
not looking at, which is byte-indistinguishable from a store nobody has shipped
to. The writer creates `accounts/`; a reader cannot, and must not pretend it
did. (The compose file therefore has the api wait on the ingest service's
**health check**, not merely on it having started.)

**A shipment to the api is 503 `no-ship`, never a 404 and never an ack.** A 404
would tell a shipper the URL does not exist, and a shipper that concludes that
stops trying — it exists, and this process is not the one that serves it. An
ack would be worse: the client advances its offset on a 200 and those bytes are
gone from its point of view while nothing was written. The offset is untouched
either way, so the client resends the identical range to the process that does.

**And the reader's `/healthz` says its counters mean nothing.** Every write
counter in that payload comes from *this process's own* `Counters`, and a
reader accepts no batch — so `records_written: 0` is structurally true of it
and says nothing about the store. The shape is otherwise unchanged; `role` and
`counters_are` are added, and only the reader carries the second, because a
caveat on every payload is a caveat nobody reads.

### The bind refusal, behind a proxy, works out to exactly the same thing

`serve.py` refuses to serve an unauthenticated read API on a non-loopback bind
(exit 4). Inside a compose network the api binds `0.0.0.0`, which `_is_loopback`
reads as **exposed** — correctly, because it is: every other container on that
network can reach it, and so can anything the operator publishes by accident.
So the refusal fires, and it is satisfied the only way it may be, by mounting a
`readers.json`. It is **not** satisfied by the proxy. Nothing in the proxy adds
authentication and nothing in it must: reader scope is decided in exactly one
place, by that file, and a second gate would be a second place for it to be
decided.

The proxy holds no token of its own and forwards `Authorization` untouched. The
MCP service holds none either — its authority is exactly the caller's forwarded
token, applied by the api against the same `readers.json`.

### Driven end to end, through the proxy

Built with Docker on this machine, brought up with `docker compose up` **from
an empty store directory**, and driven over TLS through the published port.
Every line below crossed a process boundary; the shipped bytes are the real
captured fixtures, put through `cu.ledger.append` and `cu.ship.stamp` exactly
as a workstation would.

Two accounts shipped: three stream-A records and ten stream-B records for one,
two stream-A records for the other. `docker compose ps` and `docker port`
confirm what publishes:

```
claudio-ingest-1   Up (healthy)   8787/tcp                      # exposed, NOT published
claudio-api-1      Up (healthy)   8787/tcp                      # exposed, NOT published
claudio-mcp-1      Up             8788/tcp                      # exposed, NOT published
claudio-proxy-1    Up             127.0.0.1:18080->8080/tcp,
                                  127.0.0.1:18443->8443/tcp     # the only published port

$ docker port claudio-ingest-1   ->  (nothing)
$ docker port claudio-api-1      ->  (nothing)
$ docker port claudio-mcp-1      ->  (nothing)
```

Ship, read, MCP, and a bad token at each:

```
== 1. SHIP through /sender ==
POST /sender  stream A, alpha     200  {"ok": true, "accepted": 3,  "offset": 2210, "stream": "ledger"}
POST /sender  stream B, alpha     200  {"ok": true, "accepted": 10, "offset": 6322, "stream": "samples"}
POST /sender  stream A, beta      200  {"ok": true, "accepted": 2,  "offset": 1468, "stream": "ledger"}
POST /sender  the same range again 200 {"ok": true, "accepted": 0,  "duplicate": true}
POST /sender  BAD token           401  {"ok": false, "reason": "unknown-token"}
POST /sender  a 15-char secret    401  refused at PARSE, by name, before any request:
                                       [door] tokens: entry 2 refused -- a bare list holds
                                       SELF-DESCRIBING keys, and this is not one

== 2. READ through /api/ ==
GET /api/v1/accounts              200  outcome=ok, both accounts, each with its uuid and tier
GET /api/v1/search?limit=3        200  outcome=ok, rows: 3, real request_ids and models
                                       coverage: fraction=null known=false basis=no-report-supplied
                                       store_problems: []
GET /api/v1/search  NO token      401  unanswerable / unauthorised, with a remedy
GET /api/v1/search  BAD token     401  unanswerable / unauthorised, with a remedy
GET /healthz                      404  not-exposed  (the proxy declines to publish it)
GET /nonsense                     404  no-such-route, naming the three that exist

== 3. REFUSALS, each named, none of them an empty list ==
scoped token -> another account   403  account-not-permitted
                                       "this token may not read account f1f70010-..."
   ...same token, its own account 200  outcome=ok, rows: 1

-- and the shapes this transcript ONCE DID NOT ASK, which is how the scope hole
-- was reported as proven-safe. Every row above names an account; the hole was
-- a scoped token that named none. Asked of ALL FOURTEEN routes, through the
-- proxy, with `reader-alpha`:
scoped token, no ?account= at all:
  openapi, capabilities, health   200  ok      -- they read no record
  the other eleven                403  account-required-for-this-token
  UUIDs from outside the scope appearing anywhere in any of the 14 payloads: 0
  ...and `accounts` and `aggregate/per-account` get their OWN remedy, because
     the generic "pass ?account=" is a second refusal on those two
scoped token, its own account     200  ok
  meta.accounts                          only f1f7000d-...  (was: all three)
  meta.root                              null               (was: /var/lib/claudio-usage)
  meta.scope / meta.accounts_are         present, saying the list is the TOKEN's
'*' token, same questions          200  ok, all three accounts, root present,
                                        no scope keys added -- unchanged
GET /aggregate?by=model  (no acct) 400 crosses-accounts, naming both uuids and offering
                                       ?account= or /api/v1/aggregate/per-account
GET /histogram (no account)       400  crosses-accounts  -- the hole that was once invisible
GET /aggregate?by=account         400  label-is-not-an-identity
                                       "`account` is a label ... group by account_uuid"
GET /aggregate?by=nonsense        400  unknown-column, listing the columns that exist
where.model=no-such-model         200  filtered-to-nothing, sole_cause named:
                                       "model in ['no-such-model']" eliminated 3 of 3 scanned

On every one of the 400/401/403 refusals the payload carries `refusal` and has
**no `result` key at all**, so a client looping over `p.result.rows` raises
instead of drawing an empty table. `filtered-to-nothing` is a legitimate 200
and carries `result` *and* `empty` -- rows really is empty, and `empty` says
which clause emptied it.

== 4. MCP through /mcp ==
initialize                        200  protocolVersion 2025-06-18, claudio-usage/1
tools/list                        200  11 tools, generated from the api's own OpenAPI
tools/call search                 200  isError=False outcome=ok rows=2, real rows
tools/call aggregate_per_account  200  isError=False, per-account buckets, NO cross-account total
tools/call aggregate (no account) 200  isError=TRUE  outcome=unanswerable reason=crosses-accounts
tools/call aggregate_per_account  200  isError=TRUE  reason=no-group-by, remedy "pass ?by=<column>"
  scoped token -> another account 200  isError=TRUE  reason=account-not-permitted
  scoped token -> own account     200  isError=False outcome=ok rows=1
  scoped token, search {"text": "beta@example.com"}
                                  200  isError=TRUE  account-required-for-this-token
                                       -- the exact call that used to return
                                          isError:false and beta's rows to an agent
  scoped token, search {}         200  isError=TRUE  account-required-for-this-token
  scoped token, accounts {}       200  isError=TRUE  account-required-for-this-token
  no account outside the scope appears in any of those tool results
tools/list  NO token              200  JSON-RPC error -32000 no-spec  (NOT an empty list)
```

The MCP rows are the load-bearing ones: **its authority is exactly the
caller's.** It holds no token of its own (setting `CLAUDIO_API_TOKEN` on the
served transport makes it refuse to start, exit 7) and it has no store volume,
so the same scoped token that is refused `f1f70010-…` through `/api/` is
refused it through `/mcp`, from the same `readers.json`, and a refusal arrives
as `isError: true` carrying the reason -- never as an empty list.

and the three refusals, run as real containers:

```
$ docker run ... claudio-ingest:local          # beside the running one
[door] another door already holds /var/lib/claudio-usage (door.lock)   -> exit 3

$ docker run ... claudio-ingest:local --no-api --no-ship
[door] --no-api and --no-ship together leave nothing to serve.          -> exit 8

$ docker run ... claudio-api:local             # over a store with no accounts/
[door] --no-ship and /var/lib/claudio-usage has no accounts/ directory in it.
                                                                       -> exit 9
```

while a **second reader** started beside the writer holding the lock came up
and answered, which is the whole of what the narrowing is for.

### Independence, demonstrated rather than assumed

The split is only worth its four images if the four fail apart. Each of these
was run against the stack above, over TLS through the published port, with the
other services left alone.

| what was done | what was measured |
|---|---|
| **restart `ingest` alone** (10.4 s) while polling `GET /api/v1/search` every 200 ms | **53 of 53 reads `200 ok`.** Not one failed, and the reader was never restarted. Reads do not depend on the writer being up. |
| **restart `api` alone** while POSTing 15 distinct byte ranges to `/sender` | **15 of 15 accepted**, `ok=true accepted=2` each, offsets advancing `2934 → 23489` without a gap. A shipment in flight does not notice the reader restarting. |
| **`docker kill --signal=KILL` the `mcp`** | ship → `200`, read → `200 ok`. `/mcp` → `502`. Brought back with `docker compose up -d mcp`: `tools/list` → 11 tools again, nothing else touched. |
| **a second writer** on the same store, beside the running one | refused: `[door] another door already holds /var/lib/claudio-usage (door.lock)`, **exit 3**, while the real writer kept accepting (`offset=61467`). |
| **a second reader** on the same store, beside the writer holding the lock | came up (`role reader (no shipments, no lock, no writes)`) and answered `outcome=ok` concurrently with the first. |

The last two rows are the narrowing, stated as the pair it is: the lock still
refuses a second **writer** by name, and no longer refuses a **reader** at all.

Each of those behaviours is pinned by a mutation, so none of them is a claim
resting on one run of one stack:

```
$ python server/tests/mutate.py split-
baseline: 3157 passed, 0 failed, 0 skipped

caught  split-lock-never-taken                the WRITER's lock is still taken
caught  split-lock-taken-by-reader            the reader takes NO lock
caught  split-both-halves-off                 --no-api with --no-ship is refused by name
caught  split-reader-blind-root               a reader over a root with no accounts/ refuses
caught  split-reader-acks-a-shipment          a reader refuses POST /v1/ship by name
caught  split-mcp-refusal-not-an-error        a refusal reaches an MCP client as isError
caught  split-one-connection-for-every-thread each thread gets its own DuckDB cursor
caught  split-scope-decided-by-the-token      a scoped token naming no account is refused
caught  split-scope-reaches-the-api           the door hands api.handle the caller's scope
caught  split-meta-accounts-scoped            meta.accounts is narrowed to the scope
caught  split-duckdb-spill-is-absolute        the spill directory is an absolute path
caught  split-duckdb-spill-probed             an unusable spill directory is named at boot
caught  split-reader-writes-nothing           store.py writes nothing outside the probe

13 mutations of 185 (filtered on 'split-'), 13 caught, 0 survived, 0 bad patches
```

The whole matrix, on the same tree: **185 mutations, 185 caught, 0 survived, 0
bad patches.**

The `split-lock-never-taken` row is worth reading in full, because it is the
edit that could lose data: with the lock removed, `serve()` **binds a port
where a refusal was required**, and the row fails on that rather than on a
changed exit code — "the refusal is gone, not merely renumbered".

**One rough edge, recorded rather than smoothed.** With the mcp dead, `/mcp`
answers nginx's stock **HTML** `502` page, not the JSON envelope every other
route on this proxy uses — including the catch-all `404`, which was written
precisely so that "a 404 with no route out of it" could not happen. A JSON
client gets `<html>` where it has been promised an envelope. It is honest and
it is loud, so it is not a silence; it is an inconsistency, and closing it is
an `error_page 502` in `claudio-locations.conf`.

### One DuckDB connection was shared by every request thread

Found by asking two questions at once, which nothing in the suite had ever
done. `DuckStore.con()` created one `duckdb.connect()` and handed **that same
handle** to every caller; the door is a `ThreadingHTTPServer` with
`daemon_threads`, so every request runs in its own thread, and a
`DuckDBPyConnection` holds the pending result of the last `execute` *on the
connection*. Two request threads consumed each other's rows.

Measured against a **completely static** store — nothing appending, no writer
running, so this is not the read-while-append path and the store lock narrowing
is innocent. 8 threads, 480 requests to `/api/v1/search`, twice: once as first
reported, and once re-run here against the **published api image** with the
pre-fix `store.py` bind-mounted over it, through the proxy, over TLS.

| | shared handle (as reported) | shared handle (re-run here) | `cursor()` per thread |
|---|---|---|---|
| HTTP 500 `reader-failed` | 71 | 17 | **0** |
| HTTP 200 `ok` **with the wrong figures** | 30 | 5 | **0** |
| row sets carrying another account's rows | — | 1 | **0** |

The two shared-handle columns differ because the race is a race; they agree on
the only thing that matters, which is that it is not rare. Among the five
wrong answers in the re-run were `matched: 59` and `matched: 303` each served
beside an **empty `rows` list**, and one request for account `f1f70010…`
answered with `f1f7000d…`'s rows.

The 500s are the harmless half. The 200s are the cardinal sin with a status
code on it:

- `matched: 303` served beside an **empty `rows` list**, outcome `ok` — the
  refusal-as-empty-list the whole envelope exists to prevent, delivered through
  the engine instead of through the vocabulary;
- `('alpha', 'expected (303, 303) got (60, 303)')` — one account's request
  answered with **another account's `matched`**;
- partial row sets, `(60, 59)` and `(60, 1)`.

The mechanism is not inferred. The server's own stderr caught a `request_id`
hash arriving where the `scanned` count belongs —
`ValueError: invalid literal for int() with base 10: 'f75165a92c6b44a9'`.

`cursor()` returns a connection over the same in-memory database with a result
slot of its own, so nothing is re-opened and no file is read twice. Isolated:
one connection, 6 threads, 1800 queries gives 6 `None`s and one wrong value;
`cursor()` gives 1800/1800.

The irony is one level up in the same file: `problems` was made thread-local
with the comment *"the door is a `ThreadingHTTPServer`, so two concurrent
requests share this object"*. That reasoning was applied to the list and not to
the connection it describes.

`test_api_two_requests_at_once_do_not_read_each_others_results` pins it, and
pins the two halves **separately** — the 500 count *and* the count of 200s
whose figures disagree with a serial baseline. Guarding only the 500s would
leave the silent wrong answer unguarded, and a future `except Exception: return
no-data` would turn such a test green while making the product strictly worse.
Mutated back to the shared handle: **48 faults, 8 wrong answers, rows crossing
accounts**, three named failures.

### A test that refused by `sys.exit` took the whole suite with it

Found by the mutation matrix rather than by reading it, which is the matrix
earning its place a second time. `oci/pkgindex.py`'s `cmd_modules` refuses with
`sys.exit(<message>)`, and
`test_oci_an_absent_import_is_a_refusal_unless_the_component_declared_it`
called it bare. So the mutation `duck-imports-the-driver` — which makes
`srv/duck.py` import duckdb at import time, *exactly* the condition that
refusal exists for — reported

```
BAD PATCH duck-imports-the-driver -- suite did not run
```

instead of `caught`. The `SystemExit` propagated through the middle of the
suite and the process left without printing a summary.

Two costs, and the second is the serious one. The matrix cannot tell a mutation
that killed the suite from one nothing asserts. And a **real** regression here
would black out every assertion after this point — which is `test.sh`'s
`grep -c` blackout of ~325 tests, in another language, in a file that already
records that lesson.

Every call now goes through a local helper that turns the refusal into an `rc`
and a message, so it fails by name like anything else. The two staleness cases
keep their explicit `try`, because there the `SystemExit` *is* the assertion.
Measured: `BAD PATCH ... suite did not run` before, `caught 3151 passed, 6
failed` after, naming three `oci/deferred` assertions.

The other `BAD PATCH` in that same run — `store-availability-cross-account` —
was **not** a defect: its child process was killed by hand while the matrix was
running. Re-run alone it is `caught`. It is recorded here rather than quietly
dropped, because "two bad patches" in a log with only one real cause is exactly
the sort of number that gets repeated later as evidence.

### DuckDB had nowhere to spill, and `.tmp` is relative to the CWD

`duckdb.connect()` was opened with no `temp_directory` and no `memory_limit`.
DuckDB's default is `.tmp`, **relative to the process's working directory**.
Measured inside the running api container: `Containerfile.linux.api` set no
`WORKDIR`, so cwd is `/`, the image runs as 65534 against a root-owned `/`, and
the store mount is `:ro`. Forcing a spill:

```
QUERY FAILED: IOException  IO Error: Failed to create directory ".tmp": Permission denied
```

On the wire that is `reader-failed` with `detail` reduced to the exception type
— deliberately, since the text goes to stderr — so the operator reads a fault
with no cause, and the cause is a directory name. Every measurement this design
rests on points at the query that triggers it: 45.6 GiB, 62 M rows, `GROUP BY`
over the whole corpus.

`srv/store.py` now names an absolute path (`CLAUDIO_DUCKDB_TEMP_DIR`, default
`<tempdir>/claudio-duckdb`; the api images set `/var/tmp/claudio-duckdb`),
optionally a `CLAUDIO_DUCKDB_TEMP_MAX` and a `CLAUDIO_DUCKDB_MEMORY_LIMIT`, and
**probes it at start-up** by writing and unlinking one byte — `os.access`
answers about permission bits and not about a read-only mount, a full
filesystem or a path that is really a file. Both api images gained a `WORKDIR`
so cwd is never `/` by accident.

It is a **warning, not a refusal to start**: a door that cannot spill still
answers every small question correctly and still serves the reconciler half, so
exiting would turn a degraded reader into no reader at all. Under `--read-only`
give the spill directory a writable mount, e.g.
`--tmpfs /var/tmp/claudio-duckdb:rw,size=2g`.

`CLAUDIO_DUCKDB_TEMP_MAX` is deliberately unset by default rather than guessed:
a cap smaller than a real aggregation needs turns a slow answer into a refused
one, and the image cannot know the store's size.

### The proxy resolves its upstreams once, and restarting a backend can move them

This one was found by running the stack, not by reading it, and it is the
sharpest operational edge in the file.

`claudio.conf` names the three upstreams by compose service name and says, at
length, why the address must **not** be deferred into a variable with a
`resolver`: a deferred lookup turns nginx's loud startup refusal into a steady
silent `502`. That reasoning is correct and stands. What it did not consider is
the *other* direction — nginx resolves each name **once, at configuration
load**, so an address that changes afterwards is never noticed.

Docker reassigns container addresses on restart. Restarting one service alone
reclaims the same address (measured: `ingest` kept `172.28.0.2` across a
restart, `mcp` kept `172.28.0.4`). Restarting **two at once** races them, and
they can swap. Reproduced on the third attempt of `docker compose restart
ingest api`:

```
round 3 before: ingest=172.28.0.2 api=172.28.0.3
round 3 after : ingest=172.28.0.3 api=172.28.0.2      # swapped
```

nginx still held the old pair, so both routes crossed. **And this is the split
paying for itself, in the one situation nobody designed for:**

```
POST /sender          503  {"reason": "no-ship",  "detail": "this process was started with --no-ship ...
                                                   Nothing was written and nothing was acknowledged"}
GET  /api/v1/search   503  {"outcome": "unanswerable", "refusal": {"reason": "no-view",
                                                   "detail": "this door was started with --no-api"}}
```

Two named refusals, nothing written, nothing answered out of the wrong half.
`claudio-locations.conf` predicts exactly this and calls the third case — one
whole-door upstream behind both routes — "the dangerous one", because that
configuration would have survived the same swap **with every request still
working** and the split silently undone. Here the failure is a 503 that names
the flag that caused it.

`nginx -s reload` in the proxy container restores it, and both routes were
confirmed working immediately afterwards (`accepted=2 offset=51470`,
`outcome=ok`).

**That remedy was a human typing a command, and the stack is
`restart: unless-stopped`** — so a crashed or recreated backend reaches the
crossed state with nobody watching. Two things now address it, and neither one
is a configuration change to nginx:

1. **An upstream watcher in `entrypoint-proxy.sh`.** It re-resolves the three
   service names with `getent hosts` every 15s and runs `nginx -s reload` when
   one of them *moves*. The addresses are still resolved once per configuration
   load, exactly as `claudio.conf` requires — this only decides **when** a load
   happens, so that file's argument against a `resolver` stands untouched. It
   is the "tiny sidecar" without the sidecar: no extra container, no
   supervisor.

   It **never reloads on a resolution failure**, only on a resolution that
   succeeded and disagrees with the last one — a backend that is briefly down
   resolves to nothing, and reloading on that would walk nginx into the startup
   refusal the whole design relies on not happening unattended. A failed
   `nginx -s reload` is **not** recorded as seen, so the next tick retries, and
   it says on stderr that the proxy is still pointing at the old addresses. It
   never exits the container either: a watcher that dies leaves the proxy
   working exactly as it did before this existed, so its own failure must not
   be fatal — it says so once, loudly, and stands down.

   `CLAUDIO_PROXY_WATCH=0` turns it off, `CLAUDIO_PROXY_WATCH_SECONDS` changes
   the interval, `CLAUDIO_PROXY_WATCH_NAMES` the names. Off, unavailable (no
   `getent` in the base) and on all print a line at startup naming the manual
   remedy.

   Verified in isolation under **both `dash` and `bash`** with a stubbed
   resolver: a swapped pair is detected as a change, and a name that resolves
   to nothing stands the watcher down instead of reloading. `getent` is present
   in the `nginx:alpine` base (`/usr/bin/getent`) and in FreeBSD base.

   And verified as a **controlled pair on the running stack**, because a
   self-healing claim that has only been watched heal is not one:

   | | `CLAUDIO_PROXY_WATCH=0` | watcher on |
   |---|---|---|
   | addresses swapped on | round 4 of `restart ingest api` | round 1 |
   | `POST /sender` at t+0 | **503** `no-ship` | **503** `no-ship` |
   | `GET /api/v1/health` at t+0 | **503** `no-view` | **503** `no-view` |
   | at t+18s (one tick) | *still 503, indefinitely* | **401** and **200** |
   | proxy log | — | `an upstream address moved -- reloading nginx`, with the before and after triples, then nginx's own `reconfiguring` |

   Note the t+0 column is identical, and that is the split paying for itself:
   the crossed state is two **named refusals**, not two wrong answers.

   **And note what the health check does *not* catch.** Through the crossed
   swap it reported `healthy`, correctly — the backends were answering, nothing
   was 502. It catches an upstream that is *absent*; the watcher is what
   catches one that has *moved*. They are two different faults and it is worth
   knowing which surface sees which.

2. **A health check on the proxy that probes one route per upstream.** It is
   **detection, not repair**, and that has to be said plainly: plain
   `docker compose` does *not* restart an unhealthy container — that is a Swarm
   and Kubernetes behaviour — so an unhealthy proxy here is a red line in
   `docker compose ps`. It asserts that a **backend answered**, not that it
   answered 200: `GET /sender` is a 404 from the ingest door, `GET
   /api/v1/health` a 401 from the api, `GET /mcp` a 405 from the mcp, and all
   three are correct. What is unhealthy is nginx answering *for* them, 502 or
   504. It uses `nc` rather than `wget`, because busybox `wget` exits non-zero
   on any 4xx and so cannot tell a correct 404 from a bad gateway — which is
   the single distinction the check exists to make.

The manual remedy still works and is still worth knowing:

```bash
docker compose -f server/oci/compose.yml exec proxy nginx -s reload
```

`docker compose up -d` on the stack does not do this for you, because the proxy
is not recreated when its dependency is.

### The write boundary, proven from inside the api container

Not inferred from the `:ro` in `compose.yml`. Run **inside the running api**:

```
$ grep claudio-usage /proc/mounts
virtiofs2 /var/lib/claudio-usage virtiofs ro,nosuid,nodev,relatime,...

$ echo hello > /var/lib/claudio-usage/PROOF.txt
sh: 1: cannot create ...: Read-only file system

$ echo x >> .../accounts/<uuid>/ledger.jsonl
sh: 1: cannot create ...: Read-only file system

$ python -c "fcntl.flock(open('.../door.lock','a'), LOCK_EX|LOCK_NB)"
OSError: [Errno 30] Read-only file system: '/var/lib/claudio-usage/door.lock'

$ python -c "open('.../ledger.jsonl','ab')"
OSError: [Errno 30] Read-only file system
```

`EROFS` on all four, **including taking the lock** — so the api could not
become a second writer even if `--no-ship` were removed. That is the outermost
of the three layers; the other two were checked in the same run:

- the process's real argv, read from `/proc/1/cmdline` in the running
  container, ends `--readers /etc/claudio/readers.json --no-ship`;
- `srv/store.py` contains no `write(`, no `os.remove/rename/mkdir/makedirs/
  unlink/replace`, no `shutil`, no `flock`, no `O_CREAT/O_WRONLY/O_APPEND`, and
  no `INSERT`/`CREATE TABLE`/`COPY … TO` **anywhere outside one named
  function**. Its `duckdb.connect()` takes no path, so the engine is in-memory
  and writes no database file either.

  **The one exception is `temp_dir_problem`, and it is named rather than
  quietly allowed.** It writes and unlinks one byte in the DuckDB *spill*
  directory — never in the store, and never under it — so that an unusable
  spill path is a loud warning at boot rather than an unexplained 500 on the
  first large question. That is also why this bullet is no longer a hand-run
  grep: `test_the_reader_writes_nothing_into_the_store` excises that function
  by `ast` span, strips comments and docstrings with `tokenize`, and asserts
  the primitives appear nowhere else — plus that the probe touches
  `self.temp_dir` and neither `self.root` nor `self.paths`. A guard that simply
  allowed `open(` anywhere in the file would have been satisfied by a write
  into `accounts/`. The behavioural half walks the store before and after every
  route and compares sizes, mtimes and directory entries.

### Reading while the writer appends — the existing guards cover the split

The split moves a concurrency that was already happening between two threads
across a process boundary, so what covers it is what covered it before. Both
halves were exercised rather than re-read:

| | |
|---|---|
| **sustained reads during sustained appends** | 10 batches POSTed to `/sender` while `GET /api/v1/search?limit=200` ran continuously: **23 reads, all `outcome=ok`**, row count climbing `40 → 60` monotonically, and **zero** rows with a null `request_id` returned to a client. |
| **a torn final line**, planted the way an interrupted write leaves one (no trailing newline) | DuckDB reads **4** rows and `request_id IS NULL` matches **1**; the API returns **3** and names the fourth: `unparseable-lines`, *"1 line(s) past the last byte any manifest accounts for could not be parsed as JSON. No client is going to resend them: nothing acknowledged them."* Reported on `/api/v1/search` **and** on `/api/v1/accounts`, i.e. on the reader-only routes as well. Removing the fragment returns `store_problems` to `[]`. |

Nothing was added for the split and nothing needed to be. One measurement
worth writing down because it wasted time here: on macOS a host-side append is
not immediately visible inside the container through the VirtioFS bind mount,
so the first read after planting the fragment reported `store_problems: []`
and the next one reported it correctly. That is the same bind-mount coherence
lag this repository already documents in the other direction, and it is a
property of the developer's mount, not of the guard.

### The bind refusal was re-checked, not weakened

```
$ docker run ... -e CLAUDIO_SERVER_ROLE=api claudio-api:local     # no readers.json
[door] refusing to serve the read API on 0.0.0.0 with no reader auth.
[door]   The read API returns every record in the store, email addresses included.
[door]   Write /etc/claudio/readers.json, or start with --no-api.
                                                                  -> exit 4
```

and the same image with `CLAUDIO_SERVER_HOST=127.0.0.1` and still no readers
file starts, with `WARNING the read API asks for no token … Loopback only.`
The check sees `0.0.0.0` inside the compose network and reads it as exposed,
which is correct: every other container on that network can reach it.

### What was not run

The FreeBSD half. All four FreeBSD Containerfiles, their ignore files and their
staging branches exist and are checked by the suite, and **none of the four
images has been built by `buildah` or run**: buildah and podman are not
installable on the machine this was written on, and a Linux kernel cannot
execute FreeBSD binaries.

What *has* been run here, for every component on every ABI in the matrix, is
everything up to `buildah bud` — which is more than the sentence above used to
admit. `stage-freebsd.sh` needs no FreeBSD: it is `curl`, `tar` and Python. So
the signed index, the closure, every blake2b checksum, the unpack, the prune,
the ELF architecture of all 94 files and every `DT_NEEDED` against the real
unpacked `freebsd-runtime` base are **measured**, sixteen stages of them. What
remains unverified is the image layer, the entrypoint and the first `exec`; CI
and the smoke job are where that changes. See
[what has never been executed](#what-has-never-been-executed--read-this-before-you-trust-a-table-below),
which this row belongs to.

**And four things the Linux run above did not establish**, listed so that the
table it sits under is not read as covering them:

- **A real certificate.** Everything was driven over the self-signed pair the
  proxy generates. `CLAUDIO_TLS_DIR` pointing at a real `fullchain.pem` /
  `privkey.pem` is unexercised; only the code path that *skips* generation
  would differ, but that path has not been run.
- **A non-loopback publish.** Both host ports stayed pinned to `127.0.0.1`, so
  the warnings the door prints when it binds an address that faces a network
  were read in the source, not triggered.
- **The proxy-reload rule under an orchestrator.** The upstream re-resolution
  edge above was found and reproduced by hand. What has *not* been tested is a
  rolling deployment where something else decides restart order — which is
  exactly the situation that makes it likely rather than rare.
- **Scale.** Three accounts' worth of fixtures, tens of records. Nothing here
  says anything about the 45.6 GiB figures in the storage section, which were
  measured separately and by a different method.


## The published images

There are **two** container stories in this repository and they are unrelated.
The one above — `server/Dockerfile` and `server/compose.yml` — is a development
image that contains **no source at all**: it is a pinned Python and a pinned
DuckDB, the working tree is bind-mounted, and its job is to stop the suite
self-skipping its storage half. The ones below are what get **published**: they
carry the source, they run the door, and they are meant for a receiving host.

For **Linux and for FreeBSD**, under one set of tags. This section is the Linux
image; [the FreeBSD images](#the-freebsd-images) have a section of their own,
because they are built by a different technique for a reason that is worth
reading. Everything an operator types is the same for both.

**One image per component, and there are four.** See
[The four-way split](#the-four-way-split) below for the whole of the argument;
in short, `claudio-ingest` (POST /v1/ship, the only writer, no DuckDB),
`claudio-api` (GET /api/v1/*, the same source with DuckDB added, takes no store
lock and writes nothing), `claudio-mcp` (POST /mcp, an HTTP *client* of the
api, standard library only) and `claudio-proxy` (nginx, the only published
port). All four are built for both bases. **CI publishes the api half alone
today** — the other three have Containerfiles, staging branches and tests here,
and the workflow's matrix has not been given its component axis yet, which is
said out loud rather than left to be discovered from a missing tag.

They are separate files because merging them would delete the property that
makes the first one worth having. Its `.dockerignore` is a bare `*` and it has
no `COPY`, on purpose — "edit on the host, run in the container, nobody
rebuilds an image to run a test". A published image must carry its source, so
one file doing both would have to hedge, and `docker run` would face two
unrelated jobs with a default that quietly picks one.

```
server/oci/
  compose.yml                             the DEPLOYMENT stack: all four, one published port
  Containerfile.linux.ingest              \
  Containerfile.linux.api                  |  four components, two bases,
  Containerfile.linux.mcp                  |  eight files
  Containerfile.linux.proxy                |
  Containerfile.freebsd.ingest             |  (there is deliberately NO bare
  Containerfile.freebsd.api                |   `Containerfile.<os>`: that name
  Containerfile.freebsd.mcp                |   meant the whole door, and the
  Containerfile.freebsd.proxy             /   whole point is that it is gone)
  Containerfile.linux*.dockerignore       one context filter each -- BuildKit finds
  Containerfile.freebsd*.containerignore  them by name; buildah is passed them
  fetch-pkgs.sh                           FreeBSD: resolve, verify and unpack a package closure
  stage-freebsd.sh                        FreeBSD: what each image contains, and the checks
  pkgindex.py                             FreeBSD: the pkg(8) and ELF knowledge, and four refusals
  entrypoint.sh                           ingest AND api, SHARED by both bases
  entrypoint-mcp.sh                       the mcp's,      SHARED by both bases
  entrypoint-proxy.sh                     the proxy's,    SHARED by both bases
  preflight.py                            ingest AND api, SHARED by both bases
  nginx/                                  the proxy's configuration; three of its five
                                          files are shared by both bases byte for byte
```

`Containerfile.linux.ingest` and `Containerfile.linux.api` build from **the
same source** and differ by one `pip install` and one `ENV`. That is the honest
reason two images exist rather than one image run twice: a package manager is
the thing that differs, and an image whose contents depend on a runtime flag is
an image that cannot be audited.

Both are built and published by `.github/workflows/images.yml`.

### What has never been executed — read this before you trust a table below

Each half of this section ends with a table of what was **run**. This is the
single list of what was **not**, gathered in one place so it cannot be missed
by somebody who reads only one half. Everything here is a gap in evidence, not
a known fault; each is stated because an untested assertion in a README is the
same defect as an untested assertion in code.

| never executed | why not, and what would close it |
|---|---|
| **The FreeBSD image has never been built by `buildah`** | and `buildah` is the tool CI uses. It is absent from the macOS machine this was developed on with no Darwin build to install, `skopeo` too, and `podman` 6.1.0 is present there only as a client with **no machine**. What *was* done, and it is stronger than the previous version of this row admitted: the image **builds with Docker BuildKit on this machine**, `docker build --platform freebsd/amd64`, in 2.9 s — which is the no-`RUN` technique working exactly as designed, since `FROM` and `COPY` need no FreeBSD kernel. See the FreeBSD section for what that build proved and what it still does not. |
| **No FreeBSD binary in this tree has ever run** | Every FreeBSD fact here is read out of ELF headers and package metadata. That is exactly what static analysis establishes and no more. The `smoke` job is what closes it, and it now has one cell per component — the api runs the suite and `import duckdb`, ingest imports `srv.serve` and asserts `store.available()` is False, mcp executes `mcp.py --http` onto its exit-7 refusal, and the proxy runs the staged `nginx`. Scoped to the api, as it was, a completely broken staged tree for the other three passed every static check and published. None of the four has run. |
| **The `freebsd` CI job has never been run at 24 cells** | It stages, builds and checks four components across six base/architecture pairings. All 24 *stages* have been run here, minus `buildah bud`; no cell has ever run as an Actions job. It is clean under `actionlint`, `yamllint` and `yaml.safe_load`, which proves it parses and says nothing about whether it passes. |
| **Nothing has been pushed to the four component repositories** | CI now publishes all four — `claudio-ingest`, `claudio-api`, `claudio-mcp`, `claudio-proxy` — on both operating systems, with a manifest list and an attestation each. None of those repositories exists on ghcr.io yet, so the first run also *creates* them, and a package created by a workflow is **private** by default; see *Publishing the `testing` tag*. `claudio-server` is retired rather than re-pointed: that name means the whole door — ship and read in one process — and no image is that any more, so publishing either half under it would answer 503 `no-ship` (or refuse every read) under an unchanged name. Nothing was ever pushed to it, so no puller is being broken; the rule is written down so that stays true. |
| **The numpy/pandas exclusion is an inference, not a measurement** | Four independent pieces of evidence agree that `srv/` needs neither, and dropping them takes the closure from 1183 MiB to 285 MiB. Four agreeing inferences are still not a run. The smoke job asserts it. |
| **The amd64 Linux image has never run on amd64 silicon** | It was built and exercised on arm64. The amd64 half cannot be validated on this machine at all — QEMU segfaults on `import duckdb`, which is precisely why CI builds one architecture per native runner. |
| **Podman inside a jail** | See that section. Written from documented jail permissions, not from a measurement. |
| **Nothing has ever been pushed to a registry** | For either operating system, from this machine. The tag tables describe what CI is to publish, not what is published. `:latest` in particular **does not exist yet** and is meant not to until a release. |
| **The mixed-OS `:testing` manifest list has never been pulled** | It is correct per the OCI image specification and has been resolved by no runtime here. `:testing-linux` and `:testing-freebsd` exist so this gap can never be the only way to get an image. |
| **No job in `images.yml` has ever run on GitHub Actions** | It is clean under `actionlint 1.7.12` (which runs `shellcheck` over every `run:` block and checks every expression and action reference), under `yamllint`, and under a Python `yaml.safe_load`. That proves it parses and that its shell is sound. It proves nothing about whether the jobs pass. |

What **has** run, on this machine, is the Linux image: built, started, shipped
into with real captured records, read back through the API with a reader token,
refused without one, and read back again after the image itself was deleted and
rebuilt with `--no-cache`. The table at the end of the Linux half is that run.

### Registry and tags

```
ghcr.io/<github.repository_owner>/claudio-ingest
ghcr.io/<github.repository_owner>/claudio-api
ghcr.io/<github.repository_owner>/claudio-mcp
ghcr.io/<github.repository_owner>/claudio-proxy
```

**Four repositories, one tag scheme in each.** The components are four
different programs with four different authorities, and a manifest list keys
its entries on os *and* architecture with no third field — so one repository
carrying all four would make `:testing` a name that cannot be resolved without
also saying which program you meant, and nothing in the pull can say it. Every
tag in the table below exists in each of the four.

One name serves both operating systems. `:testing` is a **mixed-OS manifest
list** — an OCI manifest list keys its entries on os *and* architecture, so
`docker pull :testing` on Linux and `podman pull :testing` on FreeBSD resolve
the same name to different images. `:testing-linux` and `:testing-freebsd`
exist beside it so that the part of this that has *not* been verified against
every runtime is never the only way to get an image.

| tag | when | what it means |
|---|---|---|
| `testing` | every push to the development branch | **the tag that exists today.** Linux and FreeBSD in one list. A moving tag, rebuilt whenever the branch moves. Not a release, and named so that nobody mistakes it for one |
| `testing-linux`, `testing-freebsd` | the same | the same images under a name that states one operating system and cannot be got wrong |
| `testing-freebsd15.1`, `testing-freebsd15.0`, `testing-freebsd14.4` | the same | one FreeBSD base each. `:testing` carries **only** `freebsd15.1`, because nothing in a manifest list distinguishes 15.1 from 14.4 — both are `freebsd/amd64`, and a runtime choosing between two identical platform keys picks whichever came first |
| `sha-<short>` | every build, from any branch | one commit, both operating systems, and **conventionally not overwritten** — see the paragraph below for why that is not the same as immutable. Better than `testing` for a deployment, because `testing` moves under you; a `@sha256:…` digest is better than both |
| `sha-<short>-linux`, `sha-<short>-freebsd15.1`, … | the same | the same, narrowed to one OS or one base |
| `sha-<short>-linux-amd64`, `sha-<short>-freebsd15.1-arm64`, … | the same | the per-architecture images every list above is assembled *from*. Published rather than hidden because the manifest job adds them by remote reference, so they must be reachable — and because when one architecture is wrong, the arch-suffixed tag is the only way to pull the broken half and look at it |
| `<git tag>`, e.g. `v0.2.0` | a push of `refs/tags/v*`, **if the FreeBSD smoke job passed** | a release |
| `latest` | the same | the newest release |

**`sha-<short>` is conventionally not overwritten. It is not immutable, and
this file used to say it was.** A registry tag is mutable by anyone with push
rights, and the workflow re-points these itself: any re-run, `workflow_dispatch`
or scheduled build at the same commit pushes over the same names — and because
both base images and the FreeBSD `latest` package branch float, a re-run need
not produce the same bytes. (*Float*, not *observed to have moved*: a review
claimed `python:3.12-slim-bookworm` moved digest inside eight days and that
could **not** be reproduced — asked three ways on 2026-08-21 it returns the
same digest as the 13 August pull. The pin rests on what a moving tag is, not
on a move anyone watched, and the difference is written down rather than
rounded up.) **A digest is the only thing in a registry that
cannot move.** Every build job prints the digest it pushed, the manifest job
assembles its lists from those digests rather than from the tag names, and it
prints the list digests too. A deployment that must not move under you pins:

```bash
docker pull ghcr.io/<owner>/claudio-api@sha256:<digest>
```

**Provenance is attested, and only where it says so.** Every published image
and three manifest lists **per component** — twelve in all — carry a signed [build
provenance](https://docs.github.com/actions/security-guides/using-artifact-attestations)
attestation naming this workflow, this repository and this commit — so a puller
can tell a legitimate build from anything else a `write:packages` token could
push under the same moving name:

```bash
gh attestation verify oci://ghcr.io/<owner>/claudio-api:testing --owner <owner>
```

What is attested, in each of the four repositories: every
`sha-<short>-<os>-<arch>` image, and the lists behind `:testing` /
`sha-<short>` / a release tag / `:latest`, `:testing-linux`, and the
**default** FreeBSD base. What is **not**: the lists for the non-default
FreeBSD bases (`:testing-freebsd15.0`, `:testing-freebsd14.4`) — their
per-architecture images are attested, the list objects are not. Said here
rather than left to be discovered by a verification that fails.

### Publishing the `testing` tag

Nothing here has ever been published. `.github/workflows/images.yml` has never
executed on GitHub Actions from any branch, so **no image in this repository
has been built on a runner, pushed to a registry, or pulled by anybody** — and
the FreeBSD images have never been built **by `buildah`**, which is the tool
the workflow uses: it is absent from the machine this was written on with no
Darwin build to install, and `podman` there is a client with no machine behind
it. One FreeBSD image *has* been built here with Docker BuildKit, which the
no-`RUN` design makes possible and which is reported in full below — but that
is a different builder producing a different manifest format, and it has never
been pushed or run. The first run of the workflow is the first time any of the
rest happens.

In order, from the development branch. **The workflow has to be committed
before it can run** — GitHub reads `.github/workflows/` from the ref being
pushed, so an uncommitted file builds nothing and reports nothing:

```bash
# 1. commit the image tree and the workflow that publishes it
git add .github/workflows/images.yml server/oci
git add CLAUDE.md server/README.md server/.gitignore server/compose.yml \
        server/srv server/tests
git commit -m "Publish the server as Linux and FreeBSD OCI images"

# 2. squash onto the branch the moving tags are scoped to, and push THAT.
#    `accounts-and-tags` is the private development history and is never
#    pushed; `pre-release` is the published squash, is what DEV_BRANCH in the
#    workflow names, and is the only ref that moves :testing. A build on any
#    other ref publishes its sha- tags and says out loud that it did not.
git checkout pre-release && git merge --squash accounts-and-tags
git commit && git push origin pre-release

# 3. watch it. The four suites run first and nothing publishes from a red
#    tree -- not even a per-commit sha- tag.
gh run watch "$(gh run list --workflow=images.yml --branch=pre-release --limit=1 --json databaseId --jq '.[0].databaseId')"

# 4. pull what it published, on each operating system
for c in ingest api mcp proxy; do
  docker pull "ghcr.io/<owner>/claudio-$c:testing"          # Linux
  podman pull "ghcr.io/<owner>/claudio-$c:testing"          # FreeBSD
done
```

To publish without a push — a re-run on the same commit, which re-points the
moving tags and the `sha-` tags alike:

```bash
gh workflow run images.yml --ref pre-release
```

**The packages are private until you make them public, and there are four of
them now.** GHCR creates each under the owner's namespace inheriting nothing,
so the first pull from another machine needs either a login or the visibility
changed — **per package**, because the setting is the package's and not the
owner's. A stack that pulls three of four and stalls on the fourth is the
likeliest first-run surprise, so it is said here rather than met there:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <owner> --password-stdin
# or, for each of claudio-ingest, claudio-api, claudio-mcp, claudio-proxy:
#   Packages -> <name> -> Package settings -> Change visibility
```

**Two rules the table alone does not make loud enough.**

*The moving tags name one branch.* `.github/workflows/images.yml` has two
branches in its push trigger, and an unscoped `:testing` would mean "whichever
of them built last" — a name nobody can reason about. `DEV_BRANCH` in that file
is the one branch they are published from; a build on any other ref publishes
its `sha-` tags and **says out loud** that it did not move `:testing`. One
consequence follows and is printed rather than left to be discovered: GitHub
runs a `schedule:` on the repository's *default* branch, so while `DEV_BRANCH`
is not the default branch the weekly build stamps `sha-` tags and does not
refresh `:testing`.

*A release is gated on the smoke job, and `:testing` is not.* That job is the
only place anything is **executed on FreeBSD** at all — the engine, the
interpreter and nginx each run there and nowhere else — so without it the
numpy/pandas exclusion and three staged trees are inferences; a release is a
claim `:testing` explicitly does not make. Its four cells are one aggregate
result, so one broken component withholds every component's release name, and
the release loop runs after the gate rather than inside it: four images meant
to be deployed together must not be able to go half-released. But letting one VM job block every publish would mean
that the day it breaks for its own reasons nobody gets an image at all, so the
moving tags stand on the build's own checks and a release does not.

**`latest` and `<git tag>` do not exist today, and that is the intended
behaviour rather than an omission: the repository has no tags at all** — the
same fact `claudio update` has to state when it finds none. `VERSION` in
`claudio` reads `0.1.0`, which is a number in a file and not a release. A
`docker pull …:latest` today therefore fails with a manifest-unknown. That is
the honest answer: `:latest` means *the newest release*, and pointing it at a
development build would hand an operator an unreleased image while telling them
it was the release. An error is recoverable; a plausible wrong image is not.
Until something is tagged, `testing` and `sha-<short>` are the whole of the
scheme. When something is tagged, CI asserts the tag and `claudio`'s own
`VERSION` agree before anything is published — an image answering to a version
it does not report is wrong for ever and silently.

Two names are **deliberately absent**, so nobody adds them back as an
oversight. A bare `<version>` tag taken from `VERSION`: the git tag already
carries it and CI asserts the two agree, and a second spelling of one fact is a
second place for it to be wrong. And a moving *per-architecture* tag such as
`testing-linux-amd64`: a name that moves **and** names one platform answers no
question `sha-<short>-linux-amd64` does not already answer exactly.

### The labels, which are the only thing you can read without running it

Both images carry the **same label keys**, set by the same mechanism — a
`LABEL` block in each Containerfile, every value passed in as a `--build-arg`
by CI. That symmetry is the point: they differ in their base and in nothing an
operator should have to learn twice.

| label | value |
|---|---|
| `org.opencontainers.image.title` | `claudio-ingest`, `claudio-api`, `claudio-mcp` or `claudio-proxy` — the component's own name, which is also its repository |
| `org.opencontainers.image.description` | the door, the core and the read API; which base it is |
| `org.opencontainers.image.source` | derived from `github.repository`, so a **fork** names itself and not this repository |
| `org.opencontainers.image.revision` | the commit. A label on **both**; it used to be a label on one and a manifest annotation on the other, which are different fields with different readers |
| `org.opencontainers.image.version` | `claudio`'s own `VERSION`. The FreeBSD image used to publish the **DuckDB** version here — a third party's number in the field that names this software |
| `org.opencontainers.image.licenses` | `BSD-2-Clause`, the SPDX identifier for the `LICENCE` the image itself carries. It said `MIT` in three places: the reference repository's licence, copied without re-checking, in a field SBOM and compliance scanners read as authoritative |
| `org.opencontainers.image.base.name` | the readable, moving base tag |
| `org.opencontainers.image.base.digest` | the digest `FROM` actually took. CI resolves the base tag to a digest and builds from *that*, so these two cannot disagree |
| `com.claudio.engine` | `duckdb <version> (<where it came from>)` — the PyPI wheel on Linux, the `py312-duckdb` package on FreeBSD |

**There is deliberately no `com.claudio.stream-a` label, and there was one.**
The FreeBSD image carried a hardcoded `stream-a="available"` and the Linux
image carried nothing — so the image whose engine is imported and
version-asserted at build time advertised no capability, while the image that
has never been executed anywhere asserted one. Nothing could falsify it either:
`:testing` publishes even when the FreeBSD smoke job fails, and that job covers
amd64/15.1 alone, so five of the six FreeBSD images carried a claim the
workflow's own comment says is not made. There is no `RUN` in that build to
derive it with, and a directory existing is not a module importing. **Engine
availability is a run-time answer, in one place**: the startup banner,
`GET /api/v1/capabilities`, and `engine.available` in `GET /healthz` — which is
served *before* reader auth precisely so it can be asked without a credential.

### What CI actually does, and what none of it proves

```
test ──┬─→ linux   (amd64 on ubuntu-latest, arm64 on ubuntu-24.04-arm)  ──┐
       └─→ freebsd (6 cells: {15.1, 15.0, 14.4} × {amd64, arm64})  ──┬────┴─→ manifest
                                                                      └─→ smoke
```

**`test` runs first and everything hangs off it**, so a red tree publishes
nothing at all — not even a per-commit `sha-` tag. It runs `make check`, which
is this project's release gate and the one place the suite list is written
down: a suite added to the Makefile is a suite CI runs, rather than a job
holding its own copy of the list to forget one from. `--require-duckdb` is
passed on top of a suite that already exits non-zero on a self-skip, so a run
that skipped **918 assertions** cannot report a pass. The engine is installed at
the version read out of `ARG DUCKDB_VERSION` — and the two Containerfiles are
asserted to pin the *same* version, because nothing else compares them and a
Linux image on one engine beside a FreeBSD image on another is two products
tested as one.

**Every action is pinned to a commit SHA**, with the version in a trailing
comment. A major tag such as `@v5` is a moving reference its publisher
re-points at will, and these jobs hold `packages: write` and a registry token —
so `@v5` is an unreviewed third party with the ability to push an image under
this repository's name. Permissions are declared **per job**: `test` and `smoke`
cannot push an image at all, and there is no workflow-level block granting
every job the union. A test asserts the *shape* — forty hex characters — rather
than a list of the four actions used today, because a list says nothing about
the fifth one somebody adds.

**A weekly build** (Monday 06:00 UTC) exists for the reference repository's
reason: FreeBSD rebuilds every port when a toolchain moves, without the
application version changing, and the DuckDB wheels and the Debian base move on
their own too. Without it the images sit on whatever was current the day
somebody last pushed.

**Unverified, and labelled rather than glossed:** *nothing in
`.github/workflows/images.yml` has ever been run.* It has not executed on GitHub
Actions from any branch. What has been checked locally is that it **parses and
that its shell is sound** — `actionlint 1.7.12`, which runs `shellcheck` over
every `run:` block and validates every expression and action reference; a
`yaml.safe_load`; and mutation of the assertions that guard it. That proves
nothing whatever about whether the jobs pass. The four action pins were
resolved with `git ls-remote --tags` against each repository and are real
commits; that the pinned versions *behave* as the file assumes is checked by
nothing here. The file carries the same list at its own foot, so the two cannot
drift apart unnoticed.

The scheme is the reference repository's
(`gabrielbelli/freebsd-oauth2-proxy-oci`) with one deliberate difference: there,
the per-architecture tag carries the **package version** the FreeBSD repository
resolved for that architecture, because those genuinely differ between
architectures — amd64 held `7.15.3_2` while aarch64 held `7.15.3_1` — and the
multi-architecture version tag is *skipped, by name*, when they disagree. Here
the version that could differ is DuckDB's, and it cannot: it is pinned to one
string in the Containerfile and **asserted at build time inside the built
image**, so a build that resolved anything else fails rather than publishing.
The refusal moved from the tagging job into the build.

### What is in the image, and what is deliberately not

**In:** `server/srv/` — the door, the reconciliation core, the coverage and
attribution modules and the DuckDB query layer — a pinned Python 3.12, DuckDB
pinned to the version the measurements in `srv/duck.py` were taken on, the two
shared files above, and `LICENCE`. Nothing else.

Measured on arm64, and stated as two numbers because one of them alone
misleads: **219 MiB unpacked on disk**, **~68 MB to pull** (compressed). Almost
all of both is the base image and the DuckDB wheel -- the wheel is a 65.6 MB
layer and `server/srv/` is 928 kB of it. Be careful which figure a tool is
giving you: `docker image inspect --format '{{.Size}}'` reports **69124430**
here, which is the *compressed* total and reads as a plausible unpacked size;
`du -sm /` from inside the running container is the honest unpacked one, and
`docker image ls` reports a third figure again on a containerd image store,
because it counts every platform in the index.

**Not in, and this is a decision rather than an omission:** `claudio` itself,
the on-demand OTLP receiver (`usage/recv/`) and the shipper (`usage/cu/`).
Those are the **workstation** half. They are installed with `make install`,
they have no third-party dependency of any kind, and `claudio usage show` works
offline on a plane with no server and no daemon — which is most of why the tool
is pleasant. Putting them in an image would suggest a container is a supported
way to run them. It is not, and the workstation half **is not containerised**.
The `Makefile` refuses to install `server/` for the mirror-image reason.

### The volume layout

The image is **stateless**. Delete it, rebuild it, run it again on the same
store and nothing is lost — verified below by doing exactly that.

| path | what | mount |
|---|---|---|
| `/var/lib/claudio-usage` | the **store**: append-only JSONL, the record of truth | a volume or a host directory, **writable by the container's uid** |
| `/etc/claudio` | `tokens.json` (who may ship) and `readers.json` (who may read) | a host directory, **read-only** |

They are separate on purpose. The two token files are secrets and are not
records; keeping them out of the store means the store stays a directory that
can be tailed, backed up and handed to a log shipper without handing the tokens
over with it. It also means `:ro` is available for exactly the files that want
it.

`/var/lib/claudio-usage` is **not** the client's `usage_dir`. That is the
directory the status-line shim writes samples into on a workstation; this is
the store a door writes on a receiving host, and in every deployment this image
is for they are different machines.

**There is no `VOLUME` instruction, deliberately.** `VOLUME` would make a
forgotten `-v` create an *anonymous* volume: the records would survive
`docker rm` in a directory named by a 64-character hex string that the operator
does not know exists and will never back up. That is worse than losing them,
because it looks like success. The preflight says out loud that nothing is
mounted instead — on every start, every time:

```
[preflight] WARNING the store /var/lib/claudio-usage is the container's own writable layer -- NOTHING IS MOUNTED THERE.
[preflight]   Every record this door accepts and acknowledges as durable is lost when the container is removed.
[preflight]   Mount a volume:  -v <host dir or volume>:/var/lib/claudio-usage
```

It warns and does not refuse, because a throwaway door over a throwaway store
is a legitimate five-minute experiment and refusing it would make the honest
case unreachable. An **unwritable** store *is* refused, exit 5, with the
`chown` to run and the numeric uid to run it with — because the alternative is
`lock_root`'s `os.makedirs` raising `PermissionError` out of the top of
`serve.py`, a traceback whose first line is about `door.lock` for a fault that
is about `-v`.

### Running it

**One container per component; there is no single-image door to pull.** The
whole-door image was split into `claudio-ingest`, `claudio-api`, `claudio-mcp`
and `claudio-proxy`, and `server/oci/compose.yml` is the stack that wires them
together — see [The four-way split](#the-four-way-split). What follows is the
same thing by hand, and it is worth reading once because every flag, mount and
refusal below is the same in the compose file.

```bash
mkdir -p /srv/claudio/store /srv/claudio/conf
chown 65534:65534 /srv/claudio/store
# One line per workstation, from `claudio key new <account>` on that machine.
# The key carries the account it ships for, so there is nothing to look up
# here and no uuid to copy between hosts.
echo '["<paste claudio key new output>"]' > /srv/claudio/conf/tokens.json
echo '{"<read-token>": "*"}'              > /srv/claudio/conf/readers.json

# The writer. It holds the store lock, so there is exactly one of these.
docker run -d --name claudio-ingest \
  -p 127.0.0.1:8787:8787 \
  -v /srv/claudio/store:/var/lib/claudio-usage \
  -v /srv/claudio/conf:/etc/claudio:ro \
  ghcr.io/<owner>/claudio-ingest:testing

# The reader. READ-ONLY on the store, and it takes no lock -- which is what
# lets it run beside the writer at all.
docker run -d --name claudio-api \
  -p 127.0.0.1:8788:8787 \
  -v /srv/claudio/store:/var/lib/claudio-usage:ro \
  -v /srv/claudio/conf:/etc/claudio:ro \
  ghcr.io/<owner>/claudio-api:testing
```

The container runs as **uid 65534, gid 65534** — numeric, and both halves
numeric, because 65534 is `nobody` in both bases but the *group* is `nogroup`
on Debian and `nobody` on FreeBSD, so a name here is a name the other image
cannot use. It also already exists in both bases, which is what lets the
FreeBSD image have a non-root user at all: it cannot create one, because it has
no `RUN`. The door needs no privilege of any kind — it binds one port, appends
to one directory and reads two files.

Configuration is environment, and anything after the image name is appended to
the door's argv verbatim. `argparse` takes the **last** occurrence of a
repeated option, so one flag can be overridden without retyping the rest —
which is why there is no `CMD` carrying the full flag list, since a `CMD` is
replaced wholesale and the flag people forget to retype is `--readers`.

| variable | default | flag it supplies |
|---|---|---|
| `CLAUDIO_SERVER_STORE` | `/var/lib/claudio-usage` | `--root` |
| `CLAUDIO_SERVER_HOST` | `0.0.0.0` | `--host` |
| `CLAUDIO_SERVER_PORT` | `8787` | `--port` |
| `CLAUDIO_SERVER_TOKENS` | `/etc/claudio/tokens.json` | `--tokens` |
| `CLAUDIO_SERVER_READERS` | `/etc/claudio/readers.json` | `--readers` |

Three more are read by `srv/store.py` rather than by the entrypoint, so they
supply no flag and are meaningful only in the **api** image:

| variable | default | what it does |
|---|---|---|
| `CLAUDIO_DUCKDB_TEMP_DIR` | `/var/tmp/claudio-duckdb` in the api images, `<tempdir>/claudio-duckdb` otherwise | where DuckDB spills a large aggregation. Probed at start-up; an unusable path is a named warning, never a 500 later |
| `CLAUDIO_DUCKDB_TEMP_MAX` | *(unset)* | a cap on that directory, so a runaway question cannot fill the host. Unset rather than guessed: a cap smaller than a real aggregation needs turns a slow answer into a refused one |
| `CLAUDIO_DUCKDB_MEMORY_LIMIT` | *(unset — DuckDB's own 80% of what the container sees)* | an explicit limit, for a container whose cgroup DuckDB reads wrongly |

and the proxy image reads three of its own:

| variable | default | what it does |
|---|---|---|
| `CLAUDIO_PROXY_WATCH` | `1` | the upstream watcher; `0` disables it and says so at startup |
| `CLAUDIO_PROXY_WATCH_SECONDS` | `15` | how often it re-resolves |
| `CLAUDIO_PROXY_WATCH_NAMES` | `ingest api mcp` | which names it watches |

```bash
docker run ... ghcr.io/<owner>/claudio-ingest:testing --port 9000   # overrides only the port
docker run ... ghcr.io/<owner>/claudio-ingest:testing sh            # the escape hatch
```

`CLAUDIO_SERVER_ROLE` is set **in the image** — `ingest` in one, `api` in the
other — and the entrypoint turns it into `--no-api` or `--no-ship`. It is not a
knob to set at run time: an unrecognised value is refused by name (exit 11), and
the two images exist so that which half you are running is a fact about the
image rather than about an environment variable somebody can mistype.

### The first thing it does is refuse, and that is the feature

With no `readers.json` mounted, the container **exits 4** and says why:

```
[door] refusing to serve the read API on 0.0.0.0 with no reader auth.
[door]   The read API returns every record in the store, email addresses included.
[door]   Write /etc/claudio/readers.json, or start with --no-api.
```

That is not a crash loop to work around. Inside a container the bind *is*
non-loopback — the port mapping is what decides who can reach it, and binding
loopback inside would make the port answer nothing, which reads as a broken
door rather than a safe one. So the container meets the door's one hard
refusal by construction, and the exposure and the authentication are decided
together or not at all.

**The entrypoint must never paper over this**, and a test asserts it: its code
may not contain `--no-api`, `--no-ui` or `127.0.0.1`. Adding either would turn
a refusal that names its own remedy into a mystery, or into a running door
serving every record in the store to anyone who can reach the port.

`--no-api` is what a door that only has to accept shipments does, and it is
**already what `claudio-ingest` runs** — the entrypoint adds it from
`CLAUDIO_SERVER_ROLE=ingest`, so that image needs no `readers.json` and cannot
meet this refusal. The refusal above is the **api** image's, and it is the one
an operator meets first:

```bash
docker run ... ghcr.io/<owner>/claudio-ingest:testing   # already --no-api
```

| exit | who | meaning |
|---|---|---|
| 0 | the door | interrupted |
| 1 | the door | cannot bind the address |
| 3 | the door | another door already holds this store (`door.lock`) |
| 4 | the door | refusing an unauthenticated read API on a non-loopback bind |
| 5 | the preflight | the store is unusable — not creatable, or not writable |
| 6 | the entrypoint | no Python in the image; the image is broken, not your configuration |

### Health checks

**There is no `HEALTHCHECK` instruction and its absence would otherwise be
invisible.** The OCI image spec has no field for one, so a `--format oci` build
drops it and the published config would simply not contain it — while a
Docker-format build of the identical file keeps it, so it appears to work
locally and vanishes on publish. This repository ships two images and one of
them is built with buildah under `--format oci`; a health check that exists in
one and not the other is a health check nothing may rely on.

Pass one at run time instead. `/healthz` is unversioned, unenveloped and served
**before** reader auth — `serve.py` answers it above the token check on
purpose, because operators and scripts read that one — so it needs no
credential. There is no `curl` in the base and there is not going to be, so use
the Python that is already there:

```bash
docker run --health-cmd \
  "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=5).status==200 else 1)\"" \
  --health-interval 30s --health-retries 3 ...
```

**A 200 from `/healthz` is not a statement that stream A can be queried.** It
means the door is up and taking shipments. Whether the DuckDB half answers is
`engine.available` in that same payload, and it is there precisely because
`/healthz` is the only surface served *before* reader auth: `/api/v1/health`
and `/api/v1/capabilities` name the engine too and both need a token, so
without it monitoring run without a reader credential could not detect a
half-functional door at all. That matters most on the FreeBSD image, whose
engine has never been executed anywhere outside one amd64 smoke job — see
*What has never been executed* above. To gate on it as well:

```bash
docker run --health-cmd \
  "python -c \"import json,urllib.request,sys; d=json.load(urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=5)); sys.exit(0 if d['engine']['available'] else 1)\"" \
  --health-interval 30s --health-retries 3 ...
```

### Multi-architecture, and why it cannot be one emulated build

`linux/amd64` and `linux/arm64`. The DuckDB wheels for both exist — 1.5.5
publishes 35 files on PyPI and the CPython 3.12 Linux pair is
`manylinux_2_26/2_28` for `x86_64` and `aarch64` — so each build is a download
and not a compile. There is **no `musllinux` wheel**, which is why the base is
Debian and not Alpine: on musl `pip` would fall back to the sdist and compile
the C++ engine.

**But the two architectures must be built on native runners, one each, and that
is measured rather than assumed.** On an arm64 host with Docker 29.6.2 and
BuildKit v0.31.2, `docker buildx build --platform linux/amd64,linux/arm64`
installs the amd64 wheel perfectly and then dies on the build's own version
assertion: `qemu: uncaught target signal 11 (Segmentation fault)`, exit 139. It
is DuckDB specifically and not a broken emulator — under the identical
`--platform linux/amd64` container, CPython, `ssl` and `hashlib` all run, and
only `import duckdb` faults. Whether that same wheel imports on real amd64
*silicon* has not been tested here and is **unverified on this machine**; it is
the ordinary case for a published manylinux wheel, and it is also beside the
point, because the conclusion does not depend on it.

So: `ubuntu-latest` for amd64, `ubuntu-24.04-arm` for arm64, then a manifest
list assembled from the two per-architecture tags — the same two-job shape the
reference repository uses, for a closely related reason. The obvious wrong
response is to delete the import assertion so an emulated build goes green;
that would publish an amd64 image whose engine has never once been imported,
which is this project's cardinal sin with a matrix cell to hide in.

### The context filter lives beside the Containerfile, not at the root

BuildKit looks for `<dockerfile>.dockerignore` beside the Dockerfile before it
looks for `.dockerignore` at the context root, and that is what
`Containerfile.linux.api.dockerignore` is. The context is the repository root,
because the image needs `server/srv/` and `server/oci/` and neither is a parent
of the other — but a root `.dockerignore` is refused by a boundary test in
`usage/tests/test_all.py` which walks the root for container files by name, on
the grounds that a root-level container file "reads as the way to run the
project". That guard is right, so the answer was to not put one there rather
than to widen it. The sidecar also scopes the rules to one image, which
matters because the two published images stage genuinely different things and a
shared list would have to be the union of both — a list that describes neither.

### Verified

Docker 29.6.2, BuildKit v0.31.2, Darwin arm64. Everything in this table was
run; the two things that were not are named as unverified underneath it.

| verified | how |
|---|---|
| the image builds, and the pinned engine is the one that ends up importable | `docker build -f server/oci/Containerfile.linux.api .` → 7.5 s; `duckdb 1.5.5` reported from inside, matching `ARG DUCKDB_VERSION` |
| the image contains the server and nothing else | `ls /opt/claudio-server` → `LICENCE entrypoint.sh preflight.py srv`; no `claudio`, no `usage/`, no tests. **219 MiB** unpacked (`du -sm /` inside), **~68 MB** compressed (`docker save \| gzip`) |
| it runs as a non-root user | `id` inside → `uid=65534(nobody) gid=65534(nogroup)` |
| the published config declares **no** health check | `docker image inspect --format '{{.Config.Healthcheck}}'` → empty |
| the default invocation refuses rather than exposing an unauthenticated API | `docker run` with no `readers.json` → the three-line refusal, **exit 4** |
| the ephemeral-store warning fires when nothing is mounted | same run, three `[preflight] WARNING` lines above the refusal |
| and does **not** fire for a bind mount or a named volume | both run clean; the named-volume case matters because Docker seeds a fresh volume from the image directory |
| an unwritable store is refused by name, not by traceback | `chmod 500` on the host directory → **exit 5**, naming `chown 65534:65534` and `--user` |
| the door starts, ships and reads through the published mapping | `POST /v1/ship` with a manifest → `200 {"accepted": 1, ...}`; `/healthz` 200 |
| reader auth is enforced through the image | `/api/v1/accounts` → **401** with no token, **200** with the mounted one |
| **the store survives the image being deleted** | shipped a record, `docker rm -f` **and `docker rmi`**, rebuilt from scratch, restarted on the same host directory → the record read back through `/api/v1/search` (DuckDB), intact |
| a single flag can be overridden without retyping the rest | `docker run ... --port 8790` → `[door] listening on http://0.0.0.0:8790/` |
| the image runs under a read-only root filesystem | `docker run --read-only` with the two mounts → the door starts and serves |
| the escape hatch works | `docker run ... sh -c '...'` → ran, `duckdb 1.5.5` |
| the sidecar `.dockerignore` is honoured, with a negative control | probe `COPY test.sh`: **fails** with the file present, **succeeds** with it removed — so the exclusion is the ignore file's doing and not an accident |
| the entrypoint is POSIX `sh` | `sh -n` **and** `dash -n` |
| the shared preflight parses on Python 3.9, the floor the rest of `server/` holds | `ast.parse(..., feature_version=(3, 9))` |
| every claim above about the image *files* | pinned by the `test_oci_*` guards in `server/tests/test_all.py`, which run against **every** `Containerfile.*` in `server/oci/` rather than against a named list |

Re-verified independently afterwards, on a clean rebuild rather than on the
build that wrote the table above — the point of a second pass being that it can
disagree, and on one row it did:

| re-verified | how |
|---|---|
| the whole path, end to end, from a `--no-cache` rebuild | build → run → `POST /v1/ship` with real captured stream-A **and** stream-B batches → `200 {"accepted": 3}` and `200 {"accepted": 10}` → `/api/v1/search` returns the three requests with their models and token counts |
| **the store outlives the image**, checked harder than before | `docker rm -f` **and** `docker rmi -f`, then `docker build --no-cache`, then restart on the same host directory → all three ledger rows read back through DuckDB, `/api/v1/accounts` names the same account uuid |
| reader auth, both directions and a wrong token | `/api/v1/accounts` and `/api/v1/search`: **401** `unauthorised` with no token **and** with a token not in `readers.json`; **200** `ok` with the mounted one. `/healthz` answers **200** with no token, as it is meant to |
| the door refuses an unauthenticated shipment | `POST /v1/ship` with no `Authorization` → **401** `no-bearer-token` |
| the named refusals are named, not empty | `/api/v1/windows` with no `?account=` → **400** `no-account` listing the account uuids it holds; `/api/v1/aggregate` with no `?by=` → **400** `no-group-by` naming `/api/v1/fields`. Neither carries a `result` key |
| the coverage tri-state survives onto the wire, live | `/api/v1/windows?account=…` → `"fraction": null, "known": false, "basis_is": "unknown"`, never `0.0` |
| **the DuckDB-absent path, through the published image itself** | `duckdb` made unimportable inside the running container: the banner prints `engine duckdb MISSING -- stream A routes will refuse by name`, all four stream-A routes answer **503** `duckdb-missing` with a remedy and **no `result` key**, and the door, stream B, the reconciler and `/api/v1/accounts` keep working — the same batches ship and `/api/v1/accounts` still finds the account. This is the evidence the FreeBSD half needs and could only get on Linux |
| every URL the FreeBSD build reaches actually resolves | `HEAD` on `packagesite.pkg` for all four ABIs → **200**; `docker manifest inspect` on `freebsd/freebsd-runtime:{15.1,15.0,14.4}` → each carries **both** `freebsd/amd64` and `freebsd/arm64` |

A third pass, on the rebuilt image after the provenance and engine-reporting
work — because the Containerfile changed materially and a table describing an
older build is a table describing a different image:

| re-verified again | how |
|---|---|
| the **hash-pinned** engine install works, and the pin covers this architecture | `docker build` → `Downloading duckdb-1.5.5-cp312-cp312-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl`, `Successfully installed duckdb-1.5.5` under `--require-hashes --no-deps`. A wrong or missing hash is a hard pip failure, so the build succeeding *is* the check |
| every provenance label lands with the value CI passes | `docker image inspect --format '{{json .Config.Labels}}'` → `licenses: BSD-2-Clause`, `version: 0.1.0` (claudio's own, not DuckDB's), `revision`, `source`, `base.name`, `base.digest`, `com.claudio.engine: duckdb 1.5.5 (PyPI manylinux wheel)`, and **no** `com.claudio.stream-a` |
| **`/healthz` names the engine, with no token** | live container: `GET /healthz` → `"engine": {"available": true, "name": "duckdb"}`, while `GET /api/v1/health` and `GET /api/v1/capabilities` both answer **401**. With `duckdb` made unimportable inside the running container and the door restarted, the same unauthenticated `/healthz` reports `"available": false` — which is the detection that was impossible before, because the two surfaces naming the engine both need a reader token |
| **`GET /api/v1/window` no longer drops the connection** | shipped the captured account that carries both a tagged and an untagged request, then `GET /api/v1/window?...&kind=5h&resets_at=…` → **HTTP 200** with `by_tag` as `[{value, value_is_null, stats}]`, the null bucket flagged and last. The same request previously returned **HTTP 000** — `json.dumps(sort_keys=True)` cannot order `str` against `None`, so the handler raised and the client got no response at all |
| **the window's coverage is refused rather than published as zeros** | same container with the engine hidden: `result.coverage` carries only `unavailable` and `note` — no `placement`, no `windows`, no `fraction` — while `GET /api/v1/windows` for the same account at the same moment reports the true `{"rows_total": 3, "in_closed_windows": 3, "placed_fraction": 1.0}` from the reconciler's own rows. Previously the singular route published a census of zeros about rows it had just said it could not read |
| the package index's signature, against the live repository | `pkgindex.py verifyindex` over `FreeBSD:15:amd64/latest`, fetched 2026-08-21: `sha256(packagesite.yaml.pub)` = `b0170035…f438`, byte for byte the fingerprint in FreeBSD's `/usr/share/keys/pkg/trusted/pkg.freebsd.org.2013102301`; RSA-2048, e=65537, PKCS#1 v1.5 SHA-256 verifies. A truncated index, a substituted key and a missing `.sig` each exit **1** by name. pkg signs the ASCII **hex string** of the file's sha256, not the digest — both were tried and only the double hash matches |
| the four action pins in the workflow are real commits, and so is the new one | `git ls-remote --tags` against each repository, 2026-08-21: `checkout@v5.1.0`, `download-artifact@v4.3.0`, `upload-artifact@v4.6.2`, `freebsd-vm@v1.5.3` and `attest-build-provenance@v4.2.2` all resolve to the pinned SHA |
| the shell is clean, not merely valid | `sh -n`, `dash -n`, `bash -n` **and** `shellcheck -s sh` with **no output** on all three scripts |
| the workflow's own claim about itself | `actionlint 1.7.12` re-run here → clean, reproducing what its footer asserts |
| all four suites | `bash test.sh` **1228/1228**; `dash test.sh` **1228/1228**; `usage` **521/521**; `server` **2448 passed, 0 failed, 0 skipped** — and **1516 passed, 0 failed, 45 skipped** with the engine blocked, which refuses to exit 0 without `--allow-skips` |
| **and one row was wrong** | the size. See above: `69 MB` was `docker image inspect --format '{{.Size}}'`, which is the **compressed** total. Unpacked it is **219 MiB**. Corrected in both places it appeared |

**Unverified, and labelled rather than glossed:**

- **The amd64 image has never been run on amd64 silicon.** It was built and
  exercised on arm64; the amd64 half of a multi-architecture build cannot be
  validated here at all, for the QEMU reason above. CI on a native amd64 runner
  is what will establish it, and the build's own version assertion is what will
  fail loudly if it does not.
- **Nothing here has been pushed to a registry.** The tag table describes what
  CI is to publish; no `ghcr.io` push has been performed from this machine. It
  follows that **no attestation has ever been generated or verified** — the
  workflow requests `id-token: write` and calls `actions/attest-build-provenance`
  on every digest it pushes, and that whole path has run nowhere. The
  `gh attestation verify` command above is what it is *for*, not something this
  machine has watched succeed.
- **Nothing published resolves its base by digest here.** The build above was
  given `BASE_DIGEST=sha256:testdigest` by hand to prove the label plumbing;
  the `docker pull` → `RepoDigests` resolution that produces the real value
  runs only in CI.

## The FreeBSD images

**Both operating systems, from one name.** The same server is published as
Linux images and as FreeBSD images, and `:testing` is a manifest list carrying
`linux/amd64`, `linux/arm64`, `freebsd/amd64` and `freebsd/arm64` — so
`docker pull` on Linux and `podman pull` on FreeBSD get the right one without
either operator choosing.

They exist because the deployment this door is for is a jail or a container on
a FreeBSD host, and an operator who runs FreeBSD should not have to run Linux
to accept shipments from their own workstations. Each component's two images
are meant to be **interchangeable**: same entrypoint, same environment
variables, same mount points, same uid, same exit codes, same refusals. A test
pairs the Containerfiles **by component**, derived from their file names, and
compares each pair against the other rather than against a written-down
expectation, so they cannot drift apart quietly.

### One image per component, on FreeBSD too

There are four, and the split is a boundary rather than tidiness.

| image | what is in it | packages | installed | download | staged after prune |
|---|---|---|---|---|---|
| `claudio-api` | the read API and the reconciler — CPython **and** DuckDB | 8 | 285.4 MiB | 52.2 MiB | **120.7 MiB** |
| `claudio-ingest` | `POST /v1/ship`, the only writer — CPython alone, **no** DuckDB | 7 | 225.6 MiB | 37.8 MiB | **60.5 MiB** |
| `claudio-mcp` | the MCP surface: `srv/mcp.py` and CPython, **no engine** | 7 | 225.6 MiB | 37.8 MiB | **60.5 MiB** |
| `claudio-proxy` | nginx, the only thing published | 2 | 9.7 MiB | 2.0 MiB | **6.4 MiB** |

Measured on **2026-08-25** against the real signed `FreeBSD:15:amd64` index and
the real `freebsd-runtime:15.1` layer, by running `stage-freebsd.sh` end to end
for each component on this machine.

**Quote the staged column, not the installed one.** `flatsize` is what `pkg`
would install; the image gets what survives the prune, and for CPython that is
a factor of four — `lib/python3.12/test` is 132.3 MiB and `config-3.12` a
further 35.7 MiB, and neither reaches an image with no compiler in it. The
ingest tree is 243.7 MiB before the prune and 60.5 MiB after. A README quoting
226 MiB for an image that contributes 60 is not wrong so much as measuring the
wrong end of the pipe, which this project has already done once with a row
size.

**`claudio-ingest` and `claudio-mcp` are byte-identical trees.** Same closure,
same prune list, same 60.5 MiB — they differ only in what is COPYed on top
(`server/srv/` entire, against `srv/mcp.py` alone) and in what the entrypoint
runs. The engine is the whole of the difference from the api: `py312-duckdb` is
59.9 MiB installed, and 120.7 − 60.5 = 60.2.

**All four, on all four ABIs**, staged and checked here — the closure resolved
from each signed index, every blake2b checksum verified, unpacked, pruned, and
the architecture of every ELF file and every `DT_NEEDED` checked against the
real unpacked base:

| ABI | ingest / mcp | api | proxy |
|---|---|---|---|
| `FreeBSD:15:amd64` | 60.5 MiB | 120.7 MiB | 6.4 MiB |
| `FreeBSD:15:aarch64` | 60.3 MiB | 117.9 MiB | 6.3 MiB |
| `FreeBSD:14:amd64` | 60.0 MiB | 120.3 MiB | 6.4 MiB |
| `FreeBSD:14:aarch64` | 59.8 MiB | 117.2 MiB | 6.3 MiB |

`py312-duckdb` is **1.5.5** in all four, which is the version `ARG
DUCKDB_VERSION` pins and the version `srv/duck.py`'s five refusals were
measured on.

**The base is 12 MiB, and that changes which argument the split rests on.**
`freebsd/freebsd-runtime` is 12.0 MiB compressed / 34 MiB unpacked on
15.1/amd64, against Debian bookworm's 108 MB under the Linux images. So on
Linux the ingest and mcp images are ~76% base image and the split is an
authority win with a modest size dividend; on FreeBSD the same split is
**60.5 MiB against 120.7** on a 12 MiB base, and the dividend is half the
image. The authority argument is the same one either way and is the reason to
do it; the FreeBSD numbers are simply the ones that also look like a reason.

**The `duckdb` import is deferred, and the check now knows the difference.**
`import duckdb` appears once in the whole package, inside `store.require()`, so
the ingest and mcp images can omit it — `store.require()` raises `DuckDBMissing`
naming the install command, and `serve.py` prints `engine duckdb MISSING —
stream A routes will refuse by name`. `pkgindex.py modules` used to walk the
tree with `ast.walk`, which descends into function bodies, so it demanded
`duckdb` of every component checked against `srv/` and **`stage-freebsd.sh
ingest` failed its own check on every run**. It now separates an import
executed at import time from one inside a function body, and the ingest branch
declares the deferred one with `--deferred-ok duckdb`. Declaring it is what
makes the check bite: move that import to module level and the flag refuses by
name, because at that point the ingest process really would need the package
before it could serve anything.

**The mcp image is exactly the door's tree minus `py312-duckdb`**, and that one
package is the whole difference: 8 packages become 7 and 121 MiB becomes 60.
The size is the weaker half of the argument. The strong half is that
`srv/mcp.py` is an **HTTP client of the read API** — it holds a reader token,
opens no file in the store and imports no sibling module — so an image with a
database handle in it would be a privilege escalation, and one without cannot
be talked into becoming one by a later edit.

That is enforced by the **context**, not by the COPY list. Each Containerfile
has its own `--ignorefile`, and the mcp image's re-includes exactly
`server/srv/mcp.py`: `store.py`, `duck.py`, `query.py`, `api.py` and `serve.py`
are not in its build context at any path, so a `COPY server/srv/` added there
fails the build by name instead of quietly producing a fatter image with an
engine in it. A test asserts both halves — the code and the ignore file — and a
mutation of either is caught.

Two smaller consequences, each with its own reason written where it happens:

- **The mcp image runs the module as a script, not as `python -m srv.mcp`.**
  `-m` imports `srv/__init__.py` first, which imports six modules of the
  reconciler; they are stdlib-only, so no DuckDB, but they are six modules in
  an image whose stated contents are "the MCP surface and nothing else", and
  `reconcile` re-exports from `ingest`, so the set only grows.
- **The mcp entrypoint refuses to serve HTTP with `CLAUDIO_API_TOKEN` in the
  environment**, exit 7. Over HTTP every caller presents their own bearer token
  and the door applies that reader's scope; an ambient token would be inherited
  by a caller who sent none, so a served MCP would see more than the person
  calling it. The stdio transport keeps the variable, because there the
  launcher *is* the caller.

```
server/oci/
  Containerfile.freebsd.ingest                  the INGEST image (no DuckDB)
  Containerfile.freebsd.api                     the API image (the same source + DuckDB)
  Containerfile.freebsd.mcp                     the MCP image
  Containerfile.freebsd.proxy                   the nginx front end
  Containerfile.freebsd*.containerignore        one context filter each, passed with --ignorefile
  fetch-pkgs.sh                                 resolve a package closure, verify it, unpack it
  stage-freebsd.sh                              what each image contains, and the checks
  pkgindex.py                                   the pkg(8) and ELF knowledge, and four refusals
  entrypoint.sh                                 the door's; SHARED with the Linux door, byte for byte
  entrypoint-mcp.sh                             the mcp's;  SHARED with the Linux mcp, byte for byte
  preflight.py                                  SHARED with the Linux door, byte for byte
  nginx/claudio.conf                            the server block; SHARED with the Linux proxy
  nginx/claudio-locations.conf                  the ROUTES;      SHARED with the Linux proxy
  nginx/nginx.freebsd.conf                      the base-specific main configuration
  nginx/claudio-tls.conf                        the TLS server, SHIPPED -- the entrypoint
                                                guarantees a certificate exists first
```

**The staged trees are four directories, never one.** `stage-freebsd.sh` takes
the component as its **first argument**, with no default, and refuses an
unknown one by name listing the four — because the wrong pick is not a build
failure, it is an mcp image carrying DuckDB. Staging is destructive (`rm -rf`
on its stage directory), so a shared default would mean one build silently
consuming another's tree. A test runs the refusal and asserts nothing was
staged.

### The proxy, and why there is a FreeBSD one at all

The question was asked with numbers rather than assumed. `nginx` on
`FreeBSD:15:amd64` closes over **`pcre2` and nothing else**: 2 packages, 9.7 MiB
installed, 2.0 MiB to download, 6.4 MiB staged — and the same two packages on
all four ABIs, 8.9 MiB installed on aarch64.

Every other library `usr/local/sbin/nginx` asks for — `libssl.so.35`,
`libcrypto.so.35`, `libz.so.6`, `libcrypt.so.5`, `libthr.so.3`, `libc.so.7` —
is already in the `freebsd-runtime` base. That used to be read off the layer
and asserted; it is now **checked**. `pkgindex.py shlibs` was run against the
real unpacked base for **all three bases on both architectures**: 6 ELF files
staged, every `DT_NEEDED` satisfied, six rows for six. The same check refuses
the mismatch it exists for, in both directions — a `FreeBSD:14` tree against a
15.1 base names `libutil.so.9` and a `FreeBSD:15` tree against a 14.4 base names
`libutil.so.10`, which is the repository's own worked example, reproduced.

So the arithmetic for the alternative, and **both ends measured the same
way**: the FreeBSD proxy is 6.4 MiB staged on a base that is 34 MiB unpacked
(12.0 MiB compressed), so **~40 MiB uncompressed**, against `nginx:alpine` at
**93.6 MB** and the Linux proxy image built from it at **94.2 MB** — both read
from `docker images` on this machine, which reports uncompressed. Quoting the
12.0 MiB compressed figure against 93.6 MB would have been the same
wrong-end-of-the-pipe mistake this section warns about two paragraphs up, so it
is not quoted that way. Had the answer been "nginx drags in a compiler",
the honest report would have been that a FreeBSD host should run the Linux
proxy or its own nginx outside the stack. It is not — it is two packages and no
compiler anywhere near it — so this image exists, and a FreeBSD operator never
has to run a Linux container to terminate TLS in front of a FreeBSD one.

Everything a `RUN` would have done for it is done by `stage-freebsd.sh` on the
build host, and each is named where it happens:

| what a `RUN` would have done | what happens instead |
|---|---|
| `mkdir` + `chown` the temp and log directories | nginx's paths are **compiled in** (`/var/tmp/nginx/*_temp`, `/var/log/nginx`, read out of the real binary, not guessed). The package ships them root-owned and this image runs as 65534, so staging moves them out of the rootfs and `COPY --chown=65534:65534` carries the ownership |
| pkg's post-install `mime.types` copy | the staging step copies `mime.types-dist` on the build host, and **refuses** rather than warning if it is absent, because the configuration `include`s it and nginx would exit at startup |
| `chown /var/run` for the pid file | `pid /var/tmp/nginx/nginx.pid;` — `/var/run` belongs to the base image and is not this build's to chown |

Two consequences an operator meets immediately. The proxy listens on **8080
and 8443**, because a process that is not root cannot bind below 1024 and
running it as root would trade the one privilege this stack does not need for a
number the runtime maps for free: `ports: ["443:8443", "80:8080"]`. And the
configuration sets **no `user` directive** and **no `daemon off;`** — the first
because nginx warns on every start when the master is not root, and a warning
nobody can act on trains people to ignore warnings; the second because the
ENTRYPOINT passes `-g "daemon off;"` and nginx refuses to start on a duplicate
directive.

The routes and what the prefix is for are the proxy's own section further up;
what matters here is that **two of the three configuration files are shared
with the Linux proxy image byte for byte**. The routes are one decision, and a
second copy is the one that goes stale — here, the stale one would be the one
facing the internet.

### There is no `RUN` instruction, and everything else follows from it

`RUN` executes a binary inside the image, and **a Linux kernel cannot execute
FreeBSD binaries** — qemu-user crosses architectures, not operating systems.
`FROM` and `COPY` execute nothing, so with every package staged beforehand the
six FreeBSD images build on ordinary `ubuntu-latest` runners, in seconds, with
no FreeBSD machine anywhere in CI. The technique is
`gabrielbelli/freebsd-oauth2-proxy-oci`'s and this repository took it wholesale.

What it costs is every instruction that runs something: no `mkdir`, no `chown`,
no `useradd`, no `ldconfig`, no `compileall`, no `pip`. Each is answered by
something that executes nothing, and the Containerfile says which instruction
each answer replaces.

| what a `RUN` would have done | what happens instead |
|---|---|
| `mkdir` + `chown` the store | the directories are staged on the runner and `COPY --chown=65534:65534` carries the ownership |
| `useradd` | uid 65534 already exists in the base — verified in the real `/etc/passwd` of `freebsd-runtime:14.4` and `:15.1` |
| `ldconfig` | `ENV LD_LIBRARY_PATH=/usr/local/lib` — see below, this one is load-bearing |
| `compileall` | `ENV PYTHONDONTWRITEBYTECODE=1`; the stdlib's own `.pyc` files come from the package, which the ports tree builds |
| `pip install duckdb` | `pkg`, staged — and PyPI has **no FreeBSD wheel**, so `pip` there means compiling the C++ engine from an sdist |

### `LD_LIBRARY_PATH` is not decoration, and it is the easiest line to delete

FreeBSD's runtime linker searches `DT_RPATH`/`DT_RUNPATH`, then
`LD_LIBRARY_PATH`, then the hints file `/var/run/ld-elf.so.hints`, then
`/lib:/usr/lib`. **`/usr/local/lib` is reached only through the hints file**,
which `ldconfig` writes at boot — and this image never boots and has no `RUN`
to invoke `ldconfig` with.

Read off the real published artefacts rather than assumed:

- `freebsd/freebsd-runtime:15.1` and `:14.4` ship **no**
  `/var/run/ld-elf.so.hints`;
- `usr/local/bin/python3.12` from the FreeBSD package has an **empty**
  `DT_RUNPATH` and needs `libpython3.12.so.1.0` and `libintl.so.8`, both of
  which live in `/usr/local/lib`.

So without that one `ENV` the image fails at the first `exec` with
`ld-elf.so.1: Shared object "libpython3.12.so.1.0" not found` — after building,
pushing and pulling perfectly. A test pins it and carries the measurement.

### DuckDB **is** packaged for FreeBSD, so the image is not degraded

`databases/py-duckdb` publishes `py312-duckdb` **1.5.5** — the exact version
`srv/duck.py`'s five refusals were measured on — for `FreeBSD:14:amd64`,
`FreeBSD:14:aarch64`, `FreeBSD:15:amd64` and `FreeBSD:15:aarch64`. Read from
the real `packagesite.pkg` index of each repository, not from a mirror or a
web page. So the FreeBSD image serves **stream A as well**, and there is no
second, lesser variant to choose between.

The pin works in the opposite direction from the Linux image's, and that is the
one real difference between them. `pip install duckdb==1.5.5` *asks* for a
version and the Linux build then proves what it got by importing it. `pkg` has
one version of a package and no way to ask for another, so the FreeBSD build
cannot request it — it can only **refuse** the one it was given. CI compares
the version `stage-freebsd.sh` resolved against `ARG DUCKDB_VERSION` and fails
when they differ, so a ports update to 1.5.6 turns the build red rather than
publishing an image whose engine nobody has measured.

**If it were ever unavailable, the degradation is already correct and it was
verified rather than assumed.** With no engine, `store.require()` raises
`DuckDBMissing` and every stream-A route answers a named 503 — never an empty
result, because "0 requests" is a perfectly plausible answer for an account
that has not shipped yet. Measured, running the real door through
`entrypoint.sh` on an interpreter with no `duckdb`:

```
[door] engine  duckdb MISSING -- stream A routes will refuse by name
```

```
GET /api/v1/search?limit=1        →  503
{
  "outcome": "unanswerable",
  "refusal": {
    "reason": "duckdb-missing",
    "remedy": "the server's query layer needs DuckDB, which is not installed.\n    python3 -m pip install duckdb          (Linux, macOS, Windows)\n    pkg install py312-duckdb               (FreeBSD; PyPI has no FreeBSD wheel, so pip would compile the engine from source)\n ...",
    "catalogue": "/api/v1/capabilities"
  }
}
```

There is **no `result` key at all** on a refusal, so a client doing
`for (row of payload.result.rows)` raises rather than rendering a tidy empty
table. `/api/v1/aggregate`, `/histogram` and `/values` answer identically; the
door, stream B, the windows, the coverage and `/api/v1/accounts` all keep
working. The `pkg install py312-duckdb` line was **added** for this image:
the remedy used to name only `pip` and `brew`, and on FreeBSD `pip` finds no
wheel and `brew` does not exist — a refusal whose remedy does not work on the
machine reading it is a refusal with no way out.

### The dependency closure, and the 898 MB that is not in the image

This is where the FreeBSD build genuinely differs from the reference
repository. oauth2-proxy is one Go binary from a port with **no** `RUN_DEPENDS`,
so its `pkgindex.py` fetches a single package and **aborts the moment that
package declares a dependency**, saying so by name. CPython and DuckDB are a
graph, so that abort had to become a walk.

**What carried over from `gabrielbelli/freebsd-oauth2-proxy-oci`:** `verify`
and the z-base-32 alphabet, unchanged — the checksum format is the same format
and it is the part that would fail silently if it were wrong; the "unsupported
checksum version is a refusal, not a skip" rule, verbatim; `--exclude '+*'`;
the index being one JSON object per line despite the `.yaml` name; the
`STORAGE_DRIVER: vfs` reason; the no-`HEALTHCHECK` reason; the matrix over
(base, arch) with the ABI spelled out; and the weekly schedule.
**What did not:** `find`, replaced by `resolve`, which walks the graph.
`shlibs`, `modules` and `arch` are new and have no counterpart there.

`py312-duckdb` declares `RUN_DEPENDS` on `py-numpy` and `py-pandas`, which pull
`openblas`, which pulls `gcc14` (359 MiB) and `binutils` (139 MiB):

| | packages | installed |
|---|---|---|
| the declared closure | 27 | 1183 MiB |
| without numpy and pandas | 8 | 285 MiB |
| after pruning, on disk | 8 | **121 MiB** |

The other two components' closures are resolved by the same walker from the
same signed index, and they are the reason the split is worth an extra
Containerfile rather than a flag:

| component | roots | packages | installed | download | staged |
|---|---|---|---|---|---|
| door | `py312-duckdb python312` | 8 | 285 MiB | 52 MiB | 121 MiB |
| mcp | `python312` | 7 | 226 MiB | 38 MiB | **60 MiB** |
| proxy | `nginx` | 2 | 10 MiB | 2 MiB | **6.4 MiB** |

`py312-duckdb` is the entire difference between the first two rows.

Excluding them is a judgement about somebody else's package, so it is written
on the command line where it can be read, and the evidence is written down
beside it:

- upstream's own metadata lists numpy, pandas, pyarrow, fsspec and ipython
  under `extra == "all"` — every one optional;
- `duckdb/__init__.py` in the staged FreeBSD package imports none of them. The
  only submodules that do are `filesystem.py` (fsspec) and `polars_io.py`
  (polars), and **neither fsspec nor polars is a FreeBSD dependency of this
  package at all** — which is the proof that those submodules are not eagerly
  imported, since the package would be broken out of the box otherwise;
- nothing in `srv/` calls `.df()`, `.arrow()`, `fetchnumpy()` or `to_df()`; the
  whole query layer is `.execute(...).fetchall()`;
- the full `server/` suite passes **2298 passed, 0 failed, 0 skipped** against
  duckdb 1.5.5 on an interpreter with numpy, pandas and pyarrow all absent.

That is four pieces of evidence and **not one of them is "we ran it on
FreeBSD"**. The smoke job is what turns it into a measurement — see the
unverified list at the end.

**A stale exclusion refuses itself.** `--without` naming a package that is not
in the closure is a named error, not a no-op: an exclusion list is written once
and read for years, and an entry that quietly stopped applying would restore
most of a gigabyte to an image whose README says it does not carry it, with
nothing anywhere saying so.

The prune is checked the same way. `lib/python3.12/test` is 132 MiB of the
tree, `config-3.12` another 36 MiB, and neither can be reached from `srv/` — but
`ModuleNotFoundError` on somebody else's machine is not something building
catches, so `pkgindex.py modules` **derives** the import list from `srv/` with
`ast` and fails if the prune took anything on it. A test asserts both
directions, because a checker that only ever answers "fine" is decoration.

**For the mcp image that check is pointed at `srv/mcp.py` alone, and that is
not a shortcut.** Pointed at the package it would demand `duckdb` be staged —
the check would insist on exactly the dependency the image exists to shed. At
the file it asserts the true thing, and the output is the argument in four
lines: `json`, `os`, `sys`, `urllib`. The proxy has no interpreter at all, so
there is no import list to check and the staging step **says so** rather than
skipping in silence.

### The base and the ABI are one row, and crossing them is silent

A 14.x image must take its packages from the `FreeBSD:14` repository, not
`FreeBSD:15`. Nothing executes during a FreeBSD build, so crossing them
**builds and pushes perfectly** and fails at the first `exec` on somebody
else's machine. Measured from the real published layers:

| | `python3.12` needs | `freebsd-runtime` ships |
|---|---|---|
| FreeBSD 14.4 | `libutil.so.9` | `libutil.so.9` |
| FreeBSD 15.1 | `libutil.so.10` | `libutil.so.10` |

`pkgindex.py shlibs` walks every ELF file in the staged tree, collects every
`DT_NEEDED`, and requires each to be satisfied by the staged tree or by the
unpacked base. Run here against the real artefacts: the correct pairing reports
`95 ELF file(s) staged, every DT_NEEDED satisfied`, and the crossed one refuses
and names `libutil.so.10`, `libssl.so.35` and `libcrypto.so.35` with the files
that wanted them. `pkgindex.py arch` is the same idea for the architecture, and
it checks **every** ELF file rather than spot-checking one binary, because a
single stray file from the wrong repository is the failure to catch.

The matrix states `base`, `arch` and `abi` independently — the reference
repository's rule — and a test derives the pairing from the matrix itself and
fails when a major version does not match.

### Building it by hand

```bash
# 1. stage, ONE COMPONENT AT A TIME. Each of these verifies the index's own
#    signature against a pinned FreeBSD repository key, resolves the closure,
#    verifies every blake2b checksum, unpacks, prunes, and then checks the
#    architecture, the module set (where there is an interpreter) and every
#    DT_NEEDED. The component is the first argument and has no default.
./server/oci/stage-freebsd.sh door  FreeBSD:15:amd64 amd64 server/oci/stage-freebsd       baserootfs
./server/oci/stage-freebsd.sh mcp   FreeBSD:15:amd64 amd64 server/oci/stage-freebsd-mcp   baserootfs
./server/oci/stage-freebsd.sh proxy FreeBSD:15:amd64 amd64 server/oci/stage-freebsd-proxy baserootfs

# 2. build, from the REPOSITORY ROOT. One Containerfile and one ignore file per
#    component; the ignore file is passed explicitly because buildah has no
#    equivalent of BuildKit's <dockerfile>.dockerignore lookup.
for c in "":door .mcp:mcp .proxy:proxy; do
  suffix=${c%%:*}; name=${c##*:}
  STORAGE_DRIVER=vfs buildah bud \
    --platform freebsd/amd64 --format oci \
    --ignorefile "server/oci/Containerfile.freebsd${suffix}.containerignore" \
    -f "server/oci/Containerfile.freebsd${suffix}" \
    -t "claudio-${name}:freebsd15.1-amd64" .
done
```

`baserootfs` is an unpacked `freebsd/freebsd-runtime` tree; without it the
shared-library check is **skipped and says so**, because a check that silently
does not run is worse than one that is not there.

A hand build makes **no provenance claim**: `SOURCE_URL`, `REVISION`,
`VERSION`, `BASE_NAME` and `BASE_DIGEST` are build args whose defaults are
`unspecified` (or, for the two base fields, the readable moving tag), so the
labels say nothing rather than something stale. CI passes all five. Add them
yourself if you are publishing what you build — and if you want the base
pinned the way CI pins it, pass
`--build-arg BASE_REF=docker.io/freebsd/freebsd-runtime@sha256:…`. Both
Containerfiles take the same five arguments by the same names.

Running it is the Linux image's command with `podman` in front of it — the
environment variables, the mounts, the uid, the flags and the exit codes are
the same table, above.

```bash
podman run -d --name claudio-api \
  -p 127.0.0.1:8787:8787 \
  -v /var/db/claudio/store:/var/lib/claudio-usage \
  -v /usr/local/etc/claudio:/etc/claudio:ro \
  ghcr.io/<owner>/claudio-api:testing-freebsd
```

The whole stack is the four images with **one** published port. The other three
are not published at all — which is what makes the proxy the only surface to
reason about and the only place a certificate goes. This is the podman spelling
of `server/oci/compose.yml`; see [The four-way split](#the-four-way-split) for
why each mount is the direction it is.

```bash
podman network create claudio

# the WRITER: no -p at all, the store read-WRITE, and the shipping tokens.
# It holds door.lock. Only one of these may run per store; a second exits 3.
podman run -d --name ingest --network claudio --network-alias ingest \
  -v /var/db/claudio/store:/var/lib/claudio-usage \
  -v /usr/local/etc/claudio/tokens.json:/etc/claudio/tokens.json:ro \
  ghcr.io/<owner>/claudio-ingest:testing-freebsd

# the READER: the same store, read-ONLY, and the readers file. It takes no
# lock, so it runs beside the writer. It binds 0.0.0.0 inside the network,
# which is why the readers.json mount is not optional -- it refuses to serve an
# unauthenticated read API on a non-loopback bind and exits 4.
podman run -d --name api --network claudio --network-alias api \
  -v /var/db/claudio/store:/var/lib/claudio-usage:ro \
  -v /usr/local/etc/claudio/readers.json:/etc/claudio/readers.json:ro \
  ghcr.io/<owner>/claudio-api:testing-freebsd

# the mcp surface: an HTTP client of the api. NO store mount of any kind, and
# no token in the environment -- callers present their own, and an ambient one
# is refused with exit 7.
podman run -d --name mcp --network claudio --network-alias mcp \
  -e CLAUDIO_API=http://api:8787 \
  ghcr.io/<owner>/claudio-mcp:testing-freebsd

# the front end: the only thing published. With no certificate mounted it
# generates a self-signed pair into /etc/claudio/tls and says so on every start.
podman run -d --name proxy --network claudio \
  -p 80:8080 -p 443:8443 \
  -e CLAUDIO_DOMAIN=claudio.example.com \
  -v /usr/local/etc/claudio/tls:/etc/claudio/tls \
  ghcr.io/<owner>/claudio-proxy:testing-freebsd
```

Start the writer first: a reader over a store with no `accounts/` directory
**exits 9** rather than answering `no-data` about every account in a store it
is not looking at, and the writer is what creates that directory.

The proxy resolves `ingest`, `api` and `mcp` when it loads its configuration
and **exits** if it cannot, rather than answering 502 for the lifetime of the
stack — so start them first, or let compose's `depends_on` do it. A loud
refusal at startup is readable where a steady 502 is a mystery.

Before that command: create the two directories and make the store writable by
the same numeric uid the image runs as. This is the FreeBSD spelling of the
Linux block above, and it is the whole of the setup.

```sh
mkdir -p /var/db/claudio/store /usr/local/etc/claudio
chown 65534:65534 /var/db/claudio/store
echo '{"<ship-token>": "<account-uuid>"}' > /usr/local/etc/claudio/tokens.json
echo '{"<read-token>": "*"}'              > /usr/local/etc/claudio/readers.json
chmod 640 /usr/local/etc/claudio/*.json
```

### Podman on a FreeBSD host, and podman inside a jail

**These are two different deployments and the first one is the one to want.**

**On the host.** podman on FreeBSD implements a container *as a jail* — that is
the native mechanism, not an emulation of one. So `podman run` on a FreeBSD
host already gives you the isolation a jail gives you, and wrapping it in a
second jail buys nothing and costs the nesting problems below. If your plan is
"a jail for the door and a jail for the front end", `podman run` twice on the
host is that plan, and each container is one of those jails.

**Inside an existing jail.** If the door must live inside a jail you already
run — a hosting arrangement you do not control, an existing service jail — then
podman is creating a jail *inside* a jail, and the parent has to permit it.
That means, on the parent's `jail.conf`, at least `children.max` above zero,
`allow.mount`, `allow.mount.devfs`, `allow.mount.procfs` if the storage driver
wants it, and `enforce_statfs=1`; and the parent needs a writable dataset for
podman's graph root.

> **UNVERIFIED, and this is the one paragraph in this file most likely to be
> wrong in detail.** No FreeBSD machine was available while this was written,
> so nothing about nested jails here has been run — not the `jail.conf` keys,
> not the storage driver, not `podman run` itself. It is written from FreeBSD's
> documented jail permissions and not from a measurement, and it is offered as
> a starting point for somebody who has a FreeBSD host in front of them, never
> as a tested recipe. The *container's own* contract — the two mount points,
> uid 65534:65534, the five environment variables, the argv passthrough, exit 4
> with no `readers.json`, exit 5 on an unwritable store — is shared with the
> Linux image byte for byte and **has** been executed, on Linux; see the tables
> at the end of each half.

**Whichever shape you choose, `--net` is not the security boundary and the
door says so on every start.** There is no TLS, no mTLS, no token expiry and no
rate limiting anywhere in this server. Bind it where only your own machines can
reach it. On a jail that means a loopback or an internal address, exactly as
`-p 127.0.0.1:8787:8787` does above.

### The tags

Every tag in the Linux table above, plus these — **in each of the four
component repositories**, since the manifest job loops over the components and
then over what the build jobs recorded, so the matrices are the only place the
target list is written down.

| tag | what it is |
|---|---|
| `testing` | **mixed-OS manifest list**: `linux/{amd64,arm64}` and `freebsd/{amd64,arm64}` at the default base. One name, both operating systems |
| `testing-linux`, `testing-freebsd` | unambiguous fallbacks, each naming one operating system |
| `testing-freebsd15.1`, `testing-freebsd15.0`, `testing-freebsd14.4` | one per supported release, multi-architecture |
| `testing-freebsd15.1-amd64`, … | the per-cell images the manifests are assembled from, published because `buildah manifest add` takes them by remote reference |
| `sha-<short>`, `sha-<short>-linux`, `sha-<short>-freebsd15.1`, … | one commit, conventionally not overwritten. Better than a moving tag for a deployment; a `@sha256:…` digest is better than either, and is what the build jobs print |

`:testing` carries exactly **one** FreeBSD base, because nothing in a manifest
list distinguishes 15.1 from 14.4 — both are `freebsd/amd64`, and a runtime
choosing between two identical platform keys picks whichever came first, which
is a coin toss dressed as a decision. The other releases get their own tags.

`testing-linux` and `testing-freebsd` exist as fallbacks, and the reason is
stated rather than implied: the mixed-OS list is correct per the OCI
specification and has **not** been verified against every runtime this could be
pulled by, so there is always a tag naming one operating system that cannot be
got wrong. And a `:testing` that silently contained only Linux would be
indistinguishable, to somebody on FreeBSD, from a registry that had never heard
of them — so the manifest job **refuses** to publish it if the default FreeBSD
base produced nothing.

13.x is absent because it is end of life; CURRENT is absent for the reference
repository's reason — its base image and its packages both move on their own,
so a weekly job would keep republishing a tag nobody watches against a branch
carrying no support commitment.

### The workstation half is still not containerised, on FreeBSD either

`claudio`, the on-demand OTLP receiver and the shipper are installed with
`make install`. They are POSIX `sh` and stdlib Python with no third-party
dependency of any kind, and `claudio usage show` works offline with no server
and no daemon — which is most of why the tool is pleasant. A FreeBSD image for
them would suggest a container is a supported way to run them. It is not.

### Verified, and not

Everything in this table was run **on this machine**, against the real
published FreeBSD package repositories and the real base image layers.

| verified | how |
|---|---|
| `py312-duckdb` exists at the pinned 1.5.5 on every target ABI | the real `packagesite.pkg` index of `FreeBSD:{14,15}:{amd64,aarch64}`, parsed |
| the closure resolver walks the graph, and the exclusion is 27 → 8 packages | `pkgindex.py resolve` on the real 15:amd64 index: `1183 MiB` → `285 MiB` |
| a stale `--without`, a wrong checksum, an unsupported checksum version and a dependency absent from the index are each **refused by name** | each one run; each exits non-zero with the reason |
| the whole staging path works against the live repository | `stage-freebsd.sh FreeBSD:15:amd64 amd64 …` → 8 packages fetched, 8 blake2b checksums verified, unpacked, pruned `304 MiB → 121 MiB` on disk. **Re-run after the index signature check was added**: exit 0, same 8 packages, `311216KiB → 123576KiB`, `arch: amd64 x95` |
| the fetcher refuses cleartext in both directions | `curl --proto '=https' http://example.com` → **exit 1**, `Protocol "http" disabled`; the script's exact flag set (`--proto '=https' --proto-redir '=https' --max-redirs 3 --retry 3 --retry-all-errors`) fetches from `pkg.freebsd.org` over https → exit 0. The default this replaces is quoted from curl 8.7.1's own manual on this machine: *"By default curl only allows HTTP, HTTPS, FTP and FTPS on redirects"* |
| the package index is signed by the key this build pins, and the pin is FreeBSD's own | `pkgindex.py verifyindex` against the live `FreeBSD:15:amd64/latest` index, 2026-08-21: `sha256(packagesite.yaml.pub)` = `b0170035…f438`, byte for byte the fingerprint in FreeBSD's `/usr/share/keys/pkg/trusted/pkg.freebsd.org.2013102301`; RSA-2048 PKCS#1 v1.5 SHA-256 verifies. A truncated index, a substituted key and a missing `.sig` each exit **1** by name |
| every module `srv/` imports survives the prune | `pkgindex.py modules` → 17 modules, each located or named as built into the interpreter |
| every `DT_NEEDED` in the staged tree is satisfied by the tree or the base | `pkgindex.py shlibs` against the unpacked `freebsd-runtime:15.1` → `95 ELF file(s) staged`, clean. **Re-run after the index signature check landed, through the whole of `stage-freebsd.sh` with a base supplied** rather than with the check self-skipping: exit 0, `arch: amd64 x95`, `shlibs: … every DT_NEEDED satisfied` |
| the base-digest resolution produces a real digest, three ways | on 2026-08-21 `python:3.12-slim-bookworm` returns `sha256:a116514e…a78134` from `RepoDigests` on the pulled image, from `docker buildx imagetools inspect --format '{{.Manifest.Digest}}'`, and from a stdlib registry fetch hashed locally — all three agreeing. **The workflow's own `skopeo inspect --raw \| sha256sum` has not been run**, because skopeo is not on this machine |
| the base layers are reachable and their digests are real, without `skopeo` | the base rootfs was pulled straight off the registry with a ~40-line stdlib client (anonymous token → index → manifest → blob), each blob's sha256 checked against the digest that named it. Index digests on 2026-08-21: `15.1` = `sha256:d9beae9d…4604`, `14.4` = `sha256:a9da791d…4135`. That is the same bytes-in, same-hash-out operation the workflow's `skopeo inspect --raw \| sha256sum` performs, so the digest-resolution step produces a real value rather than an assumed one — **but the workflow's own command has not been run** |
| **a base/ABI mismatch is caught** | the same check against `freebsd-runtime:14.4` → **exit 1**, naming `libutil.so.10` (wanted by `python3.12` and `libpython3.12.so.1.0`), `libssl.so.35` and `libcrypto.so.35` (wanted by `_ssl` and `_hashlib`), and the files that wanted them. Re-run independently: 14.4's layer really does carry `libutil.so.9` and 15.1's `libutil.so.10`, read out of the unpacked layers. This is the negative control for the row above — without it that row passes over a check that cannot fail |
| every ELF file in the staged tree is one architecture | `pkgindex.py arch` → `amd64 x95`; asked for `arm64` it refuses and names the files |
| `python3.12` has an empty `DT_RUNPATH` and the base has no `ld-elf.so.hints` | both read directly out of the artefacts, which is why `LD_LIBRARY_PATH` is set |
| uid 65534 is `nobody` in both bases, and `nogroup` is **not** 65534 on FreeBSD | `/etc/passwd` and `/etc/group` of the real 14.4 and 15.1 layers — which is why the image says `65534:65534` and not a name |
| the stream-A refusal a client actually sees with no engine | the real door, started through the real `entrypoint.sh` on Python 3.9.6 with no `duckdb`: **503**, `duckdb-missing`, remedy present, **no `result` key** |
| the door, stream B, the reconciler and `/api/v1/accounts` all work with no engine | same run |
| the shell is POSIX | `sh -n`, `dash -n` and `bash -n` on `fetch-pkgs.sh` and `stage-freebsd.sh` |
| `pkgindex.py` parses **and runs** on Python 3.9, the floor the rest of `server/` holds | `ast.parse(..., feature_version=(3, 9))`, and `verifyindex` executed under the real `Python 3.9.6` against the live index → exit 0. The signature check is ~25 lines of stdlib arithmetic and needs no `openssl` and no wheel, which is what lets it run on the same interpreters everything else here does |
| the suite is green in **both** engine configurations | `2448 passed, 0 failed, 0 skipped` with DuckDB; `1516 passed, 0 failed, 45 skipped` without, and the run **exits 1** unless `--allow-skips` is given |

The split into four images added these, all run on 2026-08-24 against the live
signed index and the real base layers:

| verified | how |
|---|---|
| all three closures resolve, on **every** matrix ABI | `pkgindex.py resolve` against the real `packagesite.pkg` of `FreeBSD:{14,15}:{amd64,aarch64}`, each index signature-checked first. Every cell carries `py312-duckdb 1.5.5`, `python312 3.12.14`, `nginx 1.30.4_4,3` and `pcre2 10.47_1`; the door is 8 packages, the mcp 7, the proxy 2, within 3 MiB of each other across the four |
| the three staged trees are what the table above says | `stage-freebsd.sh <component> FreeBSD:15:amd64 amd64 … baserootfs` run end to end for each: door `311216KiB → 123576KiB`, `arch: amd64 x95`, 17 modules located; mcp `249560KiB → 61920KiB`, `arch: amd64 x94`, **4** modules (`json`, `os`, `sys`, `urllib`); proxy `10560KiB → 6576KiB`, `arch: amd64 x6`. Every one exits 0 with `shlibs: … every DT_NEEDED satisfied` |
| the mcp tree carries **no** DuckDB, and the check that says so is not the door's | the mcp staging names `python312` alone and its module check is pointed at `srv/mcp.py`; pointed at `srv/` it would demand `duckdb` be staged, which is the dependency the image exists to shed |
| **nginx's closure really is two packages**, which is what makes a FreeBSD proxy worth building | `pkgindex.py resolve … nginx` → `2 package(s), 10 MiB installed, 2 MiB download`. `usr/local/sbin/nginx` needs `libthr.so.3 libcrypt.so.5 libpcre2-8.so.0 libssl.so.35 libcrypto.so.35 libz.so.6 libc.so.7`, read out of the real binary's `DT_NEEDED`; all but `libpcre2-8.so.0` are in the base layer, which is why `LD_LIBRARY_PATH` is set for this image too |
| **the proxy is ABI-sensitive and the existing check catches it** | the `FreeBSD:14` proxy tree checked against the `15.1` base → **exit 1**, naming `libcrypto.so.30` and `libssl.so.30` `wanted by usr/local/sbin/nginx`. The correct pairing (14 packages, 14.4 base) is clean. This is the negative control for the row above |
| nginx's paths are **read out of the binary**, not guessed | `--prefix=/usr/local/etc/nginx`, `--pid-path=/var/run/nginx.pid`, `--http-*-temp-path=/var/tmp/nginx/*_temp`, `--http-log-path=/var/log/nginx/access.log`, `--with-http_ssl_module`, `--with-http_v2_module` — from the package's own configure string. That is where the `pid` override and the two `COPY --chown` lines come from |
| **the configuration parses**, both with and without the TLS drop-in | `nginx -t` over `nginx.freebsd.conf` + `claudio.conf` + `claudio-locations.conf`, with `door` and `mcp` resolvable: *syntax is ok, test is successful*. Repeated with the TLS server block and a self-signed certificate: same. (That block is now shipped as `claudio-tls.conf` rather than mounted, because `entrypoint-proxy.sh` guarantees a certificate exists before nginx starts.) **Run on nginx 1.29 from `nginx:alpine`, NOT on FreeBSD's 1.30.4** — see the unverified list |
| **and the routes do what the table says**, probed against a live nginx with stub upstreams | `/api/v1/search?x=1` arrives at the door as `/api/v1/search?x=1` — prefix, path and query string intact — with `Authorization: Bearer tok` forwarded verbatim; `/api/v1/window/5h/1234` likewise; `POST /sender` arrives as `POST /v1/ship`; `POST /mcp` arrives at the *mcp* upstream as `/mcp`; `/healthz` is **404** with `reason: not-exposed`; `/nonsense` is **404** naming the three routes. This is the assertion the whole `/api/` paragraph exists for, and it is now a measurement rather than a reading of the manual |
| one side effect, measured and kept | `/api` with no trailing slash answers **301 → `/api/`** rather than the catch-all's 404. A location ending in `/` whose `proxy_pass` carries no URI sets nginx's `auto_redirect`; it is the helpful answer, and it comes from the very property that preserves the prefix |
| the staging script and both entrypoints are POSIX | `sh -n`, `dash -n` and `bash -n` on `fetch-pkgs.sh`, `stage-freebsd.sh`, `entrypoint.sh` and `entrypoint-mcp.sh` |
| the guards over all of this are not decoration | 18 mutations of the new files — the mcp image copying the whole package, its ignore file re-including it, `-m srv.mcp`, `/api/` gaining a URI part, `/mcp` buffering, a body limit diverging from the door's, `/healthz` quietly proxied, a `user` directive, port 80, `duckdb` in the mcp package list, a defaulted component argument, two components sharing a stage directory, the ambient-token refusal deleted, a store path in the proxy, a label not matching its file — **18 caught, 0 survived**. Three of them survived the first pass and each was a real defect in the guard, corrected and re-run: a `-m srv.mcp` check that looked at the Containerfile instead of the entrypoint that launches it; an `/api/` check satisfied by the very trailing slash it existed to catch; and a "no default" claim that no behavioural probe can reach |

**One FreeBSD image HAS been built here, and this paragraph used to say none
had.** The correction is recorded rather than quietly applied, because "never
built anywhere" was the strongest claim in this file and it was wrong. What was
run, on macOS/arm64 with Docker 29.6.2:

| verified | how |
|---|---|
| **the image builds, on a machine with no FreeBSD anything** | `docker build --platform freebsd/amd64 -f server/oci/Containerfile.freebsd.api --build-arg STAGE=server/oci/stage-freebsd-api .` → **exit 0, about 5.5 s** (1.6 s context, 1.0 s the staged tree, 2.9 s export; the first draft of this row quoted the 2.9 s export figure as the build time and that is corrected here rather than left). This is the no-`RUN` technique doing exactly what it is for: `FROM` and `COPY` execute nothing, so no FreeBSD binary ever runs during the build |
| and BuildKit's own base resolution matches the digest computed here | the build log names `freebsd/freebsd-runtime:15.1@sha256:d9beae9d…4604`, byte for byte the index digest the stdlib registry client computed independently in the row further down. Two unrelated implementations, one digest — which is the property `base.digest` is supposed to record |
| the config really says FreeBSD | `docker image inspect --format '{{.Os}}/{{.Architecture}}'` → **`freebsd/amd64`** |
| the provenance labels land, and the defaults behave as designed | all nine keys present; `licenses: BSD-2-Clause`; `base.name: docker.io/freebsd/freebsd-runtime:15.1`; `com.claudio.engine: duckdb 1.5.5 (FreeBSD py312-duckdb)`; and `version`/`revision`/`base.digest` = **`unspecified`**, which is a hand build correctly making no claim rather than a stale one |
| no health check, right uid, and the linker path is set | `Healthcheck` `<nil>`; `User` `65534:65534`; `LD_LIBRARY_PATH=/usr/local/lib` and `PYTHONDONTWRITEBYTECODE=1` in `Env` |
| it contains the server, the FreeBSD interpreter and the engine — and none of the workstation half | 5 263 members across 7 layers: `opt/claudio-server/srv/serve.py`, `entrypoint.sh`, `LICENCE`, `usr/local/bin/python3.12`, `usr/local/lib/python3.12/site-packages/duckdb/__init__.py`; **no** `claudio`, `usage/` or `tests` |
| the layers carry no FreeBSD-hostile xattrs | the workflow's own check, run over `docker save` output: 7 layers scanned, none carrying an empty-valued or `.overlay.` xattr. **Weak evidence and said so:** that failure is buildah's *overlay driver* leaving `user.overlay.origin`, which is why CI pins `STORAGE_DRIVER: vfs`. Docker was never going to produce it, so this shows the check runs and parses, not that the condition is absent from a buildah build |

**What that build does NOT establish, and it is most of it:**

- **`buildah bud --format oci` has still never run.** It is a different builder
  producing a different manifest format, and three things in this repository
  are *specifically* about buildah: `--format oci` dropping `HEALTHCHECK`, the
  `--annotation` flags, and the `STORAGE_DRIVER: vfs` xattr trap. None of them
  is exercised by a Docker build. CI is the first `buildah`.
- **The image has never been run**, on any machine. There is no FreeBSD kernel
  here, so `import duckdb` on FreeBSD remains unexecuted — the smoke job is
  still the only thing that closes it.
- **Nothing has been pushed or pulled**, so podman on FreeBSD has never
  received it and the `ExtattrSetLink` panic the `vfs` setting exists to
  prevent has neither occurred nor been ruled out.

The exact state of the machine, because "cannot be installed" was too strong
and this file may not round facts up: `buildah` is absent and there is no
Darwin build of it to install. **`skopeo` IS present now**, and two rows above
that said it was not are stale rather than wrong-at-the-time — the base rootfs
for `15.1` and `14.4` was pulled with the workflow's own
`skopeo --override-os freebsd --override-arch amd64 copy` on 2026-08-24, which
is how the shared-library check ran against a real base for all three
components. The `skopeo inspect --raw | sha256sum` digest step still has not
been run; `podman` 6.1.0 is present as a
**client with no machine**, so nothing is behind it, and creating one is a
multi-gigabyte Linux VM this machine was deliberately not asked to run. The
rest is unverified:
- **Nothing has ever been pushed to a registry**, from this machine, for either
  operating system. The tag tables describe what CI is to publish.
- **No attestation has been generated or verified anywhere.** The workflow
  requests `id-token: write` and calls `actions/attest-build-provenance` on
  every digest it pushes, including the manifest lists; none of that has run.
  The `gh attestation verify` line in the tag section is what the feature is
  for, not something anyone has watched succeed.
- **No base image has been resolved to a digest here.** The `skopeo inspect
  --raw | sha256sum` step and its `docker pull` counterpart run only in CI; the
  local build above was handed a placeholder digest to prove the label plumbing
  and nothing more.
- **The index signature check has never run inside `buildah bud`** — only
  outside it. `stage-freebsd.sh FreeBSD:15:amd64 amd64 …` was re-run end to end
  from this machine after the check was wired in: exit **0**, `index: signature
  verified against the pinned FreeBSD repository key`, 8 packages staged,
  pruned `311216KiB → 123576KiB`, `arch: amd64 x95`, all 17 modules located.
  What has not happened is the build that consumes that tree, because `buildah`
  cannot be installed here (see the paragraph above for exactly what is and
  is not on this machine).
- **The `--ignorefile` flag** is how buildah is given the context filter, since
  it has no equivalent of BuildKit's `<dockerfile>.dockerignore` lookup. It has
  not been run here. If it is wrong the build fails loudly on an unknown flag,
  which is the acceptable direction.
- **The mixed-OS `:testing` manifest list** is correct per the OCI
  specification and has not been pulled by any runtime here. `testing-linux`
  and `testing-freebsd` exist so that a problem with it costs nobody an image.
- **No FreeBSD binary in this tree has ever executed.** Every fact above about
  the packages is read from ELF headers and package metadata; that is exactly
  what static analysis can establish and no more. In particular the
  numpy/pandas exclusion is an inference from four independent pieces of
  evidence, **not a measurement**.
- **The mcp image's entrypoint calls a front end that must exist.** It runs
  `mcp.py --http --host <h> --port <p> --api <url>` for the served transport and
  `mcp.py --api <url>` for stdio, and the second of those works today. The
  first is the Streamable HTTP front end that belongs in `srv/mcp.py` beside
  `handle()` — one function, both transports, so a tool cannot exist on one and
  not the other. Until it lands, the image starts and the process refuses the
  flag; the flag spelling is the contract between the two halves and it is
  written here so it is not guessed twice.
- **Neither the mcp nor the proxy image has been built, by any builder.** Their
  Containerfiles parse, their staged trees are real and every check over them
  passes here, but `buildah bud` has run on none of the three and no FreeBSD
  binary in any of these trees has ever executed. In particular **nginx has
  never started** in this image: the configuration was validated with `nginx -t`
  on Linux nginx 1.29, not with FreeBSD's 1.30.4, and the two things that could
  still differ there are `/dev/stdout` for the access log — which needs the
  runtime to mount devfs, and fails loudly at startup if it does not — and the
  `--chown`ed temp directories, which only a request under load exercises.
- **CI still builds the door alone.** The staging script, all three
  Containerfiles and all three ignore files are here and are checked by the
  suite; the FreeBSD job's matrix has not been given its component axis, so
  `claudio-mcp` and `claudio-proxy` are not published by anything yet. The job
  says so in a comment beside the step, and this line says so here, because a
  missing tag is exactly the sort of absence nobody notices.
- **The smoke job is what closes that**, and it has not run either. It unpacks
  the staged tree over a real FreeBSD 15.1 VM — base plus staged tree is
  exactly what the image is — asserts `import duckdb`, asserts numpy and pandas
  really are absent, and runs `server/tests/test_all.py` with the staged
  interpreter. It covers **amd64 and 15.1 only**: the VM is amd64, so arm64 and
  the 15.0 and 14.4 pairings are not covered by it and are not claimed to be.
- **`:testing` publishes even if the smoke job fails, and a release does not.**
  That split is deliberate. `:testing` is not a release and is published on the
  build's own checks; letting one VM job that has never run locally block every
  publish would mean that the day it breaks for its own reasons, nobody gets an
  image at all. A `refs/tags/v*` push **is** gated on it, by name, and refuses
  with the reason.

## Why the server exists at all

Local attribution of plan movement to requests was built, worked, and was
deleted deliberately. It cannot be correct on one machine:

- **Double counting.** The percentage belongs to the *account*. Two machines
  each observe the same 5%→7% move and each attribute the full 2 pp to their own
  traffic. Summed: 4 pp of a 2 pp move, with neither machine wrong on its own
  evidence.
- **Invisible surfaces.** claude.ai in a browser, the desktop app, a phone, any
  un-instrumented machine. All consume the same quota; none report.
- **No baseline.** The first observation after any gap has nothing before it.

Every one of those needs the whole account in view. **If it does not reconcile
across machines, it should not exist.**

## The modules

| file | what it owns |
|---|---|
| `srv/wire.py` | what a shipper may send: streams, schema versions, value guards, idempotency keys |
| `srv/ingest.py` | dedupe and partition by account, in memory. Re-ingest is a no-op |
| `srv/window.py` | window reconstruction per account from stream B |
| `srv/coverage.py` | what fraction of a window had **any** machine reporting |
| `srv/attribute.py` | attributed and **residual** for a closed window |
| `srv/reconcile.py` | the entry points, `reconcile()` and `reconcile_ingest()` |
| `srv/query.py` | **the reader's rules**: what a query means, what it refuses, and the Python predicate that is the oracle |
| `srv/duck.py` | **the SQL**: a `Query` compiled for DuckDB, the pinned schema, the empty-answer accounting. Pure |
| `srv/store.py` | **the storage layer**: the DuckDB connection over `accounts/<uuid>/*.jsonl`. Impure |
| `srv/api.py` | **the contract**: `/api/v1/`, the envelope, the refusals, coverage on the wire — the guardrails SQL cannot express |
| `srv/serve.py` | **the door**: the socket, the bytes on disk, the offsets, and the wiring that puts `api.py` on a port. Impure |

## The five rules that carry the design

**A window is identified by its `resets_at` and by nothing else.** A stream-B
sample is `(t, P̂, ê)` where `t` is the observing machine's clock but `P̂` and `ê`
come from a snapshot cached inside the claude process, and that snapshot is
arbitrarily old — `replay-real.jsonl` row 49 is a reading at least 9 h 49 m
stale, reported *after* the window it names had closed, and its own timestamp
falls inside the *next* window. Membership is therefore by `resets_at`, never by
`ts`.

**The only sound cross-machine operator is `max`.** A cached snapshot can only be
old, never from the future, and utilisation is non-decreasing within a window, so
every sample is a lower bound on that window's terminal value and nothing more.
Differencing two samples, ordering them by `t`, interpolating between them: all
unsound. Pooled by `ts`, the real capture shows six in-window *decreases*; every
one is a stale snapshot.

**A forward `resets_at` is a rollover: close the window, open the next.** Most of
the feature lives in that branch. Deleting it locally dropped a replay of 65
samples from 49 records to 22 and turned a 90%→2% roll into one record followed
by permanent silence.

**The residual is the product, not an error.** It measures usage from somewhere
you are not watching. It is never redistributed into the attributed buckets to
make the numbers tidy, which rules out the obvious implementation
(`attributed_i = movement × w_i / Σw`) because that makes the residual
identically zero for every window for ever, and the numbers then look complete
and say nothing. Instead a rate — percentage points per unit of weight, fitted
across *that account's* own closed windows — converts weight to points, and
whatever the window moved beyond that is the residual. Negative residuals are
reported negative: the movement is a lower bound, so over-attribution means the
bound is loose, not that the requests are wrong.

**Coverage is the guard on the residual.** A window during which none of your
machines was listening also produces a large residual, and the two are
byte-identical in the output unless coverage sits beside it. So poor coverage is
stated in the same row, and a window with *no* coverage refuses to attribute
rather than publishing a number that reads as evidence of a surface that may not
exist. With no attestation at all, coverage is `None` — "nobody said" — never
`0.0`, which would be the claim "nobody was listening".

## Provisional, and it says so on every row

Requests are weighted by `cost_usd_reported`, Claude Code's own figure at API
list rates. On a Pro or Max plan that is **notional** — never billed — so it is a
relative weight and nothing more. It is used because it folds in all four token
classes at their correct relative prices. Which token class the plan percentage
actually tracks is unanswered, and `attribute.PROVISIONAL_NOTE` is attached to
every attribution, including the refused ones, because a weight nobody has
validated becomes a fact by repetition.

## Fixtures, and the one rule about them

Everything the suite asserts against starts as bytes captured off a real machine
in `usage/tests/fixtures/`. Where a scenario exists in no capture — a second
machine, a skewed clock, a 90%→2% rollover, an attestation — it is **derived**
from a real record by named field overrides, or **authored**, and
`tests/fixtures.py` records which. A test pins the derivation ledger field by
field and asserts the authored set is exactly `{manifest, attestation}`.

A synthetic generator was once written in this repository to match the
collector's own filters, so the generator and the filter agreed with each other
while both disagreed with Claude Code. Two suites stayed green over a wrong event
name that dropped every record and an allow-list that deleted every user tag. A
generator can prove a socket is listening. It can never prove a shape, a field
name or a label is handled.

## The query layer

`srv/query.py` is pure like the rest of the core: it is handed rows and returns
answers, so every behaviour below is testable without a socket. It filters
stream A on any column with time ranges and free text, looks up a
`request_id`/`session_id`/`prompt_id`, groups by any column or user tag, pages,
and re-shapes the reconciled windows. Four things about it are decisions rather
than features.

Its ROLE changed when DuckDB arrived and **no code was deleted from it**. SQL
subsumed the scanning, not the meaning: `query.py` is now the differential
**oracle** the storage layer is checked against, and the home of the four
decisions below — every one of which a query engine would get wrong by
answering cheerfully. `srv/api.py` puts them on the wire; `srv/store.py` runs
the SQL; neither owns a rule.

**Three empty answers, kept apart by construction.** `no-data` (nothing was
scanned), `filtered-to-nothing` (rows were scanned and every one eliminated),
and `Unanswerable` (the question is not one this data can answer) are different
values with different fields. The filtered case names the clause that did it,
from a per-clause elimination count, because an intersection of six facets that
returns nothing is otherwise six equally plausible suspects. The comparison
worth stating: `claudio usage show --by nonsense` groups every row under
`(none)` and prints a tidy one-row table — a refusal wearing an answer's
clothes — and here an unknown column is `unknown-column` with the real ones
listed.

**Every aggregate carries its coverage, and today that coverage is `None`.**
An attributed figure without coverage is the confident-wrong-number failure:
a window nobody watched and a window with a browser session behind it produce
the same residual. Nothing in this repository emits an attestation, so every
`CoverageStatement` over real data says `fraction: None, basis:
"no-attestation"` — "nobody said", never `0.0`, which is the claim "nobody was
listening". Beside it sits *placement*, which is always computable: how many of
the rows sit in a closed window, an open one, no window at all, or carry no
usable timestamp. That is the honest denominator for any breakdown, and it is
what makes a `project` breakdown — which reaches stream A only through
`session_id`, and only for sessions that produced a sample — state the fraction
it could place instead of implying it placed everything.

**No cross-account total exists anywhere in the file**, not even for tokens.
`breakdown` takes one account's rows and refuses otherwise, the way
`window.reconstruct` refuses a sample belonging to somebody else. Grouping
*by* `account_uuid` is fine — each bucket is one account and there is no total
row. Grouping across accounts by `account` or `email` is refused by its own
name (`label-is-not-an-identity`): `--tag account=` overwrites the first and the
second survives an orphaned login, so two accounts sharing a label would
silently become one bucket.

**Pagination is keyset over `(ts, request_id)`, and the reason is particular to
this store.** The usual argument against offsets is that appends shift later
pages; the sharper one here is that a week-late shipment is a designed-for
event, so a row can be appended whose `ts` belongs in the middle of a page
served yesterday — under an offset that skips a real request in silence. Both
components of the key are immutable (`ts` is the payload's own clock, and
`request_id` is a hash of the row's content), so a row is never served twice or
skipped. The one thing keyset genuinely cannot show — a row arriving *behind*
the cursor — is stated on every page with the remedy, and a cursor issued for
another query or another direction is refused rather than applied.

### The storage question, measured

The hand-rolled scan was measured first, and then DuckDB was measured against
the same corpus rather than assumed to fit. The corpus is grown from the REAL
fixtures by `bench/gen_corpus.py` — a real record, in its own key order, with
named identity and clock fields overridden and every other byte left alone —
at the projected five-year volume: **62 M stream-A rows** and **339 k stream-B
rows**.

> **Read the numbers before you run the commands.** Reproducing the figures
> below writes **48.9 GB** into `/tmp` for stream A alone, and
> `--shapes jsonl,parquet,db` then materialises a 2 GiB Parquet projection and
> a DuckDB database beside it. The whole-file scan it measures takes **351 s**
> per pass, three passes by default. This is a measurement you take once, on a
> machine you are not otherwise using — not a step in setting the server up,
> and nothing in the product needs it. `--rows 1000000` is ~789 MB and is
> enough to see the shape of the curve; that is what the script's own usage
> line suggests, and it is deliberately not the number quoted here, because
> the number quoted here is the one the table below was measured at.

```bash
# The five-year projection the table below reports. 48.9 GB and ~20 minutes.
python3 server/bench/gen_corpus.py --stream a --rows 62000000 --out /tmp/a.jsonl
python3 server/bench/gen_corpus.py --stream b --rows 339000  --out /tmp/b.jsonl
python3 server/bench/bench_duckdb.py --a /tmp/a.jsonl --b /tmp/b.jsonl \
        --shapes jsonl,parquet,db

# A cheap look at the same curve: ~789 MB, seconds rather than minutes.
python3 server/bench/gen_corpus.py --stream a --rows 1000000 --out /tmp/a.jsonl
```

First correction to a number this file used to quote: 62 M rows is **48.9 GB /
45.6 GiB**, not 45.7 GB. The 737-byte mean row is the row the *client* writes;
the row in the store carries `account_uuid`, which the shipper stamps on, and
that is 46 bytes more — **789 bytes**. The projection was measuring the wrong
end of the wire.

Medians of 3 runs after a warm-up, M2 Max, 12 threads, DuckDB 1.5.5, warm page
cache:

| query | Python scan (`query.py`) | DuckDB, JSONL in place | DuckDB, Parquet | DuckDB, persisted db |
|---|---:|---:|---:|---:|
| filtered search | 351 s | **8.06 s** | **0.070 s** | **0.007 s** |
| terms aggregation | 351 s | **8.32 s** | **0.070 s** | **0.086 s** |
| date histogram | 351 s | **8.16 s** | **0.132 s** | **0.096 s** |
| lookup by `request_id` | 351 s | **8.34 s** | **0.202 s** | **0.100 s** |
| the empty-answer accounting | 351 s | **8.14 s** | — | — |
| join, stream A × stream B | *not expressible* | **185 s** | **184 s** | *(see below)* |
| store size | 45.6 GiB | 45.6 GiB | **2.03 GiB** | 4.06 GiB |
| build time | 0 | **0** | 36 s | 31 s |

The Python figure is one measurement, not one per row: every one of these is a
full scan at **7.71 s/GiB** (measured on a 2 GiB sample of the same file;
linear against the 100 k and 1 M corpora to within 1.8%), so the whole file is
351 s whatever the question. The earlier 6.675 s/GiB in this file is superseded
— it was measured on the unstamped row.

**Does DuckDB read these files in place fast enough to need no import step?**
Below about **1.1 GiB it does**, and above it, it does not. 0.73 GiB answers in
90 ms; 45.6 GiB answers in 8.1 s. Eight seconds is 43× better than the scan it
replaces and is still not interactive, and it does not improve with a narrower
question: **a `request_id` lookup costs the same as a whole-history GROUP BY**,
because reading the JSON *is* the work and there is no index to skip it with.
That is the honest shape of "no import step": SQL and a 43× constant factor,
with cost proportional to corpus size and nothing else.

So the design is the one the storage pass specified, and the numbers pick it
rather than an argument doing so: **the JSONL stays the record of truth and
Parquet is a derived, deletable projection.** It is 115× faster, 22× smaller,
and rebuilt from the JSONL in 36 seconds — which is what makes it disposable
rather than a migration, and it plugs into exactly the seam `narrowing()`
already stated. A persisted DuckDB database was measured too and is the worse
of the two: the same query times as Parquet, twice the bytes (4.06 GiB), and it
is a *database file*, which is precisely the thing the record of truth must not
become. **Nothing has been built to maintain the projection**; the crossover is
documented, `store.py` reads the JSONL, and a projection is warranted when a
real store approaches a gigabyte, not before.

The join is the one place the argument for DuckDB is also its worst result,
and the one number here worth stating twice. `LEFT JOIN` on a *range*
(`a.ts >= w.resets_at - 18000 AND a.ts < w.resets_at`) is **185 s over JSONL
and 184 s over Parquet** — the projection that makes every other query 115×
faster buys the join **nothing at all**, because a non-equi join cannot use the
row-group statistics the rest depends on and the cost is the comparison, not
the read. It is still the one thing the hand-rolled engine cannot do at all,
and it is what ad-hoc correlation needs; but "correlation is now possible" must
not be read as "correlation is now fast", and an index would not change it
either. Bound the window set first — that is a real remedy, since the join
above is over 8 700 windows and a month is 145.

Stream B needs none of this and it is worth saying so, because the temptation
is to index both: the whole five-year corpus is 339 k rows / 202 MiB.

### What DuckDB does silently, and what is done about it

Five measurements, each of which is a refusal in `srv/duck.py`. They are here
rather than in a commit message because every one of them produces a plausible
answer.

**Inference drops a column, and the drop is silent.** 30 000 real ledger rows
with `cache_read_tokens` absent from the first 25 000 — inside the default
`sample_size` — and `read_json_auto` returns a relation of **29 columns instead
of 30**. `SELECT *` is a column narrower, `GROUP BY model` answers cheerfully,
and only a query naming the column by hand errors. Cache reads were ~89% of
this user's tokens. A reader that derives its column list from the relation —
which is what the front end is supposed to do — renders the table with that
column simply gone. So the schema is **pinned**, from `wire.LEDGER_FIELDS`,
and there is no code path that omits `columns=`.

**A pinned list is blind to a new column, so the new columns are counted.**
`SELECT new_field_2027` over a pinned read is a binder error; `read_json_auto`
sees it fine. That is `cu/otlp.py`'s allow-list one layer down, and that
allow-list is how `prompt.id`, every user `--tag` and `agent.name` were each
lost in silence. `store.unknown_keys()` names every key in the files the pinned
list does not carry, with counts.

**`ignore_errors=true` does not skip a malformed line — it emits a row of
NULLs.** 1000 good records plus one torn fragment gives `count(*) = 1001`, and
the extra row is all NULLs. Without `ignore_errors` the whole query dies
instead, so one torn byte range becomes a store that answers nothing. A torn
final line is the ordinary state of a live store — `Store._append` carries a
guard against gluing a fragment onto a good record precisely because a process
killed mid-append leaves one. So the read tolerates it, `request_id IS NULL` is
exactly the malformed set (it is a content hash present in every real row),
those rows are excluded from every count and every page, and the count is
reported as a store problem rather than quietly subtracted.

**`ignore_errors=true` does the same thing one level down, and that half was
counted by nothing.** With `columns=` pinned, a perfectly valid JSON line whose
value does not fit the pinned type has **that value** nulled and the row kept.
Measured against the real fixtures: `input_tokens: 9223372036854775808` → NULL,
`1e30` → NULL, `"abc"` → NULL, `ts: "2026-08-12T10:00:00Z"` → NULL — and in
every case `scanned=2, matched=2`, `request_id` intact, so `request_id IS NULL`
never fires and no counter moves. It was the cardinal sin in its purest form:
`_token_nulls` counted such a row and the aggregate note said *token columns
are null where the payload did not state them*, a false sentence about a figure
the payload stated precisely. There is no free way to count it — the coercion
has already happened by the time the pinned relation exists — so
`store.coerced_values()` is a **second read of the same bytes**, a diagnostics
call for `unknown_keys()`'s reason, reported at `/api/v1/diagnostics` and as a
`coerced-values` store problem. What is on the query path instead is the
truthful wording: the note names both possibilities and points at the route
that separates them.

**A pinned `BIGINT` coerces rather than refusing.** `1.5` arrives as `2`, `"7"`
as `7`, `true` as `1` — no warning on any of the three, and the first two
**add** to a sum the Python oracle excludes. `model: {"a":1}` arrives as the
string `'{"a":1}'` and `ts: "1786584663"` as a datable `1786584663.0` where the
oracle counts the row as undatable. The token columns,
`duration_ms`, `ts_ns` and `schema` are integers in all 8 real rows, so BIGINT
is what they are and the coercion is named here rather than found later. The
seven **never-observed** columns are pinned `JSON` instead, which is the whole
reason they are a separate list: nothing has ever stated what arrives in
`event_sequence`, and typing it BIGINT because the name ends in "sequence" is
the assumption-written-as-a-value this project keeps apologising for. `JSON`
keeps the value *and* its type.

**Everything binds except the one thing that matters, so that one thing gets a
whitelist instead.** An earlier draft of this layer quoted its values into the
SQL and justified it by saying binding "is not available for a table function's
path argument". Measured on 1.5.5, that is **false**: the path binds, a list of
paths binds, the pinned `columns=` map binds and keeps its types exactly, a
value inside `FILTER (WHERE …)` binds, a row comparison binds, `LIKE … ESCAPE`
binds, a JSON path binds, and so does `LIMIT`. So every value is a bind
parameter and the quoting helpers are deleted rather than left about as a
second way to do it.

That is not a tidy-up. Interpolation had a live defect with no injection in it
at all: `repr(float("-inf"))` is `-inf`, which DuckDB reads as a **column
name**. So `since=-inf` — an ordinary way to say "no lower bound", and one
`serve._num_param` accepts today, since `float("-inf")` parses — matched all 8
real rows through `query.py` and raised `Binder Error: Referenced column "inf"
not found` through this one. Bound, the two engines agree; the mutation
`duck-time-bound-written-into-the-sql` reproduces exactly that exception.

What does **not** bind is an identifier, and that measurement is the opposite
of reassuring: `SELECT $c` with `c="model"` returns the constant *string*
`'model'`, once per row — a plausible answer to a question nobody asked, with
no error anywhere. A column name therefore has exactly one possible defence, a
whitelist, and `query.compile_query` applies it before `duck.py` is reached —
**and `duck._ident` now enforces it rather than asserting it in a docstring.**
It used to escape whatever it was handed and say "only ever reached with a
checked column name"; `/api/v1/values?fields=` reached it with an unchecked one,
having gone nowhere near `compile_query`. The `"` doubling held, so there was no
injection — but every ordinary typo came back as HTTP 500 `reader-failed`,
whose remedy says *this is a fault in the reader*, with the generated SQL and
DuckDB's candidate-binding list in `detail`. That route whitelists its fields
now and refuses `unknown-column` the way `/aggregate?by=` always has; `_ident`
raises on an unchecked name as the backstop for the next caller.

**A value of the wrong type is compared, never cast.** Every URL value arrives
as a string and half the columns are numeric, and binding a VARCHAR against a
BIGINT column makes DuckDB cast — wrong in both directions. `where.input_tokens=abc`
raised `ConversionException`, which escaped as another 500 over a typo; and
where the cast succeeds it is lenient in ways equality is not. Measured on
1.5.5: `529 = ' 529 '`, `529 = '+529'`, `529 = '529.0'`, `18 = '0x12'` and
`18 = '1_8'` are all **TRUE**, so `?where.input_tokens=%20529%20` returned the
row with `outcome: ok` while `query._eq` — the oracle — matched none of them.
The reverse casts the *column*, so whether the query raises depends on the data
rather than on the question. `duck.comparable()` is the rule: a value whose
Python type the column's pinned SQL type cannot hold compiles to `FALSE`, which
is exactly what the oracle's `isinstance` comparison does, and the clause label
still names it so `filtered-to-nothing` points at the right clause.

**A NaN bound is refused, and the infinities are not.** `float("nan")` parses,
`wire._is_number` is true of it and `until < since` is false for NaN, so
nothing a bound had ever been checked for caught it. Then the two engines order
it **oppositely**: Python's `ts <= nan` is False for every row and DuckDB's
total float ordering puts NaN above infinity, so over the 8 real rows
`until=nan` gave the oracle 0 rows and the storage layer all 8, `outcome: ok`,
no note. `String(NaN)` in JavaScript is `"NaN"`, so a date field that fails to
parse in a front end sends exactly that. `compile_query` refuses it,
`_num_param` refuses it at the door and `decode_cursor` refuses a cursor
carrying one — a position `encode_cursor` never issued, which keeps its
fingerprint through a hand edit and pages for ever. `±inf` keeps working: those
are ordinary "no bound" spellings that order identically in both engines.
Parameters are **named** (`$p1`) rather than positional because
`accounting_sql` embeds the whole predicate three times, and a positional
repeat in the wrong order is a wrong answer rather than an error; DuckDB
refuses a parameter dict carrying a name the statement does not use, which is
what makes the pairing self-checking.

One more, from the client rather than the engine: `to_timestamp()` returns
`TIMESTAMPTZ`, and converting one to Python **requires `pytz`** — a second
non-stdlib dependency, acquired by writing an ordinary date histogram. Epoch
arithmetic (`CAST(ts/86400 AS BIGINT)*86400`) avoids it and avoids asserting a
time zone the data does not carry.

### Concurrency: the door appends while a query reads

Measured, on a 315 MB file with an `O_APPEND` writer running throughout, which
is exactly what `Store._append` does:

- Five successive reads all succeeded and each returned a **larger** count
  (400 217 → 400 337) as the writer advanced. No error, no torn read, no lock.
- Two reads two lines apart return two different counts, so **DuckDB re-reads
  the file every time**. Nothing is cached — which is the property this server
  already required for its own reason (a window's state is a function of `now`,
  so a cached report announces as open a window that closed an hour ago) and
  which here comes for free rather than needing an invalidation nobody would
  maintain.
- A torn final line fails a strict read outright and becomes a NULL row under
  `ignore_errors`; both are handled above.

### Installing DuckDB, and what happens without it

```bash
python3 -m pip install duckdb        # macOS, Linux, Windows; wheels for all
brew install duckdb                  # the CLI only -- the Python module is pip

# or don't install it at all, and let the container hold the pin:
docker compose -f server/compose.yml run --rm suite
```

It is a single wheel with no system dependencies. **It is the first non-stdlib
dependency in this project and it is permitted in one place: this server.**
`claudio usage` and everything under `usage/` read the same JSONL with the
standard library, offline, with no server and no daemon, and a test walks the
whole of `usage/` and asserts nothing there imports it. `claudio` itself gains
nothing at all.

Without it, `srv/store.py` raises `DuckDBMissing` naming the install command —
never an empty result, because "0 requests" is a perfectly plausible answer for
an account that has not shipped yet, and a storage layer that degrades into one
is indistinguishable from a working store over a quiet account.

### Shipping this to Wazuh, later

Deferred, and no code exists for it. When it arrives, a Wazuh agent tails the
same `accounts/<uuid>/*.jsonl` with `<log_format>json</log_format>` and nothing
in this directory changes — which is the point of the files staying the record
of truth. It is written down here so the next person does not add a second
writer to make it possible.

## The door

```bash
python3 server/srv/serve.py --root /var/lib/claudio-usage --port 8787
#   the door  POST http://127.0.0.1:8787/v1/ship
#   the API   GET  http://127.0.0.1:8787/api/v1/   (read-only)
#             no readers.json -> no token asked, and loopback ONLY:
#             any other bind refuses to start, exit 4
#             --no-api removes the API entirely
```

### Setting it up: `tokens.json`, which is the only thing you have to write

The door starts with **no** token → account mapping and refuses every batch
`401` until it has one. It says so on stderr on every start, twice, because a
door that accepts nothing and a door nobody is shipping to look identical from
the outside:

```
[door] WARNING no usable token -> account mapping in <root>/tokens.json;
       every batch will be refused 401
```

The file is a flat JSON object, `{"<token>": "<account uuid>"}` — a JSON object
rather than claudio's own `key=value` idiom because a token is any string the
operator pastes and `key=value` would have to guess where the key ends. Its
default path is `<root>/tokens.json`; `--tokens <path>` moves it. Several
tokens may name one account: that is rotation, and several machines.

The account UUID is the one in that account's own Claude Code config, which is
where the shipper reads it from too, so both ends come from one source:

```sh
# on the CLIENT machine, for the account you are going to ship
jq -r .oauthAccount.accountUuid "$(claudio account path work)/.claude.json"
```

```json
{
  "t-9f2c": "1b8d5f4e-0000-4a1b-9c3d-6f2e7a0b1c2d"
}
```

...and the matching two lines in the client's **global** conf
(`~/.claudio/claudio.conf`), which is the only layer either key is read from:

```
account.work.ship_url=http://ingest.lan:8787/v1/ship
account.work.ship_token=t-9f2c
```

Behind [the proxy](#the-four-way-split) that is the published route instead —
same bytes, same token, one hop further out:

```
account.work.ship_url=https://claudio.example.com/sender
```

`/sender` is an exact-match location with a URI in its `proxy_pass`, so the
external name is the user's and the internal one stays the wire protocol's.
It reaches the **ingest** service, which is the only process in the stack that
can write a byte.

Token values are never logged, never echoed in a response and never written to
the store. A mapping the door refuses is reported by its **position** and by
the account it named — `entry 2: '…' is not a usable account UUID` — so a typo
is fixable without the file's secrets appearing in a terminal someone
screenshots.

> The token is a **routing label**, not a security boundary: it says which
> tenant a batch belongs to and nothing more. There is no mTLS, no CA, no
> expiry and no rate limiting here. TLS exists only at the proxy, and by
> default it is a **self-signed** certificate that proxy generated for itself.
> The read API asks for a **reader** token — a separate file, `readers.json`,
> because a machine that ships is not thereby entitled to read — and on any
> non-loopback bind it refuses to start without one. See
> [What is deliberately not here](#what-is-deliberately-not-here).

Everything is under **`/api/v1/`**. The unversioned `/api/overview|windows|
window|requests|breakdown|diagnostics` routes are **gone** and answer 404 with
`remedy: "the API is /api/v1/; see /api/v1/capabilities"`. Their only consumer
was `srv/ui.py`, which is deleted; keeping a second envelope alive for a client
that does not exist is ceremony that has to be tested for ever. `GET /` is a
404 and `GET /healthz` keeps its **exact** shape — unversioned, unenveloped,
because operators and scripts read that one.

| endpoint | what it answers |
|---|---|
| `GET /api/v1/capabilities` | the self-description: routes, refusal vocabulary, coverage bases, what every `null` means, the constraints, the engine and whether it is installed |
| `GET /api/v1/fields` | the field schema — role, SQL type, groupable, and what this store has actually observed per column |
| `GET /api/v1/values?fields=` | value distributions for the sidebar, over the **filtered** result, with the never-observed columns listed rather than omitted |
| `GET /api/v1/search?account=&stream=a\|b&where.<col>=&since=&text=&cursor=&verify=` | a keyset page of rows with its coverage; `stream=b` is served from the reconciler |
| `GET /api/v1/aggregate?account=&by=` | one account's rows grouped by one column, coverage beside it |
| `GET /api/v1/aggregate/per-account?by=` | the same, fanned out per account, with **no total** |
| `GET /api/v1/histogram?account=&interval=&metric=` | epoch-aligned buckets, zero-filled |
| `GET /api/v1/lookup?field=request_id&value=` | one id to its rows, and what a miss does not mean |
| `GET /api/v1/openapi` | an OpenAPI 3.1 document, derived from the route and parameter tables. Paths and types only: it cannot express the `outcome` vocabulary or what a null means, and says so, pointing at `capabilities` |
| `GET /api/v1/accounts` | one entry per account: open windows, the last closed one, the identity's labels, the account's own silence |
| `GET /api/v1/windows?account=&kind=` | every reconstructed window, the gaps, the fitted rate and its basis |
| `GET /api/v1/window?account=&kind=&resets_at=` | one window, its partitions, the requests inside it and the samples that built it |
| `GET /api/v1/diagnostics` | refusals by name, schemas, absent fields, unreadable batches, unknown keys, the door's counters |
| `GET /api/v1/health` | the same question in **more detail**, behind the reader token: the engine's version, and the command that installs it when it is missing |
| `GET /healthz` | **unversioned and unenveloped**: the door's counters, free disk bytes, refusals by name, and `engine.available` — the only surface that answers without a reader token, so it is the only one monitoring can ask |

### The envelope, and how a refusal is expressed

Every response carries `outcome`, a closed enumeration, and **the discriminator
is key presence — not the status code and not an empty array**.

| `outcome` | `result` | `empty` | `refusal` | HTTP |
|---|---|---|---|---|
| `ok` | present, non-empty | absent | absent | 200 |
| `no-data` | present, empty | present | absent | 200 |
| `filtered-to-nothing` | present, empty | present | absent | 200 |
| `unanswerable` | **absent** | absent | present | 400 / 404 / 503 |

`result` is **absent**, not empty, on a refusal. That is the guarantee: a client
doing `for (row of payload.result.rows)` raises a `TypeError` on a refusal
instead of rendering a tidy empty table — which is the exact failure the shape
exists to prevent. Status codes are advisory; `no-data` and
`filtered-to-nothing` are both legitimate 200s. A client implements:

```js
if (p.refusal) renderRefusal(p.refusal);   // reason, detail, remedy — remedy never null
else if (p.empty) renderEmpty(p.empty);    // says WHICH of the two, and why
else render(p.result);
```

`no-data`, `filtered-to-nothing` and `unanswerable` are **three different
answers with three different messages**, and a query engine returns zero rows
for all three. That distinction is the reason a thin layer sits over DuckDB at
all rather than the SQL being exposed directly.

On `no-data` the `eliminated` map is `{}` and `sole_cause` is `null`: 0-of-0 is
not *your filter emptied it*, and blaming a clause for an empty corpus sends
somebody off to widen a filter that was never the problem. On
`filtered-to-nothing` the `message` quotes the engine's own clause label and
the `remedy` names the **URL parameter** — `remove or widen `where.model`` —
because the label names the predicate and the parameter is the thing the caller
can change.

The `no-data` message comes from a **dedicated field** on the selection, never
from `notes[0]`. `notes` is a list of caveats: `page()` unconditionally appends
the keyset-pagination note and `breakdown()` the list-rates one, so mining the
first note told a user whose search was empty about cursors and late arrivals,
and a user whose breakdown was empty about API list rates — while the generic
sentence, whose own comment explains that it is worded to be true of both a
missing and a present-but-empty ledger, was unreachable on both routes. Only
the branch that can actually tell those two apart sets the field, which is what
makes the fallback reachable at all. A present-but-empty or torn-only ledger is
the ordinary state of a store mid-first-write.

### Coverage, and why `null` alone is not enough

`x || 0`, `Number(null)`, `d3.sum` and every charting library in existence turn
a JSON null into zero without a word. So the tri-state is carried by a
**boolean** and the null corroborates it:

```json
"coverage": {
  "known": false, "fraction": null,
  "basis": "no-attestation", "basis_is": "unknown",
  "notes": ["coverage is unknown, not zero: no machine on this account has sent an attestation…"],
  "placement": {"rows_total": 812, "in_closed_windows": 640, "in_open_windows": 122,
                "unclaimed": 44, "undatable": 6, "placed_fraction": 0.7881},
  "windows": {"touched": 4, "closed": 3, "with_coverage": 0, "attestations_n": 0},
  "hosts": {"reporting": [], "dark": [], "pending": [], "silent": ["mac-1"]},
  "describes": "the 812 rows this result matched, not the account"
}
```

`known` is `true` **if and only if** `fraction` is not null, both directions
asserted. `fraction` is never `0.0` while `known` is false — `0.0` is the
positive claim *nobody was listening*, and the two must not be reachable from
one another. `basis_is` is `measured` for exactly one basis, `attestations`,
and the mapping is served at `/api/v1/capabilities.coverage_basis` so no client
holds a copy.

**There are two coverage shapes and both honour this now.** Per-window coverage
— `windows[].coverage`, `window.coverage`, `accounts[].last_closed.<kind>.coverage`
— is the reconciler's own object, and it went on the wire raw: no `known`, no
`basis_is`, and a `basis` (`unknown`, `no-attestation-names-it`) that was in no
served catalogue. Those are precisely the rows carrying `attributed_pp` and
`residual_pp`, so the object the design calls insufficient was sitting beside
every attributed figure in the store. And **both halves of the tri-state are
live in that slot**: `fraction: 0.0` with basis `no-attestation-names-it` is the
positive claim that an attesting host did not name this window, `fraction: null`
is *nobody said*, and on the wire only the JSON null separated them. Everything
the core computed still travels unchanged — `spans`, `uncovered`, `lower_bound`,
the four host lists — with `known` and `basis_is` **added**, because a reader
that reshapes a number is a second opinion about it.

`coverage_basis` now declares every basis the core can emit, and the suite
**derives** that list out of `srv/coverage.py` and `srv/query.py` with `ast`
rather than keeping a copy — a hand-maintained catalogue is a second place to
forget, and the thing it forgets renders as something else. The one basis the
core parameterises (`attestations-with-3-unreadable-span(s)`) cannot be an exact
key, so it is served as `coverage_basis_prefixes`, mapping to
`measured-with-unreadable-spans`: a fraction built from spans some of which
could not be read is a floor even by the standards of a measured one. Coverage travels on `search`, `aggregate`, `histogram`, `window`,
`windows`, `lookup` **and on their empty results** — an empty answer still has
a placement and an attestation story.

`/api/v1/capabilities.null_means` maps a JSON path to what a `null` there
means, as **data**, so a generic renderer consults it and adding a key is a
one-line edit rather than a release of the front end.

**No payload holds a list the data did not produce.** Accounts, columns, window
kinds, facet values and group-by options all arrive derived from the rows in
front of the reader. That is not tidiness — a hardcoded facet list is precisely
how six `query_source` names Claude Code has never sent survived in this
repository while *reading as coverage*. A client that hardcodes one reintroduces
the defect on its own side, and the payload gives it no excuse to.

**Nothing is cached.** A window's state is a function of `now`, so a report
derived at T and served two hours later reports as *open* a window that closed
an hour ago — and "as of 14:02" is a caveat nobody reads on a figure that looks
live. Every request re-derives from byte zero, which is the same property that
lets a week-late shipment correct a closed window, and the measured cost
(`derived_ms`) travels in every payload.

### What a front end must honour

These were the page's rules. They are the payload's now, and each is asserted
by a test that reads the payload rather than a renderer.

- **`coverage` travels beside every attributed figure**, on every row,
  including the refused ones. Today it says `fraction: null` — *nobody said* —
  because nothing emits an attestation. **It is never `0.0`**, which would be
  the claim that nobody was listening, and a client must not render null as
  zero. When `lower_bound` is set a host is *pending* and the figure can only
  rise, so it reads *at least N%*; `fraction: 0` **with** a pending host is
  "nobody has said yet" and carries no percentage at all.
- **The explanation is the server's own `coverage.notes`, verbatim and in
  order.** A fixed sentence written into a client goes stale against a store
  that contradicts it — which is exactly what happened here, a page asserting
  that nothing in this system emits an attestation, printed beside a strip
  reading `basis: attestations` and a diagnostics screen counting three.
- **`meta.store_problems` is in *every* payload, not the diagnostics one.** A
  batch the reader could not put back together otherwise makes an account
  render byte-identically to one that never shipped: "no window of this kind
  has been reconstructed", with the store holding its records and the reader
  knowing it failed to read them. It describes **the store, as of this
  answer** — one entry per problem, carrying a `count` of the lines, deduped
  across the two readers on `(reason, account_uuid, file)` and reset before
  every request. It was a list on a process-lifetime object, appended to and
  never cleared: measured against a live door over a store holding exactly
  **three** unparseable lines, 1 500 `/search` requests produced **3 116
  entries claiming 3 118 torn lines**, the payload grew from 51 kB to 1.34 MB
  on every response including `/health`, RSS went 11 MB → 143 MB, a repaired
  file kept being reported until the process restarted, and a query scoped to a
  healthy account named another account's tear. Every entry now carries
  `reason` — the same key `snap.problems` uses — because a client rendering
  `p.reason` showed `undefined` for every problem the storage half produced.
  A torn `ledger.jsonl` is also named on the reconciler-only routes
  (`/accounts`, `/windows`), from a tail scan of the bytes past the last
  acknowledged manifest: those bytes are nobody's to resend, whereas a fragment
  *before* the door's first accepted range is the interrupted write
  `Store._append` terminates on purpose and costs nothing.
- **`open` and `last_closed` are separate keys**, so a client cannot silently
  fall back from one to the other. The slot that answers *how much of my plan
  is gone* needs an open window; an account silent for six weeks used to render
  `92 %` at full width. `last_sample_age_s` is on every account and
  `stale_after_s` is on the document, which says outright that it is a display
  threshold and not a measurement — nothing in any capture states how long a
  machine may be quiet before its last reading stops describing the present.
- **A refusal arrives whole**: `reason`, `detail`, `remedy` and the
  `catalogue` that lists every reason, with a 4xx and **no `result` key**. A
  500 `reader-failed` names the exception **type** and nothing more: several
  routes take caller-supplied identifiers, and an engine exception over one of
  those carried the whole generated statement and DuckDB's candidate-binding
  list into `detail`, on an API that asks for no token. The operator still gets
  every byte of it, on the process's own stderr, which is where a fault in the
  reader belongs.
  `remedy` is never null — a refusal with no action in it reads as a fault, and
  a reader who cannot tell a broken server from an unanswerable question stops
  believing either. **Including the refusals nested per account** under
  `result.accounts[<uuid>].refusal` on `/aggregate/per-account`, which is the
  one route that reports a refusal per account and therefore the one place a
  client actually meets several at once. That dict was built by hand, bypassing
  the builder that carries the never-null fallback, so an ordinary typo
  (`?by=nonsense`) produced three nested refusals whose `remedy` could be
  `null` while every top-level assertion still passed. It is built through the
  same function as every other refusal now, so the guarantee is structural
  rather than repeated. Flattening one into an empty list is how a question this
  data cannot answer becomes a tidy table with nothing in it.
- **No total spans two accounts anywhere**, not even for tokens, and the
  refusal happens before any SQL runs. A key-name walk is not enough on its
  own and did not catch the one hole there was: `/api/v1/histogram` with no
  `account=` opened every ledger in the store and emitted
  `coalesce(sum(cost_usd_reported), 0)` over the lot — 200 ok, no refusal,
  0.2466444 across three plans with three denominators, exactly the per-account
  fan-out summed — while `/aggregate` refused the identical question by name.
  A bucket's `value` is not called *total*, so the name-based guard could not
  see a value-based hole. The guard lives in `store.py` now, so both callers
  inherit it, and the test asks every stream-A route its account-less question
  and compares against the per-account answers.

  **The same hole was open on `/values` and `/fields`, and it was opened by the
  DuckDB swap itself** — neither route existed before it. The guard was reached
  only from `breakdown()` and `histogram()`; `facets()` and
  `field_availability()` never reached it, so
  `/api/v1/values?fields=model` answered `{claude-sonnet-5: 5,
  claude-haiku-4-5: 3}` with `rows_with_a_value: 8` over three accounts —
  byte-identical to the per-account fan-out summed by hand — and `/fields`
  reported one pooled `cardinality` per column, `email` among them with
  `distinct_n: 3`. Both refuse now, from `store.py`, so a fifth caller inherits
  it too. One exception is named rather than implied: `breakdown(by=account_uuid)`
  is the one cross-account grouping that IS allowed — one bucket is one account
  and there is no total row — and it asks the same layer for that column's
  cardinality on the way past, so it passes `pooled=True`. A test asserts
  `srv/api.py` never passes it, because an escape hatch reachable from the layer
  that serves URLs is the hole again wearing a tidier name.
- **An unrecognised URL parameter is refused, never dropped.** `spec_from`
  picked the keys it knew and never looked at the remainder, so a misspelling
  vanished and the **unfiltered** answer came back as `outcome: ok`. Measured
  on the real fixtures, alpha's 3 rows against the store's 8:
  `?where.model=claude-sonnet-5` matched 2, while `?wher.model=`,
  `?wheres.model=`, `?sinc=`, `?untill=`, `?tex=`, `?limitt=`, `?ordre=` and
  `?bogus=` each matched 3. Nothing in the payload said a key had been dropped
  — only the `spec` echo went quiet, a diff the client would have had to
  compute for itself. The sharpest case is the scoping parameter, now that
  `?account=` is the guard that stops a cross-account total:

  ```
  ?stream=a&account=<alpha>       -> 3 rows, 1 account,  1 email
  ?stream=a&account_uuid=<alpha>  -> 8 rows, 3 accounts, 3 emails, ok
  ```

  `account_uuid=` is the module's own keyword and the spelling
  `store._crosses_accounts` put in its remedy until `wire_remedy()` landed, so a
  client that followed the remedy verbatim had its scoping silently discarded.
  `query.compile_query` — the declared oracle — already refused an unknown spec
  key by name, and `unknown-query-key` was already published in
  `capabilities.refusal_reasons`: the contract promised a refusal the wire
  could not produce. The check sits in `Api.handle`, not in `spec_from`,
  because six routes never call `spec_from` and were equally silent.
  `where.<col>=` and `not.<col>=` are matched by prefix and only on the routes
  that build a spec — honoured everywhere, `/accounts?where.model=x` would
  answer the unfiltered list as `ok`. **`ENDPOINT_PARAMS` is derived from the
  enforced table rather than listed a second time**, because the two copies had
  already drifted on four routes, and a drift now decides whether a request
  works rather than merely what a client is told.
- **A correctly-spelled parameter a route cannot honour is refused too.**
  `/aggregate/per-account?account=<uuid>` asked to scope a route whose whole
  purpose is to fan out over every account, and the handler granted it by
  popping the key: 200 `ok`, three accounts in the result. It names
  `/aggregate?account=<uuid>&by=<column>` in the remedy instead.
- **An account nobody has ever seen is a named 404**, not a 200 whose message
  asserts it exists. `?account=<ghost>` answered `no-data` with *the shipper
  may not have reached stream A on any machine yet* — byte-identical, bar the
  uuid, to the answer for a real account that has shipped stream B and not
  stream A. `unknown-account` was already published and already reachable on
  `/windows`; it is reachable on both halves now, and `no-data` keeps the case
  its message actually describes.
- **`?account=` is never joined into a path.** `os.path.join` discards its
  first component when the second is absolute, so `?account=/private/tmp/x`
  read `/private/tmp/x/ledger.jsonl` — proven against a live door: `lookup`
  returned the full foreign row including an email address and a cost,
  `diagnostics` returned every JSON key name in that file, and `search` leaked
  its line count as `scanned`. The read API is unauthenticated by design; its
  stated contract is *every record in the store*, not every `ledger.jsonl` this
  process can read. `Paths.stream` refuses by **membership** of the account
  list, with a realpath containment check behind it for a symlink planted
  inside the store, and `store.lookup` compiles the account into the query as
  well as into the file list.
- **The residual is a measurement**, not a gap to close — printed with its
  sign, never redistributed, and never stacked into a tidy 100%. A zero
  residual on the window that pinned the rate is arithmetic, and
  `rate_is_from_this_window` says so on the row. On the real fixtures that is
  *every* closed window there is.
- **Plan percentages are lower bounds** over quantised, possibly-stale
  snapshots — a floor, never a reading. `store.LOWER_BOUND_NOTE` is the
  sentence; the only sound cross-machine operator is `max`.
- **A `synthetic` row is counted and named** the moment it enters a result, and
  a column that has never carried a value is listed with its zero rather than
  offered as an empty control that reads as coverage.
- **Every 7d row carries a `length ASSUMED` flag**, because `L7_CONFIRMED` is
  `False` and a 7-day window's start — and therefore its coverage fraction and
  its request set — is assumed with its length.

## Running omini against this server

**omini is the official front end and it is a separate project.** There are two
ways to run it, and the difference is only where the process lives — the API
contract, the envelope and the guardrails above are identical in both.

**Bundled.** omini brings its own copy and points it at a store on the same
machine. This is the single-user shape: one command, loopback, nothing exposed.
The door and the API are the same process, so a shipment and the page that
reads it cannot disagree about which store they mean.

```bash
python3 server/srv/serve.py --root /var/lib/claudio-usage --port 8787
# then run omini with its API base pointed at:
#   http://127.0.0.1:8787/api/v1/
```

**Attached.** An omini that already exists — one instance, several people —
points at a door running somewhere else. The server holds no session, no user
and no page, so nothing about it changes between the two modes; what changes is
who can reach the port.

```bash
python3 server/srv/serve.py --root /srv/claudio-usage --host 10.0.0.5 --port 8787
```

> Attached mode puts the read API on a network. **It asks for no token and
> serves every record in the store, email addresses included.** The bearer
> token is the door's routing label for `POST /v1/ship` and nothing more; there
> is no auth in front of `GET`, no TLS and no rate limiting. Binding a
> non-loopback address prints two warnings saying exactly that. Put it behind
> something that authenticates, or keep it on loopback and let omini be
> bundled. `--no-api` serves the door alone.

Either way omini **hardcodes nothing**: `GET /api/v1/capabilities` is the
self-description, and the routes, the refusal vocabulary, the coverage bases,
the meaning of every `null` and the constraints all come from it. A client that
keeps its own copy of any of those is a client that can be confidently wrong
about a store it has never read.

## Wazuh — deferred, and nothing here changes when it arrives

There is **no Wazuh code in this repository** and none is planned as part of
this work. It is written down so the next person does not add a second writer
to make it possible.

When it arrives, a Wazuh agent tails the same files the reconciler reads:

```xml
<localfile>
  <log_format>json</log_format>
  <location>/var/lib/claudio-usage/accounts/*/ledger.jsonl</location>
</localfile>
```

That is the whole integration, and it works precisely because **the JSONL stays
the record of truth**. Nothing has to be exported, mirrored or dual-written:
the agent reads the bytes the door appended, in the format the door appended
them. A design that had moved truth into a database would need a second writer
here — and a second writer is how the two copies begin to disagree.

## What is deliberately not here

Transport security, `calibrate-q`, and any authentication in front of the API.
A front end: **omini** is that, in its own repository, and `srv/ui.py` was
deleted rather than ported. A Wazuh integration: deferred, as above.

And **no maintained index**. The seam is stated (`narrowing()`), the storage
layer that honours it exists (`srv/store.py`), and the projection that would
make it interactive at five-year volume is measured and costed above — 36
seconds to build, 22× smaller, 115× faster, deletable. What is not here is
anything that keeps such a projection up to date, because the crossover is
about 1.1 GiB and no real store is near it. Building the maintenance for a
problem nobody has yet is how the record of truth quietly becomes a database.

The **core** still draws nothing and still knows nothing about storage:
`srv/query.py` states what a query means, `srv/duck.py` compiles it and
`srv/store.py` runs it, and neither `reconcile.py` nor anything below it has
learned that any of the three exist.
