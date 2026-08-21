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
| no duckdb | **1164 passed, 0 failed, 44 skipped** |
| duckdb | 2082 passed, 0 failed, 0 skipped |

> Both totals drift upwards every time an assertion is added, and they have:
> they were first written as 1129 and 2047, were 18 out by the time the
> container work was finished, were 1154 and 2072 mid-packaging, and are 1164
> and 2082 as of the packaging run that made the release ready.
> **The figure that carries the argument is the gap, not either total** — it
> was 918 at every one of those three measurements, because every assertion
> added since has needed no engine. If you re-measure, expect the totals to
> have moved and check that the gap has not.

**918 assertions — 44% of the suite — did not run in the first case.** They are
announced rather than silent, so this is not the cardinal sin; but `1164
passed, 0 failed` is the sentence a reader acts on, and 44 `SKIP` lines scroll
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
exit 0. A developer without the engine is still entitled to the 1164 assertions
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
| duckdb | 169 caught, 0 survived, 0 bad patches | — |

> The two rows are not from one run, and the arithmetic says so: 118 + 42 + 1
> is 161, and the matrix is 169 rows now. The no-duckdb row is the measurement
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

**Absent `readers.json` means no reader auth.** That is allowed on loopback,
with a warning at startup, and it is the development default. On any other bind
the door **refuses to start**: the read API returns every record in the store,
email addresses included, so how exposed it is and who may read it are decided
together or the exposure wins by default. `--no-api` is the other way out.

`_is_loopback` compares the bind address as text rather than resolving it. A
name that resolves to 127.0.0.1 today can resolve elsewhere tomorrow, and this
is the check that decides whether an unauthenticated read API may exist.

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
| the suite runs **with** the engine and self-skips nothing | `run --rm suite` → `2082 passed, 0 failed, 0 skipped`, exit 0 — the same count as a host venv, so the container is not a different tree (re-run at every pass since: 2065 when the image was built, 2072 mid-packaging, 2082 now, matching the host venv of that day each time) |
| a skipping run refuses, and `--allow-skips` is the only way past it | exit 1 on an interpreter with no engine, naming the count and the flag; exit 0 with the flag, and exit 0 here where nothing skipped — the `suite` service still passes `--require-duckdb`, which is now redundant and still accepted |
| the `:ro` mount is genuinely read-only | `touch /repo/…` inside → `Read-only file system`, exit 1; the host checkout was unchanged afterwards |
| the mutation matrix runs through the same image | `run --rm mutate` with a one-row filter → `baseline: 2065 passed, 0 failed, 0 skipped`, row caught. That figure is the container run as taken; the **full** matrix has since been re-run on a host venv at baseline 2072 — 169 mutations, 169 caught, 0 survived, 0 bad patches |
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

It is also **not a production deployment, and not a security boundary.** The
door asks for no token on its read API and serves every record in the store,
email addresses included. `--host 0.0.0.0` in the `door` service is not a
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
#   the API   GET  http://127.0.0.1:8787/api/v1/   (read-only, no token)
#             --no-api removes it
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
account.work.ship_url=http://door.lan:8787/v1/ship
account.work.ship_token=t-9f2c
```

Token values are never logged, never echoed in a response and never written to
the store. A mapping the door refuses is reported by its **position** and by
the account it named — `entry 2: '…' is not a usable account UUID` — so a typo
is fixable without the file's secrets appearing in a terminal someone
screenshots.

> The token is a **routing label**, not a security boundary: it says which
> tenant a batch belongs to and nothing more. There is no TLS, no mTLS, no CA,
> no expiry and no rate limiting here, and the read API asks for no token at
> all. See [What is deliberately not here](#what-is-deliberately-not-here).

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
| `GET /api/v1/accounts` | one entry per account: open windows, the last closed one, the identity's labels, the account's own silence |
| `GET /api/v1/windows?account=&kind=` | every reconstructed window, the gaps, the fitted rate and its basis |
| `GET /api/v1/window?account=&kind=&resets_at=` | one window, its partitions, the requests inside it and the samples that built it |
| `GET /api/v1/diagnostics` | refusals by name, schemas, absent fields, unreadable batches, unknown keys, the door's counters |
| `GET /api/v1/health` | the same question as `/healthz`, through the envelope |
| `GET /healthz` | **unversioned and unenveloped**: the door's counters, free disk bytes, refusals by name |

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
