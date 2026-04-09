#!/usr/bin/env sh
# Test suite for claudio
#
# Usage: sh test.sh [path/to/claudio]
# Runs in a temp directory. Cleans up after itself.

set -e

CLAUDIO="${1:-./claudio}"
CLAUDIO="$(cd "$(dirname "$CLAUDIO")" && pwd)/$(basename "$CLAUDIO")"

# --- test harness ---

TESTS=0
PASSED=0
FAILED=0
FAIL_NAMES=""

pass() {
  PASSED=$((PASSED + 1))
  printf "  \033[32m✓\033[0m %s\n" "$1"
}

fail() {
  FAILED=$((FAILED + 1))
  FAIL_NAMES="$FAIL_NAMES\n    - $1"
  printf "  \033[31m✗\033[0m %s\n" "$1"
  [ -n "$2" ] && printf "    %s\n" "$2"
}

assert_eq() {
  TESTS=$((TESTS + 1))
  if [ "$2" = "$3" ]; then
    pass "$1"
  else
    fail "$1" "expected: '$3', got: '$2'"
  fi
}

assert_contains() {
  TESTS=$((TESTS + 1))
  if echo "$2" | grep -q "$3"; then
    pass "$1"
  else
    fail "$1" "output does not contain '$3'"
  fi
}

assert_not_contains() {
  TESTS=$((TESTS + 1))
  if echo "$2" | grep -q "$3"; then
    fail "$1" "output should not contain '$3'"
  else
    pass "$1"
  fi
}

assert_file_exists() {
  TESTS=$((TESTS + 1))
  if [ -e "$2" ]; then
    pass "$1"
  else
    fail "$1" "file does not exist: $2"
  fi
}

assert_file_not_exists() {
  TESTS=$((TESTS + 1))
  if [ ! -e "$2" ] && [ ! -L "$2" ]; then
    pass "$1"
  else
    fail "$1" "file should not exist: $2"
  fi
}

assert_is_symlink() {
  TESTS=$((TESTS + 1))
  if [ -L "$2" ]; then
    pass "$1"
  else
    fail "$1" "not a symlink: $2"
  fi
}

assert_is_dir() {
  TESTS=$((TESTS + 1))
  if [ -d "$2" ]; then
    pass "$1"
  else
    fail "$1" "not a directory: $2"
  fi
}

assert_exit_code() {
  TESTS=$((TESTS + 1))
  if [ "$2" = "$3" ]; then
    pass "$1"
  else
    fail "$1" "expected exit code $3, got $2"
  fi
}

# Wrapper: run claudio with test env, patched to not exec claude
run() {
  CLAUDIO_PROFILES="$PROFILES" EDITOR=true sh -c "
    # Replace 'exec claude' with a no-op for testing
    sed 's/exec claude/echo \"[claude]\" #/' '$CLAUDIO' > '$TMP/claudio-test.sh'
    sh '$TMP/claudio-test.sh' \"\$@\"
  " -- "$@" 2>&1
}

# --- setup ---

TMP=$(mktemp -d)
PROFILES="$TMP/profiles"
WORKDIR="$TMP/project"
mkdir -p "$PROFILES" "$WORKDIR"

cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

# ============================================================
printf "\n\033[1m=== help / usage ===\033[0m\n"
# ============================================================

out=$(run)
assert_contains "no args shows usage" "$out" "Usage: claudio"

out=$(run --help)
assert_contains "--help shows usage" "$out" "Usage: claudio"

out=$(run help)
assert_contains "help shows usage" "$out" "Usage: claudio"

# ============================================================
printf "\n\033[1m=== new ===\033[0m\n"
# ============================================================

out=$(run new test-profile)
assert_contains "new prints creation message" "$out" "Created profile 'test-profile'"
assert_is_dir "new creates profile directory" "$PROFILES/test-profile"

# All 9 items scaffolded
assert_file_exists "new scaffolds mcp.json" "$PROFILES/test-profile/mcp.json"
assert_file_exists "new scaffolds settings.json" "$PROFILES/test-profile/settings.json"
assert_file_exists "new scaffolds settings.local.json" "$PROFILES/test-profile/settings.local.json"
assert_file_exists "new scaffolds CLAUDE.md" "$PROFILES/test-profile/CLAUDE.md"
assert_file_exists "new scaffolds CLAUDE.local.md" "$PROFILES/test-profile/CLAUDE.local.md"
assert_is_dir "new scaffolds commands/" "$PROFILES/test-profile/commands"
assert_is_dir "new scaffolds skills/" "$PROFILES/test-profile/skills"
assert_is_dir "new scaffolds agents/" "$PROFILES/test-profile/agents"
assert_is_dir "new scaffolds hooks/" "$PROFILES/test-profile/hooks"

