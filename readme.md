# claudio

Claude Code profile manager. Bundles MCP servers, permissions, hooks, skills, commands, subagents, and instructions into named profiles. Activating a profile symlinks everything into the current directory — like a venv for Claude Code.

## Installation

```sh
chmod +x claudio
sudo cp claudio /usr/local/bin/
mkdir -p ~/.claude/profiles
```

## How It Works

A profile is a directory in `~/.claude/profiles/` that mirrors Claude Code's project-level configuration. When you activate a profile, claudio symlinks each item into the correct location in your working directory, then launches Claude Code. Claude Code picks everything up as usual — no changes to how it works.

When you're done, `claudio clean` removes the symlinks without touching the profile source files.

Different directories can have different profiles active simultaneously — each is independent.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `CLAUDIO_PROFILES` | `~/.claude/profiles` | Directory where profile directories are stored |
| `EDITOR` | `vim` | Editor used by `new` and `edit` commands |

## Profile Structure

Each item is optional. claudio only symlinks items that exist in the profile.

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

Secrets should never go in profile files. Keep them in your shell environment or a `.env` file you source separately.

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

### `claudio use <profile> [-- <claude-args>]`

Activates a profile in the current directory by symlinking its contents into place, then launches Claude Code. Any arguments after `--` are passed through to `claude`.

If a target path already exists and isn't a claudio-managed symlink, it's skipped with a warning — claudio won't clobber existing project config.

```sh
claudio use soc
claudio use dev -- -p "fix this bug"
```

### `claudio clean`

Removes all claudio-managed symlinks from the current directory and deactivates the profile. Does not delete the profile source files.

```sh
claudio clean
```

### `claudio new <profile>`

Creates a new profile directory with scaffolded `mcp.json` and `settings.json`, then opens it in `$EDITOR`. Fails if the profile already exists.

```sh
claudio new soc
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
# Create a profile from scratch
claudio new soc
# Edit specific parts
claudio edit soc mcp         # add MCP servers
claudio edit soc settings    # set permissions and hooks
claudio edit soc claude      # write project instructions

# Or capture an existing project's config
cd ~/projects/existing-setup
claudio init soc

# Use in a project
cd ~/projects/incident-response
claudio use soc

# Switch profiles
claudio clean
claudio use dev

# Check what's active
claudio current

# Inspect a profile
claudio show soc
```

## Notes

- claudio writes a `.claudio` marker file to the working directory to track the active profile. Add it to `.gitignore` if you prefer.
- Profiles are directories, not single files. A minimal profile needs only `mcp.json`; a full one can bundle the entire Claude Code config surface.
- `clean` only removes symlinks that point into the profiles directory — it won't touch files that aren't managed by claudio.
- `use` calls `exec` to replace the shell process with Claude Code — no subprocess overhead.
- `init` follows symlinks when copying, so it captures the resolved content.
- POSIX sh — no bash required, no external dependencies beyond coreutils.
