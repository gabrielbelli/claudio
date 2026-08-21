# Stream A: receiving Claude Code's native OTLP export

Stream A is what Claude Code exports by itself under `logging=local`. It already
works, including headless `claude -p`. Nothing here changes Claude Code; this is
only a **receiver**, so the export can be seen rather than assumed.

There is now exactly one. `recv/otlp-recv` used to be the "fallback" beside a
Docker collector; the container is gone and this is the real capture. Two
receivers were two implementations of one decision — what to keep — and that
duplication is how three fields were silently dropped.

| | `recv/otlp-recv` |
|---|---|
| Needs | Python 3 stdlib only |
| Protocol | **http/json only** — `otel_protocol=http/json` |
| Port | 4318 (`http_port` in `~/.claudio-usage/config`) |
| Binding | 127.0.0.1 only; a non-loopback `--host` is **refused** |
| Keeps | every log payload, verbatim, in `raw/api_request.jsonl` |
| Metrics | accepted, counted and discarded |
| Lifecycle | spawned by `claudio run`, exits itself when idle |

Both streams only ever *record*. Stream A is one row per request; relating those
rows to the plan percentages of stream B is attribution, which needs the whole
account and happens server-side — see [`../README.md`](../README.md).

If nothing is seen, the export is failing before it leaves Claude Code: run
`claude --debug`, which is the only place its OTLP exporter errors are printed.

---

## Running it

Normally you do not. `claudio run` spawns it before it `exec`s claude, the
status-line shim keeps it alive, and it exits by itself once no records have
arrived for `idle_minutes` **and** no session is still open. By hand:

```bash
claudio usage collect start   # spawn one and wait for it to bind
claudio usage collect status  # state, pid, port, live sessions, raw bytes
claudio usage collect stop    # SIGTERM, then ingest
claudio recv tail -f          # render the capture as it arrives
```

`claudio recv serve` runs one in the foreground, which is what you want when
you would rather watch it than read a file. `curl localhost:4318/healthz`
returns the running counters.

### Why not a service, and why not otelcol-contrib

Three arguments were made for an installed launchd/systemd service and all
three are settled:

1. Its flush argument was **measured false** — see the box below.
2. Its coverage argument was near-empty. claudio writes the OTLP variables only
   into the environment of the `exec`ed claude, so stream A does not exist
   outside claudio-launched sessions: an always-on daemon spends its life
   listening to an unplugged wire.
3. `launchctl`/`systemctl` cannot be stubbed in a hermetic test suite, and
   everything in this repo is pinned by one.

And not `otelcol-contrib`, which would otherwise be the obvious binary: the
design requires **idle self-exit**, which it cannot do without a second watcher
process — the daemon returning by the side door. It is 93–107MB with no
Homebrew formula. Python is already a dependency here and is testable in the
suite. otelcol wins the argument back the day remote mTLS and a durable retry
queue matter; that swap is a later, separate decision.

### Pointing Claude Code at it

This server speaks OTLP/HTTP JSON on 4318 and **cannot** accept gRPC — HTTP/2
framing plus protobuf is not something `http.server` can be talked into — and
claudio's defaults are now the same two values, so `logging=local` alone
reaches it. The protocol and endpoint are written out here because either can
be overridden in any of claudio's three config layers:

```
logging=local                         # both streams; the only switch there is
otel_protocol=http/json               # claudio's default
otel_endpoint=http://localhost:4318   # claudio's default
```

`http/json` is one of the three documented values for
`OTEL_EXPORTER_OTLP_PROTOCOL` (`grpc`, `http/json`, `http/protobuf`) — confirmed
against `code.claude.com/docs/en/monitoring-usage`, not assumed.
`claudio usage doctor` compares both values against what `claudio env` actually
exports in the current directory, because an export aimed at a port nothing
listens on fails quietly by design.

> **A protobuf body is answered `200`, recorded in `recv.warn`, and not
> decoded.** Returning an error would be more honest about the data loss, but
> OTLP clients treat 4xx/5xx as retryable and would spin. The `recv.warn` line
> names the setting to change, and the next `claudio run` prints it.
>
> `recv.warn` and not `recv.err`: the receiver is *running* when it writes
> this, and `recv.err` is the flag claudio's supervisor reads as "a start fault
> is already reported, do not spawn". One undecodable body written there
> disarmed the respawn for that session and every later one.

---

## Durability, and the wrong turn that produced it