# JSON content is valid
mcp_content=$(cat "$PROFILES/test-profile/mcp.json")
assert_contains "mcp.json has mcpServers" "$mcp_content" "mcpServers"

settings_content=$(cat "$PROFILES/test-profile/settings.json")
assert_contains "settings.json has permissions" "$settings_content" "permissions"

# Duplicate name fails
out=$(run new test-profile 2>&1 || true)
assert_contains "new rejects duplicate" "$out" "already exists"

# Missing name fails
out=$(run new 2>&1 || true)
assert_contains "new requires name" "$out" "Profile name required"

# ============================================================
printf "\n\033[1m=== list ===\033[0m\n"
# ============================================================

out=$(cd "$WORKDIR" && run list)
assert_contains "list shows profile" "$out" "test-profile"
assert_not_contains "list shows no active marker" "$out" "(active)"

# ============================================================
printf "\n\033[1m=== use ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -rf .claude .mcp.json .claudio CLAUDE.md CLAUDE.local.md

out=$(run use test-profile)
assert_contains "use prints activation" "$out" "Activated profile 'test-profile'"
assert_contains "use launches claude" "$out" "[claude]"

# All 9 symlinks created
assert_is_symlink "use links .mcp.json" ".mcp.json"
assert_is_symlink "use links settings.json" ".claude/settings.json"
assert_is_symlink "use links settings.local.json" ".claude/settings.local.json"
assert_is_symlink "use links CLAUDE.md" "CLAUDE.md"
assert_is_symlink "use links CLAUDE.local.md" "CLAUDE.local.md"
assert_is_symlink "use links commands" ".claude/commands"
assert_is_symlink "use links skills" ".claude/skills"
assert_is_symlink "use links agents" ".claude/agents"
assert_is_symlink "use links hooks" ".claude/hooks"

# Marker file written
assert_file_exists "use writes .claudio marker" ".claudio"
marker=$(cat .claudio)
assert_eq "marker contains profile name" "$marker" "test-profile"

# Symlinks point into profiles dir
link_target=$(readlink .mcp.json)
assert_contains "symlink points to profiles dir" "$link_target" "$PROFILES/test-profile"

# ============================================================
printf "\n\033[1m=== current ===\033[0m\n"
# ============================================================

out=$(run current)
assert_contains "current shows profile name" "$out" "test-profile"
assert_contains "current shows profile path" "$out" "$PROFILES/test-profile"

# ============================================================
printf "\n\033[1m=== list with active profile ===\033[0m\n"
# ============================================================

out=$(run list)
assert_contains "list marks active profile" "$out" "* test-profile"
assert_contains "list shows active tag" "$out" "(active)"

# ============================================================
printf "\n\033[1m=== show ===\033[0m\n"
# ============================================================

out=$(run show test-profile)
assert_contains "show displays profile name" "$out" "Profile: test-profile"
assert_contains "show displays mcp.json" "$out" "[mcp.json]"
assert_contains "show displays settings.json" "$out" "[settings.json]"

# ============================================================
printf "\n\033[1m=== live-link behaviour ===\033[0m\n"
# ============================================================

# Add a skill to the profile AFTER activation — should be visible via symlink
mkdir -p "$PROFILES/test-profile/skills/new-skill"
printf '%s\n' '---' 'name: new-skill' '---' 'Do something' > "$PROFILES/test-profile/skills/new-skill/SKILL.md"

assert_file_exists "live-link: skill visible after adding to profile" ".claude/skills/new-skill/SKILL.md"
skill_content=$(cat .claude/skills/new-skill/SKILL.md)
assert_contains "live-link: skill content is correct" "$skill_content" "Do something"

# Add a command
printf '# review\nReview code\n' > "$PROFILES/test-profile/commands/review.md"
assert_file_exists "live-link: command visible after adding" ".claude/commands/review.md"

# Modify mcp.json in profile — visible through symlink
printf '{"mcpServers":{"gh":{"command":"gh"}}}\n' > "$PROFILES/test-profile/mcp.json"
mcp_now=$(cat .mcp.json)
assert_contains "live-link: mcp.json change visible" "$mcp_now" '"gh"'

