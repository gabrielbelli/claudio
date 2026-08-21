# claudio usage

**Records what this machine sent, and what the plan said. It interprets
neither.**

Two append-only fact streams land on disk:

* **Stream A — per request.** Claude Code's own OTLP export: tokens, model,
  `duration_ms`, `query_source`, `session.id`, `prompt.id`, and the `cost_usd`
  Claude Code computed, each row carrying the `profile=`, `account=`, `host=`,
  `email=` and `--tag` labels claudio attached.
* **Stream B — per plan movement.** The 5-hour and 7-day percentages, with
  their `resets_at`, as the status line saw them. Recorded by claudio's
  status-line shim.

`claudio usage show` prints stream A grouped by any column or tag. It does not
tell you how much of your plan a profile consumed, and it never will — that
answer is not computable here.

```
Requests this machine sent, by profile

  profile                requests     list $        in       out   cache_rd   cache_wr
  soc-analyst                   3     0.3183      4210      2104     400213       8104
  blueteam                      1     0.1682      1002       500     300010          0

  1 of 4 request(s), $0.0006, were Claude Code's own overhead (generate_session_title).
  counted as your work: repl_main_thread, sdk
  16 plan observation(s) recorded, unattributed.
```

...followed by the standing note that `list $` is Claude Code's own figure at
list rates, which a Pro or Max plan is never charged.

---

## Why nothing is attributed locally

There used to be a `report` here that split each interval's plan movement
across the requests inside it, weighted by `cost_usd`. It is deleted, along
with `cu/join.py`, `cu/report.py` and `cu/pricing.py`. The reason is not that
the arithmetic was wrong; it is that no local process holds the facts the
arithmetic needs.

* **The percentage belongs to the account, not to this machine.** Two machines
  on one account each observe the same 5% → 7% move and each attribute the
  full 2pp to their own traffic. Summed, that is 4pp of a 2pp move, and neither
  machine is wrong on its own evidence.
* **Most of the account is invisible from here.** claude.ai in a browser,
  Claude Desktop, the phone app and any machine that is not running this tool
  all move the same percentage, and leave no trace a local process can see.
* **A gap leaves no baseline.** Percentages only change when an interactive
  session renders its status line. The first observation after a gap has
  nothing before it, so its movement lands on whatever request happened to be
  nearby — the largest single error in the old report was exactly this.

So attribution belongs on a central server, once every machine's facts can be
reconciled against a **closed** window: all requests from all hosts on one
account, against the total movement that window actually recorded, with the
leftover reported as movement that happened while nobody was watching rather
than pushed onto whoever was. That server is `server/` in this repository, and
it does exactly that — but it is a *separate* program that only sees what has
been shipped to it. This side does not change when it is running: this machine
records facts and labels nothing an estimate — a number labelled "estimate"
gets quoted as fact — and every command here still answers offline, from these
files, whether or not a server exists anywhere.

## `cost_usd` is not a bill

Claude Code derives `cost_usd` from the request's token counts at **list API
rates** and exports it on every `api_request` record. On a Pro or Max
subscription **you are never charged it**; no invoice anywhere corresponds to
the `list $` column. Read it as *how big was this request* — a relative size,
comparable between rows — and never as spend.

There is no local rate table. `cu/pricing.py` recomputed a figure Claude Code
was already sending, shipped two fabricated constants doing it (an
"introductory" Sonnet 5 rate that never applied, and a 1.25× cache-write
multiplier where the captured data proves 2.00), and is deleted. A row that
carries no `cost_usd` is stored with none: it is not guessed at, not
back-filled from tokens, and not dropped.

**The cache-read trap still matters for the token columns.** In this user's
data, cache-read tokens were ~89% of all tokens, so any figure that sums only
`input + output` is wrong by roughly an order of magnitude. All four token
classes are separate, required columns in the ledger and in `show`, never
folded together.

**`/usage` in Claude Code is ground truth for the plan.** Nothing here
competes with it.

---

## The two streams

|  | Stream A — per-request | Stream B — plan percentages |
| :--- | :--- | :--- |
| Source | Claude Code's native OTLP export | Claude Code's status line, via claudio's shim |
| Granularity | One record per API request | One record per percentage change |
| Carries tags? | **Yes** — `profile=`, `account=`, `host=`, `email=`, `--tag` | Account-wide; plus `account`, `profile`, `session_id`, `prompt_id` as recorded |
| Whose numbers? | Claude Code's token counts and `cost_usd` | **Anthropic's own, server-side** |
| Works headless? | Yes, including `claude -p` | No — the status line renders only in an interactive session |
| Written by | `recv/otlp-recv`, then `ingest` | `claudio statusline --shim` |

Both are JSONL with stable schemas. Stream A dedupes on `request_id`, a hash of
the record's own content; stream B writes only when the rounded percentage
passes the high-water mark it already holds for that window, or when the window
itself rolls over.

