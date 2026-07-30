# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`claudio` is a Claude Code profile manager. It bundles Claude Code's full project-level configuration (MCP servers, permissions, hooks, skills, commands, subagents, instructions) into named profiles. Activating a profile symlinks everything into the current directory — like a venv for Claude Code.

## Language & Conventions

- Single POSIX `sh` script (`claudio`) — no bash, no external dependencies beyond coreutils (plus Docker for MCP mode).
- No build step, no package manager.
- Install: `chmod +x claudio && sudo cp claudio /usr/local/bin/`.
- Tests: `sh test.sh` (131 tests, runs in a temp directory with a stubbed `docker`; mixed-mode tests self-skip when `jq` is absent).

## Architecture

The script is driven by a `CONFIG_MAP` constant that pairs profile items to their target paths in a project directory (e.g. `mcp.json:.mcp.json`, `settings.json:.claude/settings.json`). This single map powers `use`, `clean`, `init`, and `show` — no item-specific logic is repeated.

A `case` statement at the bottom dispatches subcommands (`list`, `use`, `clean`, `new`, `mcp`, `edit`, `show`, `current`, `init`) to `cmd_*` functions. Helpers: `_profiles` (lists profile names), `_require_profile` (validates and sets `PROFILE_PATH`), `_is_claudio_link` (checks if a symlink points into the profiles dir), `_ensure_profile_items` (fills missing items with sensible blanks), `_component_path` (maps component names to profile item paths), `_do_clean` (shared cleanup for `clean` and `use`).

MCP is wired through the **Docker MCP Toolkit** by default, with an escape hatch for MCPs Docker isn't fit for. Docker helpers: `_dockercmd`/`_docker_ok`/`_dmcp` (run `docker mcp ...` via the `CLAUDIO_DOCKER` binary), `_mcp_is_docker` (a profile is Docker-mode iff it has an `mcp.docker` marker), `_docker_id` (reads that marker), `_plain_gateway_json` (gateway-only runner), `_write_gateway_json` (regenerates `mcp.json` = the gateway runner **plus** any `mcp.extra.json` servers merged in via `jq`), `_mcp_ref` (prefixes bare server refs with `docker://`).

Mixed mode: `mcp.extra.json` (a normal `{ "mcpServers": {...} }` doc) holds non-Docker servers — remote HTTP/SSE endpoints, bespoke commands. `_write_gateway_json` merges its `mcpServers` next to the `MCP_DOCKER` gateway entry using `jq` (only invoked when the extras file exists; pure-Docker and manual profiles stay dependency-free). If jq is missing while extras exist, it warns and writes gateway-only rather than failing.

Key behaviours:
- All profile items are always present (live-link design). `_ensure_profile_items` fills blanks so symlinks are always created and later additions take effect immediately.
- `cmd_use` auto-cleans any existing profile, regenerates the gateway `mcp.json` in Docker mode, then symlinks. It activates only — it `exec claude "$@"` **only** when `-l`/`--launch` is passed.
- `cmd_new` defaults to Docker mode: creates a matching Docker MCP profile (id = claudio profile name), writes the `mcp.docker` marker + gateway runner. `--manual` (or Docker being unavailable) falls back to a hand-written `mcp.json`.
- `cmd_mcp` provides thin wrappers over `docker mcp` (`add`/`rm`/`show`/`config`, plus global `catalog` import); it delegates management to Docker rather than reimplementing it. The `extra` action edits `mcp.extra.json` and regenerates `mcp.json` so active symlinks pick up the change. Errors on manual-mode profiles.
- `cmd_init` always captures a manual-mode profile (the `mcp.docker` marker isn't in `CONFIG_MAP`, so it's never copied).
- `cmd_clean` only removes symlinks pointing into the profiles dir — won't touch non-claudio files.
- A `.claudio` marker file in cwd tracks the active profile name.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDIO_PROFILES` | `~/.claude/profiles` | Profile storage directory |
| `CLAUDIO_DOCKER` | `docker` | Docker binary for `docker mcp` calls (override for tests) |
| `EDITOR` | `vim` | Editor for `new`/`edit` commands |