# ============================================================
printf "\n\033[1m=== clean ===\033[0m\n"
# ============================================================

out=$(run clean)
assert_contains "clean prints deactivation" "$out" "Profile deactivated"

# All symlinks removed
assert_file_not_exists "clean removes .mcp.json" ".mcp.json"
assert_file_not_exists "clean removes settings.json" ".claude/settings.json"
assert_file_not_exists "clean removes settings.local.json" ".claude/settings.local.json"
assert_file_not_exists "clean removes CLAUDE.md" "CLAUDE.md"
assert_file_not_exists "clean removes CLAUDE.local.md" "CLAUDE.local.md"
assert_file_not_exists "clean removes commands" ".claude/commands"
assert_file_not_exists "clean removes skills" ".claude/skills"
assert_file_not_exists "clean removes agents" ".claude/agents"
assert_file_not_exists "clean removes hooks" ".claude/hooks"

# Marker removed
assert_file_not_exists "clean removes .claudio marker" ".claudio"

# .claude/ removed when empty
assert_file_not_exists "clean removes empty .claude/" ".claude"

# Profile source files still intact
assert_file_exists "clean preserves profile source mcp.json" "$PROFILES/test-profile/mcp.json"
assert_file_exists "clean preserves profile source settings.json" "$PROFILES/test-profile/settings.json"
assert_is_dir "clean preserves profile source skills/" "$PROFILES/test-profile/skills"

# Clean with no active profile
out=$(run clean 2>&1 || true)
assert_contains "clean with no profile is safe" "$out" "No active profile"

# ============================================================
printf "\n\033[1m=== conflict detection ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -rf .claude .mcp.json .claudio CLAUDE.md CLAUDE.local.md

# Create a real (non-claudio) file
printf '{"mcpServers":{"existing":true}}\n' > .mcp.json
mkdir -p .claude
printf '{"permissions":{}}\n' > .claude/settings.json

out=$(run use test-profile 2>&1)
assert_contains "conflict: warns about .mcp.json" "$out" "warning: .mcp.json"
assert_contains "conflict: warns about settings.json" "$out" "warning: .claude/settings.json"

# Original files untouched
original_mcp=$(cat .mcp.json)
assert_contains "conflict: original .mcp.json preserved" "$original_mcp" "existing"

# Other items still linked
assert_is_symlink "conflict: non-conflicting items still linked" "CLAUDE.md"

# Clean up for next tests
rm -f .mcp.json .claudio CLAUDE.md CLAUDE.local.md
rm -rf .claude

# ============================================================
printf "\n\033[1m=== use auto-cleans previous profile ===\033[0m\n"
# ============================================================

cd "$WORKDIR"

# Create a second profile
out=$(run new second-profile)

# Activate first
run use test-profile > /dev/null
assert_is_symlink "first profile active" ".mcp.json"
first_target=$(readlink .mcp.json)
assert_contains "points to test-profile" "$first_target" "test-profile"

# Activate second — should auto-clean first
out=$(run use second-profile)
second_target=$(readlink .mcp.json)
assert_contains "auto-clean: now points to second-profile" "$second_target" "second-profile"
marker=$(cat .claudio)
assert_eq "auto-clean: marker updated" "$marker" "second-profile"

run clean > /dev/null

# ============================================================
printf "\n\033[1m=== init ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -rf .claude .mcp.json CLAUDE.md CLAUDE.local.md .claudio

# Set up a project with some config
mkdir -p .claude/commands .claude/skills/my-skill .claude/agents
printf '{"mcpServers":{"gh":{"command":"gh"}}}\n' > .mcp.json
printf '{"permissions":{"deny":["Bash(rm *)"]}}\n' > .claude/settings.json
printf '# My project instructions\n' > CLAUDE.md
printf '%s\n' '---' 'name: my-skill' '---' 'Do stuff' > .claude/skills/my-skill/SKILL.md
printf '%s\n' '---' 'name: researcher' '---' 'Explore code' > .claude/agents/researcher.md
printf '# deploy\nDeploy the app\n' > .claude/commands/deploy.md

out=$(run init captured)
assert_contains "init prints creation message" "$out" "Created profile 'captured'"

# Existing items captured
assert_file_exists "init captures mcp.json" "$PROFILES/captured/mcp.json"
captured_mcp=$(cat "$PROFILES/captured/mcp.json")
assert_contains "init captures mcp.json content" "$captured_mcp" '"gh"'