> **There is no buffered tail to lose.** Three documents claimed the OTLP file
> exporter buffers ~4KB pages, does not flush on idle, and that a `docker kill`
> discards the tail as missing data. That is false, and it was reproduced wrong
> twice. Measured twice on contrib 0.158.0: 5 records POSTed, host-side `wc -c`
> on the bind-mounted file read **zero bytes** — then `docker kill` (SIGKILL, no
> grace period) and `docker rm -f`, and the file held all 5 records, 3355 bytes.
> The zero was macOS VirtioFS bind-mount coherence lag: the host could not see
> bytes that were already durable. With `rotation:` configured the exporter
> writes through lumberjack, which is unbuffered per write.
>
> What a graceful stop buys is the *label*: a span in `uptime.jsonl` is marked
> `clean` only when the stop was **observed** graceful, and that label is what a
> server is told about which machines were listening. Kill the receiver and you
> lose the label, not the records — a reporting loss, not an integrity one.
>
> **The lesson worth carrying: never conclude anything about durability from
> what the host sees through a bind mount.** Going native removes the bind
> mount, so the artefact cannot recur.

The native receiver keeps the property by construction rather than by luck:

- each accepted payload is **one `write(2)` on an `O_APPEND` file before the
  200 goes back**. A regular file opened `O_APPEND` writes atomically at any
  size, so concurrent requests cannot interleave and a process killed a
  microsecond after the response has lost nothing.
- a write that fails answers **503**, so the client keeps the batch and retries.
  A 200 over a failed write is the silent loss this whole design is arranged
  against.
- there is no `fsync`. That defends against the host losing power, not against
  this process dying, and the client's own retry queue is a better answer to a
  machine that went down mid-write than a stall on every record.
- `clean` in `uptime.jsonl` is written by the receiver *itself*, on its way out.
  Nothing `collect stop` does can write a clean span, so the label cannot be
  optimistic — which is the inverse of the old bug, where `docker stop`
  returning 0 after escalating to SIGKILL was recorded as graceful.

---

## What it keeps, and where the deciding happens

**Nothing is filtered or allow-listed at the door.** The Docker collector
dropped every event that was not `api_request` and ran the survivors through a
`keep_keys` allow-list; `cu/otlp.py` then did the same two jobs again one layer
in, because it stores exactly the columns the ledger has and skips every record
whose `event.name` is not `api_request` — counting and *naming* what it
skipped. One decision implemented twice is how `prompt.id`, every user `--tag`
and `agent.name` were each lost in silence: each half looked right beside the
other, and the file everyone read was the output of both.

So the keeping decision lives in `cu/otlp.py`, the only place that knows what a
ledger row is, and this file writes the bytes as they arrived. That closes the
blind spot the audit named — `raw/api_request.jsonl` used to be the collector's
own **output**, so nothing downstream could see what never arrived.

Counting at the door is the other half:

```
records: 12 accepted -> 12 written; events: api_request 5, user_prompt 4, assistant_response 3
  note: 1 attribute(s) arrived that the ledger has no column for: cost_usd_micros
```

- **every distinct `event.name` is named.** No `api_request` at all, while other
  events arrive, is drift and gets its own clause. Claude Code sends the bare
  `api_request`; the prefixed guess `claude_code.api_request` dropped every real
  record for an evening, and the symptom was indistinguishable from an idle
  machine.
- **every attribute key with no ledger column is named.** The comparison set is
  `otlp.STORED_ATTRS`, and a test derives the reader's own `a.get("…")` calls
  from the source and compares the two, so the list cannot drift into either a
  missed field or a permanent false alarm.

The counts are facts, never losses: `user_prompt` and `assistant_response` are
not ledger rows by design and every real session sends them.

Two costs of storing verbatim, stated rather than buried: the file grows faster
(hence rotation, at the 32MB/16-backup figures lumberjack was configured with),
and the log record **body** is stored as it arrives. Claude Code leaves it empty
unless you set `OTEL_LOG_USER_PROMPTS=1`, which is your own opt-in; a silent
redaction here would be a second, invisible policy of exactly the kind this
section argues against. Mode 0600, in a 0700 directory, on loopback.

---

## Lifecycle contract

Eleven paths under `~/.claudio-usage` (or `$CLAUDIO_USAGE_DIR`), declared once in
`cu/config.py` because the other half of this contract is written in POSIX sh
and cannot import them:

