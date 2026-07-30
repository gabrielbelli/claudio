# claudio

Claude Code profile manager. Bundles MCP servers, permissions, hooks, skills, commands, subagents, and instructions into named profiles. Activating a profile symlinks everything into the current directory — like a venv for Claude Code.

## Installation

```sh
chmod +x claudio
sudo cp claudio /usr/local/bin/
mkdir -p ~/.claude/profiles
```

## How It Works

A profile is a directory in `~/.claude/profiles/` that mirrors Claude Code's project-level configuration. When you activate a profile, claudio symlinks each item into the correct location in your working directory. Claude Code picks everything up as usual — no changes to how it works. Activation does **not** launch Claude Code; you run `claude` yourself (like activating a venv), or pass `-l`/`--launch` to start it immediately.

When you're done, `claudio clean` removes the symlinks without touching the profile source files.

Different directories can have different profiles active simultaneously — each is independent.

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

Prefer the old hand-written `.mcp.json` for the whole profile? Create it with `claudio new <name> --manual`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `CLAUDIO_PROFILES` | `~/.claude/profiles` | Directory where profile directories are stored |
| `CLAUDIO_DOCKER` | `docker` | Docker binary used for `docker mcp` calls |
| `EDITOR` | `vim` | Editor used by `new` and `edit` commands |

## Profile Structure

All items are always present — `new` scaffolds everything, `use` fills in any blanks before symlinking. This makes profiles **live links**: add a skill or command to the profile directory after activation and Claude sees it immediately — no need to re-activate.

```
~/.claude/profiles/<name>/
├── mcp.json              → .mcp.json                    MCP server definitions
├── settings.json         → .claude/settings.json        Permissions, hooks, env vars, sandbox
├── settings.local.json   → .claude/settings.local.json  Personal overrides
├── CLAUDE.md             → CLAUDE.md                    Project instructions
├── CLAUDE.local.md       → CLAUDE.local.md              Personal instructions
├── commands/             → .claude/commands              Custom slash commands
├── skills/               → .claude/skills               Skills with supporting files
├── agents/               → .claude/agents               Subagent personas
└── hooks/                → .claude/hooks                Hook scripts
```

Secrets should never go in profile files. Keep them in your shell environment (e.g. `GITHUB_TOKEN`) — MCP servers pass env vars through automatically.

## Commands

### `claudio list`

Lists all available profiles. Marks the active profile for the current directory with `*`.

```sh
$ claudio list
Available profiles:
  * soc  (active)
    dev
    homelab
```

### `claudio use <profile> [-l|--launch] [-- <claude-args>]`

Activates a profile in the current directory by symlinking its contents into place. It does **not** launch Claude Code — run `claude` yourself when ready. Pass `-l`/`--launch` to activate *and* launch; any arguments after `--` are then forwarded to `claude`.

If a target path already exists and isn't a claudio-managed symlink, it's skipped with a warning — claudio won't clobber existing project config.

```sh
claudio use soc                      # activate only, then run `claude` yourself
claudio use dev -l                   # activate and launch Claude Code
claudio use dev -l -- -p "fix bug"   # activate, launch, forward args to claude
```

### `claudio clean`

Removes all claudio-managed symlinks from the current directory and deactivates the profile. Does not delete the profile source files.

```sh
claudio clean
```

### `claudio new <profile> [--manual]`

Creates a new profile directory with all config items scaffolded (empty JSON files, empty directories), then opens it in `$EDITOR`. Fails if the profile already exists.

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

### `claudio edit <profile> [component]`

Opens a profile or a specific component in `$EDITOR`. Creates the component if it doesn't exist.

```sh
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

### `claudio show <profile>`

Displays the profile contents — lists each config item with its target path and contents.

```sh
claudio show soc
```

### `claudio current`

Shows the active profile for the current directory.

```sh
$ claudio current
soc (/home/user/.claude/profiles/soc)
```

### `claudio init <profile>`

Captures the current directory's Claude Code configuration into a new profile. Copies all recognised config files and directories. Useful for snapshotting an environment you want to reuse elsewhere.

```sh
claudio init my-setup
```

## Example Workflow

```sh
# Create a profile from scratch (also creates a Docker MCP profile)
claudio new soc
# Add MCP servers (managed by Docker)
claudio mcp soc add ghcr.io/acme/my-server:latest
# Edit the rest
claudio edit soc settings    # set permissions and hooks
claudio edit soc claude      # write project instructions

# Or capture an existing project's config
cd ~/projects/existing-setup
claudio init soc

# Use in a project (activate only), then start Claude yourself
cd ~/projects/incident-response
claudio use soc
claude

# Switch profiles
claudio clean
claudio use dev -l           # activate and launch in one step

# Check what's active
claudio current

# Inspect a profile
claudio show soc
```

## Testing

```sh
sh test.sh
```

Runs 131 tests covering all commands, live-link behaviour, Docker MCP mode, mixed mode (extras), the `mcp` helpers, conflict detection, and error handling. Executes in a temp directory (with a stubbed `docker`) and cleans up after itself. Mixed-mode tests self-skip when `jq` is not installed.

## Notes

- claudio writes a `.claudio` marker file to the working directory to track the active profile. Add it to `.gitignore` if you prefer.
- Profiles are live-linked directories. Changes to the profile take effect immediately in any project using it.
- `clean` only removes symlinks that point into the profiles directory — it won't touch files that aren't managed by claudio.
- `use` activates only; it does not launch Claude Code unless you pass `-l`/`--launch` (which `exec`s `claude`, replacing the shell process).
- Docker MCP mode stores the backing Docker profile id in a `mcp.docker` marker inside the profile, and (re)generates the gateway-runner `mcp.json` on each `use`. Server management is delegated to `docker mcp` / Docker Desktop.
- A profile may also carry an `mcp.extra.json` (`{ "mcpServers": { … } }`) for MCPs Docker isn't fit for — remote HTTP/SSE servers, bespoke commands. Its entries are merged next to the gateway when `mcp.json` is generated. Merging needs `jq`, and only when extras are present; pure-Docker and manual profiles have no extra dependency.
- `init` follows symlinks when copying, so it captures the resolved content (as a manual-mode profile).
- POSIX sh — no bash required, no external dependencies beyond coreutils (plus Docker for MCP mode).