assert_file_exists "init captures settings.json" "$PROFILES/captured/settings.json"
assert_file_exists "init captures CLAUDE.md" "$PROFILES/captured/CLAUDE.md"
assert_file_exists "init captures commands" "$PROFILES/captured/commands/deploy.md"
assert_file_exists "init captures skills" "$PROFILES/captured/skills/my-skill/SKILL.md"
assert_file_exists "init captures agents" "$PROFILES/captured/agents/researcher.md"

# Missing items filled with blanks
assert_file_exists "init fills settings.local.json" "$PROFILES/captured/settings.local.json"
assert_file_exists "init fills CLAUDE.local.md" "$PROFILES/captured/CLAUDE.local.md"
assert_is_dir "init fills hooks/" "$PROFILES/captured/hooks"

# Duplicate fails
out=$(run init captured 2>&1 || true)
assert_contains "init rejects duplicate" "$out" "already exists"

# Clean up project files
rm -rf .claude .mcp.json CLAUDE.md CLAUDE.local.md

# ============================================================
printf "\n\033[1m=== init follows symlinks ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -rf .claude .mcp.json CLAUDE.md CLAUDE.local.md .claudio

# Activate a profile, then init from it — should copy content, not symlinks
run use test-profile > /dev/null
out=$(run init from-links)
assert_contains "init from symlinked dir succeeds" "$out" "Created profile 'from-links'"

# Captured mcp.json should be a regular file, not a symlink
assert_file_exists "init-from-links: mcp.json exists" "$PROFILES/from-links/mcp.json"
TESTS=$((TESTS + 1))
if [ ! -L "$PROFILES/from-links/mcp.json" ]; then
  pass "init-from-links: mcp.json is a regular file (not symlink)"
else
  fail "init-from-links: mcp.json is a regular file (not symlink)" "got a symlink"
fi

run clean > /dev/null

# ============================================================
printf "\n\033[1m=== edit ===\033[0m\n"
# ============================================================

# edit with component creates missing items
rm -f "$PROFILES/test-profile/CLAUDE.local.md"
touch "$PROFILES/test-profile/CLAUDE.local.md"  # reset to blank

# Test component name mapping
for comp in mcp settings local-settings claude local-claude commands skills agents hooks; do
  TESTS=$((TESTS + 1))
  out=$(run edit test-profile "$comp" 2>&1)
  # EDITOR=true just returns 0 — we're testing it doesn't error
  if [ $? -eq 0 ]; then
    pass "edit component '$comp' succeeds"
  else
    fail "edit component '$comp' succeeds" "exited with error"
  fi
done

# Invalid component fails
out=$(run edit test-profile invalid-thing 2>&1 || true)
assert_contains "edit rejects invalid component" "$out" "Unknown component"

# ============================================================
printf "\n\033[1m=== error handling ===\033[0m\n"
# ============================================================

# Missing profile
out=$(run use nonexistent 2>&1 || true)
assert_contains "use: missing profile error" "$out" "not found"

out=$(run show nonexistent 2>&1 || true)
assert_contains "show: missing profile error" "$out" "not found"

out=$(run edit nonexistent 2>&1 || true)
assert_contains "edit: missing profile error" "$out" "not found"

# Missing name
out=$(run use 2>&1 || true)
assert_contains "use: missing name error" "$out" "Profile name required"

out=$(run new 2>&1 || true)
assert_contains "new: missing name error" "$out" "Profile name required"

out=$(run init 2>&1 || true)
assert_contains "init: missing name error" "$out" "Profile name required"

# Unknown command
out=$(run bogus-command 2>&1 || true)
assert_contains "unknown command error" "$out" "Unknown command"

# ============================================================
printf "\n\033[1m=== current with no profile ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -f .claudio
out=$(run current)
assert_contains "current with no profile" "$out" "No active profile"

# ============================================================
# Results
# ============================================================

printf "\n\033[1m=== Results ===\033[0m\n"
printf "  %d tests, \033[32m%d passed\033[0m" "$TESTS" "$PASSED"
if [ "$FAILED" -gt 0 ]; then
  printf ", \033[31m%d failed\033[0m" "$FAILED"
  printf "\n\n  Failed tests:%b\n" "$FAIL_NAMES"
  printf "\n"
  exit 1
else
  printf "\n\n"
  exit 0
fi
