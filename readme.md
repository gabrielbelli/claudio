# claudio

Claude Code **profile and account manager** — two independent axes, one tool.

A **profile** bundles MCP servers, permissions, hooks, skills, commands, subagents and instructions under a name. Activating one symlinks everything into the current directory — like a venv for Claude Code. An **account** is an isolated Claude Code config directory, so its own login. A profile says what Claude *has*; an account says who Claude *is*, and any profile runs under any account.

claudio itself is one POSIX `sh` script — no bash required, and nothing to compile. Beside it ship two standard-library Python programs it drives, `claudio usage` and `claudio recv`, which record and read telemetry. Everything else is optional: `jq` for plan-usage recording and MCP mixed mode, [`claude-statusline`](https://github.com/gabrielbelli/claude-statusline) (or any renderer) for the status line, Docker for MCP mode, `git` for [`claudio update`](#claudio-update---check).

## Installation

> **Pre-release.** Nothing is tagged and Homebrew has nothing to fetch yet.
> If you were sent here to test, [`TESTING.md`](TESTING.md) is the whole of
> what is asked: install by hand, work normally, say what broke.


### Homebrew — macOS and Linux

```sh
brew install gabrielbelli/tap/claudio
claudio --version
```

> **From the first tagged release onwards.** Nothing is tagged yet, so that line
> has nothing to fetch today. The formula lives in this repository at
> [`Formula/claudio.rb`](Formula/claudio.rb), so it cannot drift from the tree
> it installs; it calls the same `make install` as everything below.
>
> A top-level `Formula/` directory is all Homebrew needs to treat a repository
> as a tap, so this repository can be installed from directly, at `master`,
> before any tag exists:
>
> ```sh
> brew tap gabrielbelli/claudio https://github.com/gabrielbelli/claudio
> brew install --HEAD gabrielbelli/claudio/claudio
> ```
>
> **Not installed yet.** No tag exists, so the formula has never been through
> `brew install` — what has been verified is the layout it produces, because it
> installs by calling `make install`, and the test suite stages exactly that and
> runs the result, including through a symlink standing in for Homebrew's shim.
> `ruby -c` and `brew style` pass on the formula itself. Treat the two commands
> above as reviewed, not as tested.

### From a checkout — macOS, Linux, any POSIX system

```sh
git clone https://github.com/gabrielbelli/claudio
cd claudio && sudo make install
claudio --version
```

`make install` writes two things and nothing else:

| path | what |
|---|---|
| `$PREFIX/bin/claudio` | the script |
| `$PREFIX/libexec/claudio/` | `claudio usage`, `claudio recv`, and the `cu/` modules they import |

`PREFIX` defaults to `/usr/local`. To keep it in your own home directory and skip the `sudo`:

```sh
make install PREFIX="$HOME/.local"    # then put ~/.local/bin on your $PATH
```

Nothing is baked in by either: claudio finds its Python by walking out from the resolved path of the running script, so the installed tree relocates and a Homebrew symlink resolves correctly. Distribution packagers want `make install DESTDIR="$pkgdir" PREFIX=/usr`, which stages the same layout without touching the live prefix.

### From a checkout, installing nothing

There is no build step and no generated file, so the script in a clone is a working claudio:

```sh
git clone https://github.com/gabrielbelli/claudio
./claudio/claudio --version
```

`claudio usage` and `claudio recv` work too, with nothing installed and no `$PATH` entry: `_tool_path` looks beside the *resolved* script first, and in a checkout `usage/claudio-usage` and `usage/recv/otlp-recv` are exactly there. A symlink into a clone works for the same reason — `ln -s "$PWD/claudio/claudio" ~/.local/bin/claudio` resolves back to the checkout and finds its Python. This is the layout the test suite runs in, so it is the best-tested of the three.

What you give up is only what an install buys: no `claudio` on `$PATH` unless you add one, and nothing to `brew upgrade`.

> **Do not just copy `claudio` onto your `$PATH`.** That was the documented install for a while, and it is why this section is longer than one line. The script alone starts and `claudio help` looks perfect — but `claudio usage` and `claudio recv` are then missing, and `claudio recv` is what telemetry is exported *to*. claudio does say so: both verbs refuse by name, and `claudio run` prints `usage receiver: not found at otlp-recv … claudio was installed without its Python` on stderr. It is stderr a moment before Claude Code takes over the terminal, though, so the practical symptom is a ledger that stays empty. `make install` is the fix and re-running it is safe.

**What has to be there:** [Claude Code](https://code.claude.com) itself — claudio launches it and does not bundle it, so `claudio run` and `claudio account new` end in `claude: not found` (exit 127) without it — plus POSIX `sh` and `python3` (3.9 or newer; standard library only, so there is no `pip install` at any point). **What is optional:** `jq`, a status-line renderer, Docker, `git` — claudio guards every one of them, and a missing optional dependency costs a feature, never a command.

`server/` is not installed by any of this. It is the receiving end of `logging=remote`, a separate program with its own container — see [server/README.md](server/README.md). Packaging claudio for a distribution: [packaging/README.md](packaging/README.md).

## Quickstart

From nothing to a first run is **three commands** after the install:

```sh
claudio account new work                 # an isolated login; opens a browser
cd ~/projects/incident-response
claudio run --account work               # sets the account and tags, launches claude
```

That is a working state: no profile, nothing written to the directory, just the login you meant. Add a profile when you want a bundle of MCP servers, permissions and instructions to travel with the project:

```sh
# Scaffold one, then fill it in.
claudio new soc
claudio mcp soc add ghcr.io/acme/my-server:latest   # MCP servers, via Docker
claudio edit soc settings                           # permissions and hooks
claudio edit soc claude                             # project instructions

# Work. `run` links the profile, sets the account and tags, launches Claude.
cd ~/projects/incident-response
claudio run soc --account work

# Stop retyping --account in this directory.
claudio default --account work
claudio run
```

Already have a project configured by hand? `claudio init soc` captures it as a profile instead of scaffolding one.

Telemetry is **off** until you ask for it. One line turns both streams on:

```sh
echo logging=local >> ~/.claudio/claudio.conf
```

## Upgrading: "No profiles found"

> **New here? Skip this section.** It is for people who used claudio before its data moved out of `~/.claude`. If `claudio list` shows your profiles — or you have not made one yet — there is nothing to migrate.

If `claudio list` prints **No profiles found** after an upgrade, or a project that has worked for months suddenly says `Profile 'soc' not found`, or `claudio clean` says it removed nothing — your profiles are fine, they are just in the old place. claudio used to keep its data inside `~/.claude`, which is Claude Code's own directory; it now keeps its own at `~/.claudio`. Each of those three messages tells you the same thing, and one command moves everything across:

```sh
claudio migrate --dry-run     # print the plan, change nothing
claudio migrate               # do it (asks you to type `migrate` first)
```

Three things to know before you run it:

- **What moves.** `~/.claude/profiles/*`, `~/.claude/accounts/*` and `~/.claude/claudio.conf` → the same names under `~/.claudio/`. One item at a time, and never onto something that already exists: a collision is reported and skipped, the rest still moves.
- **Your logins, on macOS.** Claude Code stores each account's credential in the Keychain under a name derived from the account directory's *path*, so moving an account leaves its login behind — **every logged-in account has to log in again** afterwards. `migrate` names them before it touches anything, and again at the end as ready-to-paste `claudio account login <name>` lines. On Linux the credential is a file inside the account directory and moves with it, so in principle there is nothing to redo — but see the caveat under [`claudio migrate`](#claudio-migrate---dry-run---yes---scan-dir---undo): that half has never been run on Linux.
- **Your existing projects.** Activating a profile writes *absolute* symlinks, so every project you have used claudio in still points into `~/.claude`. `migrate` rewrites those links in place — it finds projects by scanning `$HOME` four levels deep for `.claudio` markers. For anything outside your home directory, run it a second time with `claudio migrate --scan <dir>`: `--scan` **replaces** the `$HOME` scan rather than adding to it, and re-running is safe.

Nothing else to do: the `.claudio` marker in a project stores a profile *name*, not a path, so projects need no editing. Full detail in [`claudio migrate`](#claudio-migrate---dry-run---yes---scan-dir---undo); it is safe to run again, and `claudio migrate --undo` puts everything back.

> `migrate` exists only for this one move and will be removed two releases after it. If `claudio list` already shows your profiles, you have nothing to migrate.

### The other move: `~/.claude-usage` → `~/.claudio-usage`

Recorded telemetry moved at the same time, and `migrate` does **not** cover it — it moves profiles, accounts and the config file, nothing else. If you were recording before the rename and never pinned `usage_dir=`, claudio is now writing into a fresh, empty `~/.claudio-usage` while every sample and request row you have is still in `~/.claude-usage`. `claudio usage show` says `No requests recorded yet.` and `claudio usage doctor` warns about zero samples and zero ledger rows — which is exactly what a machine that never recorded anything says, so `doctor` names this case outright when it sees it.

One command, either way round:

```sh
mv ~/.claude-usage ~/.claudio-usage                  # take the records across
echo usage_dir="$HOME/.claude-usage" >> ~/.claudio/claudio.conf   # or stay where you are
```

Nothing moves it for you: it is your data, and a diagnostic command is the wrong thing to be relocating directories.

## How It Works

A profile is a directory in `~/.claudio/profiles/` that mirrors Claude Code's project-level configuration. When you activate a profile, claudio symlinks each item into the correct location in your working directory. Claude Code picks everything up as usual — no changes to how it works.

**`use` links, `run` launches.** `claudio use soc` only symlinks the profile into the directory, like activating a venv. `claudio run soc` does that *and* resolves the account and tags before `exec`ing `claude` — so it is the command you want in almost every case. Starting `claude` yourself after `use` skips the account, which means whichever login happens to be Claude Code's default.

When you're done, `claudio clean` removes the symlinks without touching the profile source files.

Different directories can have different profiles active simultaneously — each is independent.

### Profiles and accounts

An account is an isolated `CLAUDE_CONFIG_DIR` under `~/.claudio/accounts/`. That isolation is the whole trick: each account keeps its own credentials, so logging in to one doesn't log the other out.

Profiles and accounts are **independent axes**. Neither knows about the other, and the same profile runs unchanged under any account — the `soc` profile is the same bundle of servers and instructions whether you're on your work login or your personal one. So the account is chosen **per invocation**, never bound to a profile.

That's also why an account is deliberately *not* recorded as directory state the way a profile is. A profile is symlinks on disk, and a directory has exactly one. An account is nothing but environment variables set by `claudio run`, so it's scoped to that one process. Two terminals sitting in the same project can run the same profile under different accounts at the same time — one on work, one on personal — and neither disturbs the other.

`claudio run` ties the two together: activate the profile if it isn't already, resolve the account and tags, set the environment, `exec claude`.

```sh
claudio account new work        # creates the config dir and logs in
claudio run soc --account work
```

### MCP servers via the Docker MCP Toolkit

By default a profile's MCP servers are managed by the [Docker MCP Toolkit](https://docs.docker.com/ai/mcp-catalog-and-toolkit/), not hand-written into `.mcp.json`. Each claudio profile maps 1:1 to a **Docker MCP profile** of the same name, and the generated `.mcp.json` simply runs the gateway:

```json
{ "mcpServers": { "MCP_DOCKER": {
  "command": "docker", "args": ["mcp", "gateway", "run", "--profile", "<name>"] } } }
```

Server management lives in Docker — claudio just creates the Docker MCP profile and plugs the gateway into your directory. Add/remove servers with the `claudio mcp` helpers (thin wrappers over `docker mcp`) or Docker Desktop.

**Docker is the base; extras cover the rest.** The Docker gateway is for *containerised* catalog servers. For MCPs Docker isn't fit for — remote HTTP/SSE endpoints, bespoke commands — keep a normal `mcp.extra.json` in the profile. claudio merges its `mcpServers` next to the gateway entry when generating `.mcp.json`:

```
profile/
├── mcp.docker        # id of the backing Docker MCP profile
└── mcp.extra.json    # { "mcpServers": { ... } } you maintain by hand

generated .mcp.json:
{ "mcpServers": {
    "MCP_DOCKER":       { ...gateway runner... },   # container servers, via Docker
    "netdata-hyperion": { "type": "http", "url": "…", "headers": { … } }
} }
```

Edit extras with `claudio mcp <name> extra` (regenerates `.mcp.json` so active links update). Merging uses `jq` — only needed when a profile actually has extras.

> **Why extras rather than Docker for these?** The Docker MCP Toolkit can't yet add arbitrary remote/custom MCP servers (http/sse/ssh) by URL — you'd hand-author a catalog entry per server, and a generic shape can't be fanned across N targets (a catalog entry maps to one named server; re-adding just replaces it). That gap is tracked upstream in [docker/mcp-gateway#139](https://github.com/docker/mcp-gateway/issues/139), where the fan-out use cases and a couple of proposed shapes have been added. Until it lands, `mcp.extra.json` is the pragmatic route.

Prefer the old hand-written `.mcp.json` for the whole profile? Create it with `claudio new <name> --manual`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `CLAUDIO_PROFILES` | `~/.claudio/profiles` | Directory where profile directories are stored |
| `CLAUDIO_ACCOUNTS` | `~/.claudio/accounts` | Directory where account config directories are stored |
| `CLAUDIO_CONF` | `~/.claudio/claudio.conf` | Global defaults file |
| `CLAUDIO_LEGACY` | `~/.claude` | Where claudio's data used to live, for `claudio migrate` |
| `CLAUDIO_ACCOUNT` | *(unset)* | Account to use when none is given on the command line |
| `CLAUDIO_TAGS` | *(unset)* | Extra tags, comma separated (`k=v,k=v`) |
| `CLAUDIO_HOST` | *(short hostname)* | Machine name for the automatic `host=` tag |
| `CLAUDIO_USAGE_DIR` | `~/.claudio-usage` | Where the recorder writes and `claudio usage` reads; exported into the receiver |
| `CLAUDIO_SELF` | *(set by `claudio usage`)* | The claudio `claudio usage doctor` asks about this machine's config; without it `doctor` interrogates whichever claudio is first on `$PATH`, which need not be the one that ran it |
| `CLAUDIO_DOCKER` | `docker` | Docker binary used for `docker mcp` calls |
| `CLAUDIO_USER_SETTINGS` | `~/.claude/settings.json` | Claude Code's own user settings — claudio **reads** this and never writes it |
| `CLAUDIO_MANAGED_SETTINGS` | *(OS policy path)* | Enterprise managed settings — read only, same reason |
| `CLAUDIO_GIT` | `git` | git binary used by the update check |
| `CLAUDIO_REPO` | *(the GitHub clone URL)* | Repository the update check reads tags from |
| `CLAUDIO_UPDATE` | *(unset)* | `0` disables the update machinery entirely — the switch for CI |
| `EDITOR` | `vim` | Editor used by `edit` and `mcp <profile> extra` |

Two files claudio writes into a project directory:

| File | Contents | Shareable? |
|---|---|---|
| `./.claudio` | Name of the active profile | Yes — it names the *project's* profile |
| `./.claudio.local` | Your defaults: `account=`, `tag=`, telemetry keys | No — `claudio default` gitignores it |

**Uninstalling.** Removing the program and removing your data are two separate steps, and only the second one is destructive:

```sh
sudo make uninstall          # from the checkout — or: brew uninstall claudio
claudio clean                # per project, to drop that directory's symlinks
rm -rf ~/.claudio            # every profile and EVERY STORED LOGIN
rm -rf ~/.claudio-usage      # every recorded sample and request row
```

`make uninstall` removes `$PREFIX/bin/claudio` and `$PREFIX/libexec/claudio/` and nothing else — in particular it never removes `$PREFIX/bin` or `$PREFIX/libexec` themselves, because on the `PREFIX="$HOME/.local"` path above those are directories you made and put on your `$PATH`. (Staging with `DESTDIR=` is the one exception, and only there: a stage is built empty, so the two directories are removed if the uninstall leaves them empty, which is what keeps them out of a built package.) Upgrading is just re-running the install. Claude Code's own `~/.claude` is untouched either way — claudio never writes inside it, and a test asserts that.

### Config files

Defaults for accounts, tags and telemetry live in plain config files. All three share one format — `key=value`, one per line, `#` comments and blank lines ignored — and layer from broad to narrow:

| Layer | File | Applies to |
|---|---|---|
| Global | `$CLAUDIO_CONF` (`~/.claudio/claudio.conf`) | you, everywhere |
| Profile | `$CLAUDIO_PROFILES/<profile>/claudio.conf` | every project using that profile |
| Project | `./.claudio.local` | this directory only |

`tag=` may repeat; every other key takes the **last** value in the file.

| Key | Meaning |
|---|---|
| `account=<name>` | Default account |
| `tag=key=value` | Extra tag (repeatable) |
| `logging=none` \| `local` \| `remote` | What claudio records — opt-in, off unless set |
| `otel_protocol=<proto>` | OTLP protocol (default `http/json` — what claudio's own receiver speaks) |
| `otel_endpoint=<url>` | OTLP endpoint (default `http://localhost:4318` — where it listens) |
| `usage_dir=<path>` | Where samples go (default `~/.claudio-usage`, which is where `claudio usage` looks) |
| `account.<name>.logging=…` | Per-account logging — **global conf only** |
| `account.<name>.<otel key>=…` | Per-account endpoint/protocol — **global conf only** |
| `account.<name>.ship_url=<url>` | Where that account's records are shipped — **global conf only**, no default |
| `account.<name>.ship_token=<tok>` | Bearer token sent with them, a tenant label for routing — **global conf only** |
| `statusline=<command>` | The renderer claudio's status line shim forwards to — **global conf only** |
| `receiver=<path>` | The OTLP receiver `run` starts and the shim keeps alive — **global conf only** |
| `update_check=1` \| `0` | Check for a newer claudio, or never mention it again — **global conf only** |
| `otel=1` | **Deprecated** — use `logging=local`. Read for one release, then removed |
| `usage=1` | **Deprecated** — use `logging=local`. Read for one release, then removed |

### `logging=` — one key, both streams

`logging=none|local|remote` is the whole of it. One key decides both of the streams claudio records, in every layer, with the same per-account inversion as everything else here.

| Value | Stream A (API requests, via OTLP) | Stream B (plan usage, via the status line) |
|---|---|---|
| `none` *(default)* | off | off |
| `local` | recorded to `usage_dir` | recorded to `usage_dir` |
| `remote` | recorded to `usage_dir`, **and shipped** to that account's `ship_url` | same |

> **`remote` needs a destination, and says so when it has none.** Destinations are per account and have no default: with `logging=remote` and no `account.<name>.ship_url` — a key read from the **global conf only** — nothing is shipped and `claudio run` names the account and the exact line to add. That is deliberate — see [Shipping](#shipping-remote-logging).

**`remote` implies `local`.** The file is written either way. A remote-without-local mode was considered and dropped: it saves a rotating 32MB file and buys a state in which a healthy setup is indistinguishable from a broken one.

A value that is none of those three is **off, and said out loud** — claudio will not guess which way you meant it.

```sh
# ~/.claudio/claudio.conf
account=personal
logging=local                             # default for every account
otel_endpoint=http://otel.lan:4317

account.personal.logging=none             # this account never reports, ever
account.work.logging=remote               # …and this one reports to a server
account.work.ship_url=https://usage.corp/v1/ship
account.work.ship_token=t-9f2c             # a tenant label for routing, not a password
account.work.otel_endpoint=http://otel.corp:4317

statusline=claude-statusline              # the renderer the shim forwards to
update_check=1                            # or 0, and you never hear about it again
```

The `account.<name>.` prefix works with `logging`, `otel_protocol`, `otel_endpoint`, `ship_url` and `ship_token`, and it is read **only** from the global conf — machine-level policy stays in one readable file. It is the one layer that goes narrow to broad: a privacy or reporting choice attached to an identity must not be overridable by a file inside a repo, which anyone can have written.

The two `ship_` keys go one step further: there is **no plain `ship_url` key at any layer**. A destination has no broad default to fall back to, because the failure that matters here fails closed — see below.

### Shipping (remote logging)

With `logging=remote` **and** a destination configured for the account, claudio ships two files to it: `ledger.jsonl` (one row per API request, normalised out of the raw capture by the receiver's own ingest tick — you do not have to run anything) and `samples.jsonl` (one row per plan observation). Nothing else. `raw/` is never shipped — it is larger and it can contain prompt and response bodies, and the shipper does not know its path.

There is no daemon and no cron. The shipper is a tick inside the OTLP receiver `claudio run` already starts, plus one final pass before that receiver's idle exit — so it runs exactly when there is data and stops when there is not.

**One account per destination, and nothing ships without one.**

| Configuration | What ships |
|---|---|
| `logging=remote`, `account.work.ship_url` set | work's rows and samples, and only those |
| `logging=remote`, no `ship_url` for the account | nothing; `run` names the account and the line to add |
| `logging=remote`, no account resolved | nothing; destinations are keyed by account |
| `logging=local` with a `ship_url` set | nothing; the mode decides, not the presence of a URL |
| `logging=remote` written in a project or profile file | nothing; the shipper reads the mode from the global conf only, and `run` says so |

That table is the whole feature. One `usage_dir` holds every account's records interleaved in one file — the captured fixture in this repository has three accounts in eight rows — so a shipper with a default destination, or one that shipped whatever it found, would publish your personal accounts to your employer. Records are selected by the account's own address as read from its `.claude.json`, never by the `account=` label (which `--tag account=` overwrites), and the account UUID is stamped on each stream-A row here, on the machine, because that is the only place the mapping is readable.

**When it fails, you hear about it.** A failed delivery leaves the stored byte offset exactly where it was, so the next pass resends the same range — which is safe because the server deduplicates both streams — and writes `<usage_dir>/ship.err`, which the next `claudio run` prints on stderr. A pass that delivered but could not place some rows (a row produced with no account resolved, or with `tag=email=` suppressing the address) writes `<usage_dir>/ship.warn` instead, and that is printed too — as a **running total**, because the offset moves past such a row and a per-pass warning therefore lived about fifteen seconds. Delete `<usage_dir>/state/ship-unplaceable.json` to acknowledge them.

**And when it delivers nothing at all, you hear about that too.** A destination whose configured account is healthy in every other respect can still match no row: stream A is placed by the `email=` tag against the address in that account's own `.claude.json`, so a re-login under a new address, or a `tag=email=` line, makes every row miss. claudio names the account, prints both addresses, and does not advance that stream's offset — so the backlog ships when the two agree, rather than having been consumed while nothing was said.

**The bearer token is a tenant label, not a security boundary.** It routes a batch to the right tenant. There is no mTLS, no CA and no certificate rotation here; the network between you and your server is assumed to be one you already trust, and if it is not, that is a network problem and not one this key solves.

**The other end of the wire is in this repository, and it has a report.** `server/` is the receiving door plus the reconciler that turns both streams into per-account windows, attribution and the residual — the movement no observed request explains, which is the half neither stream can give you on its own. It serves that as a read-only JSON API at `GET /api/v1/`, and it holds **no page of its own**: **omini** is the official front end and lives in its own project, running either bundled with a server of its own or attached to one that already exists. `GET /` is a 404. It is a separate program with its own readme (`server/README.md`); claudio ships to it and knows nothing else about it.

> The server's query layer is the only place in this project that uses a non-stdlib dependency — DuckDB, reading the shipped JSONL **in place**, so the files stay the record of truth. **claudio gains nothing at all from it**, and `claudio usage` still reads the same files with the standard library, offline, with no server running.

**Backups and retention are yours.** claudio never deletes a record and has no retention pass, no archive command and no disk-usage guard beyond the receiver's rotation of `raw/`. `usage_dir` is a directory of append-only files: back it up, prune it or leave it alone as you see fit.

### Migrating from `otel=` and `usage=`

`logging=` replaces both. This release still reads the old keys and tells you it is doing so; the next one deletes them.

| Old | New |
|---|---|
| `otel=1` **and** `usage=1` | `logging=local` |
| `otel=1` alone / `usage=1` alone | `logging=local` (the two streams are no longer separable) |
| `otel=0`, `usage=0`, or neither set | `logging=none` |
| `account.x.otel=0` + `account.x.usage=0` | `account.x.logging=none` |

**`logging=` anywhere retires the old keys everywhere** — in every layer, account-level ones included. It is one rule rather than a per-layer merge, because a merge leaves a config in which some old lines still bite and some do not, and because the direction had to be the one that cannot make `logging=local` quietly do nothing. The price is that an `account.personal.otel=0` stops applying the moment you write a `logging=` line, so claudio names every retired key on stderr on every run until you delete it:

```
claudio: logging=local is set, so the deprecated otel= is ignored — delete it.
```

Leave `logging=` out entirely and the old keys still decide, independently of each other exactly as before, with their own notice:

```
claudio: otel= and usage= are deprecated — use logging=none|local|remote. Still read in this release; the next will not.
```

**Account precedence**, highest first:

```
--account <name>  >  $CLAUDIO_ACCOUNT  >  ./.claudio.local  >  profile claudio.conf
                  >  machine default (claudio account default)
```

The machine default is the point of `claudio account default <name>`: most directories are ad-hoc, and you almost certainly want one of your accounts there rather than whichever login happens to be Claude Code's built-in default.

```sh
claudio account default work     # set it
claudio account default          # show it
claudio account list             # * marks it
```

If none of them resolves, `CLAUDE_CONFIG_DIR` is left unset and Claude Code uses your ordinary login — exactly the behaviour claudio had before accounts existed. `run` warns when that happens, so you never silently end up on the wrong plan:

```
$ claudio run
warning: no profile active in this directory (ad-hoc run)
warning: no account configured — using Claude Code's default login
         set one with: claudio account default <name>
tags:    host=laptop
```

Once configured, `run` states what it resolved and **where it came from**:

```
$ claudio run
warning: no profile active in this directory (ad-hoc run)
account: work (from machine default)
tags:    account=work,host=laptop,email=you@work.example

$ claudio run soc --account personal
profile: soc
account: personal (from flag)
tags:    profile=soc,account=personal,host=laptop,email=you@personal.example
```

Those three lines go to stderr, so they never pollute anything you pipe.

The telemetry keys resolve project, then profile, then global — with one deliberate exception above all three, described next.

### Tags

Every `run` is tagged, and the merged set is exported as `OTEL_RESOURCE_ATTRIBUTES`. Tags layer per key, **later wins**:

```
automatic (profile=, account=, host=, email=)  <  $CLAUDIO_CONF  <  profile claudio.conf  <  ./.claudio.local  <  $CLAUDIO_TAGS  <  --tag
```

The three config files sit in the middle in the same broad-to-narrow order every other key uses, so `tag=team=soc` in `~/.claudio/claudio.conf` tags every run on the machine and a profile or project can still override it per key.

`profile=<name>`, `account=<name>`, `host=<slug>` and `email=<address>` are added automatically, so runs are attributable to a project, a login, a machine **and the human behind it** without any configuration. Because later layers win *per key*, you can override even those (`--tag account=shared`, `--tag host=build-box`, `--tag email=redacted`).

> **`email=` is real PII, so read this before switching telemetry on.** It is the address Claude Code is logged in as, read from the resolved account's own `.claude.json` — the identity behind `account=`, which is only a label you picked and which anyone can overwrite with `--tag account=`. Two consequences worth knowing:
>
> - It lands in **every exported record**, and on a metrics backend it becomes a label, so you get one time series per address. That is usually the point, and occasionally a cardinality problem.
> - It is only emitted when an account resolves. An ad-hoc run with no account gets no `email=` — claudio deliberately does not fall back to reading `~/.claude` for the default login's address, because that login is not claudio's to describe.
>
> To suppress it, either pin the tag (`--tag email=redacted`, or `tag=email=redacted` in any config file — the automatic value sits in the bottom layer, so any of them wins) or leave telemetry off, which is the default. `OTEL_RESOURCE_ATTRIBUTES` is exported either way, but nothing reads it without an exporter.

Tag values may not contain spaces or commas — `OTEL_RESOURCE_ATTRIBUTES` is comma separated with no quoting. A real machine name usually breaks that rule, so `host=` is **sanitised, not rejected**: the domain is dropped (`web-01.corp.example.com` → `web-01`, and macOS flips between `.local` and `.lan` with the network), the name is lowercased, and every run of characters outside `[a-z0-9_-]` collapses to a single `-` (`Gabriel's MacBook Pro` → `gabriel-s-macbook-pro`). Set `CLAUDIO_HOST` to choose the name yourself — useful in a container, where the detected hostname is a random id. If nothing usable is left after sanitising, the tag is omitted rather than emitted empty.

**Telemetry is opt-in.** Nothing is exported unless `logging=local` (or `remote`) is configured; `--no-telemetry` suppresses it for a single run. `OTEL_RESOURCE_ATTRIBUTES` is always exported, but it's inert on its own — nothing reads it without an exporter configured.

**If nothing arrives at your collector, run once with `claude --debug`.** OTLP export failures are silent by default — a refused connection or an HTTP 500 produces no message and exit status 0, so a wrong `otel_endpoint` looks exactly like a working one. `--debug` writes a log to `<account>/debug/latest`; `grep -i telemetry` it and you will see whether Claude Code even tried:

```sh
claudio run --account work -- -p OK --debug
grep -i telemetry ~/.claudio/accounts/work/debug/latest
```

```
[3P telemetry] isTelemetryEnabled=true (CLAUDE_CODE_ENABLE_TELEMETRY=1)
[3P telemetry] getOtlpReaders: protocol=http/json, endpoint=http://localhost:4318
[3P telemetry] First logs export: SUCCESS
```

`SUCCESS` there means claudio's half is done and the data left the machine — anything still missing is your collector dropping it, which is a much easier thing to look at. Put `-p` **before** `--debug`: `--debug` takes an optional filter argument and will otherwise swallow the flag after it. There is deliberately no claudio option for this, because `--debug` already does the job and passes straight through `--`.

### Per-account telemetry

Telemetry keys resolve across **four** layers, and the top one is not where you would guess:

| Order | Source |
|---|---|
| 1 | `account.<account>.<key>` in the **global** conf |
| 2 | project `./.claudio.local` |
| 3 | profile `claudio.conf` |
| 4 | global `<key>` |

The account layer beating project *and* profile is the point, not an oversight. A privacy choice attached to an identity must not be overridable by a file inside a repo, which someone else may have written:

```sh
# ~/.claudio/claudio.conf
logging=local                 # log everything by default
account.personal.logging=none # …except this login, ever
```

`account.personal.logging=none` is a **guarantee**, not a default — no `.claudio.local` and no profile can turn that account's logging back on. `--no-telemetry` still silences a single run whatever the layers agreed, and when no account resolves the layer is simply skipped.

### The receiver: started on demand, never installed

Telemetry has to arrive somewhere. That somewhere is `usage/recv/otlp-recv`, a
stdlib-Python OTLP receiver in this repo — **not** a container and **not** a
launchd or systemd service. claudio starts it for you:

```
claudio run  --spawns-->  claudio recv serve        (before it execs claude)
                              ^
status line shim -------------+  one `kill -0` per render, respawns if it died
                              |
The receiver exits by itself when no records have arrived for 30 minutes AND no
session pid under <usage_dir>/sessions/ is still alive.
```

Why on demand rather than a service: claudio writes the OTLP variables only
into the environment of the claude it `exec`s, so **stream A exists only inside
sessions claudio launched**. A resident daemon would spend most of its life
listening to an unplugged wire, and `launchctl`/`systemctl` cannot be stubbed in
this repo's hermetic test suite, while everything else here is pinned by one.

claudio starts it only for an export aimed at itself — `otel_protocol=http/json`
to a **loopback** `otel_endpoint` — and those are its defaults, so
`logging=local` on its own is enough:

```
logging=local
otel_protocol=http/json               # the default; here for the record
otel_endpoint=http://localhost:4318   # the default; here for the record
```

They were `grpc` and `4317` until recently, inherited from the Docker collector
this replaced, and that combination is one nothing here can serve: the
documented opt-in turned telemetry on, started no receiver, and sent every record to a closed
port — which Claude Code drops in silence, by design. An export to **another
host** is still left alone without a word, because claudio cannot know what is
listening there; a loopback export in a protocol this receiver cannot speak is
named on stderr at the next launch, because that one can only be a local
mistake.

Four properties are worth knowing, because each is a failure that has a shape:

- **The launch never waits on it.** The spawn is detached and nothing checks
  that it came up: Claude Code's export batches over seconds, so the receiver
  has claude's own startup to bind in, and a launch that blocks on a listener is
  a worse failure than a late bind.
- **A receiver that cannot start is never chased.** The supervisor is a status
  line, so it runs several times a second — an uncapped one would fork-bomb the
  machine. Three things stop that: whoever loses the `recv.lock` race walks away,
  a fault already written to `recv.err` is not retried, and past **5 starts in
  60 seconds** that never reached a bind nothing is started at all.
- **It says why.** `claudio run` prints `recv.err` and `recv.warn` on stderr, so
  a receiver that is quietly not there announces itself at the next launch
  rather than at the next time you go looking for data. A successful bind clears
  both. They are two files because they answer two questions: `recv.err` is why
  a **start** failed and is what stands the supervisor down, while `recv.warn`
  is what a receiver that was up reported — an undecodable body, a failed write
  — which is a loss worth printing and never a reason to stop respawning.
- **A data directory it cannot use is a diagnostic, not a shrug.** If
  `usage_dir` cannot be created, cannot be written, or holds a `recv.lock` that
  is not a lock, `claudio run` says so on stderr and names the directory. The
  alternative is the silent one: telemetry on, nothing listening, every record
  dropped at the client for ever.
- **`claudio run` registers the session.** `exec` preserves the pid, so the file
  written at `<usage_dir>/sessions/$$` names the claude process itself. The
  receiver sweeps that directory and unlinks the pids that no longer answer;
  claudio is gone a moment later and can never come back to tidy up.

`receiver=<path>` in the **global** conf names the receiver to start, and
overrides everything below it. With no such line claudio looks in three places,
in order, all derived from the **resolved** path of the running script — so a
Homebrew symlink lands on the real Cellar path rather than on `/opt/homebrew`:

| | layout |
|---|---|
| `<dir>/usage/recv/otlp-recv` | a checkout, run in place |
| `<dir>/../libexec/claudio/recv/otlp-recv` | an install: `bin/claudio` beside `libexec/claudio/` |
| `otlp-recv` | last resort, a bare name on `$PATH` |

A wrong guess is reported by name at the next `claudio run`, never retried in
silence. The third line is why copying `claudio` on its own is not an install:
that fallback names a program nothing ever puts on `$PATH`, so the receiver
never starts and the export goes to a closed port.

### Status line, and recording plan usage

Claude Code hands its status line command a JSON object on stdin, and that
object is the **only** place the 5-hour and 7-day plan percentages exist —
nothing else on the machine can see them. claudio registers a shim of its own
so they can be recorded on the way past:

```
Claude Code --JSON on stdin--> claudio statusline --shim --> a sample
                                        |
                                        +--JSON--> your renderer --> the bar
```

The shim reads the payload once, appends a sample, then hands the identical
bytes to the command named by `statusline=` and passes its output through
untouched. Point the global conf at a renderer:

```sh
# ~/.claudio/claudio.conf
statusline=claude-statusline
logging=local                 # record plan usage (off by default)
```

**What is registered is the path you invoke, not the file it resolves to.** That matters exactly once, and permanently: `brew upgrade` deletes the old `Cellar/claudio/<version>/` directory, so a status line registered at the resolved path would stop existing at your next upgrade — nothing drawn, nothing recorded, and `claudio account list` still reporting the account as wired, because it matches the shim by its arguments and not by a path that differs on every machine. claudio registers `<prefix>/bin/claudio`, which the upgrade relinks.

**It is registered against accounts, not profiles.** An account *is* a
`CLAUDE_CONFIG_DIR`, so `~/.claudio/accounts/<name>/settings.json` is the
**user-level** settings file for every session run under that account —
recording follows the identity, into any directory, with or without a profile.
That is the right axis, because the quota is account-wide.

A profile must never write one. A profile's `settings.json` is symlinked to
`.claude/settings.json`, which is **project** level and Claude Code resolves
project ahead of user: a profile-written status line would shadow the shim and
silently stop recording in exactly the projects you work in most. `claudio
statusline` says so when it finds one.

**Every account is born wired.** `claudio account new` writes the shim whether
or not `logging=` is set and whether or not a renderer is configured, so turning
either on later is a one-line edit rather than a re-setup. A shim with nothing to do
prints nothing at all, which is indistinguishable from having no status line.

**When recording is off the shim gets out of the way.** It reads its three
config keys in a single pass over the config files, checks the receiver with one
`kill -0` (a builtin, and only in a session that exports at all), and then
`exec`s the renderer without touching stdin or `jq`. It is
dispatched from the top of the script rather than the command table at the
bottom — a POSIX shell parses a file incrementally, so exiting from there never
reads the other ~4 000 lines. Measured, medians of 60 runs against the cost of
starting the shell at all: 2.8 ms under `dash` and 6.1 ms under `bash`, against
a 2.5 ms / 5.3 ms floor. From the command table it would be 9.1 ms / 16.4 ms.

**claudio looks before it writes.** Enterprise managed settings and the
account's own `settings.json` are checked first, and if either already sets a
status line, claudio reports it and writes nothing:

```
Created account 'work' (/Users/you/.claudio/accounts/work)

You already have a status line configured:
  /Library/Application Support/ClaudeCode/managed-settings.json
  bash /opt/corp/bar.sh

Left alone — claudio never overwrites a status line it did not set.
An account's settings.json is the USER-level file for every session run
under it, so writing one would replace what you already have.
...
  Preview it:   claudio statusline --preview
  Use it here:  claudio statusline --set work
```

Deliberately *not* consulted for that check: `~/.claude/settings.json` (Claude
Code does not read it at all when `CLAUDE_CONFIG_DIR` is set — the account's
file replaces it), the other accounts (otherwise the first wired account stops
every later one from ever being wired), and the project files in whatever
directory you happen to be standing in (an account is used in every directory).

**Retrofitting.** Accounts created before the shim existed record nothing, and
that is invisible unless something says so — `claudio account list` grows a
column for it, and `claudio statusline --all` wires every account at once. An
account whose `settings.json` claudio wrote *before* the shim — a status line
calling the renderer directly — is **upgraded** rather than refused: the shim
forwards to the same renderer, so the line drawn is identical and recording
starts working. One byte different from what claudio would have written and it
is your document again, left alone with the block printed for you to paste.

```
$ claudio account list
Accounts (in /Users/you/.claudio/accounts):
 * work                 logged in      shim     you@example.com
   personal             logged in      no shim
   spare                not logged in  other

Some accounts do not run claudio's status line shim, so they record
no plan usage. Wire them with: claudio statusline --all
```

#### What a sample looks like

Samples are appended to `usage_dir/samples.jsonl` (default
`~/.claudio-usage/samples.jsonl`) as one JSON object per line. A record is
written only when the plan has actually **moved** — see below — so the file
grows a few times an hour, not a few times a second.

> **That default path is a contract, not a coincidence.** The reader that comes
> with claudio — `usage/claudio-usage`, see [usage/README.md](usage/README.md) —
> looks in `~/.claudio-usage` for exactly this file. If you set `usage_dir`
> somewhere else, point the reader at the same directory or it will find nothing
> and say so as if you had never recorded anything.

claudio keeps two bookkeeping files per account in the same directory, and
neither is a sample: `.last_<config-dir-path>` holds that account's high-water
marks and reset epochs, and `account_<config-dir-path>.json` caches the
identity block (uuid, address, organisation, tier) lifted out of the account's
own 180 KB `.claude.json`. Both are disposable — deleting them costs one
`reason: first` record to re-establish the baseline.

```json
{"ts":1786489912,"account_uuid":"...","account_email":"you@example.com",
 "organization_uuid":"...","rate_limit_tier":"default_claude_max_20x",
 "five_hour_pct":7.000000000000001,"five_hour_resets_at":1786489800,
 "seven_day_pct":76,"seven_day_resets_at":1786564800,"session_id":"...",
 "model":"claude-opus-5[1m]","session_cost_usd":186.99,"profile":"soc",
 "client":"claude-code/2.1.228","project":"/Users/you/src/acme","schema":2,
 "reason":"change","host":"mba","account":"work","prompt_id":"...",
 "agent":"general","tags":"profile=soc,account=work,host=mba,email=..."}
```

`profile`, `account`, `host` and `email` are **lifted out of**
`$OTEL_RESOURCE_ATTRIBUTES` rather than derived again, so they are identical to
the tags on the OTel stream by construction — the two have to join,
and two independent derivations would eventually disagree. `session_id` and
`prompt_id` are recorded for the same reason — they are what a join would key
on — but claudio does not join anything and does not interpret a percentage:
it captures the two numbers, stamps them, and stops. Note that `account_email`
is real PII and lands
in every record; it comes from the resolved account's own `.claude.json` and
from nowhere else.

#### When a record is written

Not on every tick. Each window keeps a **high-water mark** and a reset epoch:

| What happened | Recorded? |
|---|---|
| The percentage rose past the mark | yes — `reason: change` |
| `resets_at` moved forward: the window rolled over | yes — `reason: reset`, whichever way the percentage went |
| `resets_at` moved backward | no — an older snapshot from a concurrent session |
| The percentage fell, or did not move | no |
| No state file yet, or an unusable one | yes — `reason: first` |

Replaying 65 consecutive real samples, that keeps 49 of them. The rollover row
is not an edge case: without it a window going 90% → 2% records the first
sample and then nothing at all, because 2% never beats 90% again.

Percentages are quantised to whole numbers **only to decide whether anything
moved** — what is stored is the raw float, byte for byte. The server computes
`used_percentage` as `utilization * 100`, which is where `7.000000000000001`
comes from; comparing raw floats would fire on every single tick.

**Honesty about concurrency.** The rule removes most negative movement, not
all of it. One record carries both windows, so the window that did *not* fire
records whatever this tick happened to see, which may sit below something
already written; and the state file is an unlocked read-modify-write, so
simultaneous sessions can emit duplicates and out-of-order records. Anything
computing deltas downstream must clamp negative ones rather than assume
monotonicity.

**A recorder fault costs a sample, never the line.** The rendered output is
delivered before any of this runs, every failure path is silent, and the shim
exits with the renderer's status whatever the recorder did.

### `claudio list`

Lists all available profiles. Marks the active profile for the current directory with `*`.

```sh
$ claudio list
Available profiles:
  * soc  (active)
    dev
    homelab
```

### `claudio use <profile>`

Links a profile into the current directory by symlinking its contents into place. It takes no other arguments and never launches anything — `claudio run` is the command that launches, and it links first, so `use` is only for setting a directory up without starting a session.

If a target path already exists and isn't a claudio-managed symlink, it's skipped with a warning — claudio won't clobber existing project config.

```sh
claudio use soc      # link only
claudio run          # …then launch, with the account and tags applied
```

### `claudio run [profile] [--account <name>] [--tag k=v] [--no-telemetry] [-- <claude-args>]`

The one-step launcher. Resolves the account and tags, exports the environment, then `exec`s `claude`.

**The profile is optional.** Given one, `run` activates it first if it isn't already active. Given none, it falls back to whichever profile is active in the current directory — and if there is no profile at all, it simply sets the account and tags and launches. Most directories are ad-hoc: you want a particular login and some tags there, not a full project config linked in. `run` never creates a profile, a marker or a symlink in a directory that didn't already have one.

Unlike `use`, nothing about the account is written to the directory — it exists only in the launched process. That's what lets a second terminal in the same project run under a different account.

```sh
claudio run                              # active profile (if any), configured account
claudio run soc                          # activate soc if needed, then launch
claudio run soc --account work           # same profile, work login
claudio run soc --account personal       # …and the same profile, personal login
claudio run --tag task=triage            # extra tag, just for this run
claudio run soc -- -p "fix the bug"      # forward args to claude

# Ad-hoc directory — no profile anywhere, nothing written to disk:
cd ~/scratch/some-experiment
claudio run --account personal
claudio run --account work --tag client=acme
```

### `claudio env [profile] [--account <name>] [--tag k=v] [--no-telemetry]`

Prints exactly the environment *variables* `run` would set, as `export` lines, without launching anything. Unlike `run`, it does **not** link the profile — if you name one that isn't linked here, `env` says so on stderr, because a session tagged `profile=soc` without soc's servers and instructions is exactly the mis-attribution tags exist to prevent.

```sh
$ claudio env soc --account work
export CLAUDE_CONFIG_DIR=/home/user/.claudio/accounts/work
export OTEL_RESOURCE_ATTRIBUTES='profile=soc,account=work,host=laptop,email=you@work.example'

$ eval "$(claudio env soc --account work)"   # apply to the current shell
```

Values are quoted only when they need it, which is why the tag string above is and the path is not: `@` is outside the set of characters that are safe to `eval` bare.

### `claudio account <list|new|login|path|default|rm> [name]`

Manages accounts — isolated `CLAUDE_CONFIG_DIR`s under `~/.claudio/accounts/`. Each holds its own login, so accounts never evict one another.

```sh
claudio account list          # accounts, and whether each is logged in
claudio account new work      # create the config dir AND log in (opens a browser)
claudio account login work    # log in again to an existing account
claudio account path work     # print the account's config dir
claudio account default work  # machine-wide fallback account
claudio account rm work       # delete the account and its stored login
```

`new` finishes the job rather than printing the next command to type: an account with no login does nothing, so it creates the directory and hands straight off to `claude /login`. `login` is there for re-authenticating later.

```sh
$ claudio account list
Accounts (in /home/user/.claudio/accounts):
   personal             logged in      you@personal.example
 * work                 not logged in

* machine default (used when nothing more specific applies)
```

The address answers the question the account *name* cannot: `work` is a label you chose, and nothing stops two of them pointing at the same login. It is shown only while the account is actually logged in — the address stays in the file after a login is orphaned (see [`migrate`](#claudio-migrate---dry-run---yes---scan-dir---undo)), and printing it then would imply a session that is not there.

`rm` makes you retype the account name to confirm, since it takes the stored login with it. Login state is read from `oauthAccount` in the account's `.claude.json` — the portable signal, given that macOS keeps the credentials themselves in the Keychain.

### `claudio default [--account <name>] [--tag k=v]`

Saves defaults for the current directory into `.claudio.local`, so you stop passing `--account` on every run. Setting an account replaces the existing one; tags are appended.

Because `.claudio.local` is personal rather than part of the project, `default` also adds it to `.gitignore` when the directory is a git repo, creating `.gitignore` if there isn't one.

```sh
claudio default --account work
claudio default --tag team=soc --tag env=prod
claudio run                       # now runs under work, tagged
```

### `claudio key new <account>`

Prints one line for the server's `tokens.json`:

```sh
claudio key new work
00000000-0000-4000-8000-000000000000.EXAMPLE_ONLY_not_a_real_key_do_not_use_0000
```

Paste it into the list on the server and that account can ship. Nothing else to
configure, and nothing to look up.

**The left half is the account's own uuid**, which is why this runs here rather
than on the server: the uuid lives in this account's `.claude.json` and nowhere
the person configuring the door can see it. Before, setting up shipping meant
copying a 36-character string between machines and getting a 409 with no clue
in it when you copied the wrong one.

The right half is 32 bytes from `/dev/urandom`, base64url. It is a shared
secret, not a keypair: it proves the workstation is allowed to ship, and the
`https://` certificate proves the server is the server. Cryptographic
workstation identity — mTLS or Ed25519 — is roadmapped for the day the door is
reachable outside a trusted network.

**Rotation is running this again**, pasting the new line, and deleting the old
one. There is no expiry and no schedule: a key you can replace in ten seconds
does not need one.

### `claudio statusline [--preview | --set <account> | --all]`

Works with the `statusline=<command>` and `logging=` keys (see [Status line, and
recording plan usage](#status-line-and-recording-plan-usage)). With no argument
it reports what is configured, whether recording is on, which accounts are
wired, and what is in effect **in this directory**.

```sh
claudio statusline                  # what's configured, wired and in effect
claudio statusline --preview        # run the renderer for real and show the line
claudio statusline --set work       # wire the shim into account 'work'
claudio statusline --all            # wire it into every account
```

`--preview` runs the renderer exactly as Claude Code would — through a shell,
with a sample session payload on stdin — under the environment `claudio run`
would build here, so the account, profile and telemetry variables a status line
reads are all really there. It runs the **renderer**, not the shim: the bytes
are the same either way, and going through the shim would append a fabricated
sample to your recording on every preview. A command that fails is reported
with its exit code rather than an empty line.

`--set` and `--all` write the `statusLine` block into an account's
`settings.json`. Either refuses if that file is one you have edited, or if it
already carries a status line that is not claudio's: adding a key to JSON
without `jq` means regenerating the document, which would drop your other
settings, so claudio prints the exact block to paste and the path to paste it
into. `--all` carries on past a refusal and exits non-zero at the end, so one
untouchable account does not stop the rest being wired.

There is a fourth form, `claudio statusline --shim`, which is what an account's
`settings.json` actually runs. You should never need to type it.

### `claudio usage <collect|ingest|show|doctor|install>`

The reader over everything the shim and the receiver recorded. Standard-library Python, shipped in `libexec/claudio/` beside the script and run in place from a checkout; `claudio usage` is the verb, `usage/claudio-usage` is the file.

| | |
|---|---|
| `collect start` / `collect stop` | start and stop the receiver by hand — normally `claudio run` does it |
| `ingest` | turn the receiver's raw capture into `ledger.jsonl` |
| `show` | what this machine sent: totals, breakdowns, filters |
| `doctor` | check the setup end to end, and exit non-zero if anything is wrong |
| `install` | print the config claudio needs, and write a default one |

**Nothing here attributes anything.** The plan percentage belongs to the *account*, and no single machine can see the whole account — two machines watching the same 5% → 7% move would each claim the full 2 points, and browser, desktop and phone sessions are invisible from here. `show` reports what this machine sent, which is a different question. Attribution is [`server/`](server/README.md)'s job.

`doctor` exits non-zero on a machine where nothing is configured yet, and that is correct rather than a fault: every `WARN` it prints sets the exit status, because it used to print `WARN ledger rows: 0` and exit 0. Its first two lines say **where** it looked and **which claudio** it asked — two of its verdicts are claudio's own answers, read out of a subprocess, and an answer attributable to no particular binary is one nobody can check.

**Three empty answers, not one.** An empty ledger says so; a `--since` that eliminated every row says *that*, quotes the bound you typed and the number of rows it removed, and says outright that the ledger is not empty; and `--by <key>` that no row carries as a column or as a `--tag` is refused with the list of keys that are there. Any `--tag` key of your own remains a legitimate axis — what is refused is the key nothing carries, because a typo and a real column full of blanks used to render byte-identical tables.

Full detail, including the ledger's columns and every diagnostic: [usage/README.md](usage/README.md).

### `claudio recv <serve|tail>`

The OTLP receiver — stream A's only door onto this machine. `claudio run` spawns it detached and the status-line shim keeps it alive, so this verb is for looking at it rather than for running it: `claudio recv serve` in one terminal shows what a session is exporting, live.

| | |
|---|---|
| `serve` | accept OTLP/HTTP JSON on loopback, in the foreground |
| `tail` | render the raw capture readably |

It binds loopback only and refuses anything else by name. It is not a service: it exits by itself after 30 idle minutes with no live session, and how it is started, supervised and capped is [above](#the-receiver-started-on-demand-never-installed). `receiver=<path>` in the global conf names a different one, and both halves of claudio read that key from the same file.

Full detail: [usage/recv/README.md](usage/recv/README.md).

### `claudio clean`

Removes all claudio-managed symlinks from the current directory and deactivates the profile. Does not delete the profile source files, and only removes symlinks that point into the profiles directory — it won't touch files claudio doesn't manage.

```sh
claudio clean
```

It also says so when it removed **nothing** — "Profile deactivated (no claudio symlinks found to remove)" — and, if the directory still holds symlinks pointing at claudio's old location, lists them and tells you to run `claudio migrate --scan .`. A deactivation message printed over a directory full of untouched symlinks is the one failure worth being loud about.

### `claudio migrate [--dry-run] [--yes] [--scan <dir>] [--undo]`

One-time, for the `~/.claude` → `~/.claudio` move described in [Upgrading](#upgrading-no-profiles-found). It always prints the full plan — what would move, which projects would be re-linked, what happens to your logins — and then asks you to type `migrate` to confirm.

```sh
claudio migrate --dry-run          # print the plan and stop
claudio migrate                    # do it
claudio migrate --scan ~/work      # re-link projects under ~/work instead of $HOME (repeatable)
claudio migrate --yes              # skip the typed confirmation (scripting)
claudio migrate --undo             # put everything back
```

> **This will be removed two releases after the move.** If `claudio list` shows your profiles, you have nothing to migrate.

Two things make it more than a `mv`:

**Your logins.** On macOS the credential is not in the account directory — Claude Code keeps it in the Keychain under a name derived from the config directory's *path*. Moving an account leaves its login behind, so **every logged-in account has to log in again** afterwards. Nothing is deleted from the Keychain, and because the name is a pure function of the path, `claudio migrate --undo` moves the accounts back and the logins work again. On Linux the credential is a file inside the directory and moves with it, so no re-login is needed. `migrate` tells you which case you are in, and which accounts are affected, before it moves anything.

> **The Linux half of that has never been run on Linux.** claudio was written and tested on macOS; the Linux message is what `migrate` prints when `uname -s` is not `Darwin`, and the claim behind it comes from Claude Code's documented credential storage, not from an observed migration. Nothing about the move is platform-specific — it is `mv` and `ln -s` either way, and no credential is ever touched — so the risk is confined to the *advice*: if Linux turns out to key its credential on the path too, you would be told no re-login was needed and then find you had to. Run `claudio migrate --dry-run` first, and if a login does not survive, `claudio account login <name>` puts it back.

**Your projects' symlinks.** Activating a profile writes *absolute* symlinks into a project. Move the profiles directory and claudio does not merely dangle them, it stops recognising them as its own: `clean` reports success while removing nothing, and `use` reports success while relinking nothing. `migrate` therefore rewrites the link targets in place, matching by **target prefix** rather than by the list of items it manages — so a claudio link at a path claudio never writes (a hand-made `.mcp.json.bak`, say) is fixed too, while links that have nothing to do with claudio are left alone. A link whose new target does not exist yet is repointed anyway and reported, because `use` will fill that path in; a link belonging to a profile whose move was refused is left exactly as it is, since it still works.

**Which projects it looks at.** With no `--scan`, it searches `$HOME` four levels deep for `.claudio` markers — deep enough for every project in a normal home directory, and fast. Passing `--scan <dir>` **replaces** that search rather than extending it, so name every root you care about in one command, or run `migrate` once for `$HOME` and again for the rest. Each root is also treated as a project in its own right, so `claudio migrate --scan .` still fixes the current directory after a `clean` has deleted its marker.

Safe to run again at any time: it only moves what is still in the old place and only rewrites links that still point there. It never overwrites an existing destination — a collision is reported, that item and its links are left alone, the rest of the migration continues, and the command exits non-zero so a script notices. Collisions are **shown in the plan**, marked `CONFLICT`, before you are asked to confirm, so `--dry-run` tells you about them while you can still do something about it. If anything at all was skipped or failed, the closing summary says `FINISHED WITH PROBLEMS — the migration is INCOMPLETE` instead of reporting where your data now lives; when it does report that, the move really happened.

Interrupting it is safe. Data is moved one profile or account at a time with `mv`, so a kill leaves every item either wholly moved or wholly untouched, never half-copied, and re-running finishes the job. A crash between the move and the re-link leaves projects pointing at the old path — exactly the state a plain re-run repairs.

> **Odd characters in paths.** Spaces are fine everywhere — home directory, project, profile name. A **tab** in a *project* path or a *link* name is handled too. A tab in a *profile or account name* is refused by name and left where it is (rename it and run `migrate` again); a symlink whose name contains a **newline** cannot be repointed at all and is reported so you can fix it by hand. Nothing in either case is skipped silently.

> **Set any of `CLAUDIO_PROFILES`, `CLAUDIO_ACCOUNTS`, `CLAUDIO_CONF` or `CLAUDIO_LEGACY` and the automatic scan switches off.** Having been told your directories are somewhere unusual, `migrate` will not go on to assume your projects are in the usual place: it moves the data and re-links only the roots you name with `--scan`.

### `claudio update [--check]`

Reports whether a newer claudio exists and names the command that installs it. It does **not** install anything itself: claudio was put where it is by something that owns the install — Homebrew, a distribution package, or `make install` from a checkout, quite possibly with `sudo` — and the honest thing is to hand back to whichever of those it was. It is also no longer one file to overwrite: `$PREFIX/bin/claudio` without the matching `$PREFIX/libexec/claudio/` is a claudio whose two telemetry verbs cannot find themselves.

```sh
claudio --version         # what you are running
claudio update            # check, and say how to upgrade
claudio update --check    # the same, but writes nothing at all (not even the stamp)
```

> **There are no release tags yet.** Until the first one is pushed, a non-Homebrew `claudio update` can only ever say *"No releases are tagged yet in `<repo>` — nothing to compare against."* It deliberately does not say "up to date": an update check that claims good news on no evidence is one nobody believes later.

**How it decides.** If claudio is running from under a Homebrew Cellar, brew owns the install and the version list, so `update` defers to it: the installed version is the directory name under `<prefix>/Cellar/claudio/`, the available one comes from the tap formula under `<prefix>/Library/Taps/`, and the answer is `brew upgrade claudio`. `brew` itself is never invoked — it is Ruby and takes about 2.9 seconds to start, against 0.007 seconds to read those two paths. Anywhere else, the embedded `VERSION` is compared against the newest tag from `git ls-remote --tags`, which needs no clone, no auth for a public repo, and no `gh`.

**The notice inside `claudio run`.** `run` never waits on the network. It reads one small stamp file (`~/.claudio/update-stamp`, alongside the global conf); if that is more than 24 hours old it forks a detached refresh and carries straight on to launching Claude Code. A new version is announced **once per version**, not once per run.

The whole thing is opt-in, with three states in `update_check=` in the global conf:

| `update_check` | Behaviour |
|---|---|
| *unset* | Never checks. Prints the offer **once**, on stderr, without prompting |
| `1` | Enabled: one background check a day, one notice per new version |
| `0` | Declined. Nothing is checked and nothing is ever said again |

```sh
echo update_check=1 >> ~/.claudio/claudio.conf    # yes
echo update_check=0 >> ~/.claudio/claudio.conf    # never
```

Set `CLAUDIO_UPDATE=0` to switch off every part of this for a single run or for a whole CI environment.

### `claudio new <profile> [--manual]`

Creates a new profile directory with all config items scaffolded (empty JSON files, empty directories) and prints what to do next. It does not open an editor — use `claudio edit <profile> [component]` when you want one. Fails if the profile already exists.

By default it also creates a matching **Docker MCP profile** and writes a gateway-runner `.mcp.json` (Docker MCP mode). Pass `--manual` to skip that and hand-write `.mcp.json` yourself. If the Docker MCP Toolkit isn't available, it falls back to manual mode automatically.

```sh
claudio new soc            # Docker MCP mode (default)
claudio new legacy --manual  # hand-written .mcp.json
```

### `claudio mcp <profile> <action> ...`

Thin helpers over `docker mcp` for a Docker-mode profile's servers. Management stays in Docker — these just save you the syntax.

```sh
claudio mcp soc add ghcr.io/acme/my-server:latest   # bare refs default to docker://
claudio mcp soc add catalog://mcp/docker-mcp-catalog/github
claudio mcp soc rm github                            # remove a server
claudio mcp soc                                      # show the profile's servers (+ extras)
claudio mcp soc extra                                # edit mcp.extra.json (non-Docker MCPs)
claudio mcp soc config --set key=value               # passthrough to docker mcp profile config
claudio mcp catalog mcp/community-registry:latest    # import a catalog from an OCI reference
claudio mcp catalog                                  # list imported catalogs
```

### `claudio edit [profile] [component]`

Opens a profile or a specific component in `$EDITOR`. Creates the component if it doesn't exist. With no profile named, it edits the one active in the current directory.

```sh
claudio edit                 # the profile active here
claudio edit soc             # open the whole profile directory
claudio edit soc mcp         # edit mcp.json
claudio edit soc settings    # edit settings.json
claudio edit soc claude      # edit CLAUDE.md
claudio edit soc skills      # edit skills directory
claudio edit soc agents      # edit agents directory
claudio edit soc hooks       # edit hooks directory
claudio edit soc commands    # edit commands directory
```

Available components: `mcp`, `settings`, `local-settings`, `claude`, `local-claude`, `commands`, `skills`, `agents`, `hooks`.

### `claudio show [profile] [component]`

Lists each config item with its target path and size — one line each, so the output doesn't grow with the profile. Name a component (the same names `edit` takes) to print that item's contents instead. With no profile named, it shows the one active in the current directory.

```sh
$ claudio show soc
Profile: soc
Path:    /home/user/.claudio/profiles/soc
MCP:     Docker MCP Toolkit (docker profile: soc)

[mcp.json] -> .mcp.json  (8 lines)
[CLAUDE.md] -> CLAUDE.md  (112 lines)
[skills] -> .claude/skills  (6 files)
…

$ claudio show soc claude    # print CLAUDE.md itself
```

### `claudio current`

Prints the active profile for the current directory. When there is none it says so on stderr and exits non-zero, so `p=$(claudio current) || p=none` works.

```sh
$ claudio current
soc (/home/user/.claudio/profiles/soc)
```

### `claudio init <profile>`

Captures the current directory's Claude Code configuration into a new profile. Copies all recognised config files and directories, following symlinks so it captures the resolved content (as a manual-mode profile). Useful for snapshotting an environment you want to reuse elsewhere.

```sh
claudio init my-setup
```

### `claudio help`

Prints the command summary — the same text as `claudio` with no arguments, and the list this reference is checked against by the test suite. `claudio --version` prints the version alone; it is a flag rather than a verb, deliberately, so that the verb list stays a list of things that do something.

```sh
claudio help
claudio --version
```

## Testing

```sh
make check       # bash test.sh, dash test.sh, and both Python suites
```

Or one at a time:

```sh
bash test.sh
dash test.sh                      # /bin/sh on macOS is bash 3.2, which hides portability bugs
python3 usage/tests/test_all.py   # the recorder's reader
python3 server/tests/test_all.py  # the reconciler — NEEDS DuckDB; see server/README.md
```

The reconciler's suite **refuses** on an interpreter without DuckDB rather than reporting a partial pass: 44 of its 192 test functions — 918 assertions — self-skip without the engine, and a banner over an exit 0 is nothing that CI, `&&` chains or `make` read. It names the two ways to supply the engine, and `--allow-skips` accepts the partial run if that is genuinely what you want (`make check SERVER_SUITE_ARGS=--allow-skips`).

Run **both** shells. `claudio` is executed by whichever shell you run the suite with, so `dash test.sh` is what actually proves the script is POSIX rather than accidentally bash — each run prints `claudio under: <shell>` so you can see which one you just checked.

Covers all commands, live-link behaviour, Docker MCP mode, mixed mode (extras), the `mcp` helpers, conflict detection and error handling; accounts, tag layering, account precedence, opt-in telemetry and its per-account override; the status line, including every way claudio can find one already in effect and refuse to write; the update check, its three states, its Homebrew path and the assertion that it never blocks `run`; and the whole `~/.claude` → `~/.claudio` migration — the plan, the confirmation, the moves, the symlink repointing, idempotence, `--undo`, and the guarantee that nothing is ever overwritten. The help text is checked against the dispatch table in **both** directions, and the environment-variable tables in `claudio`, this file and `CLAUDE.md` are checked against the variables the script actually reads.

It also runs the **real `Makefile`** into a temporary `DESTDIR` and then runs the installed `claudio usage` and `claudio recv` out of the result, with a decoy of each on `$PATH` so a resolver that fell through to a bare name is caught rather than congratulated. That is the guard on the failure this project's install instructions used to be.

Everything executes in a temp directory with stubbed `docker` and `git`, and never reads or writes the real `~/.claude` or `~/.claudio` — Claude Code's own settings files are redirected too, the migration tests get their own legacy and destination trees, and there are explicit assertions that everything stayed inside them. Mixed-mode, JSON-validity and every usage-recording test self-skips when `jq` is not installed, so **the total is machine-dependent** and is deliberately not quoted here: every version of this sentence that named a number has been wrong within a release.

The reader in `usage/` has its own suite, in Python because it is. Its fixtures are **real captured payloads with the identities replaced** — synthetic addresses, account labels and UUIDs, consistently remapped so the joins still join. Structure, field names and token counts are untouched, because validating against a generator written to match your own parser is how the six silent data-loss bugs in its history survived review.

## Licence

[BSD 2-Clause](LICENCE) — © 2026 Gabriel Belli. Copy it, change it, ship it; just keep the notice.