Stream A's dedupe is **two halves, and it needed both**. An exclusive lock stops
two ingests running at once, and `ledger.read` drops a repeated `request_id` as
it reads. Neither substitutes for the other: without the lock a `collect stop`
racing a manual `ingest` appends everything twice (reproduced — two runs each
printing "ingested 8 new requests", 16 rows for 8 requests, every `show` bucket
doubled and `doctor` reporting health throughout), and without the read-side
dedupe a ledger already doubled that way stays wrong for ever, because a lock
added today cannot undo yesterday's rows.

---

## Install

**There is nothing here to install separately.** `claudio usage` and `claudio
recv` ship with claudio and are installed by its `make install` — see
[Installation](../readme.md#installation) in the top-level readme. In a
checkout they are `usage/claudio-usage` and `usage/recv/otlp-recv`; installed,
they are `$PREFIX/libexec/claudio/`, and claudio finds them either way.

Requires `python3` (3.9 or newer, standard library only — no `pip install`) and
`jq` for claudio's recorder. **Docker is not needed**: the receiver is
`recv/otlp-recv`, a file in this repo, spawned on demand.

**Nothing here needs DuckDB, and a test asserts it.** DuckDB is the project's
first non-stdlib dependency and it is permitted in exactly one place — the
server's query layer. Everything under `usage/` reads the same JSONL with the
standard library, **offline, with no server, no daemon and no network**:
`claudio usage show`, `ingest` and `doctor` all work on a machine that has never
heard of the server. That property is most of why this tool is pleasant to use,
so it is guarded rather than hoped for — one test greps every source file here
for the import, and a second BLOCKS the name at the import system and runs the
real CLI over a real ledger, because a grep alone would pass a module that
reached the driver through `importlib`.

**One config line and no commands, from nothing to a first recorded request.**
Add to `~/.claudio/claudio.conf` — both streams are off until you do:

```
logging=local                       # BOTH streams: per-request export, and
                                    # the plan percentages
```

Then just use claude. There is no collector to start.

`logging=none|local|remote` is the whole switch, in any of claudio's three
config layers, with a per-account form (`account.<name>.logging=`) that
outranks all of them. It replaced two separate keys, `otel=` and `usage=`,
which this release still reads and names on stderr; the next one deletes them.

Two more keys exist and you should not normally need either, because they are
already claudio's defaults:

```
otel_protocol=http/json             # claudio's default; shown for the record
otel_endpoint=http://localhost:4318 # claudio's default; shown for the record
```

They are written out because either can be overridden in any of three config
layers and because the pairing is the whole seam: the receiver is stdlib
Python, and gRPC is HTTP/2 framing plus protobuf — not something `http.server`
can be talked into — so it speaks OTLP/HTTP JSON on 4318 and nothing else.

They were **not** claudio's defaults until recently: it defaulted to the Docker
collector's gRPC on 4317, so turning telemetry on started no receiver and sent
every record to a port with nothing behind it. An export
aimed at a port nothing listens on, or in a framing nothing can decode, fails
**quietly** — Claude Code's exporter is silent by design — so that default was
this project's cardinal sin arriving as the feature's normal behaviour.
`claudio usage doctor` still checks *both* values against what `claudio env`
actually exports in the current directory: a mismatch is a WARN with a non-zero
exit, never a surprise six sessions later.

`claudio usage doctor` checks the chain end to end — including asking claudio
what it exports here, so you do not have to run `claudio env | grep OTEL` and
interpret it yourself.

`claudio usage install` is optional: it prints the claudio lines above and
gives the config file a home if you want to override the endpoint.

**Recording is claudio's, not this tool's.** The status-line shim that captures
the percentages lives there, so `logging=` is the only switch — and
deliberately the only one. A `record=` key in `~/.claudio-usage/config` used to
gate a second recorder in this repo; two keys for one decision meant `doctor`
read the wrong one and reported "recording is OFF" while samples were arriving
through the shim. The key and the second recorder are both deleted.

---

## Use

There is nothing to start. `claudio run` spawns the receiver before it `exec`s
claude, the status-line shim keeps it alive, and it exits by itself once no
records have arrived for `idle_minutes` **and** no session is still open:

```bash
claudio run                     # the receiver comes up with the session
#   ... do the work you want to record ...
claudio usage ingest            # raw OTLP files -> ledger.jsonl
claudio usage show
```

`claudio usage collect start` / `stop` / `status` still exist and run the same
process by hand — useful for a session claudio did not launch, and for `collect
stop`, which ingests as it goes. `claudio recv serve` runs one in the
foreground when you would rather watch it than read a file.

> **The receiver is a process, not a container, and that is the end of a
> measured wrong turn.** Three documents claimed the OTLP file exporter buffers
> ~4KB pages, does not flush on idle, and that a `docker kill` silently
> discards the tail. That is false, and it was reproduced wrong twice. Measured
> twice on contrib 0.158.0: 5 records POSTed, host-side `wc -c` on the
> bind-mounted file read **zero bytes** — then `docker kill` (SIGKILL, no grace
> period) and `docker rm -f`, and the file held all 5 records, 3355 bytes. The
> zero was macOS VirtioFS bind-mount coherence lag: the host could not see
> bytes that were already durable. With `rotation:` configured the exporter
> writes through lumberjack, which is unbuffered per write. **There is no
> buffered tail to lose.**
>
> That claim was the main argument for a resident service, and it was the only
> one with any force: the other two do not survive contact either. Stream A
> exists **only** inside claudio-launched sessions, because claudio writes the
> OTLP variables into the environment of the `exec`ed claude and nowhere else,
> so an always-on daemon spends its life listening to an unplugged wire. And
> `launchctl`/`systemctl` cannot be stubbed in a hermetic suite, while every
> promise in this repo is pinned by one.
>
> **The lesson worth carrying: never conclude anything about durability from
> what the host sees through a bind mount.** Going native removes the bind
> mount, so the artefact cannot recur.

The native receiver keeps the durability property by construction rather than
by luck: each accepted payload is one `write(2)` on an `O_APPEND` file
**before** the 200 goes back, and a write that fails answers 503 so the client
keeps the batch and retries. Nothing is ever held only in memory, which is what
makes an on-demand process that exits by itself a safe design at all.

`clean` in `uptime.jsonl` is **observed, never assumed** — and the old bug is
now inverted rather than re-fixed. `docker stop -t 10` returns 0 *after*
escalating to SIGKILL (measured: rc 0, exit code 137) and the code wrote
`clean:true` over it. Here the receiver writes its own `stop` record on the way
out, so `clean` is earned by getting there: nothing `collect stop` does can
write a clean span. Kill it and you lose the label, never the records.

### Counters, and why they exist

`collect stop` prints the receiver's own counters, and `doctor` reads them live
off `/healthz` or out of the last snapshot once the process has gone:

```
records: 12 accepted -> 12 written; events: api_request 5, user_prompt 4, assistant_response 3
```

That line is the only artefact on this machine that can tell *nothing arrived*
apart from *everything was dropped*, and every bug this project has had was the
second one wearing the first one's face.

It answers one question the old `:8888` scrape could not. `raw/api_request.jsonl`
used to be the **collector's own output**, so nothing downstream could ever see
what never arrived: a field deleted by `keep_keys` was invisible in the only
file anyone read, which is how `prompt.id`, every user `--tag` and `agent.name`
were each lost in silence. The receiver counts at the door, so:

- **every distinct `event.name` is named.** No `api_request` at all, while other
  events arrive, is drift — Claude Code sends `api_request`, and a prefixed
  guess (`claude_code.api_request`) dropped every real record for an evening
  with no symptom but a zero. That is now a printed clause, not an inference.
- **every attribute key that arrives and has no ledger column is named.** The
  list it is compared against is `otlp.STORED_ATTRS`, and a test derives the
  reader's own `a.get("…")` calls out of the source and compares the two, so it
  cannot drift into either a missed field or a permanent false alarm.

The event counts are printed as **facts, never as losses**: `user_prompt` and
`assistant_response` are not ledger rows by design, and every real session
sends them.

### `show`

```bash
claudio usage show
claudio usage show --by model
claudio usage show --by account --since 24h
claudio usage show --by verify          # any claudio --tag key
```

| flag | default | meaning |
|---|---|---|
| `--by` | `profile` | `profile`, `account`, `model`, `session_id`, `query_source`, `host`, `terminal_type`, **or any claudio `--tag` key**; an unknown key groups everything under `(none)` rather than erroring |
| `--since` | — | ISO-8601, a bare epoch, or a relative offset (`24h`, `7d`); keeps requests at or after it |

Requests only. The plan observations are counted at the foot of the output and
otherwise left alone, because relating the two is the server's job.

### Ingest, and re-reading the raw files

**The receiver ingests on its own tick, and once more before it exits.** You
should never have to type `ingest` for records to reach the ledger or the
shipper; the verb is there for when you want the report, or want it now.

That was not true until recently, and the gap was the whole of stream A.
`do_ingest` was reached from exactly two places — the `ingest` verb and
`collect stop` — so on a fully configured `logging=remote` machine the receiver
wrote `raw/`, the shipper read `ledger.jsonl`, and nothing turned one into the
other: the ledger never existed, the shipper reported "no such file yet,
nothing to say", and both `ship.err` and `ship.warn` stayed absent. Plan
percentages shipped and request rows did not, which on the server is
indistinguishable from a machine that made no requests. The ingest step now
runs in the watchdog, under the same `flock` a manual ingest takes: a tick that
collides with your `claudio usage ingest` is a skipped tick, never a doubled
ledger.

**Rotation will not delete a capture file ingest has not read.** `raw/` is
rotated at `raw_max_megabytes` and pruned to `raw_max_backups`, and the prune
now asks the ingest offset first. A file whose stored offset is short of its
size — or that has no offset at all — is kept past the cap and named in
`recv.warn`, which `claudio run` and `doctor` both print. A raw directory that
grows is visible in a byte count and is emptied by ingesting; a deleted record
is neither. (Measured before the interlock existed: 6000 payloads in at 1 MB /
2 backups, then one ingest — 1900 rows, 4100 records gone, with `doctor`
printing `6000 accepted -> 6000 written` directly above `ledger rows: 1900` and
exiting 0. `doctor` now compares those two numbers itself.)

`ingest` records a byte offset per raw file so it only reads what is new. That
offset is stored with the file's **identity** — `(st_dev, st_ino)` plus a hash
of its first 256 bytes, which is what any tail-follow keeps — and with an
**anchor**, a hash of the 256 bytes immediately *before* the offset. Reading
resumes only when the file proves it is the same file and the bytes under the
offset prove they are the ones that were read. Anything else restarts from
byte 0 and says why.

The anchor is there because identity alone is not enough **on Linux**, which is
where a shipping host runs. Inode numbers come straight back there, so a file
unlinked and rewritten at the same path takes its old inode — and the first 256
bytes of an append-only file rebuilt from its own start are unchanged by
construction, so all three identity fields match a file that is not the one the
offset describes. Measured with the same six lines on both: macOS reported a
new `st_ino` for the replacement, Debian reported the identical triple. Without
the anchor the resume lands inside somebody else's bytes and everything before
it is skipped in silence, which is the failure described in the next paragraph,
reintroduced on a different operating system.

An offset written before anchors existed carries none, so it cannot be
confirmed and is not trusted: it restarts once, named, exactly as a
pre-identity integer offset does.

That is safe at any time, because `ledger.request_id` is derived from content:
a re-read costs one parse, never a duplicate row. It was not always *possible*,
which is the point — the offset used to be keyed on path alone, so when the raw
file was replaced under a live offset the next ingest resumed at the stale
position and skipped everything before it in silence. On the machine this was
found on that put **five of eight real records permanently out of reach** (57%
of the captured spend) while `ingest` printed "ingested 0 new requests".

```bash
claudio usage ingest --from-zero    # ignore stored offsets entirely
```

Use it when you have reason to think records are missing. Offsets written
before identity was recorded cannot be verified, so the first ingest after
upgrading rescans once by itself and says so. To rebuild the ledger completely
— which is only complete if no raw file has been rotated away — remove
`ledger.jsonl` and run `ingest --from-zero`.

`ingest` never consumes past the last newline in a file, so a torn trailing
line (the normal state of a file the receiver is still writing) is left for
the next pass rather than counted as corruption.

**Only one ingest runs at a time.** `collect stop` ingests by itself, so a
manual `ingest` beside it is an ordinary accident rather than an exotic one.
The second one refuses, loudly, naming the lock and the likely culprit:

```
another ingest is already running (lock: ~/.claudio-usage/state/ingest.lock).
```

It fails rather than waiting, because a silent wait hides the concurrency from
the only person who can do anything about it. The lock is `flock`, so it dies
with the process and a crash cannot strand it.

#### What ingest says when something was dropped

Three things can cost you records, and each used to be silent. None of them
fails the command or freezes the byte offset — they are reports:

| line | what happened |
|---|---|
| `read 8 log records, kept 0, skipped 8 (event.name: claude_code.api_request)` | Claude Code renamed the event. Every record is skipped and the offset advances past them, so `--from-zero` will not bring them back either. Until this line existed the symptom was "ingested 0 new requests", which is what an idle machine prints. **`kept` counts what parsed, not what was written** — a re-read parses rows the ledger already holds, so only the `ingested N new requests` line below says what landed. |
| `recovered 1 record from a torn line` | A process died mid-write, leaving a fragment with no newline, so the next payload was appended to it and both arrived as one unparseable line. The complete record behind the fragment is cut out and kept. |
| `1 record(s) skipped as duplicates of a DIFFERENT request` | Two genuinely different requests hashed to one `request_id`. The hash is deliberately **not** widened to fix it — that would re-identify every row already written and double the ledger on the next ingest — so the collision is reported instead, and `doctor` repeats it. |

### `query_source`, and what it is for

Claude Code labels its own overhead. Four values appear across the captured
records: `repl_main_thread` and `sdk` are the user's own work;
`generate_session_title` and `prompt_suggestion` are work Claude Code started
on its own initiative — a session title you never asked for, a follow-up
suggestion after your turn was already answered.

`show` counts them and names both sides at the foot of its output — the
overhead sources *these* rows actually contain, and every source counted as
your work; it does not filter them out, and there is no `--only-overhead`. That flag existed because
the old report filtered *before* weighting, so excluding overhead reweighted
everyone else's share of plan movement. There is no share now.

`cu/config.py`'s `OVERHEAD_SOURCES` holds the two names, and every one of them
was **read off a captured payload**. It is a deny-list, so an unrecognised
future source counts as your work and is *named* in the "counted as your work"
line — new overhead announces itself the first time it is seen. An earlier list
held six plausible names Claude Code has never sent, so it matched nothing at
all; guessing the strings was worse than having no list, because it read as
coverage.

---

## Recording stream B

**The recorder lives in claudio, and is enabled there.** What Claude Code runs
as its status line is `claudio statusline --shim`: it reads the payload piped
in, appends a sample to `<usage_dir>/samples.jsonl` (default
`~/.claudio-usage/`), and forwards the identical bytes to whatever renderer you
had configured. Turn it on with `logging=local` in `~/.claudio/claudio.conf`;
it needs `jq`. A recorder fault costs a sample, never the render.

**Your own status line keeps working.** The shim is a wrapper, not a
replacement: it forwards the payload bytes unchanged to whatever renderer
`statusline=` names, so a custom renderer needs no knowledge of any of this.
That is why there is only one recorder now — `recorder/cu-record`, a second
implementation living here, was reachable only by a status line the user wrote
themselves to extract nine values and pass them positionally, and nothing
shipped such a wrapper. It is deleted, with its `record=` key and its tests.

**Why the dedupe rule is not "write when the value changes".** Concurrent
sessions hold rate-limit snapshots of different ages and ping-pong the shared
state file. The real 65-record history contains 3 in-window 5-hour decreases, 4
seven-day decreases, an identical-timestamp pair reporting 5% and 4%, and
`resets_at` flapping backwards. Naive change-detection records every
oscillation.

The rule is per-window **high-water marks with forward-only reset acceptance**.
Replaying the real 65 records through it yields **exactly 49** — 44 `change`, 4
`reset`, 1 `first` — dropping 16 pure artefacts and leaving a series that is
monotone within each window. Monotone-within-a-window is a property of the
*recording*, not an interpretation of it, and it is what any later
reconstruction of the window needs. That replay is a test, not a claim — it
lives in claudio's `test.sh` alongside the recorder itself, replaying
`fixtures/replay-v1.jsonl` against a golden derived by an independent
implementation in another language.

Percentages are stored **raw**. `used_percentage` is `utilization * 100`, which
is why `7.000000000000001` (that is `0.07 * 100`) appears in the data.
Quantisation is a trigger for writing a record, never a transformation of one.

`cu/samples.py` reads the file back and says which accounts appear in it, and
that is all it does. It used to reconstruct each window and its intervals so
that movement could be split across this machine's requests; that went with the
join. Reconstructing a window is only sound once *every* machine's samples for
that account are in one place, so it belongs where they arrive.

---

## What lands on disk

```
~/.claudio-usage/
├── samples.jsonl          stream B, append-only
├── ledger.jsonl           stream A, canonical, one row per request
├── raw/api_request.jsonl  every log payload as it arrived, + rotations
├── uptime.jsonl           receiver start/stop, so a gap in stream A is
│                          distinguishable from a quiet period
├── recv.pid               pid, token, port, started-at — one field per line
├── recv.lock              spawn hand-off (a directory: `mkdir` is the atom)
├── recv.err               why the last START failed; printed by the next run,
│                          and the flag the supervisor stands down on
├── recv.warn              what a RUNNING receiver reported (an undecodable
│                          body, a failed write); printed, never a spawn gate
├── recv.starts            failed starts in the window (the crash-loop cap)
├── recv-counters.json     last counter snapshot, so they outlive the process
├── ship.err               why the last shipping pass delivered nothing; the
│                          offsets are untouched, so it is a retry not a loss
├── ship.warn              rows a DELIVERING pass could not place against any
│                          account, and lines that would not parse — RUNNING
│                          TOTALS, cleared only by deleting the file below
├── machine-id             an opaque per-machine id for the batch manifest,
│                          created on first use and nothing else
├── sessions/<pid>         one per claudio-launched session; swept by the
│                          receiver, and half of the idle-exit condition
├── account_*.json         claudio's cache of the account's identity, read
│                          out of that CLAUDE_CONFIG_DIR's .claude.json
├── .last_*                claudio's per-account high-water marks (stream B)
├── state/                 ingest offsets, ship offsets, and
│                          ship-unplaceable.json — the running totals behind
│                          ship.warn; delete it to acknowledge them
│                          (separate offset files: an
│                          ingest must never advance the shipper past records
│                          it never sent), and ingest.lock (flock only — it is
│                          never read, and never left stale)
└── config                 optional; every key has a working default
```

**Nothing is ever written inside `~/.claude`** — that is Claude Code's
directory. A test asserts it. A `usage_dir=` pointing there is refused by
every writer *and named* by `claudio usage`, which will not pass it on: a
reader aimed at a directory nothing writes to reports an empty machine, and is
believed.

**`usage_dir` is one key resolved on one side.** It layers global → profile →
project in claudio's own config, and claudio exports the result as
`CLAUDIO_USAGE_DIR` when it `exec`s either Python verb — which it did for the
receiver and, until recently, not for the reader. The reader looked only at the
environment variable and otherwise defaulted, so `usage_dir=/data/usage`
recorded into `/data/usage` while `claudio usage show` read `~/.claudio-usage`
and printed `No requests recorded yet.` over a full ledger. One resolver, in
the half that already has the config layering; an explicit `CLAUDIO_USAGE_DIR`
in the environment is the narrowest layer on both sides and is never
re-decided.

**The directory itself was renamed.** It was `~/.claude-usage` before `claudio
usage` and `claudio recv` became subcommands. Nothing moves it for you and
`claudio migrate` does not cover it — but `doctor` names it when the new
directory is empty and the old one is not, because two zeroes are otherwise
indistinguishable from a machine that never recorded. `mv ~/.claude-usage
~/.claudio-usage`, or `usage_dir=` to stay where you are.

**Three columns exist only because a row cannot be reconstructed later.**
`ts_ns` is the nanosecond integer the payload carried: `ts` is a float of it
and does *not* round-trip (19 significant digits into a double that holds
15–17), so without it a row cannot recompute its own `request_id` from its own
columns once the raw file has rotated away. `schema` is what lets a later
reader tell a row written before a column existed from one whose value was
genuinely null. `client_version` names the Claude Code that produced the bytes,
which every field-name drift so far has been diagnosed without.

Token columns are `null` when the payload did not state them, never a confident
`0` — but the `request_id` hash still sees the 0-coerced value, because the id
is an identity rather than a reading and changing what it is computed from
re-identifies every row already on disk.

**Fabricated rows declare themselves, and that now matters more.** `synth-otlp`
POSTs to the same receiver that feeds the ledger, so a fabricated payload lands
in `raw/api_request.jsonl` beside the real ones — there is no separate debug
file to POST at any more. `recv/synth-otlp` tags every payload
with a `synthetic=1` resource attribute and `cu/otlp.py` turns that into
`source: "synthetic"` on the row, permanently. Before that, `source` was the
constant `"otlp"` for everything: two generator rows sat in the live ledger
indistinguishable from real consumption and became the top line of a real
account's report. `show` does not filter them out — the row says what it is,
and filtering is for whatever consumes the ledger. Only a self-declaration
counts; guessing which rows look fabricated would be the same class of mistake
as guessing `query_source` names.

### Privacy

The receiver binds **loopback only** — `127.0.0.1`, and a `--host` that is not
loopback is refused with a non-zero exit rather than quietly clamped. It holds
an unauthenticated stream of one person's request metadata, including their
email address; there is no flag that publishes it.

**What it stores, it stores verbatim, and that is a deliberate reversal.** The
Docker collector filtered every event that was not `api_request` at the door
and ran the surviving attributes through a `keep_keys` allow-list. `cu/otlp.py`
then did the same two jobs again one layer in, because it stores exactly the
columns the ledger has and skips every record whose `event.name` is not
`api_request`, counting and *naming* what it skipped. One decision implemented
twice is how three fields were silently dropped — each half looked right beside
the other, and the file everyone read was the output of both.

So the keeping decision now lives in exactly one place, `cu/otlp.py`, and the
receiver's file is the bytes as they arrived. Two consequences, stated rather
than buried:

- `raw/api_request.jsonl` holds `user_prompt` and `assistant_response` records
  too, and every attribute Claude Code sends — including ones no ledger column
  wants. They are **counted and named** at the door, which is the point: the
  structural blind spot was that the raw file used to be the collector's own
  output, so nothing downstream could see what never arrived.
- the log record **body** is stored as it arrives. Claude Code leaves it empty
  unless you set `OTEL_LOG_USER_PROMPTS=1`, which is your own opt-in; a silent
  redaction here would be a second, invisible policy of exactly the kind this
  paragraph is arguing against. The file is mode 0600 in a 0700 directory.

The ledger is unchanged: it stores the allow-listed columns and nothing else,
`session.id` and `prompt.id` included, because both are local-only grouping
axes and `session.id` is part of the content hash that makes ingest idempotent.
Resource attributes stay a **deny-list** (`telemetry.sdk.*`, `process.*`,
`os.*`, `host.arch`) — an allow-list over them deleted every `--tag` the user
had invented, which is the whole reason for tagging.

Metrics are accepted and discarded: pre-aggregated, no per-request granularity,
and `session.id` on them would make unbounded-cardinality series. They are
answered `200` with an empty OTLP success message rather than `404`, because an
OTLP client treats 4xx and 5xx as retryable and would send the same body for
ever.

> `email` is real PII. claudio puts it on every exported record, and on a
> metrics backend it becomes a label — one series per address. Suppress it with
> `tag=email=redacted` in any claudio config layer.

**Under `logging=remote` those rows leave the machine, whole.** The shipper
sends `ledger.jsonl` and `samples.jsonl` as they are — `email`, `session_id`,
`prompt_id` and all — because a per-request reconciliation against a closed
window cannot be done over an aggregate. There is no field-stripping mode, and
that is a decision rather than an omission: the separation that matters is the
destination, which is set per account, so a work account can report to a
company server while a personal one reports nowhere. Suppressing `email` with
`tag=email=redacted` still works and has one consequence worth knowing — a row
with no address cannot be placed against any account, so it is not shipped at
all, and the count lands in `ship.warn` rather than disappearing.

---

## Troubleshooting

**Nothing arrives.** Check in this order:

1. `claudio usage doctor` — it checks recording, the receiver, `recv.err`
   and `recv.warn`, the counters, sample and ledger counts, duplicate and colliding rows, the
   uptime ledger, and what claudio actually exports here. **Every `WARN` it
   prints sets a non-zero exit status**; there is no third state, so `doctor`
   is usable from a script. It used to print `WARN ledger rows: 0` and exit 0,
   which meant a doubled ledger and every unclean shutdown passed a health
   check. Read its first two lines: `data root:` is where it looked, and
   `asking claudio:` is **which claudio** it asked. Two of its verdicts are
   claudio's own answers read out of a subprocess, and it used to ask a bare
   name — i.e. whichever claudio is first on `$PATH`, which on a machine with
   both a checkout and an install is a different build reading a different
   config, and reported recording OFF while the one that ran it was recording.
2. The `counters:` line. This is the one that separates *nothing arrived* from
   *everything was dropped*: `0 accepted` with an empty `events:` means the
   export never reached this machine, while `events: user_prompt 4` with no
   `api_request` means it arrived and Claude Code has renamed the event.
3. `recv.err`, which `doctor` prints and the next `claudio run` prints. A
   receiver that could not bind exits immediately, and the export it was meant
   to catch then fails silently at the client — this file is the only thing
   standing between that and an empty ledger nobody can explain. `recv.warn`
   is its twin for a receiver that was *up*: an undecodable body, a write that
   failed. Two files rather than one because claudio's supervisor refuses to
   respawn while `recv.err` exists, so a running receiver writing there used to
   disarm the respawn for every later session.
4. `claudio env | grep OTEL` — no `OTEL_EXPORTER_OTLP_ENDPOINT` means
   `logging=` is not naming `local` or `remote` for that profile or account;
   `grpc` or `4317` means someone has overridden claudio's defaults back to the
   Docker collector's shape.
5. `claude --debug` — Claude Code prints its OTLP exporter errors there and
   nowhere else. A silent exporter failure looks exactly like an idle machine
   from this side.

**"The receiver is not running" is not a fault.** It is spawned by `claudio
run` and leaves once nothing has arrived for `idle_minutes` **and** no session
is still open, so a machine nobody is using has none. Both conditions, always:
a timer alone would orphan someone who spends forty minutes reading with claude
still open, and a session check alone would never exit at all once a session
file is stranded by a crash.

**Protocol mismatch.** claudio and the receiver now agree on
`otel_protocol=http/json` and 4318 by default, but either key can be set by
hand in three config layers. The receiver speaks **http/json only**: a protobuf
body is answered `200` (a 4xx makes the client spin) and recorded in
`recv.warn` naming the setting to change — see `recv/README.md`. A *loopback*
export in any other protocol is also named by `claudio run` on stderr, because
that one is unambiguously a local mistake rather than someone else's collector.

**Prove the plumbing without spending quota.** `recv/synth-otlp` fabricates
payloads in the captured shapes and POSTs them. It can prove a socket is
listening and a shape is handled. It can never prove a rate, a label or a
consumption figure — and since it now POSTs to the receiver that feeds the
ledger, its rows land there, labelled `source: "synthetic"` for life. See
`recv/README.md`.

---

## Tests

```bash
python3 usage/tests/test_all.py      # from the repository root
python3 tests/test_all.py            # ...or from this directory
```

The recorder's own suite is claudio's `test.sh`, where the recorder lives.

Every expected value is hand-computed and the arithmetic is shown in a comment
above the assertion.

### Fixtures are captured bytes, and that rule is the whole lesson

`recv/synth-otlp` was originally written to match the collector's filters, so
the generator and the filters agreed with each other and **neither matched
Claude Code**. Two bugs hid behind that agreement for an evening — the filter
matched `claude_code.api_request` while the real event name is `api_request`,
and a resource allow-list deleted every user `--tag` — and more were found
afterwards only by reconciling against captured bytes.

So `tests/fixtures/` holds real capture:

| fixture | what it is |
|---|---|
| `real-api-request.jsonl` | captured OTLP payloads, byte for byte, off the Docker collector that used to write them: 7 lines, 8 `api_request` records (line 3 carries two — a batched export). Post-filter, so it shows only what was kept; the receiver's own capture is pre-filter and holds `user_prompt` and `assistant_response` too |
| `real-payloads/*.json` | lines 1–4 and 7 of the above, split out and named for what each captures |
| `real-samples.jsonl` | stream B over the same minutes: 14 samples, 3 accounts, every one carrying a `prompt_id` |
| `replay-real.jsonl` | the 65-record stream-B history, anonymised; pins that every sample reads back exactly as written, raw floats and an implausible `resets_at: 1` included (the 49/65 dedupe it also pins is claudio's, and tested there) |

The suite holds down, among others: `intValue` arriving as a JSON string;
`prompt.id` surviving both allow-lists; user `--tag` labels surviving ingest;
synthetic rows labelling themselves; a torn trailing line not counting as
malformed; `request_id` deduping a re-read; an ingest offset being rejected
when the file's identity changed; a payload being on disk before its `200`;
metrics being accepted and dropped with the empty success shape that stops a
retry storm; a crash-looping receiver refusing itself; the idle exit needing
both of its conditions; `otlp.STORED_ATTRS` being unable to drift from the
`a.get()` calls it describes; every config key having a working default;
`doctor` reporting the export as a verdict rather than printing a check for you
to run; nothing being written inside `~/.claude`; and the real 65-sample replay
reading back unaltered, raw floats and an implausible `resets_at: 1` included.

---

## Shipping to a central server

`cu/ship.py` is the shipper. It runs as a tick inside `recv/otlp-recv` — the
process that is alive exactly when data exists — plus one unconditional final
pass before that receiver's idle exit. There is no cron, no launchd and no
systemd unit, for the same reasons the receiver itself is not a service.

What the server can do that no machine here can:

- **Reconcile a closed window.** All hosts' requests for one account against
  the movement that window actually recorded — the only place the sum can be
  made to balance.
- **Name the unexplained remainder honestly.** Movement in a window with no
  requests from *any* reporting host is evidence of a browser, desktop or phone
  session, or of a machine that was not recording. A single machine cannot tell
  those apart from its own silence.
- **Group by host for free.** `host` is already a first-class tag.

Three things about *what* is shipped, each of which corrects an earlier plan
written on this page:

- **Rows, not aggregates.** An earlier draft here promised
  `(window_id, bucket, requests, tokens, list_usd)` and "never raw `session.id`
  or `email`". That is not what ships: `ledger.jsonl` and `samples.jsonl` go
  over the wire as the rows they are, `email` and `session_id` included, because
  the reconciliation the server exists to do is per request against a closed
  window and an aggregate cannot be re-cut afterwards. The control that
  replaces it is the destination itself — `logging` and `ship_url` are set per
  account, so a work account reports to a company server and a personal one
  reports nowhere, which is a stronger separation than any field list.
- **Never `raw/`.** That file is larger and can contain prompt and response
  bodies. The shipper does not know its path, and a test asserts both that the
  source never names it and that a payload sitting there never reaches the wire.
- **Byte-identical lines.** A stream-B line ships exactly as it sits on disk; a
  stream-A line ships as its own bytes with `"account_uuid"` spliced in, because
  stream A carries no UUID and its `account` is a label `--tag account=` can
  overwrite. Strip that one key and you have the file's line back, so a captured
  request body is `diff`-able against `ledger.jsonl`.

Delivery is idempotent, which is what lets a failure be simple: the stored byte
offset advances **only** on an acknowledged write, so a refused POST resends the
identical range next tick. Stream A dedupes on `request_id`; stream B dedupes on
a hash of the whole record plus the shipping host (`srv/wire.sample_key`) — and
not, as an earlier draft here said, on `(account_uuid, resets_at, round(pct))`,
which the capture falsifies: `replay-real.jsonl` rows 0 and 1 share a timestamp
and a window with different percentages from two sessions, so that tuple would
delete one of two real records. Two config dirs on one machine, or two machines
watching the same plan, legitimately produce duplicate *observations* — the file
is a stream of observations, not events, and that duplication is exactly the
ambiguity the server exists to resolve.

Attestations (stream C — which machine was listening for which window) are
designed in `server/srv/wire.py` and **nothing in this repository emits one**.
The shipper sends two streams, not three.

Configuration, all in claudio's global conf and nowhere else:

```sh
logging=remote                                   # or account.work.logging=remote
account.work.ship_url=https://usage.corp/v1/ship
account.work.ship_token=t-9f2c                   # a tenant label for routing
```

The other end of that URL is `server/srv/serve.py` — `POST /v1/ship`, NDJSON,
optional gzip, durable before it answers 200. It is **for a trusted network and
must not face the internet**: see `server/README.md`, which says so once and
plainly along with everything else the door refuses to do.

That server also serves a read-only API at `/api/v1/`, which is what **omini**,
the official front end, consumes; the server holds no page of its own. Its
query layer is the one place DuckDB is used, reading the shipped JSONL **in
place** so the files stay the record of truth. None of that reaches back here:
shipping is a one-way POST, and this side neither knows nor needs to know what
the far end reads the bytes with.

With no `ship_url` for the account nothing is shipped, and `claudio run` says
which account and which line to add. Failures land in `<usage_dir>/ship.err`
and rows that name no account at all land in `<usage_dir>/ship.warn`; `claudio
run` prints both until they are gone.

Three states that used to be silent are now named in those two files.

**A destination that places nothing.** Stream A is matched on the `email` tag
against the address in that account's own `.claude.json`. If the two diverge —
an account re-logged under a new address, or a `tag=email=` line suppressing
the one claudio emits — every row misses, and the shipper used to consume the
whole ledger into an offset and report perfect health. It now names the
account, prints both addresses, and **does not advance the ledger offset** for
a destination that has never placed a row, so the backlog ships the moment the
two agree instead of having been eaten while nobody was told.

**`ship.warn` is cumulative.** A row that can never be placed is a
non-repeating event, and the offset moves past it, so a per-pass warning lived
one tick — about fifteen seconds — and the next pass deleted it. The counts are
accumulated in `state/ship-unplaceable.json` and restated every pass; delete
that file to acknowledge them and the warning clears with it.

**A ledger that exists and cannot be read is a failure, not a silence.**
`chmod 000 ledger.jsonl` used to read as "no such file yet", which cleared the
`ship.err` the previous pass had written and ended shipping with every
diagnostic surface clean.

The door's answer is also read now rather than discarded: a refusal reaches
`ship.err` as its `reason` and `detail` (`disk-nearly-full — 12 bytes free,
reserve is 20`) instead of as `HTTP 507`, and the offset the door says it holds
is compared with the one this machine is about to store.

Backups, retention and disk growth are the operator's job. Nothing here deletes
a record or prunes `usage_dir`.
