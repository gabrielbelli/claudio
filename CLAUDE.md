# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`claudio` is a Claude Code profile manager. It bundles Claude Code's full project-level configuration (MCP servers, permissions, hooks, skills, commands, subagents, instructions) into named profiles. Activating a profile symlinks everything into the current directory — like a venv for Claude Code.

## Language & Conventions

- Single POSIX `sh` script (`claudio`) — no bash, no external dependencies beyond coreutils.
- No build step, no package manager.
- Install: `chmod +x claudio && sudo cp claudio /usr/local/bin/`.
- Tests: `sh test.sh` (102 tests, runs in a temp directory).

## Architecture

The script is driven by a `CONFIG_MAP` constant that pairs profile items to their target paths in a project directory (e.g. `mcp.json:.mcp.json`, `settings.json:.claude/settings.json`). This single map powers `use`, `clean`, `init`, and `show` — no item-specific logic is repeated.

A `case` statement at the bottom dispatches subcommands (`list`, `use`, `clean`, `new`, `edit`, `show`, `current`, `init`) to `cmd_*` functions. Helpers: `_profiles` (lists profile names), `_require_profile` (validates and sets `PROFILE_PATH`), `_is_claudio_link` (checks if a symlink points into the profiles dir), `_ensure_profile_items` (fills missing items with sensible blanks), `_component_path` (maps component names to profile item paths), `_do_clean` (shared cleanup for `clean` and `use`).

Key behaviours:
- All profile items are always present (live-link design). `_ensure_profile_items` fills blanks so symlinks are always created and later additions take effect immediately.
- `cmd_use` auto-cleans any existing profile before activating, then calls `exec claude "$@"` (replaces shell process).
- `cmd_clean` only removes symlinks pointing into the profiles dir — won't touch non-claudio files.
- A `.claudio` marker file in cwd tracks the active profile name.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDIO_PROFILES` | `~/.claude/profiles` | Profile storage directory |
| `EDITOR` | `vim` | Editor for `new`/`edit` commands |