| path | shape | who writes it |
|---|---|---|
| `recv.pid` | 4 lines: pid, token, port, started-at | the receiver, temp+rename |
| `recv.lock` | a **directory** (`mkdir` is the POSIX atom) | the spawner; released by the receiver once bound |
| `recv.err` | one timestamped line | the receiver (start faults only); printed by the next `claudio run`, and the supervisor's do-not-spawn flag |
| `recv.warn` | one timestamped line | the receiver, while running; printed the same way, and deliberately not a spawn gate |
| `recv.starts` | one epoch per failed start | the receiver; truncated by a successful bind |
| `recv-counters.json` | last counter snapshot | the receiver, on each watchdog tick and at exit |
| `sessions/<pid>` | one file per session | `claudio run`, before `exec claude` |
| `raw/api_request.jsonl` | the capture, + rotations | the receiver |
| `ship.err` | one timestamped line | the shipper: nothing was delivered, offsets unchanged, so it is a retry |
| `ship.warn` | one timestamped line | the shipper: delivered, but some rows can never be placed |
| `machine-id` | one opaque hex id | the shipper, on first use; the batch manifest's `machine_id` |

**One field per line, never tab-separated.** A tab is IFS whitespace, so a run
of tabs collapses and an empty field disappears, shifting every later one —
claudio learned that in `migrate`. `{ read pid; read token; } < recv.pid` is two
builtins and no fork, which is what a status line rendering several times a
second can afford.

**The token is what makes ownership exact.** `kill -0` is cheap and says "alive"
for a recycled pid; nothing portable is both cheap and exact (there is no
`/proc` on macOS and a start time costs a fork). So the pidfile is removed only
by the receiver whose token it carries — otherwise a receiver shutting down
while its replacement is already binding deletes the new one's pidfile, the
supervisor respawns over a live process, the spawn cannot bind, and the user
reads a `recv.err` about a fault that never happened. `/healthz` serves the
token for any caller that can afford a socket; `doctor` uses it.

**Idle exit needs both conditions**: no records for `idle_minutes` *and* no live
pid under `sessions/`. A timer alone orphans someone reading for forty minutes
with claude still open. A session check alone never exits once a file is
stranded by a crash — so dead entries are swept on every watchdog tick.

**The shipper rides the watchdog tick.** Under `logging=remote` with a
destination configured, `cu/ship.py` runs on every tick and once more,
unconditionally, in the `finally` before the process exits — so shipping needs
no cron, no launchd and no systemd unit, for the same three reasons this
receiver is not a service. The final pass is not a nicety: the tick is up to
`check_seconds` behind, and without it everything written after the last tick
would wait for the next session to start a receiver. `ship.tick` swallows every
exception and writes what it swallowed to `ship.err`, because a traceback
escaping into the watchdog thread would end the tick loop and with it the idle
exit and the session sweep. It reads `ledger.jsonl` and `samples.jsonl` and
**never `raw/`** — that file is larger and can carry prompt and response bodies.

**A crash loop refuses itself.** A receiver that cannot bind dies instantly, and
a supervisor firing several times a second would fork-bomb. Starts that never
reached a bind are counted in a 60-second window; past five, it writes `recv.err`
and exits without binding. The supervisor is expected to carry a cheaper cap of
its own — this is the backstop that holds when the supervisor is wrong, and the
half that can leave a note saying why nothing is recording.

---

## Proving it without spending quota

Do **not** run `claude` to test the plumbing. `recv/synth-otlp` fabricates
payloads in the documented shapes and POSTs them. It is a development tool and
deliberately not a `claudio` verb, so it is run by path —
`usage/recv/synth-otlp` in a checkout, `$PREFIX/libexec/claudio/recv/synth-otlp`
once installed:

```bash
usage/recv/synth-otlp                                   # metrics + logs -> :4318
usage/recv/synth-otlp --endpoint http://localhost:14318 --signal logs
usage/recv/synth-otlp --print                           # inspect, POST nothing
```

> **Its rows now land in the ledger.** There is no separate debug capture file
> any more, so a fabricated payload goes into `raw/api_request.jsonl` beside the
> real ones. That is survivable only because every synthetic payload carries a
> `synthetic=1` resource attribute and `cu/otlp.py` stamps `source:
> "synthetic"` on the row for life — which exists because two generator rows
> once sat in a live ledger indistinguishable from real consumption and became
> the top line of a real account's report. Point it at a throwaway
> `CLAUDIO_USAGE_DIR` if you would rather not find out how well that holds.

Verified output through `tail`:

```
15:26:24 api_request            prompt.id=114c10e2-… cost_usd=0.08158 input_tokens=2
15:26:24 user_prompt            prompt.id=114c10e2-… session.id=2f0eb9cf-…
15:26:33 assistant_response     prompt.id=114c10e2-… duration_ms=8600
```

**The event names carry no `claude_code.` prefix.** They arrive as bare
`api_request`, `user_prompt`, `assistant_response` in the `event.name`
attribute — verified against `tests/fixtures/real-payloads/`. The collector's
filter matched `claude_code.api_request` for an evening and therefore dropped
every real record; this document said the same thing, which is why the bug
survived being read.

All three now reach `raw/api_request.jsonl`, and `ingest` turns **one** of them
into a ledger row and names the two it skipped. That is the design: the drop is
visible, counted and attributable to one file.

---

## What you will actually see when real data arrives

### Metrics (`/v1/metrics`)

| Metric | Unit | Note |
|---|---|---|
| `claude_code.session.count` | count | one per session start |
| `claude_code.token.usage` | tokens | **split by `type`**: `input`, `output`, `cacheRead`, `cacheCreation` — four data points, not one |
| `claude_code.cost.usage` | USD | Claude Code's own figure, computed from tokens at API **list rates**; a Pro or Max plan is never charged it |
| `claude_code.active_time.total` | s | wall time actively working |

Also emitted, not used here: `lines_of_code.count`, `pull_request.count`,
`commit.count`, `code_edit_tool.decision`.

All of these are **accepted and discarded**: pre-aggregated, so no per-request
granularity, and `session.id` on them would make unbounded-cardinality series.
The response is a `200` carrying an empty OTLP success message — `{}` for a
JSON request, zero bytes for a protobuf one, which really is
`ExportMetricsServiceResponse{}` — because a 4xx or 5xx makes the client retry
the same body for ever.

### Log records (`/v1/logs`)

Carried as the `event.name` attribute, not as the record body, and
**unprefixed**:

- `user_prompt`
- `api_request` ← the only one that becomes a ledger row
- `assistant_response`
- `tool_result`, `tool_decision`, `api_error`

Only `api_request` has been captured on this machine; the other four are from
Claude Code's documentation and are named here as expectations, not as
observations. All of them are now **stored** — the ledger is where the choosing
happens.

### Resource attributes

`profile`, `account`, `host`, `email` — these four are claudio's, injected via
`OTEL_RESOURCE_ATTRIBUTES` by `_build_env`, plus any `--tag` the user chose.
Alongside them Claude Code sets `service.name` and `service.version`.

`session.id` and `terminal.type` are **log-record** attributes, not resource
attributes — that is where `cu/otlp.py` reads them from. No captured payload
has carried `user.email`, `user.account_uuid` or `organization.id` at any
level.

> `email` is real PII. It lands in every exported record, and on a metrics
> backend it becomes a label — one series per address.

### Where `prompt.id` appears

`prompt.id` is a UUID v4 set on **every event of one user turn**:
`user_prompt` → `api_request` → `assistant_response` → `tool_result` →
`tool_decision`. It is not on metrics.

**It is recorded, not used.** Nothing here relates stream A to stream B: that
is attribution, it needs the whole account, and it is the central server's job
(see [`../README.md`](../README.md)). What `prompt.id` gives the server is the one
thing timestamps cannot — stream B samples carry a `prompt_id` too, because
claudio's status-line shim records the turn that was in flight when it
rendered, so a percentage observation can *name* the turn beside it instead of
being matched to whichever request happened to be nearest in time. That matters
because a turn's requests straddle the render that observes it (3 turns out of
3 in the captured data), and one real turn missed the observation closing its
own second by **32 ms** — stream B's clock is whole seconds, stream A carries
milliseconds. A record with no `prompt.id` — anything written before the column
existed — simply has none.

`session.id` names the session, not the turn. It is a grouping axis for `show`
and part of the content hash that makes ingest idempotent, and it is not a
correlation key between the streams.

It was discarded twice on the way in — once by the collector's `keep_keys` and
once by `cu/otlp.py`'s own hardcoded attribute list, the same failure one layer
further in. There is one allow-list left now, and what it drops is counted at
the door.
