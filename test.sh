#!/usr/bin/env sh
# Test suite for claudio
#
# Usage: bash test.sh [path/to/claudio]
#        dash test.sh [path/to/claudio]
# Run BOTH, every time. Runs in a temp directory. Cleans up after itself.

set -e

CLAUDIO="${1:-./claudio}"
CLAUDIO="$(cd "$(dirname "$CLAUDIO")" && pwd)/$(basename "$CLAUDIO")"

# The shell `claudio` itself is executed with — the whole point of running the
# suite twice. It must be the shell running test.sh, NOT a hardcoded `sh`:
# /bin/sh on macOS is bash 3.2, which accepts [[ ]], arrays and other bash-isms
# silently, so a hardcoded `sh` gave `dash test.sh` zero dash coverage of
# claudio (verified: a [[ ]] injected into claudio still passed 335/335).
TEST_SH=$(ps -p $$ -o comm= 2>/dev/null)
TEST_SH=${TEST_SH##*/}          # /bin/dash -> dash
TEST_SH=${TEST_SH#-}            # -bash (login shell) -> bash
command -v "$TEST_SH" >/dev/null 2>&1 || TEST_SH=sh

# CLAUDIO_UPDATE IS CLEARED FOR THE WHOLE SUITE, and this line is load-bearing.
#
# Every wrapper but the update ones passes CLAUDIO_UPDATE=0 explicitly; the
# update ones say nothing, which used to mean "inherits whatever the developer
# exported". `CLAUDIO_UPDATE=0` is what claudio's own header recommends FOR CI
# -- and CI is exactly where this suite runs -- so a correctly configured CI
# machine failed 31 assertions in the update section with nothing anywhere
# naming the environment as the cause. Worse than the failures was the one that
# still passed: "run does not wait on the update check", which is timed against
# a deliberately slow git and is trivially fast when the check never happens.
#
# Cleared rather than unset because the kill switch is a value comparison
# (`[ "$CLAUDIO_UPDATE" = 0 ]`), so empty is the unset state either way; and it
# is done once here rather than per wrapper so a wrapper added tomorrow cannot
# reintroduce it. Said out loud when it had to override something, because a
# kill switch that silently stops killing is the wrong kind of quiet.
if [ -n "$CLAUDIO_UPDATE" ]; then
  printf "  (note: CLAUDIO_UPDATE=%s cleared — the suite tests the update check)\n" \
    "$CLAUDIO_UPDATE"
fi
CLAUDIO_UPDATE=
export CLAUDIO_UPDATE

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
  # `if`, not a trailing `[ … ] &&`: this is the last statement of the function,
  # so under `set -e` a detail-less fail() would return non-zero and abort the
  # whole suite at the first failure. That is the trap CLAUDE.md warns about.
  if [ -n "$2" ]; then printf "    %s\n" "$2"; fi
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
  if echo "$2" | grep -q -e "$3"; then
    pass "$1"
  else
    fail "$1" "output does not contain '$3'"
  fi
}

assert_not_contains() {
  TESTS=$((TESTS + 1))
  if echo "$2" | grep -q -e "$3"; then
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

# Wrapper: run claudio with test env, patched to not exec claude.
# CLAUDIO_ACCOUNTS/CLAUDIO_CONF are forced into the temp tree so tests never
# touch the real ~/.claude. CLAUDIO_ACCOUNT/CLAUDIO_TAGS/CLAUDIO_HOST are
# forwarded so a test can set them per call: `out=$(CLAUDIO_ACCOUNT=a run env)`.
# $TEST_SH, not `sh`: see the note at the top of this file.
run() {
  CLAUDIO_PROFILES="$PROFILES" CLAUDIO_ACCOUNTS="$ACCOUNTS" CLAUDIO_CONF="$CONF" \
  CLAUDIO_LEGACY="$LEGACY" \
  CLAUDIO_ACCOUNT="$CLAUDIO_ACCOUNT" CLAUDIO_TAGS="$CLAUDIO_TAGS" \
  CLAUDIO_HOST="$CLAUDIO_HOST" \
  CLAUDIO_USER_SETTINGS="$USER_SETTINGS" \
  CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
  CLAUDIO_UPDATE=0 CLAUDIO_GIT="$GIT_STUB" \
  CLAUDIO_DOCKER="$DOCKER_STUB" EDITOR=true \
  "$TEST_SH" "$CLAUDIO_UNDER_TEST" "$@" 2>&1
}

# claudio with a migration-specific storage layout: its own "new" tree and its
# own legacy tree, so the migration tests neither see nor disturb the profiles
# the rest of the suite built — and, like `run`, reach nothing outside $TMP.
# $MIGOLD/$MIGNEW are reset per scenario by mk_legacy.
run_mig() {
  CLAUDIO_PROFILES="$MIGNEW/profiles" CLAUDIO_ACCOUNTS="$MIGNEW/accounts" \
  CLAUDIO_CONF="$MIGNEW/claudio.conf" CLAUDIO_LEGACY="$MIGOLD" \
  CLAUDIO_HOST=testhost CLAUDIO_DOCKER="$DOCKER_STUB" EDITOR=true \
  CLAUDIO_USER_SETTINGS="$USER_SETTINGS" \
  CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
  CLAUDIO_UPDATE=0 CLAUDIO_GIT="$GIT_STUB" \
  "$TEST_SH" "$CLAUDIO_UNDER_TEST" "$@" 2>&1
}

# claudio with the status line tests' own accounts tree, so wiring, refusing
# and retrofitting accounts never disturbs the ones the rest of the suite built
# — and, like `run`, reaches nothing outside $TMP. It has to be a wrapper:
# `run` pins CLAUDIO_ACCOUNTS to $ACCOUNTS, so a prefix assignment on a call to
# it is silently overridden, and whether a prefix assignment on a FUNCTION call
# survives the call at all differs between shells.
run_sl() {
  CLAUDIO_PROFILES="$PROFILES" CLAUDIO_ACCOUNTS="$SLACCT" CLAUDIO_CONF="$CONF" \
  CLAUDIO_LEGACY="$LEGACY" CLAUDIO_HOST="$CLAUDIO_HOST" \
  CLAUDIO_USER_SETTINGS="$USER_SETTINGS" \
  CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
  CLAUDIO_UPDATE=0 CLAUDIO_GIT="$GIT_STUB" \
  CLAUDIO_DOCKER="$DOCKER_STUB" EDITOR=true \
  "$TEST_SH" "$CLAUDIO_UNDER_TEST" "$@" 2>&1
}

# A pre-move installation in $MIGOLD: two profiles, two accounts (one logged
# in), a global conf, and the stray .DS_Store a real profiles dir accumulates.
mk_legacy() {
  rm -rf "$MIGOLD" "$MIGNEW"
  mkdir -p "$MIGOLD/profiles/soc/skills" "$MIGOLD/profiles/soc/hooks" \
           "$MIGOLD/profiles/hermetic-mig" \
           "$MIGOLD/accounts/work" "$MIGOLD/accounts/spare"
  printf '{"mcpServers":{}}\n' > "$MIGOLD/profiles/soc/mcp.json"
  printf 'soc instructions\n'  > "$MIGOLD/profiles/soc/CLAUDE.md"
  printf 'skill\n'             > "$MIGOLD/profiles/soc/skills/a.md"
  printf '{"oauthAccount":{"emailAddress":"a@example.com"}}\n' \
                               > "$MIGOLD/accounts/work/.claude.json"
  printf 'account=work\n'      > "$MIGOLD/claudio.conf"
  : > "$MIGOLD/profiles/.DS_Store"
}

# A project activated under the old layout: absolute links into $MIGOLD, one of
# them at a path CONFIG_MAP never mentions (.mcp.json.bak — `use` and `clean`
# can never reach it), plus one link that is nothing to do with claudio.
mk_legacy_project() {
  rm -rf "$1"
  mkdir -p "$1/.claude"
  ln -s "$MIGOLD/profiles/soc/mcp.json"  "$1/.mcp.json"
  ln -s "$MIGOLD/profiles/soc/CLAUDE.md" "$1/CLAUDE.md"
  ln -s "$MIGOLD/profiles/soc/skills"    "$1/.claude/skills"
  ln -s "$MIGOLD/profiles/soc/hooks"     "$1/.claude/hooks"
  ln -s "$MIGOLD/profiles/soc/mcp.json"  "$1/.mcp.json.bak"
  ln -s "$TMP/outside-target"            "$1/.git-profile"
  echo soc > "$1/.claudio"
}

# Feed a status line payload to the shim, the way Claude Code does: one JSON
# object on stdin, the command run through a shell.
#
# Its own wrapper rather than `run` for three reasons. It needs stdin. It needs
# a CLAUDE_CONFIG_DIR and an OTEL_RESOURCE_ATTRIBUTES, which is how the shim
# learns who the session is — set $SHIMCFG / $SHIMTAGS around a call. And it
# does NOT merge stderr into stdout, because the whole promise of the shim is
# that what reaches stdout is the renderer's bytes and nothing else.
#
# HOME is redirected into $TMP, and that is not tidiness. `usage_dir=` defaults
# to $HOME/.claudio-usage, so any test that did not set the key would append to
# the developer's real, irreplaceable samples file. Overriding HOME makes the
# default itself hermetic, which is the only version of this that cannot be
# forgotten by the next test someone adds.
#
# $SHIMTEL/$SHIMEP/$SHIMPROTO are the three variables `claudio run` exports into
# the environment of the claude it execs, which Claude Code then passes on to
# the status line. They are how the shim knows this session has a stream A at
# all, so they are how it decides whether there is a receiver to keep alive.
# Empty in every test that does not set them, which is the same as a session
# nobody launched with `claudio run`.
SHIMCFG=""
SHIMTAGS=""
SHIMTEL=""
SHIMEP=""
SHIMPROTO=""
shim() {
  # $1 = the JSON payload.
  printf '%s' "$1" | \
  CLAUDE_CODE_ENABLE_TELEMETRY="$SHIMTEL" \
  OTEL_EXPORTER_OTLP_ENDPOINT="$SHIMEP" \
  OTEL_EXPORTER_OTLP_PROTOCOL="$SHIMPROTO" \
  HOME="$FAKEHOME" \
  CLAUDIO_PROFILES="$PROFILES" CLAUDIO_ACCOUNTS="$ACCOUNTS" CLAUDIO_CONF="$CONF" \
  CLAUDIO_LEGACY="$LEGACY" CLAUDIO_HOST="$CLAUDIO_HOST" \
  CLAUDIO_USER_SETTINGS="$USER_SETTINGS" \
  CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
  CLAUDIO_UPDATE=0 CLAUDIO_GIT="$GIT_STUB" CLAUDIO_DOCKER="$DOCKER_STUB" \
  CLAUDE_CONFIG_DIR="$SHIMCFG" OTEL_RESOURCE_ATTRIBUTES="$SHIMTAGS" \
  "$TEST_SH" "$CLAUDIO_UNDER_TEST" statusline --shim
}

# A status line payload with the rate-limit block filled in.
# $1=5h pct $2=5h resets_at $3=7d pct $4=7d resets_at. Pass a bare word for a
# JSON literal (null, "n/a", {"a":1}) — these go in unquoted on purpose, so a
# test can send something that is not a number.
payload() {
  printf '{"session_id":"sess-1","cwd":"/p","prompt_id":"pid-1","agent_type":"general",'
  printf '"model":{"id":"claude-opus-5[1m]"},"version":"2.1.228",'
  printf '"cost":{"total_cost_usd":1.25},'
  printf '"workspace":{"current_dir":"/p","project_dir":"/proj"},'
  printf '"rate_limits":{"five_hour":{"used_percentage":%s,"resets_at":%s},' "$1" "$2"
  printf '"seven_day":{"used_percentage":%s,"resets_at":%s}}}' "$3" "$4"
}

# One field of the single record in $1's samples.jsonl, or of the $3'th record.
field() {
  # $1 = usage dir, $2 = jq path, $3 = 1-based record number (default 1)
  jq -r "$2" "$1/samples.jsonl" 2>/dev/null | sed -n "${3:-1}p"
}

nrecords() {
  if [ -f "$1/samples.jsonl" ]; then wc -l < "$1/samples.jsonl" | tr -d ' '; else echo 0; fi
}

count_links() { find "$1" -type l | wc -l | tr -d ' '; }

count_dangling() {
  find "$1" -type l | while IFS= read -r _l; do
    if [ ! -e "$_l" ]; then echo x; fi
  done | wc -l | tr -d ' '
}

# Extract one `export NAME=value` line from `claudio env` output.
envval() {
  # $1 = env output, $2 = variable name
  # `env` single-quotes values so its output is safe to eval; strip the
  # wrapping quotes to compare against the bare value.
  echo "$1" | sed -n "s/^export $2=//p" | sed "s/^'//; s/'\$//"
}

# The argv the launch shim was handed, rendered as `<arg> <arg>` — one bracketed
# field per argument, so an empty argument is visible as `<>` and two arguments
# are never mistakable for one containing a space.
#
# Taken from the "[claude]" marker to the END of the output rather than with a
# line-oriented grep: an argument may itself contain a newline, and that it
# survives intact is one of the properties this pins.
# Prints "(no launch)" rather than an empty string when the shim never ran, so
# that `assert_eq "$(argv "$out")" ""` — the shape that pins "claude was handed
# NO arguments" — cannot pass on output where claude was never launched at all.
# An unescaped `[claude]` is a bracket expression, so the case pattern is
# quoted to keep it a literal.
argv() {
  # $1 = run output
  case "$1" in
    *'[claude]'*) printf '%s' "$1" | sed -n '/^\[claude\]/,$p' | sed '1s/^\[claude\] *//' ;;
    *) printf '(no launch)' ;;
  esac
}

# --- setup ---

TMP=$(mktemp -d)
PROFILES="$TMP/profiles"
ACCOUNTS="$TMP/accounts"
CONF="$TMP/claudio.conf"
WORKDIR="$TMP/project"
# Pointed at a directory that does not exist, so `migrate`'s hint is dead for
# the rest of the suite and its output cannot depend on whether the developer
# running the tests happens to have a pre-move install in their real ~/.claude.
LEGACY="$TMP/no-legacy-here"
# Claude Code's own settings files, which claudio READS to decide whether a
# status line is already in effect. Pointed into $TMP because the developer's
# real ~/.claude/settings.json very often HAS a statusLine: read it and `new`
# takes the "left alone" branch on their machine and the "wrote it" branch on
# CI. Neither file exists unless a test creates it.
USER_SETTINGS="$TMP/user-claude-settings.json"
MANAGED_SETTINGS="$TMP/managed-settings.json"
MIGOLD="$TMP/mig-old"
MIGNEW="$TMP/mig-new"
# The HOME the shim runs under: see shim(). It must not be $TMP itself, or the
# "usage_dir defaults to ~/.claudio-usage" test could not tell the default from
# an explicitly configured directory.
FAKEHOME="$TMP/fakehome"
mkdir -p "$PROFILES" "$WORKDIR" "$FAKEHOME"
# The target of the deliberately-not-ours symlink in mk_legacy_project. A real
# file, so "did migrate leave it alone" is not confused with "it was dangling".
printf 'not claudio\n' > "$TMP/outside-target"

# The copy of claudio the tests actually run: `exec claude` replaced by a shim
# so a launch is observable as "[claude]" instead of taking over the process,
# and the argv it was handed is observable with it. Built once here rather than
# re-sed'ing on all 300+ `run` calls.
#
# The shim prints one bracketed field per argument — `[claude] <-p> <hi>` — and
# three properties make that shape load-bearing rather than decorative:
#
#  * It renders EVERY argument, including an empty one (`<>`) and one holding
#    an embedded newline. Byte-for-byte passthrough is a deliberate design
#    property of _parse_run_opts/RUN_SHIFT, and it had no test at all.
#  * printf, not echo. dash's echo expands backslash escapes inside its
#    arguments, so `a\nb` reached the assertion as `a`+newline under dash and
#    as a literal `a\nb` under bash — an argv assertion written on the echo
#    form passes under one shell and fails under the other, which is the exact
#    "bash test.sh alone shows nothing" trap this suite exists to avoid.
#  * The replacement text carries NO trailing `#`. The old stub had one, to
#    suppress `"$@"` so a launch printed as a bare `[claude]`; but two of the
#    three `exec claude` sites are `exec claude /login`, and the `#` commented
#    the literal `/login` out. Reporting argv at all means it must be right at
#    every site, or the first login-path assertion pins a lie.
#
# The definition is inserted as ONE line after the real shebang, so the copy's
# line numbers stay within one of claudio's own and the shebang stays first.
CLAUDIO_UNDER_TEST="$TMP/claudio-test.sh"
{
  sed -n '1p' "$CLAUDIO"
  printf '%s\n' '_fake_claude() { printf "[claude]"; for _fc_a in "$@"; do printf " <%s>" "$_fc_a"; done; printf "\n"; }'
  sed '1d; s/exec claude/_fake_claude/' "$CLAUDIO"
} > "$CLAUDIO_UNDER_TEST"

# The suite must not inherit account/tag settings from the developer's shell.
unset CLAUDIO_ACCOUNT
unset CLAUDIO_TAGS

# The automatic host= tag would otherwise be the developer's real machine name,
# making every exact OTEL_RESOURCE_ATTRIBUTES assertion machine dependent. Pin
# it; the tests that exercise detection and sanitisation set it themselves.
CLAUDIO_HOST=testhost
export CLAUDIO_HOST

# Hermetic `docker` stub so Docker-MCP mode is exercised without a real daemon.
# Records every invocation to $TMP/docker.log and simulates success.
DOCKER_STUB="$TMP/docker-stub"
DOCKER_LOG="$TMP/docker.log"
cat > "$DOCKER_STUB" <<STUB
#!/bin/sh
echo "\$@" >> "$DOCKER_LOG"
[ "\$1" = "mcp" ] || exit 0
shift
case "\$1 \$2" in
  "profile create") exit 0 ;;
  "profile show")   echo "servers: []"; exit 0 ;;
  "profile server") exit 0 ;;
  "profile config") exit 0 ;;
  "profile remove") exit 0 ;;
  "catalog pull")   echo "pulled"; exit 0 ;;
  "catalog ls")     echo "no catalogs"; exit 0 ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$DOCKER_STUB"
: > "$DOCKER_LOG"

# Hermetic `git` stub, the same pattern as the docker one and for a stronger
# reason: the update check is the only part of claudio that would otherwise
# reach the network. It answers `ls-remote` from $GIT_TAGS — one tag name per
# line, rewritten per scenario, and legitimately EMPTY for the no-releases-yet
# case, which is what the real repo returns today.
GIT_STUB="$TMP/git-stub"
GIT_LOG="$TMP/git.log"
GIT_TAGS="$TMP/git-tags"
cat > "$GIT_STUB" <<STUB
#!/bin/sh
echo "\$@" >> "$GIT_LOG"
for a in "\$@"; do
  if [ "\$a" = "ls-remote" ]; then
    [ -f "$GIT_TAGS" ] || exit 0
    while IFS= read -r t; do
      [ -n "\$t" ] || continue
      printf '%s\trefs/tags/%s\n' 0000000000000000000000000000000000000000 "\$t"
    done < "$GIT_TAGS"
    exit 0
  fi
done
exit 0
STUB
chmod +x "$GIT_STUB"
: > "$GIT_LOG"
: > "$GIT_TAGS"

# The update machinery keeps its own state (a stamp next to the global conf),
# so it gets its own conf directory: the rest of the suite shares $CONF, and a
# stray `update_check=` or a stamp in it would leak into unrelated sections.
UPDDIR="$TMP/upd"
UPDCONF="$UPDDIR/claudio.conf"
UPDSTAMP="$UPDDIR/update-stamp"
UPDPROJ="$TMP/upd-project"
mkdir -p "$UPDDIR" "$UPDPROJ"

# claudio with the update machinery LIVE — the only wrapper that does not set
# CLAUDIO_UPDATE=0. The git binary is still the stub, so "live" never means
# "networked", and the stamp follows CLAUDIO_CONF into $UPDDIR for free.
#
# The suite clears CLAUDIO_UPDATE at the top (see there for why), so "does not
# set it" really does mean live here rather than "inherits the developer's".
run_upd() {
  CLAUDIO_PROFILES="$PROFILES" CLAUDIO_ACCOUNTS="$ACCOUNTS" CLAUDIO_CONF="$UPDCONF" \
  CLAUDIO_LEGACY="$LEGACY" CLAUDIO_HOST=testhost \
  CLAUDIO_USER_SETTINGS="$USER_SETTINGS" \
  CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
  CLAUDIO_GIT="$GIT_STUB" CLAUDIO_DOCKER="$DOCKER_STUB" EDITOR=true \
  "$TEST_SH" "$CLAUDIO_UNDER_TEST" "$@" 2>&1
}

# The refresh is deliberately detached, so the only way to observe it is to
# wait for its write. Bounded, and it returns rather than aborting the suite.
wait_for_stamp() {
  _wi=0
  while [ "$_wi" -lt 20 ]; do
    if grep -q "$1" "$UPDSTAMP" 2>/dev/null; then return 0; fi
    sleep 1
    _wi=$((_wi + 1))
  done
  return 1
}

# The same bounded wait, for any file. The git log needs one for the same
# reason the stamp does, and the race is not obvious: refreshes are DETACHED,
# so one spawned by an earlier test can still be in flight. The stub logs
# before it writes the stamp, so that straggler can log, have its line wiped by
# this section's `: > "$GIT_LOG"`, and only then write the stamp — satisfying
# wait_for_stamp while this run's own git call has not logged yet. Observed as
# an intermittent failure of "the refresh went through git" under load. Waiting
# hides no regression: a refresh that never runs still fails the assertion.
wait_for_match() {
  _wj=0
  while [ "$_wj" -lt 20 ]; do
    if grep -q "$2" "$1" 2>/dev/null; then return 0; fi
    sleep 1
    _wj=$((_wj + 1))
  done
  return 1
}

# `uname` stub, so the macOS-only login warning can be tested on both branches
# from either platform. Used by prepending $UNAME_DIR to PATH for one call:
# a variable assignment in front of a function call is exported to the commands
# it runs (checked under both bash and dash) and does not outlive the call.
UNAME_DIR="$TMP/unamebin"
mkdir -p "$UNAME_DIR"
cat > "$UNAME_DIR/uname" <<'STUB'
#!/bin/sh
case "$1" in
  -n) echo stubhost ;;
  *)  echo "${FAKE_UNAME_S:-Linux}" ;;
esac
STUB
chmod +x "$UNAME_DIR/uname"

# The real update stamps, fingerprinted BEFORE any test runs.
#
# The hermeticity claim is that the suite never creates or touches these — not
# that they do not exist. They legitimately do exist for anyone who has actually
# used `claudio run`, so asserting plain absence makes the suite pass on a clean
# machine and fail on a maintainer's, which is precisely the per-developer
# divergence this probe exists to catch. Content, not just presence, so a
# rewrite of an already-present stamp is caught too.
_real_stamp_state() {
  if [ -f "$1" ]; then printf 'present %s' "$(cksum < "$1")"; else printf 'absent'; fi
}
REAL_STAMP_CLAUDIO="$HOME/.claudio/update-stamp"
REAL_STAMP_CLAUDE="$HOME/.claude/update-stamp"
REAL_STAMP_CLAUDIO_BEFORE=$(_real_stamp_state "$REAL_STAMP_CLAUDIO")
REAL_STAMP_CLAUDE_BEFORE=$(_real_stamp_state "$REAL_STAMP_CLAUDE")

# The developer's REAL recording, fingerprinted the same way and for a sharper
# reason: `usage_dir=` defaults to $HOME/.claudio-usage, so a shim test that ran
# with the default would append to a file of irreplaceable samples. shim()
# below points HOME at $TMP so the default cannot reach it; this is the proof.
REAL_SAMPLES="$HOME/.claudio-usage/samples.jsonl"
REAL_SAMPLES_BEFORE=$(_real_stamp_state "$REAL_SAMPLES")

# The same probe for the receiver's own files, which live in that same real
# directory: a pidfile, a lock, a start counter and a sessions directory. A
# spawn test that resolved usage_dir to the default would create them there and
# could start a receiver on the developer's machine — so the whole directory
# listing is fingerprinted, not one file of it.
_real_recv_state() {
  if [ -d "$HOME/.claudio-usage" ]; then
    printf 'present %s' "$(ls -a "$HOME/.claudio-usage" | sort | cksum)"
  else
    printf 'absent'
  fi
}
REAL_RECV_BEFORE=$(_real_recv_state)

# Set to 1 by the Results section at the very bottom. Anything that exits
# before then is an ABORT, not a pass: `set -e` killed the run mid-way — an
# `out=$(run …)` capture whose claudio exited non-zero, an unmatched grep, a
# trailing `[ … ] &&`. That trap has bitten three times, and the reason it
# keeps costing time is that an abort prints NO results line at all, so both a
# human skimming the tail and a CI step grepping for "N failed" read silence as
# success. This makes the failure announce itself instead.
SUITE_COMPLETED=0

cleanup() {
  _rc=$?                      # must stay the first statement: $? is fragile
  if [ "$SUITE_COMPLETED" != 1 ]; then
    printf "\n\033[31m\033[1m=== SUITE ABORTED ===\033[0m\n" >&2
    printf "  set -e killed the run after %d tests (%d passed, %d failed).\n" \
      "$TESTS" "$PASSED" "$FAILED" >&2
    printf "  No results line was printed, so this is NOT a pass.\n" >&2
    printf "  Cause: the statement after the last test above exited non-zero.\n" >&2
    if [ "$_rc" = 0 ]; then _rc=1; fi
  fi
  rm -rf "$TMP"
  exit "$_rc"
}
trap cleanup EXIT

# State the shell claudio is being exercised under, so "I ran the suite" can be
# checked against "I ran it under both shells" without reading the harness.
printf "\n\033[1mclaudio under: %s\033[0m  (run this suite under bash AND dash)\n" "$TEST_SH"

# ============================================================
printf "\n\033[1m=== portability ===\033[0m\n"
# ============================================================

# The no-bash-isms rule, enforced rather than trusted. An array or `[[ ]]` is a
# syntax error in dash, which would otherwise abort the suite mid-run with no
# named failure — this turns that into one legible test.
TESTS=$((TESTS + 1))
if "$TEST_SH" -n "$CLAUDIO" 2>/dev/null; then
  pass "claudio parses under $TEST_SH"
else
  fail "claudio parses under $TEST_SH" "$("$TEST_SH" -n "$CLAUDIO" 2>&1 | head -2)"
fi

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

# Docker MCP mode is the default: marker written, mcp.json is a gateway runner
assert_file_exists "new writes mcp.docker marker" "$PROFILES/test-profile/mcp.docker"
assert_eq "mcp.docker holds docker profile id" "$(cat "$PROFILES/test-profile/mcp.docker")" "test-profile"
assert_contains "new mode message" "$out" "Docker MCP mode"
assert_contains "mcp.json is a gateway runner" "$mcp_content" "gateway"
assert_contains "gateway runner targets docker profile" "$mcp_content" "test-profile"
assert_contains "new calls docker mcp profile create" "$(cat "$DOCKER_LOG")" "profile create --name test-profile"

# --manual opts out of Docker MCP wiring
out=$(run new manual-profile --manual)
assert_contains "new --manual message" "$out" "manual MCP mode"
assert_file_not_exists "new --manual writes no marker" "$PROFILES/manual-profile/mcp.docker"
manual_mcp=$(cat "$PROFILES/manual-profile/mcp.json")
assert_not_contains "new --manual mcp.json is not a gateway runner" "$manual_mcp" "gateway"

# Duplicate name fails
out=$(run new test-profile 2>&1 || true)
assert_contains "new rejects duplicate" "$out" "already exists"
assert_contains "duplicate profile hints at edit" "$out" "claudio edit test-profile"

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
assert_not_contains "use does NOT auto-launch claude" "$out" "\[claude\]"
assert_contains "use prints run hint" "$out" "claudio run"

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

# Docker-mode activation notes the gateway wiring
assert_contains "use notes Docker MCP wiring" "$out" "Docker MCP Toolkit"

# ============================================================
printf "\n\033[1m=== use takes no claude arguments ===\033[0m\n"
# ============================================================

# `use` links and nothing else; -l/--launch used to exec claude here without
# ever resolving an account or tags, which is the failure `run` exists to warn
# about. The flag is gone and its arguments are now an error, not a silent no-op.
out=$(run use test-profile -l 2>&1 || true)
assert_contains "use rejects the removed -l flag" "$out" "Unexpected argument: -l"
assert_not_contains "use never launches claude" "$out" "\[claude\]"
assert_contains "use points at run for launching" "$out" "claudio run test-profile"

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

# The summary is one line per item — it must not grow with the profile's data.
assert_not_contains "show summarises rather than dumping contents" "$out" "mcpServers"
assert_contains "show points at the per-component form" "$out" "claudio show test-profile <component>"

# ...and the component form prints the file itself, same vocabulary as edit.
out=$(run show test-profile mcp)
assert_contains "show <component> prints the item's contents" "$out" "mcpServers"

out=$(run show test-profile bogus-thing 2>&1 || true)
assert_contains "show rejects an invalid component" "$out" "Unknown component"

# show and edit fall back to the profile active in cwd
out=$(run show)
assert_contains "show with no argument uses the active profile" "$out" "Profile: test-profile"
rc=0
run edit > /dev/null 2>&1 || rc=$?
assert_exit_code "edit with no argument uses the active profile" "$rc" "0"

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

# `clean` must remove ONLY symlinks that point into the profiles dir. Both docs
# promise it "can never eat a file claudio does not manage", but nothing tested
# the guard: neutering _is_claudio_link to `return 0` kept the suite fully green
# while `clean` deleted a user's own symlink. These pin all three shapes a
# CONFIG_MAP target can take when it is not claudio's.
cd "$WORKDIR"
rm -rf .claude .mcp.json .claudio CLAUDE.md CLAUDE.local.md
mkdir -p "$TMP/foreign"
printf 'my own shared instructions\n' > "$TMP/foreign/CLAUDE.md"
ln -s "$TMP/foreign/CLAUDE.md" CLAUDE.md          # symlink, but not into $PROFILES
printf '{"mcpServers":{}}\n' > .mcp.json           # a real file, not a symlink
mkdir -p .claude/skills                            # a real directory
echo test-profile > .claudio                       # marker so clean proceeds

out=$(run clean 2>&1)
assert_contains "clean with foreign files still deactivates" "$out" "Profile deactivated"
assert_is_symlink "clean leaves a non-claudio symlink alone" "CLAUDE.md"
assert_eq "clean does not follow a foreign symlink" "$(cat CLAUDE.md)" "my own shared instructions"
assert_file_exists "clean leaves an unmanaged regular file alone" ".mcp.json"
assert_is_dir "clean leaves an unmanaged directory alone" ".claude/skills"
assert_file_exists "clean does not delete the foreign symlink's target" "$TMP/foreign/CLAUDE.md"

rm -rf .claude .mcp.json .claudio CLAUDE.md CLAUDE.local.md "$TMP/foreign"

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
printf "\n\033[1m=== mcp helpers (Docker MCP Toolkit) ===\033[0m\n"
# ============================================================

# add: bare image ref gets a docker:// scheme
: > "$DOCKER_LOG"
out=$(run mcp test-profile add ghcr.io/acme/srv:latest 2>&1)
assert_contains "mcp add calls profile server add" "$(cat "$DOCKER_LOG")" "profile server add test-profile"
assert_contains "mcp add defaults bare ref to docker://" "$(cat "$DOCKER_LOG")" "--server docker://ghcr.io/acme/srv:latest"

# add: an explicit scheme passes through untouched
: > "$DOCKER_LOG"
run mcp test-profile add catalog://mcp/docker-mcp-catalog/github > /dev/null 2>&1
assert_contains "mcp add preserves catalog:// scheme" "$(cat "$DOCKER_LOG")" "--server catalog://mcp/docker-mcp-catalog/github"
assert_not_contains "mcp add does not double-prefix" "$(cat "$DOCKER_LOG")" "docker://catalog"

# rm: removes by name
: > "$DOCKER_LOG"
run mcp test-profile rm github > /dev/null 2>&1
assert_contains "mcp rm calls profile server remove" "$(cat "$DOCKER_LOG")" "profile server remove test-profile github"

# show (default action)
: > "$DOCKER_LOG"
run mcp test-profile > /dev/null 2>&1
assert_contains "mcp <profile> shows servers" "$(cat "$DOCKER_LOG")" "profile show test-profile"

# catalog import (the annoying store dialog, as one command)
: > "$DOCKER_LOG"
out=$(run mcp catalog mcp/community-registry:latest 2>&1)
assert_contains "mcp catalog pulls the OCI reference" "$(cat "$DOCKER_LOG")" "catalog pull mcp/community-registry:latest"

# manual-mode profile has no Docker servers to manage
out=$(run mcp manual-profile add whatever 2>&1 || true)
assert_contains "mcp on manual profile errors" "$out" "manual MCP mode"

# edit mcp on a Docker-mode profile prints management hint (does not open editor)
out=$(run edit test-profile mcp 2>&1)
assert_contains "edit mcp (docker) prints helper hint" "$out" "claudio mcp test-profile add"

# ============================================================
printf "\n\033[1m=== mcp extras (mixed mode: Docker + non-Docker) ===\033[0m\n"
# ============================================================

run new mixed-profile > /dev/null 2>&1

if command -v jq > /dev/null 2>&1; then
  # A remote HTTP server that Docker isn't fit for
  cat > "$PROFILES/mixed-profile/mcp.extra.json" <<'JSON'
{
  "mcpServers": {
    "netdata-remote": {
      "type": "http",
      "url": "http://example.com:19999/mcp",
      "headers": { "Authorization": "Bearer ${KEY}" }
    }
  }
}
JSON

  # `mcp <profile> extra` regenerates mcp.json, merging gateway + extras
  run mcp mixed-profile extra > /dev/null 2>&1
  merged=$(cat "$PROFILES/mixed-profile/mcp.json")
  assert_contains "extras: gateway entry still present" "$merged" "MCP_DOCKER"
  assert_contains "extras: remote server merged in" "$merged" "netdata-remote"
  assert_contains "extras: remote url merged" "$merged" "example.com"

  # `use` regenerates the merged file into the active .mcp.json
  cd "$WORKDIR"
  rm -rf .claude .mcp.json .claudio CLAUDE.md CLAUDE.local.md
  run use mixed-profile > /dev/null 2>&1
  active_mcp=$(cat .mcp.json)
  assert_contains "extras: active .mcp.json has gateway" "$active_mcp" "MCP_DOCKER"
  assert_contains "extras: active .mcp.json has remote server" "$active_mcp" "netdata-remote"
  run clean > /dev/null 2>&1

  # show reflects extras
  out=$(run show mixed-profile)
  assert_contains "show notes extras" "$out" "mcp.extra.json"
  out=$(run mcp mixed-profile show 2>&1)
  assert_contains "mcp show lists extra server" "$out" "netdata-remote"
else
  printf "  (skipped — jq not installed)\n"
fi

# ============================================================
printf "\n\033[1m=== error handling ===\033[0m\n"
# ============================================================

# Missing profile
out=$(run use nonexistent 2>&1 || true)
assert_contains "use: missing profile error" "$out" "not found"
assert_contains "missing profile lists what is available" "$out" "Available:"
assert_contains "missing profile says how to create one" "$out" "claudio new nonexistent"

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

# Profile names become path components, so they are validated like account
# names: `claudio new ../evil` used to create a sibling of the profiles dir.
out=$(run new ../evil-profile 2>&1 || true)
assert_contains "new rejects a traversing profile name" "$out" "Invalid profile name"
assert_file_not_exists "new does not escape the profiles dir" "$PROFILES/../evil-profile"

out=$(run init ../evil-init 2>&1 || true)
assert_contains "init rejects a traversing profile name" "$out" "Invalid profile name"

out=$(run use "my profile" 2>&1 || true)
assert_contains "profile names may not contain whitespace" "$out" "may not contain whitespace"

# Profiles and accounts are both listed with a `*/` glob, which skips dotdirs,
# so a dotted name would create something `list` could never show again.
out=$(run new .hidden 2>&1 || true)
assert_contains "new rejects a dot-prefixed profile name" "$out" "Invalid profile name"
assert_file_not_exists "new does not create a hidden profile" "$PROFILES/.hidden"

out=$(run account new .hidden 2>&1 || true)
assert_contains "account new rejects a dot-prefixed name" "$out" "Invalid account name"

# ============================================================
printf "\n\033[1m=== current with no profile ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -f .claudio
# "Nothing set" is stderr + non-zero, matching `account default`, so
# `p=$(claudio current) || p=none` works for both.
rc=0
out=$(run current) || rc=$?
assert_contains "current with no profile" "$out" "No active profile"
assert_exit_code "current with no profile exits non-zero" "$rc" "1"

# ============================================================
printf "\n\033[1m=== account ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
run clean > /dev/null 2>&1 || true
rm -rf .claude .mcp.json CLAUDE.md CLAUDE.local.md .claudio .claudio.local .gitignore .git

# Empty accounts dir
out=$(run account list)
assert_contains "account list with no accounts" "$out" "No accounts"

# new
# `new` creates AND logs in — an account with no login is useless, so there is
# no second command to remember.
out=$(run account new work)
assert_contains "account new prints creation" "$out" "Created account 'work'"
assert_is_dir "account new creates config dir" "$ACCOUNTS/work"
assert_contains "account new logs in straight away" "$out" "Logging in to account 'work'"
assert_contains "account new hands off to claude /login" "$out" "\[claude\]"

# Hermetic: accounts land in CLAUDIO_ACCOUNTS, never in the real ~/.claude
run account new hermetic-probe > /dev/null
assert_is_dir "account new writes into CLAUDIO_ACCOUNTS" "$ACCOUNTS/hermetic-probe"
assert_file_not_exists "account new does not touch ~/.claudio" "$HOME/.claudio/accounts/hermetic-probe"

run account new personal > /dev/null
run account new conf-acct > /dev/null
run account new local-acct > /dev/null
run account new env-acct > /dev/null
run account new cli-acct > /dev/null

# Duplicate
rc=0
out=$(run account new work 2>&1) || rc=$?
assert_contains "account new rejects duplicate" "$out" "already exists"
assert_exit_code "account new duplicate exits non-zero" "$rc" "1"

# list
out=$(run account list)
assert_contains "account list shows accounts dir" "$out" "$ACCOUNTS"
assert_contains "account list shows work" "$out" "work"
assert_contains "account list shows personal" "$out" "personal"
assert_contains "account list reports logged-out state" "$out" "not logged in"

# Login state is read from the account's own .claude.json
printf '{"oauthAccount":{"emailAddress":"a@example.com"}}\n' > "$ACCOUNTS/personal/.claude.json"
out=$(run account list)
assert_contains "account list detects logged-in account" "$out" "logged in"

# path
out=$(run account path work)
assert_eq "account path prints config dir" "$out" "$ACCOUNTS/work"

# Unknown account
rc=0
out=$(run account path nope 2>&1) || rc=$?
assert_contains "account path: unknown account errors" "$out" "Account not found: nope"
assert_contains "account path: unknown account hints at new" "$out" "account new nope"
assert_exit_code "account path unknown exits non-zero" "$rc" "1"

out=$(run account new 2>&1 || true)
assert_contains "account new requires a name" "$out" "Usage: claudio account new"

out=$(run account bogus 2>&1 || true)
assert_contains "account rejects unknown action" "$out" "Unknown account action"

# rm requires typed confirmation
run account new throwaway > /dev/null
out=$(printf 'wrong\n' | run account rm throwaway || true)
assert_contains "account rm aborts on wrong confirmation" "$out" "Aborted"
assert_is_dir "account rm keeps dir when aborted" "$ACCOUNTS/throwaway"

out=$(printf 'throwaway\n' | run account rm throwaway)
assert_contains "account rm confirms removal" "$out" "Removed account 'throwaway'"
assert_file_not_exists "account rm deletes config dir" "$ACCOUNTS/throwaway"

# ============================================================
printf "\n\033[1m=== env: auto tags ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -f .claudio.local

out=$(run env test-profile)
assert_contains "env tags the profile automatically" "$out" "OTEL_RESOURCE_ATTRIBUTES=profile=test-profile"
assert_contains "env says it does not link the profile" "$out" "only sets variables"
assert_not_contains "env without account has no account tag" "$out" "account="
assert_not_contains "env without account leaves CLAUDE_CONFIG_DIR unset" "$out" "CLAUDE_CONFIG_DIR"

out=$(run env test-profile --account work)
assert_eq "env with account exports CLAUDE_CONFIG_DIR" "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/work"
assert_eq "env tags profile and account" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,account=work,host=testhost"

# --account=<name> form
out=$(run env test-profile --account=work)
assert_eq "env accepts --account=name" "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/work"

# Unknown account
rc=0
out=$(run env test-profile --account nope 2>&1) || rc=$?
assert_contains "env rejects unknown account" "$out" "Account not found: nope"
# Same invariant as run: refusing must mean printing nothing eval-able.
assert_not_contains "env prints no exports when the account is unknown" "$out" "export "
# The exit code is the load-bearing half of this invariant, and the ONLY half
# for env. `_build_env` validates before it emits anything, so "no export line"
# is equally true when the exit is swallowed — that assertion cannot fail.
# `eval "$(claudio env --account nope)"` must not quietly succeed with no
# account set, so pin the status too.
assert_exit_code "env exits non-zero on an unknown account" "$rc" "1"

out=$(run env test-profile --account 2>&1 || true)
assert_contains "env: --account needs a value" "$out" "--account needs a value"

# env falls back to the active profile
run use test-profile > /dev/null
out=$(run env)
assert_eq "env falls back to active profile" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost"
assert_not_contains "env is quiet when the profile is linked" "$out" "only sets variables"
run clean > /dev/null

# ============================================================
printf "\n\033[1m=== env: same profile, two accounts ===\033[0m\n"
# ============================================================

out_a=$(run env test-profile --account work)
out_b=$(run env test-profile --account personal)

dir_a=$(envval "$out_a" CLAUDE_CONFIG_DIR)
dir_b=$(envval "$out_b" CLAUDE_CONFIG_DIR)
assert_eq "account work maps to its own config dir" "$dir_a" "$ACCOUNTS/work"
assert_eq "account personal maps to its own config dir" "$dir_b" "$ACCOUNTS/personal"
TESTS=$((TESTS + 1))
if [ "$dir_a" != "$dir_b" ]; then
  pass "two accounts yield different CLAUDE_CONFIG_DIR"
else
  fail "two accounts yield different CLAUDE_CONFIG_DIR" "both were '$dir_a'"
fi

assert_contains "same profile tag under account work" "$out_a" "profile=test-profile"
assert_contains "same profile tag under account personal" "$out_b" "profile=test-profile"
assert_contains "account tag differs (work)" "$out_a" "account=work"
assert_contains "account tag differs (personal)" "$out_b" "account=personal"

# ============================================================
printf "\n\033[1m=== tag layering ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -f .claudio.local

# --tag on its own
out=$(run env test-profile --tag env=adhoc)
assert_eq "--tag is appended to the auto tags" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost,env=adhoc"

out=$(run env test-profile --tag=env=adhoc)
assert_eq "--tag=k=v form works" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost,env=adhoc"

out=$(run env test-profile --tag env=adhoc --tag team=soc)
assert_eq "--tag is repeatable" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost,env=adhoc,team=soc"

# Layer 1: the global conf. A machine-wide tag= used to be read by nothing at
# all — silently dropped, with the docs promising all three layers. $CONF is
# shared with every later section and does not exist yet here, so it is removed
# again below rather than left behind.
printf 'tag=team=global\ntag=fleet=eu\n' > "$CONF"
out=$(run env test-profile)
assert_eq "global conf tags are picked up" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost,team=global,fleet=eu"

# The global layer sits BELOW the automatic tags, like every other config file:
# a machine-wide tag=host= must win, or `tag=email=redacted` cannot suppress
# the PII tag machine-wide either.
printf 'tag=host=globalhost\n' > "$CONF"
out=$(run env test-profile)
assert_eq "global conf tag=host= overrides the auto host tag" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=globalhost"

# Layer 2: profile conf. Asserted against a global tag of the SAME key, so the
# ordering is pinned rather than mere presence: below the profile line and the
# global value would win instead.
printf 'tag=team=global\ntag=fleet=eu\n' > "$CONF"
printf 'tag=env=profile-conf\ntag=team=soc\n' > "$PROFILES/test-profile/claudio.conf"
out=$(run env test-profile)
assert_eq "profile conf overrides global conf" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost,team=soc,fleet=eu,env=profile-conf"

rm -f "$CONF"
out=$(run env test-profile)
assert_eq "profile conf tags are picked up" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost,env=profile-conf,team=soc"

# Layer 3: .claudio.local overrides the profile conf
printf 'tag=env=project-local\n' > .claudio.local
out=$(run env test-profile)
assert_eq ".claudio.local overrides profile conf" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost,env=project-local,team=soc"

# Layer 4: CLAUDIO_TAGS overrides .claudio.local
out=$(CLAUDIO_TAGS=env=from-env run env test-profile)
assert_eq "CLAUDIO_TAGS overrides .claudio.local" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost,env=from-env,team=soc"

# Layer 5: --tag overrides everything
out=$(CLAUDIO_TAGS=env=from-env run env test-profile --tag env=from-flag)
assert_eq "--tag overrides CLAUDIO_TAGS" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost,env=from-flag,team=soc"

# A tag may also override the automatic ones
out=$(run env test-profile --tag profile=renamed)
assert_eq "--tag can override the auto profile tag" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=renamed,host=testhost,env=project-local,team=soc"

# CLAUDIO_TAGS is comma separated
out=$(CLAUDIO_TAGS=a=1,b=2 run env test-profile)
assert_contains "CLAUDIO_TAGS splits on commas (a)" "$out" "a=1"
assert_contains "CLAUDIO_TAGS splits on commas (b)" "$out" "b=2"

# $CONF is shared with every later section: leave nothing behind.
rm -f .claudio.local "$PROFILES/test-profile/claudio.conf" "$CONF"

# ============================================================
printf "\n\033[1m=== auto host tag ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -f .claudio.local

# Present by default, alongside profile= and account=.
out=$(run env test-profile)
assert_eq "host tag is emitted automatically" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=testhost"

out=$(run env test-profile --account work)
assert_eq "host tag sits after profile and account" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,account=work,host=testhost"

# Machine identity does not depend on a profile or an account existing.
mkdir -p "$TMP/hostonly"
out=$(cd "$TMP/hostonly" && run env)
assert_eq "host tag is emitted with no profile and no account" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "host=testhost"

# Detected from the system when CLAUDIO_HOST is empty. The value is whatever
# this machine is called, so assert the shape rather than a literal.
detected=$(CLAUDIO_HOST='' run env test-profile)
detected=$(envval "$detected" OTEL_RESOURCE_ATTRIBUTES)
assert_contains "host tag is detected when CLAUDIO_HOST is unset" "$detected" "host="
TESTS=$((TESTS + 1))
if echo "$detected" | grep -q 'host=[a-z0-9_-][a-z0-9_-]*\(,\|$\)'; then
  pass "detected host tag is a lowercase slug"
else
  fail "detected host tag is a lowercase slug" "got: $detected"
fi

# --- sanitisation: the raw name is never trusted ---
# OTEL_RESOURCE_ATTRIBUTES is comma separated with no quoting, so spaces,
# commas, apostrophes and case all have to be normalised away.
out=$(CLAUDIO_HOST="Gabriel's MacBook Pro" run env test-profile)
assert_eq "host slug strips spaces, case and punctuation" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=gabriel-s-macbook-pro"

out=$(CLAUDIO_HOST="web-01.corp.example.com" run env test-profile)
assert_eq "host slug drops the domain" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=web-01"

out=$(CLAUDIO_HOST="a,b" run env test-profile)
assert_eq "host slug removes commas" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=a-b"

out=$(CLAUDIO_HOST="--Weird__Box--" run env test-profile)
assert_eq "host slug trims leading and trailing dashes" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=weird__box"

# A name that sanitises to nothing must drop the tag, not emit `host=`.
out=$(CLAUDIO_HOST="!!!" run env test-profile)
assert_eq "unusable host name emits no host tag" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile"

# The sanitised value always satisfies _validate_tag, so it round-trips as a
# hand-written tag too.
out=$(CLAUDIO_HOST="Gabriel's MacBook Pro" run env test-profile --tag other=x)
assert_not_contains "sanitised host tag never contains a space" "$out" "host=[a-z0-9_-]* "

# --- explicit overrides beat the automatic value, at every layer ---
out=$(run env test-profile --tag host=laptop)
assert_eq "--tag host= overrides the auto host tag" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=laptop"

out=$(CLAUDIO_TAGS=host=from-env run env test-profile)
assert_eq "CLAUDIO_TAGS host= overrides the auto host tag" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=from-env"

printf 'tag=host=from-profile-conf\n' > "$PROFILES/test-profile/claudio.conf"
out=$(run env test-profile)
assert_eq "profile conf host= overrides the auto host tag" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=from-profile-conf"

printf 'tag=host=from-project\n' > .claudio.local
out=$(run env test-profile)
assert_eq ".claudio.local host= beats profile conf and auto" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=from-project"

out=$(run env test-profile --tag host=from-flag)
assert_eq "--tag host= beats every other layer" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,host=from-flag"

# An override keeps the auto slot's position rather than moving to the end.
out=$(run env test-profile --account work --tag host=laptop --tag team=soc)
assert_eq "overridden host keeps its position in the tag order" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,account=work,host=laptop,team=soc"

rm -f .claudio.local "$PROFILES/test-profile/claudio.conf"

# ============================================================
printf "\n\033[1m=== invalid tags ===\033[0m\n"
# ============================================================

rc=0
out=$(run env test-profile --tag noequals 2>&1) || rc=$?
assert_contains "tag without = is rejected" "$out" "Tag must be key=value"
assert_exit_code "invalid tag exits non-zero" "$rc" "1"

out=$(run env test-profile --tag "env=a b" 2>&1 || true)
assert_contains "tag with a space is rejected" "$out" "may not contain spaces or commas"

out=$(run env test-profile --tag "env=a,b=c" 2>&1 || true)
assert_contains "tag with a comma is rejected" "$out" "may not contain spaces or commas"

out=$(run env test-profile --tag 2>&1 || true)
assert_contains "--tag needs a value" "$out" "--tag needs key=value"

out=$(run env test-profile --bogus 2>&1 || true)
assert_contains "env rejects unknown option" "$out" "Unknown option: --bogus"
assert_contains "unknown option points at -- for claude flags" "$out" "after --"

# ============================================================
printf "\n\033[1m=== account precedence ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -f .claudio.local

# Nothing configured anywhere: CLAUDE_CONFIG_DIR must stay unset
out=$(run env test-profile)
assert_not_contains "no account anywhere: CLAUDE_CONFIG_DIR not exported" "$out" "CLAUDE_CONFIG_DIR"

# Layer 1: profile conf
printf 'account=conf-acct\n' > "$PROFILES/test-profile/claudio.conf"
out=$(run env test-profile)
assert_eq "profile conf supplies the account" "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/conf-acct"
assert_contains "profile conf account is tagged" "$out" "account=conf-acct"

# Layer 2: .claudio.local beats the profile conf
printf 'account=local-acct\n' > .claudio.local
out=$(run env test-profile)
assert_eq ".claudio.local beats profile conf" "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/local-acct"

# Layer 3: CLAUDIO_ACCOUNT beats .claudio.local
out=$(CLAUDIO_ACCOUNT=env-acct run env test-profile)
assert_eq "CLAUDIO_ACCOUNT beats .claudio.local" "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/env-acct"

# Layer 4: --account beats everything
out=$(CLAUDIO_ACCOUNT=env-acct run env test-profile --account cli-acct)
assert_eq "--account beats CLAUDIO_ACCOUNT" "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/cli-acct"
assert_contains "--account is the tagged account" "$out" "account=cli-acct"

rm -f .claudio.local "$PROFILES/test-profile/claudio.conf"

# The account axis is independent of the profile: second-profile picks up the
# same account without any per-profile wiring.
out=$(run env second-profile --account work)
assert_eq "any profile runs under any account" "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/work"
assert_contains "account is independent of profile" "$out" "profile=second-profile,account=work"

# ============================================================
printf "\n\033[1m=== telemetry (opt-in) ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -f .claudio.local
: > "$CONF"

out=$(run env test-profile)
assert_not_contains "telemetry off by default" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
assert_not_contains "no exporter configured by default" "$out" "OTEL_EXPORTER_OTLP_ENDPOINT"
assert_contains "resource attributes exported even with telemetry off" "$out" "OTEL_RESOURCE_ATTRIBUTES"

# Global conf turns it on
printf 'otel=1\n' > "$CONF"
out=$(run env test-profile)
assert_contains "otel=1 in global conf enables telemetry" "$out" "export CLAUDE_CODE_ENABLE_TELEMETRY=1"
assert_contains "telemetry sets metrics exporter" "$out" "OTEL_METRICS_EXPORTER=otlp"
assert_contains "telemetry sets logs exporter" "$out" "OTEL_LOGS_EXPORTER=otlp"
# The defaults are the shape claudio's OWN receiver serves, and that is the
# whole of their justification. They were `grpc` and 4317 — the Docker
# collector's shape — while `run` starts a receiver only for an http/json
# loopback export, so the documented opt-in (`otel=1` and nothing else) turned
# telemetry on, started nothing, and exported every record into a closed port,
# which Claude Code drops in silence by design.
assert_eq "telemetry protocol defaults to what claudio's receiver speaks" \
  "$(envval "$out" OTEL_EXPORTER_OTLP_PROTOCOL)" "http/json"
assert_eq "telemetry endpoint defaults to the port it listens on" \
  "$(envval "$out" OTEL_EXPORTER_OTLP_ENDPOINT)" "http://localhost:4318"

# --no-telemetry suppresses it for this invocation
out=$(run env test-profile --no-telemetry)
assert_not_contains "--no-telemetry suppresses telemetry" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
assert_contains "--no-telemetry keeps resource attributes" "$out" "OTEL_RESOURCE_ATTRIBUTES=profile=test-profile"

# Endpoint/protocol overrides
printf 'otel=1\notel_protocol=http/protobuf\notel_endpoint=http://collector:4318\n' > "$CONF"
out=$(run env test-profile)
assert_eq "otel_protocol is honoured" "$(envval "$out" OTEL_EXPORTER_OTLP_PROTOCOL)" "http/protobuf"
assert_eq "otel_endpoint is honoured" "$(envval "$out" OTEL_EXPORTER_OTLP_ENDPOINT)" "http://collector:4318"

# .claudio.local overrides the global conf
printf 'otel=0\n' > .claudio.local
out=$(run env test-profile)
assert_not_contains ".claudio.local otel=0 overrides global otel=1" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f .claudio.local

# Profile conf can enable it on its own
: > "$CONF"
printf 'otel=1\n' > "$PROFILES/test-profile/claudio.conf"
out=$(run env test-profile)
assert_contains "profile conf can enable telemetry" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
out=$(run env second-profile)
assert_not_contains "profile conf telemetry does not leak to other profiles" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f "$PROFILES/test-profile/claudio.conf"

: > "$CONF"

# ============================================================
printf "\n\033[1m=== per-account telemetry ===\033[0m\n"
# ============================================================
# The one resolver that goes narrow to broad: account.<name>.<key> in the
# GLOBAL conf outranks project and profile, because a privacy choice attached
# to an identity must not be overridable by a file inside a repo.

cd "$WORKDIR"
rm -f .claudio.local
: > "$CONF"

# Turns telemetry on for an account that the global default leaves off.
printf 'account.work.otel=1\n' > "$CONF"
out=$(run env test-profile --account work)
assert_contains "account.<x>.otel=1 enables telemetry for that account" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
out=$(run env test-profile --account personal)
assert_not_contains "account telemetry does not leak to another account" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
out=$(run env test-profile)
assert_not_contains "account layer is skipped when no account resolves" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"

# The headline case: otel=0 for one identity, whatever any repo says.
printf 'otel=1\naccount.personal.otel=0\n' > "$CONF"
out=$(run env test-profile --account work)
assert_contains "global otel=1 still applies to other accounts" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
out=$(run env test-profile --account personal)
assert_not_contains "account.<x>.otel=0 overrides global otel=1" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"

printf 'otel=1\n' > .claudio.local
out=$(run env test-profile --account personal)
assert_not_contains "account.<x>.otel=0 beats .claudio.local otel=1" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f .claudio.local

printf 'otel=1\n' > "$PROFILES/test-profile/claudio.conf"
out=$(run env test-profile --account personal)
assert_not_contains "account.<x>.otel=0 beats profile conf otel=1" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f "$PROFILES/test-profile/claudio.conf"

# Endpoint and protocol take the same layer.
printf 'otel=1\notel_endpoint=http://global:4317\naccount.work.otel_endpoint=http://work:4317\naccount.work.otel_protocol=http/protobuf\n' > "$CONF"
out=$(run env test-profile --account work)
assert_eq "account.<x>.otel_endpoint beats the global endpoint" "$(envval "$out" OTEL_EXPORTER_OTLP_ENDPOINT)" "http://work:4317"
assert_eq "account.<x>.otel_protocol beats the default" "$(envval "$out" OTEL_EXPORTER_OTLP_PROTOCOL)" "http/protobuf"
out=$(run env test-profile --account personal)
assert_eq "another account keeps the global endpoint" "$(envval "$out" OTEL_EXPORTER_OTLP_ENDPOINT)" "http://global:4317"

printf 'otel=1\notel_endpoint=http://global:4317\naccount.work.otel_endpoint=http://work:4317\n' > "$CONF"
printf 'otel_endpoint=http://project:4317\n' > .claudio.local
out=$(run env test-profile --account work)
assert_eq "account endpoint beats .claudio.local" "$(envval "$out" OTEL_EXPORTER_OTLP_ENDPOINT)" "http://work:4317"
out=$(run env test-profile --account personal)
assert_eq ".claudio.local still wins where no account key exists" "$(envval "$out" OTEL_EXPORTER_OTLP_ENDPOINT)" "http://project:4317"
rm -f .claudio.local

# --no-telemetry is checked before the otel lookup, so it silences a run even
# when the account layer switched telemetry on.
printf 'account.work.otel=1\n' > "$CONF"
out=$(run env test-profile --account work --no-telemetry)
assert_not_contains "--no-telemetry beats account.<x>.otel=1" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
assert_contains "--no-telemetry still keeps resource attributes" "$out" "OTEL_RESOURCE_ATTRIBUTES"

# The account layer is global-conf-only: a repo cannot grant itself one.
printf 'otel=1\naccount.personal.otel=0\n' > "$CONF"
printf 'account.personal.otel=1\n' > .claudio.local
out=$(run env test-profile --account personal)
assert_not_contains "account.<x>. keys in .claudio.local are inert" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f .claudio.local

printf 'otel=0\naccount.work.otel=1\n' > "$CONF"
printf 'account.work.otel=1\n' > "$PROFILES/test-profile/claudio.conf"
out=$(run env test-profile --account personal)
assert_not_contains "account.<x>. keys in a profile conf are inert" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f "$PROFILES/test-profile/claudio.conf"

# _conf_values matches a quoted case pattern, so the dotted key is a literal:
# it must not be picked up by a lookup of `otel` or of `account`.
printf 'account.work.otel=1\naccount.work.otel_endpoint=http://work:4317\n' > "$CONF"
out=$(run env test-profile)
assert_not_contains "a dotted key does not answer a plain otel= lookup" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
out=$(run env test-profile)
assert_not_contains "a dotted key is not read as a machine default account" "$out" "CLAUDE_CONFIG_DIR"

# ...and a real account= line still resolves with dotted keys around it.
printf 'account.work.otel=1\naccount=work\n' > "$CONF"
out=$(run env test-profile)
assert_eq "account= still resolves alongside account.<x>.<key> lines" "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/work"
assert_contains "the resolved default account picks up its own telemetry" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"

# An account name claudio has never heard of is inert, not an error.
printf 'otel=1\naccount.nosuch.otel=0\n' > "$CONF"
out=$(run env test-profile --account work)
assert_contains "an unknown account.<x>.<key> is inert" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"

# $CONF is shared with every later section: leave nothing behind.
: > "$CONF"
rm -f .claudio.local

# ============================================================
printf "\n\033[1m=== logging= (one key for both streams) ===\033[0m\n"
# ============================================================
# `logging=none|local|remote` replaces the juggling of otel= and usage=. These
# mirror the otel= cases above case for case, because the layering rule is
# meant to be exactly the same one — the account inversion included.

cd "$WORKDIR"
rm -f .claudio.local
: > "$CONF"

out=$(run env test-profile)
assert_not_contains "logging is off when nothing sets it" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"

printf 'logging=local\n' > "$CONF"
out=$(run env test-profile)
assert_contains "logging=local enables telemetry" "$out" "export CLAUDE_CODE_ENABLE_TELEMETRY=1"
assert_eq "logging=local exports the protocol claudio's own receiver speaks" \
  "$(envval "$out" OTEL_EXPORTER_OTLP_PROTOCOL)" "http/json"
assert_eq "logging=local exports the endpoint it listens on" \
  "$(envval "$out" OTEL_EXPORTER_OTLP_ENDPOINT)" "http://localhost:4318"

# `remote` IMPLIES local — the file is written either way. A
# remote-without-local mode was considered and dropped: it saves a rotating
# 32MB file and buys a state where a healthy setup is indistinguishable from a
# broken one.
printf 'logging=remote\n' > "$CONF"
out=$(run env test-profile)
assert_contains "logging=remote implies local, so stream A is on too" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"

printf 'logging=none\n' > "$CONF"
out=$(run env test-profile)
assert_not_contains "logging=none exports nothing" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"

# The three plain layers, narrow beats broad, exactly as otel= does.
printf 'logging=local\n' > "$CONF"
printf 'logging=none\n' > .claudio.local
out=$(run env test-profile)
assert_not_contains ".claudio.local logging=none overrides global logging=local" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f .claudio.local

: > "$CONF"
printf 'logging=local\n' > "$PROFILES/test-profile/claudio.conf"
out=$(run env test-profile)
assert_contains "a profile conf can turn logging on by itself" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
out=$(run env second-profile)
assert_not_contains "a profile's logging does not leak to other profiles" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
printf 'logging=none\n' > .claudio.local
out=$(run env test-profile)
assert_not_contains "the project file beats the profile" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f .claudio.local "$PROFILES/test-profile/claudio.conf"

# The inversion, which is the whole reason this key is not resolved like the
# others: a reporting or privacy choice attached to an identity must not be
# overridable by a file inside a repo that anyone may have written.
printf 'logging=local\naccount.personal.logging=none\n' > "$CONF"
out=$(run env test-profile --account work)
assert_contains "global logging=local still applies to other accounts" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
out=$(run env test-profile --account personal)
assert_not_contains "account.<x>.logging=none overrides global logging=local" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
printf 'logging=local\n' > .claudio.local
out=$(run env test-profile --account personal)
assert_not_contains "account.<x>.logging=none beats .claudio.local logging=local" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f .claudio.local
printf 'logging=local\n' > "$PROFILES/test-profile/claudio.conf"
out=$(run env test-profile --account personal)
assert_not_contains "account.<x>.logging=none beats a profile conf logging=local" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f "$PROFILES/test-profile/claudio.conf"

printf 'account.work.logging=remote\n' > "$CONF"
out=$(run env test-profile --account work)
assert_contains "account.<x>.logging=remote turns it on for that account" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
out=$(run env test-profile --account personal)
assert_not_contains "...and not for another" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
out=$(run env test-profile)
assert_not_contains "...and the layer is skipped when no account resolves" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"

# Global conf only: a repo cannot grant itself an account-level key.
printf 'logging=local\naccount.personal.logging=none\n' > "$CONF"
printf 'account.personal.logging=local\n' > .claudio.local
out=$(run env test-profile --account personal)
assert_not_contains "account.<x>.logging in .claudio.local is inert" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f .claudio.local
printf 'account.work.logging=local\n' > "$PROFILES/test-profile/claudio.conf"
printf 'logging=none\n' > "$CONF"
out=$(run env test-profile --account work)
assert_not_contains "account.<x>.logging in a profile conf is inert" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f "$PROFILES/test-profile/claudio.conf"

# The dotted key is matched as a literal, so it must not answer a plain lookup.
printf 'account.work.logging=local\n' > "$CONF"
out=$(run env test-profile)
assert_not_contains "a dotted key does not answer a plain logging= lookup" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"

# --no-telemetry sits above everything the layers agreed on.
printf 'account.work.logging=remote\n' > "$CONF"
out=$(run env test-profile --account work --no-telemetry)
assert_not_contains "--no-telemetry beats account.<x>.logging=remote" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
assert_contains "--no-telemetry still keeps resource attributes" "$out" "OTEL_RESOURCE_ATTRIBUTES"

# A value claudio does not know is not licence to guess: guessing `local`
# turns on something nobody asked for, and guessing `none` in silence is the
# loss this project calls its cardinal sin. So it is off, and it is named.
printf 'logging=lokal\n' > "$CONF"
out=$(run env test-profile)
assert_not_contains "an unknown logging= value does not enable anything" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
assert_contains "...and it is named rather than swallowed" "$out" "logging=lokal is not a value claudio knows"
assert_contains "...with the values that are" "$out" "none|local|remote"

# The combination nothing covered, and the one that costs the most: a typo in
# the new key while the machine still carries the old one. The unknown-value
# branch `return 0`d past BOTH the fallback and the retirement notice, so the
# export was lost and the only thing said was "claudio does not know that
# value" — whose reasonable reading is that the previous configuration still
# stands. It did not.
printf 'logging=lokal\notel=1\n' > "$CONF"
out=$(run env test-profile 2>&1)
assert_not_contains "an unknown logging= value still retires the old keys" \
  "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
assert_contains "...and names the key it retired" "$out" "deprecated otel= is ignored"
assert_contains "...without losing the unknown-value message" \
  "$out" "logging=lokal is not a value claudio knows"
printf 'logging=lokal\nusage=1\n' > "$CONF"
out=$(run env test-profile 2>&1)
assert_contains "the same for a deprecated usage=" "$out" "deprecated usage= is ignored"
# And it must not name a mode the user never wrote: the `elif` below the
# branch says "logging=none is set", which is nobody's configuration.
assert_not_contains "...and never claims a mode nobody configured" \
  "$out" "logging=none is set"

# `remote` ships now, so what has to be said out loud is every state in which
# the user believes it is shipping and it is not. Silence is reserved for the
# one configuration that actually works — which is what makes the silence
# readable as good news.
printf 'logging=remote\n' > "$CONF"
out=$(run env test-profile 2>&1)
assert_contains "logging=remote with no account says nothing is shipped" \
  "$out" "no account resolved"
assert_contains "...and names where destinations live" "$out" "account.<name>.ship_url"
assert_contains "...while still recording locally" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"

out=$(run env test-profile --account work 2>&1)
assert_contains "logging=remote with no ship_url names the account" \
  "$out" "account work has no destination"
assert_contains "...and the exact line to add" "$out" "account.work.ship_url=<url>"
assert_contains "...and says the records are kept, not lost" "$out" "kept locally"

printf 'logging=remote\naccount.work.ship_url=http://sink.example/v1\n' > "$CONF"
out=$(run env test-profile --account work 2>&1)
assert_not_contains "a configured destination says nothing at all" \
  "$out" "no destination"
assert_contains "...and still records" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
out=$(run env test-profile --account personal 2>&1)
assert_contains "...and the other account is still named" \
  "$out" "account personal has no destination"

printf 'logging=remote\notel=1\n' > "$CONF"
out=$(run env test-profile --account work 2>&1)
assert_contains "...and the note survives a deprecation notice beside it" \
  "$out" "account work has no destination"
assert_contains "...which is still given" "$out" "deprecated otel= is ignored"

printf 'logging=local\naccount.work.ship_url=http://sink.example/v1\n' > "$CONF"
out=$(run env test-profile --account work 2>&1)
assert_not_contains "logging=local carries no shipping note" "$out" "shipped"

: > "$CONF"
rm -f .claudio.local

# ============================================================
printf "\n\033[1m=== account.<x>.ship_url (global conf only, fail closed) ===\033[0m\n"
# ============================================================
# The destination is the one setting where the broad config layers are not a
# convenience but a hole: a `.claudio.local` or a profile `claudio.conf` inside
# a repository someone else wrote must never be able to name where this
# machine's telemetry is posted. So `_ship_conf` reads ONLY
# `account.<name>.ship_url` and ONLY from the global conf — the account
# inversion with nothing left to fall through to.
#
# These mirror the account.<x>.otel cases above case for case, because it is
# meant to be the same rule with the fall-through removed.

cd "$WORKDIR"
rm -f .claudio.local
: > "$CONF"

printf 'logging=remote\naccount.work.ship_url=http://work.example/v1\n' > "$CONF"
out=$(run env test-profile --account work 2>&1)
assert_not_contains "account.<x>.ship_url configures that account" "$out" "no destination"
out=$(run env test-profile --account personal 2>&1)
assert_contains "...and does not leak to another account" "$out" "account personal has no destination"
out=$(run env test-profile 2>&1)
assert_contains "...and is skipped when no account resolves" "$out" "no account resolved"

# There is no plain key. This is the whole difference from _telemetry_conf, and
# it is what makes the failure mode fail CLOSED: a machine-wide default
# destination would ship every account's rows to one server, and the real
# capture interleaves three accounts in one ledger file.
printf 'logging=remote\nship_url=http://global.example/v1\n' > "$CONF"
out=$(run env test-profile --account work 2>&1)
assert_contains "a plain ship_url= is not a destination for anyone" \
  "$out" "account work has no destination"

# Global conf only: a repo cannot grant itself a destination.
printf 'logging=remote\n' > "$CONF"
printf 'account.work.ship_url=http://evil.example/v1\n' > .claudio.local
out=$(run env test-profile --account work 2>&1)
assert_contains "account.<x>.ship_url in .claudio.local is inert" \
  "$out" "account work has no destination"
rm -f .claudio.local

printf 'account.work.ship_url=http://evil.example/v1\n' > "$PROFILES/test-profile/claudio.conf"
out=$(run env test-profile --account work 2>&1)
assert_contains "account.<x>.ship_url in a profile conf is inert" \
  "$out" "account work has no destination"
rm -f "$PROFILES/test-profile/claudio.conf"

# The dotted key is matched as a literal, so it cannot answer a plain lookup
# and an account named `*` cannot inject a glob.
printf 'logging=remote\naccount.work.ship_url=http://work.example/v1\n' > "$CONF"
out=$(run env test-profile --account personal 2>&1)
assert_contains "a dotted ship_url does not answer another account's lookup" \
  "$out" "account personal has no destination"

# The mode layer the shipper cannot honour, said rather than left to look like
# shipping: the receiver is machine-wide, with no cwd and no profile, so it
# reads `logging=` from the global conf only.
printf 'account.work.ship_url=http://work.example/v1\nlogging=local\n' > "$CONF"
printf 'logging=remote\n' > .claudio.local
out=$(run env test-profile --account work 2>&1)
assert_contains "logging=remote from a project file cannot ship, and says so" \
  "$out" "shipper reads the mode from"
assert_contains "...naming the layer it actually came from" "$out" ".claudio.local"
rm -f .claudio.local

printf 'logging=remote\naccount.work.ship_url=http://work.example/v1\naccount.work.logging=remote\n' > "$CONF"
out=$(run env test-profile --account work 2>&1)
assert_not_contains "...and an account-level remote in the global conf is fine" \
  "$out" "shipper reads the mode from"

: > "$CONF"
rm -f .claudio.local

# ============================================================
printf "\n\033[1m=== otel=/usage= deprecation (both work this release) ===\033[0m\n"
# ============================================================
# `logging=` replaces them, and the replaced keys are read for one release with
# a warning. Both halves are pinned: the old keys must still work, and a user
# whose old key is now being IGNORED must be told so — a key silently dropped
# is exactly the failure this project exists to avoid, and a deprecation is
# precisely the moment it happens.

cd "$WORKDIR"
rm -f .claudio.local
: > "$CONF"

printf 'otel=1\n' > "$CONF"
out=$(run env test-profile)
assert_contains "the deprecated otel=1 still enables telemetry" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
assert_contains "...and says it is deprecated" "$out" "otel= is deprecated"
assert_contains "...naming the key that replaces it" "$out" "logging=none|local|remote"

printf 'usage=1\n' > "$CONF"
out=$(run env test-profile)
assert_contains "the deprecated usage=1 is reported too" "$out" "usage= is deprecated"
assert_not_contains "...and usage= alone still exports no telemetry" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"

printf 'otel=1\nusage=1\n' > "$CONF"
out=$(run env test-profile)
assert_contains "both deprecated keys are named in one line" "$out" "otel= and usage= are deprecated"

# The old keys stay INDEPENDENT of each other while they last, so a machine
# already carrying otel=1 and usage=0 behaves across the upgrade exactly as it
# did before it.
printf 'otel=1\nusage=0\n' > "$CONF"
out=$(run env test-profile)
assert_contains "otel=1 with usage=0 still exports" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"

# logging= wins wherever it is found, and the ignored key is said out loud.
printf 'logging=none\notel=1\n' > "$CONF"
out=$(run env test-profile)
assert_not_contains "logging=none beats a deprecated otel=1" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
assert_contains "...and says the old key is being ignored" "$out" "so the deprecated otel= is ignored"

printf 'logging=local\notel=0\nusage=0\n' > "$CONF"
out=$(run env test-profile)
assert_contains "logging=local beats a deprecated otel=0" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
assert_contains "...naming both ignored keys" "$out" "so the deprecated otel= and usage= are ignored"

# ONE rule, not a per-layer merge: logging= anywhere retires the old keys
# everywhere, whatever layer either sits in. A merge would leave a config in
# which some old lines still bite and some do not, and the direction chosen is
# the one that cannot make `logging=local` quietly do nothing.
printf 'otel=1\n' > "$CONF"
printf 'logging=none\n' > .claudio.local
out=$(run env test-profile)
assert_not_contains ".claudio.local logging=none retires a global otel=1" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
rm -f .claudio.local
printf 'logging=none\n' > "$CONF"
printf 'otel=1\n' > .claudio.local
out=$(run env test-profile)
assert_not_contains "a narrower otel=1 does not survive a logging= anywhere" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
assert_contains "...and the retired key is named" "$out" "so the deprecated otel= is ignored"
rm -f .claudio.local

# The price of that rule, stated out loud rather than discovered: an
# account.<x>.otel=0 stops applying the moment logging= appears. It is a
# privacy key, so it is named on stderr on every run until the line is gone.
printf 'logging=local\naccount.personal.otel=0\n' > "$CONF"
out=$(run env test-profile --account personal)
assert_contains "logging=local retires even an account-level otel=0" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY=1"
assert_contains "...and that is reported, never silent" "$out" "so the deprecated otel= is ignored"
assert_contains "...telling you what to do about it" "$out" "delete it"

# A config file that is going to stop working is not a telemetry setting, so
# --no-telemetry must not silence the notice about it. Printed ABOVE the
# --no-telemetry return for exactly this reason.
printf 'otel=1\n' > "$CONF"
out=$(run env test-profile --no-telemetry)
assert_not_contains "--no-telemetry still suppresses the export" "$out" "CLAUDE_CODE_ENABLE_TELEMETRY"
assert_contains "...but not the deprecation notice" "$out" "otel= is deprecated"

# Nothing deprecated configured, nothing said. A warning printed on every run
# of a correctly configured machine is a warning nobody reads.
printf 'logging=local\n' > "$CONF"
out=$(run env test-profile)
assert_not_contains "a machine on logging= alone is never nagged" "$out" "deprecated"

: > "$CONF"
rm -f .claudio.local

# ============================================================
printf "\n\033[1m=== default ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -f .claudio.local

out=$(run default 2>&1 || true)
assert_contains "default with no options shows usage" "$out" "Usage: claudio default"

out=$(run default --account work)
assert_contains "default reports the saved account" "$out" "Default account for this project: work"
assert_file_exists "default writes .claudio.local" ".claudio.local"
assert_contains "default records account=" "$(cat .claudio.local)" "account=work"

# The saved default is what env resolves to
out=$(run env test-profile)
assert_eq "default account is used by env" "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/work"

# Re-running replaces the account line rather than duplicating it
run default --account personal > /dev/null
assert_eq "default replaces account= (count)" "$(grep -c '^account=' .claudio.local | tr -d ' ')" "1"
assert_contains "default replaces account= (value)" "$(cat .claudio.local)" "account=personal"
assert_not_contains "old default account is gone" "$(cat .claudio.local)" "account=work"

# Tags are appended and preserved across an account rewrite
run default --tag team=soc > /dev/null
assert_contains "default records tag=" "$(cat .claudio.local)" "tag=team=soc"
run default --account work > /dev/null
assert_contains "default preserves tags when rewriting account" "$(cat .claudio.local)" "tag=team=soc"
assert_eq "default still holds one account line" "$(grep -c '^account=' .claudio.local | tr -d ' ')" "1"

out=$(run env test-profile)
assert_eq "default tags and account both resolve" "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "profile=test-profile,account=work,host=testhost,team=soc"

# Unknown account is rejected before anything is written
out=$(run default --account nope 2>&1 || true)
assert_contains "default rejects unknown account" "$out" "Account not found: nope"
assert_not_contains "default did not write the bad account" "$(cat .claudio.local)" "account=nope"

# .gitignore handling: only in a git repo that already has one
rm -f .claudio.local
mkdir -p .git
: > .gitignore
run default --account work > /dev/null
assert_contains "default appends .claudio.local to .gitignore" "$(cat .gitignore)" "^\.claudio\.local$"
run default --account personal > /dev/null
assert_eq "default does not duplicate the .gitignore entry" "$(grep -c '^\.claudio\.local$' .gitignore | tr -d ' ')" "1"
rm -rf .git .gitignore .claudio.local

# ============================================================
printf "\n\033[1m=== run ===\033[0m\n"
# ============================================================

cd "$WORKDIR"
rm -f .claudio.local

# With an explicit profile: activates and reports, then launches claude
run clean > /dev/null 2>&1 || true
out=$(run run test-profile)
assert_contains "run activates and reports the profile" "$out" "profile: test-profile"
assert_contains "run launches claude" "$out" "\[claude\]"
assert_is_symlink "run activates the profile in cwd" ".mcp.json"
marker=$(cat .claudio)
assert_eq "run records the active profile" "$marker" "test-profile"

# No profile argument: falls back to the active profile
out=$(run run)
assert_contains "run falls back to the active profile" "$out" "profile: test-profile"

# Options are parsed even with no profile argument
out=$(run run --account work)
assert_contains "run without profile still parses --account" "$out" "profile: test-profile"
assert_contains "run without profile reports the account" "$out" "account: work"

out=$(run run test-profile --account personal)
assert_contains "run reports the chosen account" "$out" "account: personal"

# The merged tag set is the feature's own output — run must show it landed.
out=$(run run test-profile --tag client=acme)
assert_contains "run reports the merged tags" "$out" "tags:"
assert_contains "run confirms an ad-hoc tag landed" "$out" "client=acme"

rc=0
out=$(run run test-profile --account nope 2>&1) || rc=$?
assert_contains "run rejects unknown account" "$out" "Account not found: nope"
# The invariant behind _run_prelude: the account is validated in the CALLER's
# shell, because _build_env runs inside $( ) where an `exit` is swallowed.
# Asserting the message alone would still pass if claude then launched anyway.
assert_not_contains "run stops before launching claude" "$out" "\[claude\]"
assert_exit_code "run exits non-zero on an unknown account" "$rc" "1"

out=$(run run test-profile --tag bad 2>&1 || true)
assert_contains "run rejects an invalid tag" "$out" "Tag must be key=value"

# Profile + options + `--` in one line: RUN_SHIFT must count the profile too,
# or the shift leaves it in "$@" (or overshoots, which aborts dash outright).
out=$(run run test-profile --account work --tag a=b -- --model opus)
assert_contains "run accepts profile, options and -- together" "$out" "profile: test-profile"
# Escaped: an unescaped [claude] is a BRACKET EXPRESSION matching any one of
# c/l/a/u/d/e, so it matched "profile: test-profile" and this assertion was
# vacuous. It is now an exact argv comparison, which cannot be vacuous at all.
assert_contains "run still launches claude after --" "$out" "\[claude\]"
assert_eq "run passes only the post-- arguments to claude" "$(argv "$out")" "<--model> <opus>"

# Switching profile via run re-activates
out=$(run run second-profile)
assert_contains "run switches profile" "$out" "profile: second-profile"
assert_eq "run updates the marker" "$(cat .claudio)" "second-profile"
assert_contains "run relinks to the new profile" "$(readlink .mcp.json)" "second-profile"

out=$(run run nonexistent 2>&1 || true)
assert_contains "run rejects an unknown profile" "$out" "not found"

run clean > /dev/null

# Ad-hoc directories: no profile at all. run must still switch account and tags
# without linking any project config into the directory.
mkdir -p "$TMP/bare"

out=$(cd "$TMP/bare" && run run 2>&1 || true)
assert_contains "run with nothing configured still launches" "$out" "\[claude\]"
assert_contains "run with nothing configured warns about the profile" "$out" "no profile active"
assert_contains "run with nothing configured warns about the account" "$out" "no account configured"

out=$(cd "$TMP/bare" && run run --account work 2>&1 || true)
assert_contains "run without a profile reports the account" "$out" "account: work"
assert_eq "run without a profile sets CLAUDE_CONFIG_DIR" \
  "$(cd "$TMP/bare" && run env --account work | sed -n "s/^export CLAUDE_CONFIG_DIR=//p")" \
  "$ACCOUNTS/work"

out=$(cd "$TMP/bare" && run env --account work --tag client=acme)
assert_eq "run without a profile omits the profile tag but keeps account and tags" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "account=work,host=testhost,client=acme"

# The whole point: an ad-hoc directory must stay untouched.
assert_file_not_exists "ad-hoc run creates no .claudio marker" "$TMP/bare/.claudio"
assert_file_not_exists "ad-hoc run creates no .mcp.json" "$TMP/bare/.mcp.json"
assert_file_not_exists "ad-hoc run creates no .claude dir" "$TMP/bare/.claude"

# An unconfigured directory must say so rather than silently using whichever
# login happens to be default.
out=$(cd "$TMP/bare" && run run 2>&1 || true)
assert_contains "run warns when no profile is active" "$out" "no profile active in this directory"
assert_contains "run warns when no account is configured" "$out" "no account configured"
assert_contains "run says how to fix the missing account" "$out" "claudio account default"

out=$(cd "$TMP/bare" && run run --account work 2>&1 || true)
assert_contains "run reports the account source" "$out" "account: work (from flag)"

cd "$WORKDIR"

# ============================================================
printf "\n\033[1m=== argv passthrough ===\033[0m\n"
# ============================================================
#
# Byte-for-byte passthrough to claude is why _parse_run_opts returns RUN_SHIFT,
# a COUNT, instead of flattening the remaining arguments into a string:
# flattening lost empty arguments, split on embedded newlines and glob-expanded
# on re-split. Until the launch shim rendered argv, none of that was observable
# and the whole property was guarded by nothing.

out=$(run run test-profile -- -p hi)
assert_eq "-- forwards arguments to claude" "$(argv "$out")" "<-p> <hi>"
assert_not_contains "-- is consumed, not forwarded" "$(argv "$out")" '<-->'

# A launch with nothing after -- must hand claude no arguments at all, rather
# than an empty one.
out=$(run run test-profile)
assert_eq "a plain run passes claude no arguments" "$(argv "$out")" ""
out=$(run run test-profile --)
assert_eq "a bare -- passes claude no arguments" "$(argv "$out")" ""

# The three claudio options are consumed; everything after -- is claude's, even
# when it repeats a claudio option name.
out=$(run run test-profile --account work --tag a=b --no-telemetry -- --account nope --tag x)
assert_eq "claudio options are not forwarded, claude's lookalikes are" \
  "$(argv "$out")" "<--account> <nope> <--tag> <x>"

# Empty arguments survive. `echo "$@"` could not show this at all: an empty
# argument is indistinguishable from an extra space once the list is joined.
out=$(run run test-profile -- -p '' after)
assert_eq "an empty argument survives" "$(argv "$out")" "<-p> <> <after>"

# An argument holding a newline stays ONE argument. This is the case that dies
# first if RUN_SHIFT is ever replaced by a flattened string.
out=$(run run test-profile -- "$(printf 'a\nb')" tail)
assert_eq "an embedded newline stays inside one argument" \
  "$(argv "$out")" "$(printf '<a\nb> <tail>')"

# A literal backslash-n must NOT become a newline. This is the assertion that
# fails under exactly one shell if the shim ever goes back to echo, because
# dash's echo expands backslash escapes inside its arguments and bash's does not.
out=$(run run test-profile -- 'a\nb')
assert_eq "a literal backslash escape is not expanded" "$(argv "$out")" '<a\nb>'

# Glob characters must reach claude unexpanded, and a quoted argument holding
# spaces must stay one argument.
out=$(run run test-profile -- '*.md' 'two words')
assert_eq "a glob is not expanded and spaces do not split" \
  "$(argv "$out")" "<*.md> <two words>"

# Same guarantee with no profile named: _run_prelude adds the profile to
# RUN_SHIFT, so the count differs by one between these two shapes.
out=$(cd "$TMP/bare" && run run -- -p hi)
assert_eq "-- forwards arguments with no profile named" "$(argv "$out")" "<-p> <hi>"

# The login path is a launch too, and its argv must be the /login it claims.
# The old stub commented this out and printed the account name in its place.
out=$(run account login work)
assert_eq "account login launches claude /login" "$(argv "$out")" "</login>"

cd "$WORKDIR"

# ============================================================
printf "\n\033[1m=== machine-wide default account ===\033[0m\n"
# ============================================================

out=$(run account default 2>&1 || true)
assert_contains "account default errors when unset" "$out" "No machine default account set"

out=$(run account default personal)
assert_contains "account default sets the machine default" "$out" "Machine default account: personal"
assert_eq "account default reports it back" "$(run account default)" "personal"
assert_contains "account default is stored in the global conf" "$(cat "$CONF")" "account=personal"

out=$(run account list)
assert_contains "account list marks the machine default" "$out" "* personal"

# It is the LAST layer: used only when nothing more specific applies.
out=$(cd "$TMP/bare" && run env 2>&1)
assert_eq "machine default applies in an ad-hoc dir" \
  "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/personal"
assert_eq "machine default tags the account" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "account=personal,host=testhost,email=a@example.com"

# email= is the identity behind account=, which is only a label. It travels
# with account=: absent when no account resolves, so the default login is never
# described by claudio and the suite never reads the developer's real $HOME.
out=$(cd "$TMP/bare" && run env --account personal 2>&1)
assert_contains "email tag accompanies a resolved account" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "email=a@example.com"
assert_contains "an account with no stored login gets no email tag" \
  "$(envval "$(cd "$TMP/bare" && run env --account work 2>&1)" OTEL_RESOURCE_ATTRIBUTES)" "account=work"
assert_not_contains "...and specifically no empty email=" \
  "$(envval "$(cd "$TMP/bare" && run env --account work 2>&1)" OTEL_RESOURCE_ATTRIBUTES)" "email="
# With the machine default removed, nothing resolves an account, so claudio has
# no account to describe — and must not fall back to the real $HOME to find one.
# `|| true`: grep exits 1 when it filters out every line, and the suite runs
# under `set -e`, so an all-account= conf would abort the whole run here.
cp "$CONF" "$CONF.keep"; grep -v '^account=' "$CONF.keep" > "$CONF" || true
assert_not_contains "no email tag when no account resolves" \
  "$(envval "$(cd "$TMP/bare" && run env --tag x=1 2>&1)" OTEL_RESOURCE_ATTRIBUTES)" "email="
mv "$CONF.keep" "$CONF"
out=$(cd "$TMP/bare" && run env --tag email=redacted 2>&1)
assert_contains "an explicit email tag overrides the automatic one" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "email=redacted"
assert_not_contains "the overridden address is gone, not duplicated" \
  "$(envval "$out" OTEL_RESOURCE_ATTRIBUTES)" "a@example.com"

out=$(cd "$TMP/bare" && run env --account work 2>&1)
assert_eq "--account beats the machine default" \
  "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/work"

out=$(cd "$TMP/bare" && CLAUDIO_ACCOUNT=work run env 2>&1)
assert_eq "CLAUDIO_ACCOUNT beats the machine default" \
  "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/work"

mkdir -p "$TMP/bare2"
printf 'account=work\n' > "$TMP/bare2/.claudio.local"
out=$(cd "$TMP/bare2" && run env 2>&1)
assert_eq ".claudio.local beats the machine default" \
  "$(envval "$out" CLAUDE_CONFIG_DIR)" "$ACCOUNTS/work"

out=$(run account default nosuchaccount 2>&1 || true)
assert_contains "account default rejects an unknown account" "$out" "Account not found"
out=$(run account default ../escape 2>&1 || true)
assert_contains "account default rejects a traversing name" "$out" "Invalid account name"

# Reset so later tests are unaffected by a machine default.
rm -f "$CONF"

cd "$WORKDIR"

# ============================================================
printf "\n\033[1m=== statusline: wiring accounts ===\033[0m\n"
# ============================================================
# What gets registered with Claude Code is claudio's shim, and it is registered
# against ACCOUNTS, not profiles. An account IS a CLAUDE_CONFIG_DIR, so its
# settings.json is the USER-level file for every session run under it, which is
# where recording has to live: the 5-hour and 7-day quota is account-wide.
#
# A profile must never write one — its settings.json is symlinked to
# .claude/settings.json, which is PROJECT level and outranks the account's, so a
# profile-written status line would shadow the shim in exactly the projects you
# work in most. These tests used to pin the profile behaviour; what they were
# really protecting is "did claudio look before it wrote?", and that still
# matters, one level up.
#
# Every source file lives in $TMP: the developer's real ~/.claude/settings.json
# usually DOES have a statusLine, and reading it would make these take a
# different branch on their machine than on CI.

SLPROJ="$TMP/sl-project"
rm -rf "$SLPROJ"; mkdir -p "$SLPROJ"
rm -f "$USER_SETTINGS" "$MANAGED_SETTINGS"
: > "$CONF"
SLACCT="$TMP/sl-accounts"
rm -rf "$SLACCT"; mkdir -p "$SLACCT"

# A stand-in renderer. It consumes the JSON payload Claude Code feeds a status
# line on stdin and prints the context only claudio's environment can supply,
# so the preview tests can prove all three arrived.
SLBAR="$TMP/sl-bar.sh"
cat > "$SLBAR" <<'STUB'
#!/bin/sh
_in=$(cat)
case "$_in" in *hook_event_name*) _json=yes ;; *) _json=no ;; esac
printf 'bar cfg=%s tags=%s json=%s\n' \
  "${CLAUDE_CONFIG_DIR:-none}" "${OTEL_RESOURCE_ATTRIBUTES:-none}" "$_json"
STUB
chmod +x "$SLBAR"

# --- profiles are out of it entirely, configured or not ---
out=$(cd "$SLPROJ" && run new sl-none --manual)
assert_not_contains "new says nothing about a status line" "$out" "Status line"
assert_not_contains "new writes no statusLine into a profile" \
  "$(cat "$PROFILES/sl-none/settings.json")" "statusLine"
# Pins the blank _blank_settings_json generates: the writer and the blank come
# from one function, and this is what stops them drifting apart.
assert_eq "the blank settings.json is byte-for-byte what it always was" \
  "$(cat "$PROFILES/sl-none/settings.json")" '{
  "permissions": {
    "allow": [],
    "deny": []
  }
}'
printf 'statusline=sh %s\n' "$SLBAR" > "$CONF"
out=$(cd "$SLPROJ" && run new sl-fresh --manual)
assert_contains "new still reports the profile it created" "$out" "Created profile 'sl-fresh'"
assert_not_contains "a configured status line still does not reach a profile" \
  "$(cat "$PROFILES/sl-fresh/settings.json")" "statusLine"
assert_not_contains "...and new does not mention one" "$out" "Status line"

# --- account new is born wired ---
out=$(run_sl account new born)
assert_contains "account new still creates and logs in" "$out" "Created account 'born'"
sl_json=$(cat "$SLACCT/born/settings.json")
assert_contains "account new writes a statusLine block" "$sl_json" '"statusLine"'
assert_contains "the block is a command status line" "$sl_json" '"type": "command"'
assert_contains "the block runs claudio's shim, not the renderer" "$sl_json" "statusline --shim"
assert_not_contains "...so the renderer is NOT what Claude Code calls" "$sl_json" "sl-bar.sh"
assert_contains "the shim is named by absolute path" "$sl_json" '"command": "/'
assert_contains "the account settings write keeps permissions" "$sl_json" "permissions"
assert_contains "account new reports what it wired in" "$out" "Status line:"
if command -v jq > /dev/null 2>&1; then
  assert_contains "what account new wrote is valid JSON with the command in place" \
    "$(jq -r '.statusLine.command' "$SLACCT/born/settings.json")" "statusline --shim"
  assert_eq "...and permissions survived as an object, not a string" \
    "$(jq -r '.permissions.allow | length' "$SLACCT/born/settings.json")" "0"
else
  printf "  (skipped — jq not installed)\n"
fi

# Wired regardless of whether anything is configured yet: turning recording or a
# renderer on later must be a conf edit, not a re-setup of every account.
: > "$CONF"
out=$(run_sl account new bare)
assert_contains "an account is wired even with nothing configured" \
  "$(cat "$SLACCT/bare/settings.json")" "statusline --shim"
printf 'statusline=sh %s\n' "$SLBAR" > "$CONF"

# --- ...unless something already provides one: report, never overwrite ---
printf '{"statusLine":{"type":"command","command":"acct"}}\n' > "$SLACCT/taken-settings.json"
mkdir -p "$SLACCT/pre"
cp "$SLACCT/taken-settings.json" "$SLACCT/pre/settings.json"
out=$(run_sl account new pre 2>&1 || true)
assert_contains "account new refuses an account that already exists" "$out" "already exists"
rm -rf "$SLACCT/pre"

# An account directory that already has a status line: `--set` is the path that
# can hit it, and it must decline rather than replace.
mkdir -p "$SLACCT/hasone"
cp "$SLACCT/taken-settings.json" "$SLACCT/hasone/settings.json"
sl_rc=0
out=$(run_sl statusline --set hasone) || sl_rc=$?
assert_contains "--set leaves an account's own status line alone" "$out" "left alone"
assert_exit_code "...and says so with a non-zero exit" "$sl_rc" 1
assert_eq "the file it declined to touch is unchanged" \
  "$(cat "$SLACCT/hasone/settings.json")" '{"statusLine":{"type":"command","command":"acct"}}'

# Enterprise managed settings outrank every settings file, so wiring an account
# would write a file that never runs. Say so rather than claim success.
printf '{"statusLine":{"type":"command","command":"policy"}}\n' > "$MANAGED_SETTINGS"
out=$(run_sl account new undermanaged)
assert_contains "managed settings count as a status line already in effect" \
  "$out" "You already have a status line configured"
assert_contains "...naming the policy file" "$out" "$MANAGED_SETTINGS"
assert_contains "...saying why it declined" "$out" "never overwrites a status line it did not set"
assert_contains "...and that an account's settings.json is user level" "$out" "USER-level"
assert_contains "...offering the preview it promises" "$out" "claudio statusline --preview"
assert_contains "...offering the way to switch" "$out" "claudio statusline --set undermanaged"
assert_file_not_exists "nothing was written into the new account" \
  "$SLACCT/undermanaged/settings.json"
rm -f "$MANAGED_SETTINGS"
rm -rf "$SLACCT/undermanaged"

# The three sources an ACCOUNT check must NOT consult. Each of these was a live
# bug in the profile-shaped version of this code:
#   $USER_SETTINGS — ~/.claude/settings.json is not read at all when
#   CLAUDE_CONFIG_DIR is set, so consulting it would decline on most machines.
printf '{"statusLine":{"type":"command","command":"mine"}}\n' > "$USER_SETTINGS"
out=$(run_sl account new despite-user)
assert_contains "the user's own ~/.claude settings do not block an account" \
  "$(cat "$SLACCT/despite-user/settings.json")" "statusline --shim"
assert_eq "...and that file was read at most, never written" \
  "$(cat "$USER_SETTINGS")" '{"statusLine":{"type":"command","command":"mine"}}'
rm -f "$USER_SETTINGS"
#   another account's file — otherwise the first wired account stops every
#   later one from ever being wired.
out=$(run_sl account new despite-sibling)
assert_contains "an already-wired sibling account does not block a new one" \
  "$(cat "$SLACCT/despite-sibling/settings.json")" "statusline --shim"
#   the project files in cwd — an account is used in every directory.
mkdir -p "$SLPROJ/.claude"
printf '{"statusLine":{"type":"command","command":"proj"}}\n' > "$SLPROJ/.claude/settings.json"
out=$(cd "$SLPROJ" && run_sl account new despite-project)
assert_contains "the project file you happen to be standing in does not block it" \
  "$(cat "$SLACCT/despite-project/settings.json")" "statusline --shim"
rm -rf "$SLPROJ/.claude"

# --- statusline --set: one account ---
rm -rf "$SLACCT"; mkdir -p "$SLACCT/one" "$SLACCT/two"
out=$(run_sl statusline --set one)
assert_contains "--set wires the named account" "$out" "one                  wired"
assert_contains "--set names the command it registered" "$out" "statusline --shim"
assert_contains "--set names the renderer it forwards to" "$out" "$SLBAR"
assert_contains "--set says whether recording is on" "$out" "Recording is off"
assert_contains "the block landed in the account" \
  "$(cat "$SLACCT/one/settings.json")" '"statusLine"'
assert_file_not_exists "--set touched no other account" "$SLACCT/two/settings.json"
out=$(run_sl statusline --set one)
assert_contains "--set on an account that already has the shim is a no-op" \
  "$out" "already wired"

# With recording on it says so, because that is the question --set leaves you
# asking and the answer lives in a different key.
printf 'statusline=sh %s\nusage=1\n' "$SLBAR" > "$CONF"
out=$(run_sl statusline --set two)
assert_contains "--set reports recording on when usage=1" "$out" "Recording is on"

# ...and the per-account inversion is part of that answer. account.<name>.usage
# is advertised as a guarantee, so reporting the plain layers only would tell
# the one user who deliberately switched an identity off that it is on.
printf 'statusline=sh %s\nusage=1\naccount.two.usage=0\n' "$SLBAR" > "$CONF"
rm -f "$SLACCT/two/settings.json"
out=$(run_sl statusline --set two)
assert_contains "--set honours account.<name>.usage=0 in what it reports" \
  "$out" "Recording is off"

# The same two answers under the key that replaces it, so the report cannot go
# on reading a retired key while the recorder reads the new one.
printf 'statusline=sh %s\nlogging=local\n' "$SLBAR" > "$CONF"
rm -f "$SLACCT/two/settings.json"
out=$(run_sl statusline --set two)
assert_contains "--set reports recording on when logging=local" "$out" "Recording is on"
printf 'statusline=sh %s\nlogging=local\naccount.two.logging=none\n' "$SLBAR" > "$CONF"
rm -f "$SLACCT/two/settings.json"
out=$(run_sl statusline --set two)
assert_contains "--set honours account.<name>.logging=none in what it reports" \
  "$out" "Recording is off"
# ...and the retirement of the old key is said where the answer is given.
printf 'statusline=sh %s\nlogging=local\nusage=1\n' "$SLBAR" > "$CONF"
rm -f "$SLACCT/two/settings.json"
out=$(run_sl statusline --set two)
assert_contains "--set names a deprecated key it is no longer reading" \
  "$out" "so the deprecated usage= is ignored"

# The REMEDY, not just the verdict. `account.<name>.logging` is read from the
# global conf only and beats the plain global key — that inversion is the whole
# guarantee the key is sold on — so "Turn it on with: logging=local in <conf>"
# was printed at a user who already had exactly that line in exactly that file.
printf 'statusline=sh %s\nlogging=local\naccount.two.logging=none\n' "$SLBAR" > "$CONF"
rm -f "$SLACCT/two/settings.json"
out=$(run_sl statusline --set two)
assert_contains "--set names the layer that switched recording off" \
  "$out" "account.two.logging=none in $CONF"
assert_not_contains "...instead of advice the user has already followed" \
  "$out" "Turn it on with: logging=local"
# With nothing named anywhere, the generic remedy is still the right one.
printf 'statusline=sh %s\n' "$SLBAR" > "$CONF"
rm -f "$SLACCT/two/settings.json"
out=$(run_sl statusline --set two)
assert_contains "--set falls back to the generic remedy when no layer decided" \
  "$out" "Turn it on with: logging=local in $CONF"

printf 'statusline=sh %s\n' "$SLBAR" > "$CONF"

# No renderer is a legitimate silent-sensor setup, so --set wires it and warns
# rather than refusing.
: > "$CONF"
mkdir -p "$SLACCT/silent"
out=$(run_sl statusline --set silent)
assert_contains "--set works with no renderer configured" \
  "$(cat "$SLACCT/silent/settings.json")" "statusline --shim"
assert_contains "...and warns that nothing will be drawn" "$out" "No renderer yet"
printf 'statusline=sh %s\n' "$SLBAR" > "$CONF"

# A settings.json claudio did not write is someone's work. Regenerating the
# document without jq would drop their keys, so it refuses — usefully.
mkdir -p "$SLACCT/handmade"
printf '{\n  "permissions": {\n    "allow": ["Bash"]\n  }\n}\n' > "$SLACCT/handmade/settings.json"
sl_rc=0
out=$(run_sl statusline --set handmade) || sl_rc=$?
assert_contains "--set refuses to rewrite JSON claudio does not own" "$out" "claudio did not write"
assert_contains "...handing back the exact block to paste" "$out" '"statusLine"'
assert_contains "...with the command already filled in" "$out" "statusline --shim"
assert_contains "...naming the file" "$out" "$SLACCT/handmade/settings.json"
assert_contains "the hand-edited file is untouched" \
  "$(cat "$SLACCT/handmade/settings.json")" "Bash"
assert_not_contains "...and gained no statusLine" \
  "$(cat "$SLACCT/handmade/settings.json")" "statusLine"
assert_exit_code "--set exits non-zero when it refuses" "$sl_rc" 1

# The retrofit case that actually exists on a machine that has used claudio
# before: an account whose settings.json claudio wrote itself, calling the
# renderer directly because the shim did not exist yet. Refusing that would
# leave --all useless for exactly the accounts it was built for, so it is
# upgraded — the shim forwards to the same renderer, so the line is identical
# and recording starts working.
mkdir -p "$SLACCT/pre-shim"
run_sl statusline --set pre-shim > /dev/null
# Rewrite it into the shape the previous release wrote: the blank plus a
# statusLine pointing straight at the renderer.
python_free_prev=$(printf '{\n  "permissions": {\n    "allow": [],\n    "deny": []\n  },\n  "statusLine": {\n    "type": "command",\n    "command": "sh %s"\n  }\n}\n' "$SLBAR")
printf '%s' "$python_free_prev" > "$SLACCT/pre-shim/settings.json"
sl_rc=0
out=$(run_sl statusline --set pre-shim) || sl_rc=$?
assert_exit_code "a status line claudio wrote before the shim is upgraded, not refused" "$sl_rc" 0
assert_contains "...and it says so rather than claiming a fresh write" "$out" "upgraded"
assert_contains "the shim replaced the direct call" \
  "$(cat "$SLACCT/pre-shim/settings.json")" "statusline --shim"
assert_not_contains "...and the direct call is gone" \
  "$(cat "$SLACCT/pre-shim/settings.json")" "sl-bar.sh"
# One byte different and it is somebody's edit again, not claudio's document.
printf '%s' "$python_free_prev" | sed 's/"deny": \[\]/"deny": ["Bash"]/' \
  > "$SLACCT/pre-shim/settings.json"
sl_rc=0
out=$(run_sl statusline --set pre-shim) || sl_rc=$?
assert_exit_code "an edited one is refused again" "$sl_rc" 1
assert_contains "...naming it as someone's own line" "$out" "left alone"
assert_contains "the edit survived untouched" \
  "$(cat "$SLACCT/pre-shim/settings.json")" "Bash"
rm -rf "$SLACCT/pre-shim"

out=$(run statusline --set nosuchaccount 2>&1 || true)
assert_contains "--set validates the account name" "$out" "Account not found: nosuchaccount"
out=$(run statusline --set 2>&1 || true)
assert_contains "--set requires an account" "$out" "account name required"
out=$(run statusline --set ../evil 2>&1 || true)
assert_contains "--set refuses a name that would escape the accounts dir" \
  "$out" "Invalid account name"

# --- statusline --all: the retrofit ---
rm -rf "$SLACCT"; mkdir -p "$SLACCT/a1" "$SLACCT/a2" "$SLACCT/a3"
printf '{"statusLine":{"type":"command","command":"theirs"}}\n' > "$SLACCT/a2/settings.json"
sl_rc=0
out=$(run_sl statusline --all) || sl_rc=$?
assert_contains "--all wires the first account" "$out" "a1                   wired"
assert_contains "--all leaves the one that is spoken for" "$out" "a2 .*left alone"
assert_contains "--all carries on to the last account" "$out" "a3                   wired"
assert_exit_code "--all exits non-zero when it could not wire them all" "$sl_rc" 1
assert_contains "a1 got the shim" "$(cat "$SLACCT/a1/settings.json")" "statusline --shim"
assert_contains "a3 got the shim" "$(cat "$SLACCT/a3/settings.json")" "statusline --shim"
assert_eq "a2 is byte-for-byte what it was" \
  "$(cat "$SLACCT/a2/settings.json")" '{"statusLine":{"type":"command","command":"theirs"}}'
sl_rc=0
out=$(run_sl statusline --all) || sl_rc=$?
assert_contains "--all is idempotent on the ones it did" "$out" "a1                   already wired"
mkdir -p "$TMP/no-accounts-at-all"
SLACCT_KEEP=$SLACCT; SLACCT="$TMP/no-accounts-at-all"
out=$(run_sl statusline --all 2>&1 || true)
SLACCT=$SLACCT_KEEP
assert_contains "--all says so when there are no accounts" "$out" "No accounts"

# --- account list makes the drift visible ---
out=$(run_sl account list)
assert_contains "account list marks a wired account" "$out" "a1 .*shim"
assert_contains "account list marks one running someone else's line" "$out" "a2 .*other"
rm -f "$SLACCT/a3/settings.json"
out=$(run_sl account list)
assert_contains "account list marks an unwired account" "$out" "a3 .*no shim"
assert_contains "...and says how to fix the drift" "$out" "claudio statusline --all"
assert_contains "account list still shows the login state" "$out" "not logged in"
out=$(run account list)
assert_contains "the email column is still last, after the shim column" "$out" "personal .*a@example.com"

# --- statusline --preview: the promise the report makes must be real ---
rm -f "$USER_SETTINGS"
out=$(cd "$SLPROJ" && run statusline --preview)
assert_contains "--preview names the command it is about to run" "$out" "Previewing: sh $SLBAR"
assert_contains "--preview renders the line" "$out" "bar cfg="
assert_contains "--preview feeds it the JSON payload Claude Code would" "$out" "json=yes"

printf 'account=work\n' > "$SLPROJ/.claudio.local"
out=$(cd "$SLPROJ" && run statusline --preview)
assert_contains "--preview resolves the account the same way run does" "$out" "account: work"
assert_contains "--preview exports CLAUDE_CONFIG_DIR to the command" "$out" "cfg=$ACCOUNTS/work"
assert_contains "--preview exports the tags too" "$out" "tags=account=work"
rm -f "$SLPROJ/.claudio.local"

# A renderer that does not run is the commonest way to get this wrong, and the
# preview exists precisely to show you that before Claude Code does.
printf 'statusline=%s/sl-not-here.sh\n' "$TMP" > "$CONF"
sl_rc=0
out=$(cd "$SLPROJ" && run statusline --preview 2>&1) || sl_rc=$?
assert_exit_code "--preview fails when the command does" "$sl_rc" 1
assert_contains "--preview says the command failed" "$out" "exited"
assert_contains "...and where to change it" "$out" "$CONF"

# --- the report, and the unconfigured case ---
printf 'statusline=sh %s\n' "$SLBAR" > "$CONF"
out=$(cd "$SLPROJ" && run_sl statusline)
assert_contains "statusline reports what is configured" "$out" "Configured (statusline="
assert_contains "...with the command" "$out" "sh $SLBAR"
assert_contains "...and whether recording is on" "$out" "Recording plan usage: off"
assert_contains "...listing every account and whether it is wired" "$out" "a1 *wired"
assert_contains "...including the one that is not" "$out" "a3 *not wired"
assert_contains "...and how to fix that" "$out" "claudio statusline --all"
assert_contains "...and that nothing is in effect here yet" \
  "$out" "No status line is in effect in this directory"
printf 'statusline=sh %s\nusage=1\n' "$SLBAR" > "$CONF"
out=$(cd "$SLPROJ" && run_sl statusline)
assert_contains "statusline reports recording on" "$out" "Recording plan usage: on"
# The report is about what is in effect HERE, so it must resolve the account
# this directory would run under and apply account.<name>.usage — otherwise it
# contradicts the guarantee the key is sold on.
printf 'statusline=sh %s\nusage=1\naccount=a1\naccount.a1.usage=0\n' "$SLBAR" > "$CONF"
out=$(cd "$SLPROJ" && run_sl statusline)
assert_contains "statusline honours the resolved account's usage key" \
  "$out" "Recording plan usage: off"
# The same pair under logging=, and the report names the mode it read — "on"
# with no source is what let the report and the recorder drift apart before.
printf 'statusline=sh %s\nlogging=remote\n' "$SLBAR" > "$CONF"
out=$(cd "$SLPROJ" && run_sl statusline)
assert_contains "statusline reports recording on under logging=remote" \
  "$out" "Recording plan usage: on (logging=remote)"
printf 'statusline=sh %s\nlogging=local\naccount=a1\naccount.a1.logging=none\n' "$SLBAR" > "$CONF"
out=$(cd "$SLPROJ" && run_sl statusline)
assert_contains "statusline honours the resolved account's logging key" \
  "$out" "Recording plan usage: off"
# ...and says WHICH line decided. "off" with no reason is the same failure as
# the wrong reason: the user edits the plain key, re-runs, and gets the
# identical message with no way to reach the layer that actually decided.
assert_contains "...naming the layer that decided it" \
  "$out" "account.a1.logging=none in $CONF"

# The deprecation notice on the BARE report. The identical line in
# _statusline_wire_footer was pinned and this one was not, so deleting it left
# the suite fully green — and this is the command a user actually runs to ask
# what is in effect. Every other logging= test here sets the key alone, so no
# assertion ever produced a non-empty note on this path.
printf 'statusline=sh %s\nlogging=local\notel=1\n' "$SLBAR" > "$CONF"
out=$(cd "$SLPROJ" && run_sl statusline)
assert_contains "the status report names a deprecated key it is no longer reading" \
  "$out" "so the deprecated otel= is ignored"
printf 'statusline=sh %s\notel=1\n' "$SLBAR" > "$CONF"
out=$(cd "$SLPROJ" && run_sl statusline)
assert_contains "...and names one it IS still reading" "$out" "otel= is deprecated"

# Recording that cannot possibly happen must not report as on. The shim blanks
# its usage dir when the resolved usage_dir is inside ~/.claude and returns
# without a word, by design — so the report answered from the mode alone said
# "on (logging=local)" while nothing was ever written, for ever.
printf 'statusline=sh %s\nlogging=local\nusage_dir=%s/.claude/usage\n' \
  "$SLBAR" "$FAKEHOME" > "$CONF"
out=$(cd "$SLPROJ" && HOME="$FAKEHOME" run_sl statusline)
assert_contains "recording into ~/.claude is reported OFF, not on" \
  "$out" "Recording plan usage: off"
assert_contains "...with the refusal named" "$out" "inside ~/.claude"
assert_not_contains "...and never as on" "$out" "Recording plan usage: on"
printf 'statusline=sh %s\nlogging=local\nusage_dir=%s/elsewhere\n' \
  "$SLBAR" "$TMP" > "$CONF"
out=$(cd "$SLPROJ" && HOME="$FAKEHOME" run_sl statusline)
assert_contains "a usable usage_dir still reports on" \
  "$out" "Recording plan usage: on (logging=local)"
printf 'statusline=sh %s\n' "$SLBAR" > "$CONF"

printf '{"statusLine":{"type":"command","command":"mine"}}\n' > "$USER_SETTINGS"
out=$(cd "$SLPROJ" && run statusline)
assert_contains "statusline reports what is already in effect here" "$out" "In effect in this directory"
assert_contains "...naming the file" "$out" "$USER_SETTINGS"
rm -f "$USER_SETTINGS"

# A PROJECT-level file is the one way a wired account silently stops recording,
# so the report names it rather than reporting a hit and moving on.
mkdir -p "$SLPROJ/.claude"
printf '{"statusLine":{"type":"command","command":"proj"}}\n' > "$SLPROJ/.claude/settings.json"
out=$(cd "$SLPROJ" && run statusline)
assert_contains "statusline warns when a project file shadows the account" \
  "$out" "PROJECT-level file"
assert_contains "...spelling out the consequence" "$out" "records nothing"
rm -rf "$SLPROJ/.claude"

out=$(cd "$SLPROJ" && run statusline --bogus 2>&1 || true)
assert_contains "statusline rejects an unknown option" "$out" "Unknown option: --bogus"
assert_contains "...and says what it does accept" \
  "$out" "claudio statusline \[--preview | --set <account> | --all\]"

: > "$CONF"
sl_rc=0
out=$(run statusline 2>&1) || sl_rc=$?
assert_exit_code "statusline exits non-zero when nothing at all is set up" "$sl_rc" 1
assert_contains "statusline says how to configure itself" "$out" "No status line configured"
assert_contains "...spelling out the line to add" "$out" "statusline=claude-statusline"
assert_contains "...and where to add it" "$out" "$CONF"
assert_contains "...and how to wire it in" "$out" "claudio statusline --all"
out=$(run statusline --preview 2>&1 || true)
assert_contains "--preview says the same thing" "$out" "No status line configured"
# ...but recording alone is a complete setup, so the report must not call it
# unconfigured just because nothing is drawn.
printf 'usage=1\n' > "$CONF"
sl_rc=0
out=$(cd "$SLPROJ" && run_sl statusline) || sl_rc=$?
assert_exit_code "statusline is happy with recording and no renderer" "$sl_rc" 0
assert_contains "...saying there is no renderer" "$out" "No renderer configured"
assert_contains "...and that recording is on anyway" "$out" "Recording plan usage: on"

# --- hermeticity ---
# A static invariant, not a snapshot: claudio has no write path into Claude
# Code's own directory today, and the point is that it never grows one. The
# `|| true` matters — a grep that filters everything out aborts the suite.
sl_writes=$(grep -n 'USER_SETTINGS\|MANAGED_SETTINGS' "$CLAUDIO" \
  | grep -E '>[[:space:]]*"?\$(USER|MANAGED)_SETTINGS|(mv|cp|rm|touch|tee|sed -i)[^|]*\$(USER|MANAGED)_SETTINGS' \
  || true)
assert_eq "nothing in claudio writes to Claude Code's own settings files" "$sl_writes" ""
assert_file_not_exists "the statusline tests never touched the real ~/.claudio" \
  "$HOME/.claudio/profiles/sl-fresh"
assert_file_not_exists "...nor the real ~/.claude" "$HOME/.claude/profiles/sl-fresh"
assert_file_not_exists "...nor the real accounts" "$HOME/.claudio/accounts/a1"

# $CONF and $USER_SETTINGS are shared with every later section: leave nothing.
: > "$CONF"
rm -f "$USER_SETTINGS" "$MANAGED_SETTINGS"
cd "$WORKDIR"

# ============================================================
printf "\n\033[1m=== the status line shim ===\033[0m\n"
# ============================================================
# `claudio statusline --shim` is what an account's settings.json runs, so it
# executes on every render. It reads the payload, records a sample, and hands
# the identical bytes to the renderer named by statusline=.
#
# The property that matters most is the one asserted hardest below: a recorder
# fault must cost you a sample, never your status line and never a non-zero
# exit. Everything else here is speed and passthrough.

UDIR="$TMP/usage"
SHIMCFG="$ACCOUNTS/work"
SHIMTAGS=""
rm -rf "$UDIR"

# A renderer that proves what reached it: the bytes on stdin, byte-counted, and
# the environment. `cksum` of stdin, so "byte for byte" is a real claim.
SHIMBAR="$TMP/shim-bar.sh"
cat > "$SHIMBAR" <<'STUB'
#!/bin/sh
_in=$(cat)
printf 'RENDER cksum=%s cfg=%s rc=%s\n' \
  "$(printf '%s' "$_in" | cksum | tr -s ' ' | cut -d' ' -f1)" \
  "${CLAUDE_CONFIG_DIR:-none}" "${SHIMBAR_RC:-0}"
exit "${SHIMBAR_RC:-0}"
STUB
chmod +x "$SHIMBAR"

PAY=$(payload 10 1786489800 40 1786564800)
PAY_CKSUM=$(printf '%s' "$PAY" | cksum | tr -s ' ' | cut -d' ' -f1)

# --- recording OFF: the path that runs forever ---
printf 'statusline=sh %s\nusage_dir=%s\n' "$SHIMBAR" "$UDIR" > "$CONF"
out=$(shim "$PAY")
assert_contains "the shim renders the line when recording is off" "$out" "RENDER cksum="
assert_contains "...handing the renderer the payload byte for byte" "$out" "cksum=$PAY_CKSUM"
assert_contains "...with the account's config dir still exported" "$out" "cfg=$ACCOUNTS/work"
assert_file_not_exists "recording off records nothing" "$UDIR/samples.jsonl"
assert_file_not_exists "...and creates no usage directory at all" "$UDIR"

# The renderer's exit status is the shim's: it is a pass-through, so a broken
# renderer must not look like a working one.
sh_rc=0
SHIMBAR_RC=3 out=$(SHIMBAR_RC=3 shim "$PAY") || sh_rc=$?
assert_exit_code "the shim passes the renderer's exit status through" "$sh_rc" 3

# A bare command word is exec'd directly rather than through a shell, which is
# a whole process saved on every render — and `statusline=claude-statusline` is
# the documented form, so it is the common case, not the clever one. Both
# spellings must reach the renderer with the payload intact.
printf 'statusline=cat\nusage_dir=%s\n' "$UDIR" > "$CONF"
out=$(shim "$PAY")
assert_eq "a bare command word is run without a shell, payload intact" "$out" "$PAY"
# ...and a command LINE still goes through one, because it has to.
printf 'statusline=sh %s\nusage_dir=%s\n' "$SHIMBAR" "$UDIR" > "$CONF"
out=$(shim "$PAY")
assert_contains "a command line with arguments still works" "$out" "cksum=$PAY_CKSUM"

# No renderer configured is a legitimate silent sensor, not an error.
printf 'usage_dir=%s\n' "$UDIR" > "$CONF"
sh_rc=0
out=$(shim "$PAY") || sh_rc=$?
assert_exit_code "no renderer and no recording is not an error" "$sh_rc" 0
assert_eq "...and draws nothing at all, rather than a blank line" "$out" ""

# --- recording ON ---
# The render half holds with or without jq, and is asserted either way: the
# whole promise is that recording is the part that degrades. The recorded half
# needs jq to parse the payload, so it self-skips — like the mixed-mode tests.
printf 'statusline=sh %s\nusage=1\nusage_dir=%s\n' "$SHIMBAR" "$UDIR" > "$CONF"
out=$(shim "$PAY")
assert_contains "the shim still renders the line when recording is on" "$out" "RENDER cksum="
assert_contains "...still byte for byte" "$out" "cksum=$PAY_CKSUM"
assert_eq "...and the renderer's output is all that reaches stdout" \
  "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" "1"
if command -v jq > /dev/null 2>&1; then
  assert_file_exists "recording on writes a sample" "$UDIR/samples.jsonl"
  assert_eq "one payload, one record" "$(nrecords "$UDIR")" "1"
else
  printf "  (skipped — jq not installed, and the recorder needs it)\n"
fi

# --- a recorder fault costs a sample, never the render ---
# An unwritable usage directory is the cheapest way to make the append fail
# without touching claudio's source.
rm -rf "$UDIR"; mkdir -p "$UDIR"; chmod 500 "$UDIR"
sh_rc=0
out=$(shim "$PAY") || sh_rc=$?
assert_exit_code "an unwritable usage dir does not fail the render" "$sh_rc" 0
assert_contains "...and the line is still drawn" "$out" "RENDER cksum="
# ROOT IGNORES THE MODE BITS, so the premise has to be checked rather than
# assumed: as root -- which is the ordinary state inside a CI container -- the
# chmod above leaves the directory perfectly writable, the recorder writes its
# sample exactly as designed, and the assertion below fails while claudio is
# behaving correctly. Verified on Debian in a container, where this was one of
# four failures that were all the environment and none of them the code. The
# probe is the same question the recorder asks, so it cannot disagree with it.
if ( : > "$UDIR/.writable-probe" ) 2>/dev/null; then
  rm -f "$UDIR/.writable-probe"
  printf "  (skipped — running as root, so chmod 500 does not make it unwritable)\n"
else
  assert_file_not_exists "...and no record was written" "$UDIR/samples.jsonl"
fi
chmod 700 "$UDIR"; rm -rf "$UDIR"

# jq missing is the other half of the same promise: recording needs it, the
# status line does not. PATH is stripped to a directory holding everything but
# jq, so `command -v jq` genuinely fails inside the shim.
NOJQ="$TMP/nojq-bin"
rm -rf "$NOJQ"; mkdir -p "$NOJQ"
for _b in "$TEST_SH" sh cat printf grep sed tr awk mkdir mv rm chmod cksum cut \
          uname basename dirname date head tail wc ls readlink expr sort; do
  _p=$(command -v "$_b" 2>/dev/null) && ln -sf "$_p" "$NOJQ/$_b"
done
sh_rc=0
out=$(PATH="$NOJQ" shim "$PAY") || sh_rc=$?
assert_exit_code "no jq on PATH does not fail the render" "$sh_rc" 0
assert_contains "...and the line is still drawn" "$out" "RENDER cksum="
assert_file_not_exists "...and nothing was recorded" "$UDIR/samples.jsonl"

# A payload that is not JSON at all. jq fails, the record is dropped, the
# render is untouched — and nothing is created, so the next tick tries again.
rm -rf "$UDIR"
sh_rc=0
out=$(shim 'this is not json') || sh_rc=$?
assert_exit_code "a garbage payload does not fail the render" "$sh_rc" 0
assert_contains "...the line is still drawn" "$out" "RENDER cksum="
assert_file_not_exists "...and nothing was recorded" "$UDIR/samples.jsonl"
sh_rc=0
out=$(shim '') || sh_rc=$?
assert_exit_code "an empty payload does not fail the render" "$sh_rc" 0

# --- the record is written AFTER the line is delivered ---
# The ordering is the whole graceful-degradation promise, and it is invisible
# from the outside unless the renderer is made to observe it: this one reports
# whether the sample file already existed when it ran. If the recorder ever
# moves ahead of the render, `sawfile=yes` and this fails.
rm -rf "$UDIR"
ORDBAR="$TMP/ord-bar.sh"
cat > "$ORDBAR" <<STUB
#!/bin/sh
cat > /dev/null
if [ -f "$UDIR/samples.jsonl" ]; then printf 'sawfile=yes\n'; else printf 'sawfile=no\n'; fi
STUB
chmod +x "$ORDBAR"
printf 'statusline=sh %s\nusage=1\nusage_dir=%s\n' "$ORDBAR" "$UDIR" > "$CONF"
out=$(shim "$PAY")
assert_eq "the renderer runs before anything is recorded" "$out" "sawfile=no"
if command -v jq > /dev/null 2>&1; then
  assert_eq "...and the record lands once the renderer is done" "$(nrecords "$UDIR")" "1"
  # ...and on the second tick the file the FIRST tick wrote is visible, which
  # is what proves `sawfile=no` above was ordering rather than simply "never
  # recorded" — the assertion would pass on a recorder that did nothing at all.
  out=$(shim "$(payload 20 1786489800 40 1786564800)")
  assert_eq "the second render sees the first record, so the probe works" "$out" "sawfile=yes"
else
  printf "  (skipped — jq not installed, and the recorder needs it)\n"
fi

# --- the disabled path is the one that runs forever, so time it ---
# Not a microbenchmark: a generous ceiling that only an early-dispatch
# regression can breach. Moving the --shim case down into cmd_statusline costs
# ~5 ms (dash) / ~8 ms (bash) per render, measured, and nothing else fails when
# someone tidies it there — this is the only test that would notice.
printf 'statusline=sh %s\nusage_dir=%s\n' "$SHIMBAR" "$UDIR" > "$CONF"
sl_t0=$(date +%s)
sl_i=0
while [ "$sl_i" -lt 20 ]; do shim "$PAY" > /dev/null; sl_i=$((sl_i + 1)); done
sl_t1=$(date +%s)
sl_el=$(( sl_t1 - sl_t0 ))
TESTS=$((TESTS + 1))
if [ "$sl_el" -le 4 ]; then
  pass "20 recording-off renders finish in ${sl_el}s (early dispatch intact)"
else
  fail "20 recording-off renders finish in ${sl_el}s (early dispatch intact)" \
    "took ${sl_el}s; the --shim case has probably been moved into cmd_statusline"
fi
# The position itself, statically, so the failure names the cause rather than
# leaving the next person to infer it from a stopwatch. The shim must be
# dispatched in the first fifth of the file, far above `case $CMD in`.
sl_early=$(grep -n '= --shim' "$CLAUDIO" | head -1 | cut -d: -f1)
sl_disp=$(grep -n '^case \$CMD in' "$CLAUDIO" | head -1 | cut -d: -f1)
TESTS=$((TESTS + 1))
if [ -n "$sl_early" ] && [ -n "$sl_disp" ] && [ "$sl_early" -lt "$((sl_disp / 5))" ]; then
  pass "the shim is dispatched near the top of claudio, not from the table"
else
  fail "the shim is dispatched near the top of claudio, not from the table" \
    "early dispatch at line '$sl_early', dispatch table at line '$sl_disp'"
fi

: > "$CONF"
rm -rf "$UDIR"

# ============================================================
printf "\n\033[1m=== usage recording ===\033[0m\n"
# ============================================================
# The 5-hour and 7-day plan percentages exist only in the JSON Claude Code
# pipes to a status line, so this is the only place they can be captured. The
# rule that decides WHETHER to write a record is the load-bearing part, and it
# is pinned below by replaying two fixtures derived from real data.

UDIR="$TMP/usage"
SHIMCFG="$ACCOUNTS/work"
SHIMTAGS=""
rm -rf "$UDIR"
# No renderer: these tests are about what is recorded, and a renderer would
# only add noise to stdout.
printf 'usage=1\nusage_dir=%s\n' "$UDIR" > "$CONF"

if ! command -v jq > /dev/null 2>&1; then
  printf "  (skipped — jq not installed, and the recorder needs it)\n"
else

# --- the record schema ---
SHIMTAGS="profile=soc,account=work,host=box,email=a@example.com"
shim "$(payload 7.000000000000001 1786489800 76 1786564800)" > /dev/null
assert_eq "one record" "$(nrecords "$UDIR")" "1"
# The 15 v1 keys, in v1 order, then the v2 additions. Pinned as one string
# because order is the compatibility promise: a v1 reader still finds what it
# expects at the front of the object.
assert_eq "the record carries the v1 keys in v1 order, then the v2 additions" \
  "$(field "$UDIR" 'keys_unsorted|join(",")')" \
  "ts,account_uuid,account_email,organization_uuid,rate_limit_tier,five_hour_pct,five_hour_resets_at,seven_day_pct,seven_day_resets_at,session_id,model,session_cost_usd,profile,client,project,schema,reason,host,account,prompt_id,agent,tags"
assert_eq "schema is 2" "$(field "$UDIR" '.schema')" "2"
assert_eq "the first record's reason is first" "$(field "$UDIR" '.reason')" "first"
# The raw float, byte for byte. Quantisation is a trigger, never a
# transformation: 7.000000000000001 is 0.07*100 as the server computes it, and
# storing a rounded 7 would throw away the only figure Anthropic actually sent.
assert_eq "the raw float is stored unmodified" \
  "$(field "$UDIR" '.five_hour_pct|tojson')" "7.000000000000001"
assert_eq "the reset epoch is stored" "$(field "$UDIR" '.five_hour_resets_at')" "1786489800"
assert_eq "the seven-day window is stored too" "$(field "$UDIR" '.seven_day_pct')" "76"
assert_eq "session_id comes from the payload" "$(field "$UDIR" '.session_id')" "sess-1"
assert_eq "model comes from the payload" "$(field "$UDIR" '.model')" "claude-opus-5[1m]"
assert_eq "client is prefixed" "$(field "$UDIR" '.client')" "claude-code/2.1.228"
assert_eq "project prefers project_dir" "$(field "$UDIR" '.project')" "/proj"
assert_eq "the cost is stored raw" "$(field "$UDIR" '.session_cost_usd')" "1.25"
# prompt_id is the join key to the OTel stream, so losing it costs the join.
assert_eq "prompt_id is recorded" "$(field "$UDIR" '.prompt_id')" "pid-1"
# The source this was ported from read .agent.name against a payload that
# carries agent_type at top level, so `agent` was null in all 65 real records.
# Ported as the fix, not faithfully.
assert_eq "agent comes from agent_type, which is where it actually is" \
  "$(field "$UDIR" '.agent')" "general"
# The tags are lifted out of OTEL_RESOURCE_ATTRIBUTES rather than re-derived,
# which is what makes them identical to the OTel stream's BY CONSTRUCTION.
assert_eq "profile is lifted from the tags" "$(field "$UDIR" '.profile')" "soc"
assert_eq "account is lifted from the tags" "$(field "$UDIR" '.account')" "work"
assert_eq "host is lifted from the tags" "$(field "$UDIR" '.host')" "box"
assert_eq "the whole tag string is kept verbatim" \
  "$(field "$UDIR" '.tags')" "profile=soc,account=work,host=box,email=a@example.com"
assert_eq "no truncation flag on an ordinary record" \
  "$(field "$UDIR" '.truncated')" "null"

# ...and with no tags at all it falls back to what claudio can work out itself.
rm -rf "$UDIR"; SHIMTAGS=""
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
assert_eq "account falls back to the config dir's name" "$(field "$UDIR" '.account')" "work"
assert_eq "host falls back to claudio's own host slug" "$(field "$UDIR" '.host')" "testhost"

# Identity comes from the account's .claude.json, never from $HOME: Claude
# Code's own default login is not claudio's to describe, and reading it would
# make the suite report the developer's real address.
rm -rf "$UDIR"
printf '%s\n' '{"oauthAccount":{"accountUuid":"uu-1","emailAddress":"who@example.com","organizationUuid":"org-1","organizationRateLimitTier":"default_claude_max_20x"}}' \
  > "$ACCOUNTS/work/.claude.json"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
assert_eq "account_uuid comes from the account's .claude.json" \
  "$(field "$UDIR" '.account_uuid')" "uu-1"
assert_eq "account_email too" "$(field "$UDIR" '.account_email')" "who@example.com"
assert_eq "organization_uuid too" "$(field "$UDIR" '.organization_uuid')" "org-1"
# userRateLimitTier is null on a Max plan and organizationRateLimitTier holds
# the value; reading only the first regressed all 65 real records to null.
assert_eq "rate_limit_tier falls back to the organisation's tier" \
  "$(field "$UDIR" '.rate_limit_tier')" "default_claude_max_20x"
assert_file_exists "the identity is cached, because .claude.json is ~180 KB" \
  "$UDIR/account_$(printf '%s' "${ACCOUNTS#/}" | tr / _)_work.json"
rm -f "$ACCOUNTS/work/.claude.json"

# No config dir at all: the four identity fields are null and nothing is read
# out of the developer's home.
rm -rf "$UDIR"; SHIMCFG=""
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
assert_eq "with no account, identity is null rather than the default login's" \
  "$(field "$UDIR" '[.account_uuid,.account_email]|tojson')" "[null,null]"
SHIMCFG="$ACCOUNTS/work"

# --- one bad field costs one field, never the record ---
rm -rf "$UDIR"
shim "$(payload '"n/a"' 1786489800 40 1786564800)" > /dev/null
assert_eq "a non-numeric percentage does not lose the record" "$(nrecords "$UDIR")" "1"
assert_eq "...the bad window is null" "$(field "$UDIR" '.five_hour_pct')" "null"
assert_eq "...and the good one is still there" "$(field "$UDIR" '.seven_day_pct')" "40"
rm -rf "$UDIR"
shim "$(payload '{"a":1}' 1786489800 40 1786564800)" > /dev/null
assert_eq "an object where a number belongs does not reach a numeric field" \
  "$(field "$UDIR" '.five_hour_pct')" "null"
rm -rf "$UDIR"
shim '{"rate_limits":"oops","session_id":"s"}' > /dev/null
assert_eq "a malformed rate_limits parent records nothing at all" "$(nrecords "$UDIR")" "0"
rm -rf "$UDIR"
shim '{"session_id":"s","model":{"id":"m"}}' > /dev/null
assert_eq "a payload with no rate limits creates no usage directory" \
  "$(nrecords "$UDIR")" "0"
assert_file_not_exists "...and no directory either" "$UDIR"

# --- the epoch guards ---
# Floor: the real data contains five_hour_resets_at: 1, which without a floor
# reads as an ancient window and makes every later tick a rollover.
rm -rf "$UDIR"
shim "$(payload 10 1 40 null)" > /dev/null
assert_eq "an implausible epoch is dropped, not stored" \
  "$(field "$UDIR" '.five_hour_resets_at')" "null"
assert_eq "...and the percentage is still recorded" "$(field "$UDIR" '.five_hour_pct')" "10"
# Ceiling: one epoch far enough in the future can never be beaten, so _u_win
# reads every later, honest epoch as a stale snapshot from another session and
# returns without recording — the window is silenced for good, with no
# self-healing path short of deleting the state file.
#
# Two shapes, because they hit two different guards. A millisecond timestamp is
# 13 digits and dies on the LENGTH check, which has to come first: shell
# arithmetic wraps silently past 2^63, so a long mark compares as whatever it
# happens to wrap to.
rm -rf "$UDIR"
shim "$(payload 10 1786478400000 40 null)" > /dev/null
assert_eq "a millisecond epoch is refused" "$(field "$UDIR" '.five_hour_resets_at')" "null"
shim "$(payload 20 1786478400000 40 null)" > /dev/null
assert_eq "...and the window keeps recording afterwards" "$(nrecords "$UDIR")" "2"
# ...and this one is 11 digits, so it passes the length check and only the
# ceiling stops it. Year 5138, which is not a rate-limit window.
rm -rf "$UDIR"
shim "$(payload 10 99999999999 40 null)" > /dev/null
assert_eq "an epoch far past any plausible window is refused too" \
  "$(field "$UDIR" '.five_hour_resets_at')" "null"
shim "$(payload 20 1786489800 40 null)" > /dev/null
assert_eq "...so an honest epoch afterwards is not read as a stale snapshot" \
  "$(nrecords "$UDIR")" "2"
assert_eq "...and it is the honest one that is stored" \
  "$(field "$UDIR" '.five_hour_resets_at' 2)" "1786489800"

# --- the high-water rule ---
rm -rf "$UDIR"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
assert_eq "the first sample is recorded" "$(nrecords "$UDIR")" "1"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
assert_eq "an identical sample is not" "$(nrecords "$UDIR")" "1"
shim "$(payload 9 1786489800 39 1786564800)" > /dev/null
assert_eq "a DECREASE is not: concurrent sessions hold older snapshots" \
  "$(nrecords "$UDIR")" "1"
shim "$(payload 11 1786489800 40 1786564800)" > /dev/null
assert_eq "a rise past the high-water mark is" "$(nrecords "$UDIR")" "2"
assert_eq "...and it is a change" "$(field "$UDIR" '.reason' 2)" "change"
# 10.4 and 10 quantise to the same integer, so nothing has moved as far as the
# threshold is concerned. Comparing the raw floats would fire on every tick.
shim "$(payload 11.4 1786489800 40 1786564800)" > /dev/null
assert_eq "a change below the quantiser's resolution is not a change" \
  "$(nrecords "$UDIR")" "2"
# The rollover branch is most of the feature, not an edge case: without it a
# 90% -> 2% window never records again, because 2% never beats 90%.
shim "$(payload 2 1786507800 40 1786564800)" > /dev/null
assert_eq "a rollover is recorded even though the percentage FELL" \
  "$(nrecords "$UDIR")" "3"
assert_eq "...and it is labelled reset" "$(field "$UDIR" '.reason' 3)" "reset"
assert_eq "...carrying the new window's boundary" \
  "$(field "$UDIR" '.five_hour_resets_at' 3)" "1786507800"
shim "$(payload 3 1786507800 40 1786564800)" > /dev/null
assert_eq "...and the new window records from its own baseline" "$(nrecords "$UDIR")" "4"
# A reset epoch that goes BACKWARDS is another session's older snapshot.
shim "$(payload 99 1786489800 40 1786564800)" > /dev/null
assert_eq "an older snapshot is ignored entirely, however high its percentage" \
  "$(nrecords "$UDIR")" "4"

# --- the state file ---
STATEF="$UDIR/.last_$(printf '%s' "${ACCOUNTS#/}" | tr / _)_work"
assert_file_exists "the state file is named after the config dir" "$STATEF"
assert_eq "the state is one line" "$(wc -l < "$STATEF" | tr -d ' ')" "1"
assert_eq "it has eight tab-separated fields" \
  "$(awk -F'\t' '{print NF}' "$STATEF")" "8"
# `-` and not empty: tab is IFS whitespace, so a run of tabs collapses into one
# delimiter and an empty field disappears on the way back in, silently shifting
# every later mark. That made a settled window look like a change every tick.
rm -rf "$UDIR"
shim "$(payload 10 null 40 null)" > /dev/null
assert_eq "an unknown mark is written as the - sentinel, never empty" \
  "$(awk -F'\t' '{print $2 "|" $4}' "$STATEF")" "-|-"
assert_eq "...and the field count is still eight" \
  "$(awk -F'\t' '{print NF}' "$STATEF")" "8"
assert_eq "the state records the version and the quantiser precision" \
  "$(awk -F'\t' '{print $7 "|" $8}' "$STATEF")" "2|0"
# A mark read back out gets exactly the validation an incoming one gets: one
# implausible epoch or one non-numeric percentage otherwise freezes the window
# for good, and there is no path that clears it.
rm -rf "$UDIR"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
printf 'uu\t1786478400000\tnotanumber\t-\t-\t1\t2\t0\n' > "$STATEF"
shim "$(payload 11 1786489800 40 1786564800)" > /dev/null
assert_eq "a corrupt state file re-establishes the baseline in one record" \
  "$(nrecords "$UDIR")" "2"
assert_eq "...labelled first, which is the path that already existed" \
  "$(field "$UDIR" '.reason' 2)" "first"
# A stored PERCENTAGE mark too long to compare is the same freeze by another
# route: nothing beats it, so the window never records again. This is the guard
# a first attempt at this port left out, and it cost the last record of the
# edge fixture — a legitimate 18% that could not beat a stored 1e30.
# Both windows are given settled marks, so the ONLY thing that can produce a
# second record is the five-hour one recovering. Leaving the seven-day mark
# unset would let it fire instead and the assertion would pass either way.
rm -rf "$UDIR"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
printf 'uu\t1786489800\t1000000000000000000000000000000\t1786564800\t40\t1\t2\t0\n' \
  > "$STATEF"
shim "$(payload 18 1786489800 40 1786564800)" > /dev/null
assert_eq "a mark too long to compare is cleared, not obeyed for ever" \
  "$(nrecords "$UDIR")" "2"
assert_eq "...and the honest percentage is what gets recorded" \
  "$(field "$UDIR" '.five_hour_pct' 2)" "18"
# ...and an implausible stored EPOCH is cleared on read, not only on arrival:
# a usage_dir shared between two machines, or one hand edit, would otherwise
# stick for ever.
rm -rf "$UDIR"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
printf 'uu\t99999999999\t5\t-\t-\t1\t2\t0\n' > "$STATEF"
shim "$(payload 11 1786489800 40 1786564800)" > /dev/null
assert_eq "an implausible stored epoch is cleared on the way back in" \
  "$(nrecords "$UDIR")" "2"
assert_eq "...re-establishing the baseline rather than silencing the window" \
  "$(field "$UDIR" '.reason' 2)" "first"

# --- the 1023-byte cap ---
# The governing limit is not PIPE_BUF but the shell's stdio flush unit, BUFSIZ,
# which is 1024 on Darwin: a longer line becomes several write(2) calls and
# concurrent sessions tear. Measured at exactly the 1023/1024 boundary.
rm -rf "$UDIR"
SHIMTAGS="pad=$(printf 'x%.0s' $(seq 1 1200))"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
assert_eq "a pathological record still produces exactly one line" \
  "$(nrecords "$UDIR")" "1"
sl_len=$(awk '{print length($0)}' "$UDIR/samples.jsonl")
TESTS=$((TESTS + 1))
if [ "$sl_len" -le 1023 ]; then
  pass "the line is capped at 1023 bytes (got $sl_len)"
else
  fail "the line is capped at 1023 bytes (got $sl_len)" "concurrent appends will tear"
fi
assert_eq "...and says it was truncated" "$(field "$UDIR" '.truncated')" "true"
# session_id and prompt_id are the keys this log joins to the OTel stream on, so
# a shared cap must leave them alone until it falls below their own length.
assert_eq "session_id survives the truncation" "$(field "$UDIR" '.session_id')" "sess-1"
assert_eq "prompt_id survives it too" "$(field "$UDIR" '.prompt_id')" "pid-1"
assert_eq "reason is never truncated, because a truncated one would be a lie" \
  "$(field "$UDIR" '.reason')" "first"
assert_eq "the numbers the dedupe rule reads are untouched" \
  "$(field "$UDIR" '.five_hour_pct')" "10"
SHIMTAGS=""

# --- the replay: the rule, against real data ---
# fixtures/replay-v1.jsonl is 65 consecutive samples taken from a real machine;
# replay-expected.tsv is what the rule should keep of them, derived by an
# independent implementation in another language (fixtures/derive-expected.py)
# rather than blessed from whatever this code happened to print. It contains
# every anomaly the rule exists to absorb: 3 in-window 5-hour decreases, 4
# seven-day decreases, 6 backwards 5-hour resets_at flaps, and one pair of
# samples with identical timestamps reporting different percentages.
replay() {
  # $1 = fixture, $2 = golden. Prints nothing; sets $REPLAY_GOT.
  rm -rf "$UDIR"
  while IFS= read -r _row; do
    [ -n "$_row" ] || continue
    _p=$(printf '%s' "$_row" | jq -c '{session_id:(.session_id // "s"),cwd:"/p",
      prompt_id:"pid",agent_type:"a",model:{id:(.model // "m")},version:"1.0.0",
      cost:{total_cost_usd:(.session_cost_usd // 0)},
      workspace:{current_dir:"/p",project_dir:"/p"},
      rate_limits:{five_hour:{used_percentage:.five_hour_pct,resets_at:.five_hour_resets_at},
                   seven_day:{used_percentage:.seven_day_pct,resets_at:.seven_day_resets_at}}}')
    shim "$_p" > /dev/null 2>&1
  done < "$1"
  REPLAY_GOT=$(jq -r '[.reason,
      (if .five_hour_pct == null then "" else (.five_hour_pct|tojson) end),
      (if .seven_day_pct == null then "" else (.seven_day_pct|tojson) end)]|@tsv' \
    "$UDIR/samples.jsonl" 2>/dev/null)
}

FIXTURES=$(dirname "$CLAUDIO")/fixtures
if [ -f "$FIXTURES/replay-v1.jsonl" ]; then
  replay "$FIXTURES/replay-v1.jsonl"
  assert_eq "replaying 65 real samples keeps exactly the 49 the rule should" \
    "$REPLAY_GOT" "$(cat "$FIXTURES/replay-expected.tsv")"
  # The edge fixture is the same rule against values that only appear when
  # something has gone wrong: resets_at of 1, a millisecond epoch, "n/a", an
  # object, and 1e30 — which without the 18-digit guard becomes a high-water
  # mark nothing can beat, silencing the window for the rest of the file.
  replay "$FIXTURES/replay-edge.jsonl"
  assert_eq "the edge fixture replays exactly as derived, hostile values and all" \
    "$REPLAY_GOT" "$(cat "$FIXTURES/replay-edge-expected.tsv")"
else
  printf "  (skipped — fixtures/ not next to claudio)\n"
fi

fi   # jq present

# --- usage_dir, and the one directory it must refuse ---
rm -rf "$UDIR"
# ~/.claude is Claude Code's directory and its layout is Claude Code's to
# change. Refuse rather than squat, however it is configured.
printf 'usage=1\nusage_dir=%s/.claude/samples\n' "$FAKEHOME" > "$CONF"
out=$(shim "$(payload 10 1786489800 40 1786564800)")
assert_file_not_exists "usage_dir inside ~/.claude is refused" "$FAKEHOME/.claude"
printf 'usage=1\nusage_dir=%s\n' "$FAKEHOME/.claude" > "$CONF"
out=$(shim "$(payload 10 1786489800 40 1786564800)")
assert_file_not_exists "...and so is ~/.claude itself" "$FAKEHOME/.claude"
# The default, which is what an unconfigured user gets.
rm -rf "$FAKEHOME/.claudio-usage"
printf 'usage=1\n' > "$CONF"
out=$(shim "$(payload 10 1786489800 40 1786564800)")
if command -v jq > /dev/null 2>&1; then
  assert_file_exists "usage_dir defaults to ~/.claudio-usage" \
    "$FAKEHOME/.claudio-usage/samples.jsonl"
else
  printf "  (skipped — jq not installed)\n"
fi
rm -rf "$FAKEHOME/.claudio-usage"

# --- usage= layering, mirroring the otel= tests exactly ---
# Same resolver rule, second implementation: _u_conf is fork-free because it
# runs on every render, and these are the tests that stop it drifting from
# _telemetry_conf. If you change one rule, this section fails.
UDIR="$TMP/usage-layers"
SHIMCFG="$ACCOUNTS/work"
recorded() {
  # 1 if the last shim call recorded, 0 if not. Fresh dir each time.
  rm -rf "$UDIR"
  printf '%s' "$1" > /dev/null
  shim "$(payload 10 1786489800 40 1786564800)" > /dev/null 2>&1
  nrecords "$UDIR"
}
SLW="$TMP/sl-layer-project"
rm -rf "$SLW"; mkdir -p "$SLW"
mkdir -p "$PROFILES/usage-prof"
cd "$SLW"

if ! command -v jq > /dev/null 2>&1; then
  printf "  (skipped — jq not installed)\n"
else
printf 'usage_dir=%s\n' "$UDIR" > "$CONF"
assert_eq "recording is off unless something says usage=1" "$(recorded)" "0"
printf 'usage_dir=%s\nusage=1\n' "$UDIR" > "$CONF"
assert_eq "the global conf can turn it on" "$(recorded)" "1"
printf 'usage=0\n' > "$SLW/.claudio.local"
assert_eq "the project file beats the global conf" "$(recorded)" "0"
printf 'usage=1\n' > "$PROFILES/usage-prof/claudio.conf"
printf 'usage-prof\n' > "$SLW/.claudio"
assert_eq "...and still beats the profile" "$(recorded)" "0"
rm -f "$SLW/.claudio.local"
printf 'usage_dir=%s\nusage=0\n' "$UDIR" > "$CONF"
assert_eq "the profile beats the plain global key" "$(recorded)" "1"
# The inversion. A privacy choice attached to an identity must not be
# overridable by a file inside a repo, which anyone can have written.
printf 'usage_dir=%s\nusage=1\naccount.work.usage=0\n' "$UDIR" > "$CONF"
assert_eq "account.<x>.usage=0 beats the global usage=1" "$(recorded)" "0"
printf 'usage=1\n' > "$SLW/.claudio.local"
assert_eq "...and beats .claudio.local usage=1, unlike every other layer" \
  "$(recorded)" "0"
assert_eq "...and beats the profile's usage=1 too" "$(recorded)" "0"
rm -f "$SLW/.claudio.local" "$SLW/.claudio"
# ...but only from the global conf, and only for the account that resolved.
printf 'usage_dir=%s\nusage=1\naccount.other.usage=0\n' "$UDIR" > "$CONF"
assert_eq "another account's key does not apply" "$(recorded)" "1"
printf 'usage_dir=%s\nusage=1\n' "$UDIR" > "$CONF"
printf 'account.work.usage=0\n' > "$SLW/.claudio.local"
assert_eq "an account.<x>. key in the project file is inert" "$(recorded)" "1"
rm -f "$SLW/.claudio.local"
# The account comes from CLAUDE_CONFIG_DIR, which is all the shim can trust.
printf 'usage_dir=%s\nusage=1\naccount.work.usage=0\n' "$UDIR" > "$CONF"
SHIMCFG=""
assert_eq "with no account resolved the account layer is skipped" "$(recorded)" "1"
SHIMCFG="$TMP/somewhere-else"
assert_eq "a config dir outside the accounts tree is not an account either" \
  "$(recorded)" "1"
SHIMCFG="$ACCOUNTS/work"
# usage_dir layers the same way, which is what makes a per-project recording
# directory possible at all.
printf 'usage=1\nusage_dir=%s\n' "$TMP/usage-global" > "$CONF"
printf 'usage_dir=%s\n' "$UDIR" > "$SLW/.claudio.local"
rm -rf "$UDIR" "$TMP/usage-global"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
assert_eq "usage_dir layers too" "$(nrecords "$UDIR")" "1"
assert_file_not_exists "...so the global one was not used" "$TMP/usage-global"
rm -f "$SLW/.claudio.local"

# --- logging= on the render path, mirroring the block above key for key ---
# The shim resolves the SAME rule fork-free, three keys in one traversal now
# (logging, the usage it replaces, usage_dir). This is the section that stops
# _u_conf and _logging_resolve drifting apart, so every case above has its
# counterpart here.
printf 'usage_dir=%s\nlogging=none\n' "$UDIR" > "$CONF"
assert_eq "logging=none does not record" "$(recorded)" "0"
printf 'usage_dir=%s\nlogging=local\n' "$UDIR" > "$CONF"
assert_eq "logging=local records" "$(recorded)" "1"
printf 'usage_dir=%s\nlogging=remote\n' "$UDIR" > "$CONF"
assert_eq "logging=remote implies local, so it records too" "$(recorded)" "1"
printf 'usage_dir=%s\nlogging=lokal\n' "$UDIR" > "$CONF"
assert_eq "a value claudio does not know reads as off, never as a guess" "$(recorded)" "0"

printf 'usage_dir=%s\nlogging=local\n' "$UDIR" > "$CONF"
printf 'logging=none\n' > "$SLW/.claudio.local"
assert_eq "the project file beats the global conf" "$(recorded)" "0"
printf 'logging=local\n' > "$PROFILES/usage-prof/claudio.conf"
printf 'usage-prof\n' > "$SLW/.claudio"
assert_eq "...and still beats the profile" "$(recorded)" "0"
rm -f "$SLW/.claudio.local"
printf 'usage_dir=%s\nlogging=none\n' "$UDIR" > "$CONF"
assert_eq "the profile beats the plain global key" "$(recorded)" "1"
rm -f "$SLW/.claudio"

# The inversion, on the render path.
printf 'usage_dir=%s\nlogging=local\naccount.work.logging=none\n' "$UDIR" > "$CONF"
assert_eq "account.<x>.logging=none beats the global logging=local" "$(recorded)" "0"
printf 'logging=local\n' > "$SLW/.claudio.local"
assert_eq "...and beats .claudio.local logging=local, unlike every other layer" \
  "$(recorded)" "0"
printf 'usage-prof\n' > "$SLW/.claudio"
assert_eq "...and beats the profile's logging=local too" "$(recorded)" "0"
rm -f "$SLW/.claudio.local" "$SLW/.claudio"
printf 'usage_dir=%s\nlogging=local\naccount.other.logging=none\n' "$UDIR" > "$CONF"
assert_eq "another account's key does not apply" "$(recorded)" "1"
printf 'usage_dir=%s\nlogging=local\n' "$UDIR" > "$CONF"
printf 'account.work.logging=none\n' > "$SLW/.claudio.local"
assert_eq "an account.<x>. key in the project file is inert" "$(recorded)" "1"
rm -f "$SLW/.claudio.local"
printf 'usage_dir=%s\nlogging=local\naccount.work.logging=none\n' "$UDIR" > "$CONF"
SHIMCFG=""
assert_eq "with no account resolved the account layer is skipped" "$(recorded)" "1"
SHIMCFG="$TMP/somewhere-else"
assert_eq "a config dir outside the accounts tree is not an account either" \
  "$(recorded)" "1"
SHIMCFG="$ACCOUNTS/work"

# The overlap release: both keys work, and the new one retires the old.
printf 'usage_dir=%s\nusage=1\n' "$UDIR" > "$CONF"
assert_eq "the deprecated usage=1 still records" "$(recorded)" "1"
printf 'usage_dir=%s\nlogging=none\nusage=1\n' "$UDIR" > "$CONF"
assert_eq "logging=none retires a deprecated usage=1" "$(recorded)" "0"
printf 'usage_dir=%s\nlogging=local\nusage=0\n' "$UDIR" > "$CONF"
assert_eq "logging=local retires a deprecated usage=0" "$(recorded)" "1"
printf 'usage_dir=%s\nlogging=none\n' "$UDIR" > "$CONF"
printf 'usage=1\n' > "$SLW/.claudio.local"
assert_eq "a narrower usage=1 does not survive a logging= anywhere" "$(recorded)" "0"
rm -f "$SLW/.claudio.local"

# usage_dir still layers with two keys in front of it, which is the whole
# reason the traversal reads three at once rather than three times.
printf 'logging=local\nusage_dir=%s\n' "$TMP/usage-global2" > "$CONF"
printf 'usage_dir=%s\n' "$UDIR" > "$SLW/.claudio.local"
rm -rf "$UDIR" "$TMP/usage-global2"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
assert_eq "usage_dir still layers under logging=" "$(nrecords "$UDIR")" "1"
assert_file_not_exists "...so the global one was not used" "$TMP/usage-global2"
rm -f "$SLW/.claudio.local"

# The case the test above CANNOT reach, and the one the three-key guard exists
# for. `_u_conf_next` blanks the NAME of each key that has been answered, so a
# broader layer can legitimately be entered with only the third key still live
# — "" "" usage_dir. Under the old two-key guard (`[ -n "$2$3" ]`) that call
# returns immediately, usage_dir is never read from the broader layer, and
# every sample lands in $HOME/.claudio-usage instead. Above, logging= is in the
# GLOBAL conf, so key 1 is still live when the global layer is read and the
# narrow guard passes; here both of the first two keys are answered by the
# project file, which is exactly the mid-migration shape the overlap release
# invites.
printf 'usage_dir=%s\n' "$UDIR" > "$CONF"
printf 'logging=local\nusage=1\n' > "$SLW/.claudio.local"
rm -rf "$UDIR" "$FAKEHOME/.claudio-usage"
shim "$(payload 10 1786489800 40 1786564800)" > /dev/null
assert_eq "usage_dir is still read when it is the ONLY key left live" \
  "$(nrecords "$UDIR")" "1"
assert_file_not_exists "...not the HOME default" \
  "$FAKEHOME/.claudio-usage/samples.jsonl"
rm -f "$SLW/.claudio.local"
rm -rf "$FAKEHOME/.claudio-usage"

# A status line renders; it never diagnoses. The deprecation notice is
# `claudio run`'s to give — a shim that printed it would put a warning into
# the status bar several times a second, and with no renderer configured it
# would be the ONLY thing on the line.
printf 'usage_dir=%s\nlogging=local\nusage=1\notel=1\n' "$UDIR" > "$CONF"
rm -rf "$UDIR"
out=$(shim "$(payload 10 1786489800 40 1786564800)" 2>&1)
assert_eq "the shim prints nothing at all, deprecated keys or not" "$out" ""
assert_eq "...having recorded anyway" "$(nrecords "$UDIR")" "1"
fi

rm -f "$PROFILES/usage-prof/claudio.conf"
cd "$WORKDIR"
: > "$CONF"
SHIMCFG=""; SHIMTAGS=""
rm -rf "$UDIR" "$TMP/usage"

# The one file this whole feature must never touch.
assert_eq "the recorder never wrote to the developer's real samples file" \
  "$(_real_stamp_state "$REAL_SAMPLES")" "$REAL_SAMPLES_BEFORE"

# ============================================================
printf "\n\033[1m=== the OTLP receiver: spawn and supervise ===\033[0m\n"
# ============================================================
# Stream A is written by a receiver spawned on demand by `claudio run` and kept
# alive by the status line shim. Two properties carry the whole thing and both
# are failure-shaped: it must never make the launch or a render wait, and it
# must never turn a receiver that cannot start into a fork bomb — the shim runs
# several times a second, so an uncapped supervisor is a machine on its knees.
# The cap is therefore tested FIRST, before anything that spawns at all.

RDIR="$TMP/recv"
RPROJ="$TMP/recv-project"
RECV_LOG="$TMP/recv-stub.log"
RECV_STUB="$TMP/recv-stub"
mkdir -p "$RPROJ"

# A stand-in for usage/recv/otlp-recv. It records how it was called — argv and
# the data directory it was handed — and then behaves as $RECV_MODE says:
#
#   bind  writes a pidfile the way the receiver does (pid, token, port,
#         started), releases the spawn lock and stays alive under its own name,
#         so `ps` can identify it exactly as the real one would be identified.
#   slow  takes three seconds to do anything at all: the launch must not wait.
#   flap  releases the lock and exits 0 without binding and without a word —
#         the crash loop the cap exists for, and the one shape that leaves
#         neither a pidfile nor a recv.err behind to stop it.
#   die   exits non-zero without binding.
#
# It also reports how many session entries were under sessions/ AT THE MOMENT
# IT RAN — the same probe shape the recorder's ordering test uses. That is what
# makes "the session is registered before the receiver is spawned" a
# behavioural assertion rather than a reading of the source.
cat > "$RECV_STUB" <<STUB
#!/bin/sh
_ns=0
for _nf in "\$CLAUDIO_USAGE_DIR"/sessions/*; do
  if [ -e "\$_nf" ]; then _ns=\$((_ns + 1)); fi
done
{ printf 'argv:'; for a in "\$@"; do printf ' <%s>' "\$a"; done
  printf ' dir=<%s> sessions=%s\n' "\$CLAUDIO_USAGE_DIR" "\$_ns"; } >> "$RECV_LOG"
case "\${RECV_MODE:-flap}" in
  bind) printf '%s\n%s\n%s\n%s\n' "\$\$" "tok-\$\$" 4318 1 > "\$CLAUDIO_USAGE_DIR/recv.pid"
        rmdir "\$CLAUDIO_USAGE_DIR/recv.lock" 2>/dev/null
        sleep 20 ;;
  slow) sleep 3; rmdir "\$CLAUDIO_USAGE_DIR/recv.lock" 2>/dev/null ;;
  flap) rmdir "\$CLAUDIO_USAGE_DIR/recv.lock" 2>/dev/null; exit 0 ;;
  die)  rmdir "\$CLAUDIO_USAGE_DIR/recv.lock" 2>/dev/null; exit 7 ;;
esac
STUB
chmod +x "$RECV_STUB"
RECV_MODE=flap
export RECV_MODE

# A telemetry configuration pointed at claudio's own receiver: http/json on
# loopback, which is the only shape the stdlib receiver can serve, plus the
# usage_dir the two sides share and the stub to run.
recv_conf() {
  printf 'otel=1\notel_protocol=%s\notel_endpoint=%s\nusage_dir=%s\nreceiver=%s\n' \
    "${1:-http/json}" "${2:-http://localhost:4318}" "$RDIR" "${3:-$RECV_STUB}" > "$CONF"
}

# How many times the stub has been started. The spawn is detached, so a count
# read too early is a race — every assertion that expects a start waits for it.
recv_starts_seen() {
  # `|| :` fixes the STATUS, never the output: `grep -c` prints 0 and exits 1
  # when it counts nothing. The suite runs under `set -e`, and `burst=$(...)`
  # below is a bare assignment, so the helper's status IS that statement's —
  # which aborted the whole run at the first receiver regression and took every
  # later section (migrate, the statusline retrofit, update, both doc-drift
  # guards) with it. An abort is not a pass, so a helper that can only be
  # called safely inside `[ ]` is a trap the next caller falls into.
  if [ -f "$RECV_LOG" ]; then grep -c '^argv:' "$RECV_LOG" || :; else echo 0; fi
}
recv_wait_starts() {
  # $1 = the count to wait for. Bounded, and it returns rather than aborting.
  _rwi=0
  while [ "$_rwi" -lt 20 ]; do
    if [ "$(recv_starts_seen)" -ge "$1" ]; then return 0; fi
    sleep 1
    _rwi=$((_rwi + 1))
  done
  return 1
}
recv_reset() {
  # Settle, then wipe. Every spawn is DETACHED, so a child of the previous
  # scenario can still be on its way to the log, and a line that landed after
  # the wipe would be counted against the next scenario — the same straggler
  # race wait_for_match carries a note about. The parent appends to recv.starts
  # BEFORE it forks, so "as many starts logged as attempts claimed" is an exact
  # quiescence test and needs no sleep of a guessed length.
  _rri=0
  while [ "$_rri" -lt 20 ]; do
    _rra=0
    if [ -f "$RDIR/recv.starts" ]; then _rra=$(grep -c . "$RDIR/recv.starts"); fi
    if [ "$(recv_starts_seen)" -ge "$_rra" ]; then break; fi
    sleep 1
    _rri=$((_rri + 1))
  done
  # A `bind` stub is still alive by design, and it is the only thing that ever
  # writes a pidfile here, so its own pidfile is how it is stopped. Then wipe
  # tolerantly: a stub releasing the lock while `rm -rf` walks the directory
  # answers "not empty", and under `set -e` that would abort the suite rather
  # than fail a test.
  if [ -f "$RDIR/recv.pid" ]; then
    _rrp=$(head -1 "$RDIR/recv.pid" 2>/dev/null)
    case $_rrp in [0-9]*) kill "$_rrp" 2>/dev/null || : ;; esac
  fi
  rm -rf "$RDIR" 2>/dev/null || :
  : > "$RECV_LOG"
}

# The harness checking itself, because this helper is read as a value by a bare
# assignment further down and the suite runs under `set -e`. A zero count is
# the NORMAL reading of a healthy tree in half the scenarios below, so a helper
# that exits 1 on zero is an abort waiting for the first real regression.
: > "$RECV_LOG"
rss_rc=0
rss_v=$(recv_starts_seen) || rss_rc=$?
assert_eq "a zero start count prints 0" "$rss_v" "0"
assert_eq "...and exits 0, so a bare assignment cannot abort the suite" "$rss_rc" "0"

# Everything the receiver and claudio share is declared once, in Python, and
# hardcoded once, in sh — so the two spellings are compared here. A cap that
# disagrees with itself refuses in one process while the other is still
# spawning, which is the fork bomb with extra steps.
COLLECTOR_PY="$(dirname "$CLAUDIO")/usage/cu/collector.py"
if [ -f "$COLLECTOR_PY" ]; then
  py_max=$(sed -n 's/^MAX_STARTS = \([0-9][0-9]*\).*/\1/p' "$COLLECTOR_PY" | head -1)
  py_win=$(sed -n 's/^START_WINDOW = \([0-9][0-9]*\).*/\1/p' "$COLLECTOR_PY" | head -1)
  sh_max=$(sed -n 's/^RECV_MAX_STARTS=\([0-9][0-9]*\).*/\1/p' "$CLAUDIO" | head -1)
  sh_win=$(sed -n 's/^RECV_START_WINDOW=\([0-9][0-9]*\).*/\1/p' "$CLAUDIO" | head -1)
  assert_contains "the cap was extracted from claudio" "$sh_max" "^[0-9][0-9]*$"
  assert_eq "claudio's start cap is the receiver's start cap" "$sh_max" "$py_max"
  assert_eq "...and so is the window it counts over" "$sh_win" "$py_win"
else
  printf "  (skipped — usage/cu/collector.py not next to claudio)\n"
  sh_max=5
fi

# --- the respawn cap ---------------------------------------------------------
# A receiver that cannot bind dies instantly. The supervisor sees it die and
# starts another, several times a second, for ever. The cap is what turns that
# into a refusal: past N starts in the window nothing is spawned, recv.err says
# why, and the next `claudio run` prints it.
recv_reset
mkdir -p "$RDIR"
now=$(date +%s)
i=0
while [ "$i" -lt "$sh_max" ]; do printf '%s\n' "$((now - i))" >> "$RDIR/recv.starts"; i=$((i + 1)); done
printf 'statusline=sh %s\nusage_dir=%s\nreceiver=%s\n' "$SHIMBAR" "$RDIR" "$RECV_STUB" > "$CONF"
SHIMTEL=1; SHIMEP="http://localhost:4318"; SHIMPROTO="http/json"
out=$(shim "$PAY")
assert_contains "a capped receiver still renders the status line" "$out" "RENDER cksum="
assert_eq "the cap refuses to spawn once the window is full" "$(recv_starts_seen)" "0"
assert_file_exists "...and writes recv.err so the next run can say why" "$RDIR/recv.err"
assert_contains "...naming the refusal, not just failing" \
  "$(cat "$RDIR/recv.err")" "refusing to start"

# The window is a window: attempts older than it do not count, or one bad
# minute would silence recording until someone deleted a file.
recv_reset
mkdir -p "$RDIR"
i=0
while [ "$i" -lt "$sh_max" ]; do
  printf '%s\n' "$((now - 600 - i))" >> "$RDIR/recv.starts"; i=$((i + 1))
done
out=$(shim "$PAY")
recv_wait_starts 1 || true
assert_eq "attempts older than the window do not count against the cap" \
  "$(recv_starts_seen)" "1"

# A corrupted line in recv.starts may cost an accounting, never a render.
# `0123456789` is digits and is still FATAL to `$(( ))` in both shells — dash
# "Illegal number", bash "value too great for base" — so the cap compares with
# `test`, which parses base 10, rather than evaluating what it read.
recv_reset
mkdir -p "$RDIR"
printf '0123456789\n0000\n' > "$RDIR/recv.starts"
out=$(shim "$PAY")
assert_contains "a leading-zero line in recv.starts still renders" "$out" "RENDER cksum="
recv_wait_starts 1 || true
assert_eq "...and the receiver is still started" "$(recv_starts_seen)" "1"

# End to end: a receiver that fails silently, driven by a supervisor firing as
# fast as the test can call it. The property is a bound, not a number — the
# spawn lock legitimately turns some attempts away — and the second burst is
# what makes it a bound rather than a coincidence.
recv_reset
i=0
while [ "$i" -lt 20 ]; do shim "$PAY" > /dev/null; i=$((i + 1)); done
sleep 1
burst=$(recv_starts_seen)
TESTS=$((TESTS + 1))
if [ "$burst" -ge 1 ] && [ "$burst" -le "$sh_max" ]; then
  pass "20 renders against a dying receiver start it $burst times, never more than $sh_max"
else
  fail "20 renders against a dying receiver start it $burst times, never more than $sh_max" \
    "started $burst times; the cap or the lock is not holding"
fi
i=0
while [ "$i" -lt 10 ]; do shim "$PAY" > /dev/null; i=$((i + 1)); done
sleep 1
assert_eq "...and ten more renders start it no further times" "$(recv_starts_seen)" "$burst"

# --- the shim only supervises a session that HAS a stream A ------------------
# The OTLP variables exist only in the environment of a claude that `claudio
# run` launched. A status line anywhere else has no receiver to keep alive, and
# must not start one.
recv_reset
SHIMTEL=""; SHIMEP=""; SHIMPROTO=""
out=$(shim "$PAY")
assert_contains "a session with no telemetry still renders" "$out" "RENDER cksum="
assert_eq "...and starts no receiver" "$(recv_starts_seen)" "0"

SHIMTEL=1; SHIMEP="http://localhost:4318"; SHIMPROTO=grpc
out=$(shim "$PAY") > /dev/null
assert_eq "a grpc export starts nothing — this receiver cannot serve it" \
  "$(recv_starts_seen)" "0"

SHIMPROTO="http/json"; SHIMEP="http://collector.example.com:4318"
out=$(shim "$PAY") > /dev/null
assert_eq "an export to another host starts nothing either" "$(recv_starts_seen)" "0"

SHIMEP="http://localhost:4318"
out=$(shim "$PAY")
recv_wait_starts 1 || true
assert_contains "a session exporting here renders as usual" "$out" "RENDER cksum="
assert_eq "...and starts the receiver behind it" "$(recv_starts_seen)" "1"

# ...and neither does a render. The shim's stdout is the status line itself, so
# a background child that inherited it would hold the line open until the
# RECEIVER exited — the dash trap again, one process further in, where it would
# look like Claude Code hanging rather than like a slow launch.
recv_reset
RECV_MODE=slow
sl_t0=$(date +%s)
out=$(shim "$PAY")
sl_t1=$(date +%s)
el=$((sl_t1 - sl_t0))
TESTS=$((TESTS + 1))
if [ "$el" -le 2 ] && echo "$out" | grep -q "RENDER cksum="; then
  pass "a render does not wait on the receiver either (${el}s)"
else
  fail "a render does not wait on the receiver either (${el}s)" \
    "took ${el}s against a receiver that takes 3s, or drew nothing"
fi
RECV_MODE=flap

# A fault already reported is not chased. recv.err is cleared by the receiver
# the moment it binds, so its presence means the current fault is unresolved —
# and re-reporting it several times a second is the fork this design refuses.
recv_reset
mkdir -p "$RDIR"
printf '2026-01-01 00:00:00  cannot bind 127.0.0.1:4318\n' > "$RDIR/recv.err"
out=$(shim "$PAY")
assert_contains "a reported fault still renders" "$out" "RENDER cksum="
assert_eq "...and is not chased again by the supervisor" "$(recv_starts_seen)" "0"

# A live receiver is left alone. `bind` writes a pidfile the way the real one
# does, so this is the healthy path: one file read, a kill -0, and nothing else.
recv_reset
RECV_MODE=bind
out=$(shim "$PAY")
recv_wait_starts 1 || true
if wait_for_match "$RDIR/recv.pid" '[0-9]'; then
  RECV_ALIVE=$(head -1 "$RDIR/recv.pid")
else
  RECV_ALIVE=""
fi
assert_contains "the spawned receiver wrote a pidfile" "$RECV_ALIVE" "^[0-9][0-9]*$"
out=$(shim "$PAY")
sleep 1
assert_eq "a live receiver is not respawned" "$(recv_starts_seen)" "1"
assert_contains "...and the render is untouched by any of it" "$out" "RENDER cksum="

# --- claudio run -------------------------------------------------------------
cd "$RPROJ"

# Telemetry off: nothing is started and nothing is written. The receiver is
# opt-in exactly as far as telemetry is, and not one step further.
recv_reset
: > "$CONF"
out=$(run run)
assert_contains "run with telemetry off still launches" "$out" '\[claude\]'
assert_eq "...and starts no receiver" "$(recv_starts_seen)" "0"
assert_file_not_exists "...and creates no usage directory" "$RDIR"

# The whole point: the export is aimed at this machine, so the thing that
# answers it is started before claude is.
recv_reset
RECV_MODE=flap
recv_conf
out=$(run run)
assert_contains "run with a loopback http/json export launches claude" "$out" '\[claude\]'
recv_wait_starts 1 || true
assert_eq "...and starts the receiver" "$(recv_starts_seen)" "1"
assert_contains "...as a serving receiver, on the port the export names" \
  "$(cat "$RECV_LOG")" "argv: <serve> <--quiet> <--port> <4318>"
assert_contains "...pointed at the same usage_dir claudio records into" \
  "$(cat "$RECV_LOG")" "dir=<$RDIR>"

# The refcount. `exec` preserves the pid, so what is written here IS the claude
# process — which is why claudio can be gone a moment later and the entry still
# means something.
sess=$(ls "$RDIR/sessions" 2>/dev/null | head -1)
assert_contains "run registers the session pid for the receiver's refcount" "$sess" "^[0-9][0-9]*$"

# The same corrupted accounting file, on the path where it is worst. The cap is
# read in the MAIN shell of `claudio run` — no subshell, no EXIT trap — so
# `$(( 0123456789 ))` did not fail the check, it killed the process before
# claude was ever launched. One torn write in a shared usage_dir against the
# tool's primary verb, explained by nothing but a raw shell error.
recv_reset
mkdir -p "$RDIR"
printf '0123456789\n' > "$RDIR/recv.starts"
recv_conf
rc=0
out=$(run run) || rc=$?
assert_exit_code "a leading-zero line in recv.starts does not abort run" "$rc" "0"
assert_contains "...and claude is still launched" "$out" '\[claude\]'

# The documented opt-in, and nothing else. `otel=1` with claudio's own
# defaults has to be a configuration that works end to end: it used to default
# to grpc on 4317 — the Docker collector's shape — so telemetry came on,
# nothing was started, and every record went to a closed port, which Claude
# Code drops in silence by design. That is this project's cardinal sin arriving
# as the default experience of the feature.
recv_reset
printf 'otel=1\nusage_dir=%s\nreceiver=%s\n' "$RDIR" "$RECV_STUB" > "$CONF"
out=$(run run)
assert_contains "otel=1 alone launches claude" "$out" '\[claude\]'
recv_wait_starts 1 || true
assert_eq "...and starts the receiver on claudio's own defaults" "$(recv_starts_seen)" "1"
assert_contains "...on the port those defaults name" \
  "$(cat "$RECV_LOG")" "argv: <serve> <--quiet> <--port> <4318>"

# Someone else's collector is not ours to start, and neither is a protocol this
# receiver cannot speak. The remote case is silent — claudio cannot know
# whether something is listening on an endpoint it did not choose — but a
# LOOPBACK endpoint in a protocol nothing here speaks is unambiguously a local
# mistake, so that one is said out loud rather than left to fail quietly.
recv_reset
recv_conf grpc
out=$(run run)
assert_eq "a grpc export starts nothing" "$(recv_starts_seen)" "0"
assert_contains "...and says so, because the endpoint is this machine" \
  "$out" "which nothing here can serve"
assert_contains "...naming the two lines to change" "$out" "otel_protocol=http/json"
recv_conf http/json http://collector.example.com:4318
out=$(run run)
assert_eq "an export to another host starts nothing" "$(recv_starts_seen)" "0"
recv_conf grpc http://collector.example.com:4317
out=$(run run)
assert_not_contains "...and someone else's collector is not diagnosed at all" \
  "$out" "which nothing here can serve"

# --no-telemetry silences the run, and that has to include the receiver: the
# flag exists to make one run export nothing at all.
recv_reset
recv_conf
out=$(run run --no-telemetry)
assert_eq "--no-telemetry starts no receiver" "$(recv_starts_seen)" "0"

# usage_dir inside ~/.claude is refused here for the same reason it is refused
# by the recorder: that directory is Claude Code's and its layout is Claude
# Code's to change.
recv_reset
printf 'otel=1\notel_protocol=http/json\notel_endpoint=http://localhost:4318\nusage_dir=%s\nreceiver=%s\n' \
  "$HOME/.claude/usage" "$RECV_STUB" > "$CONF"
out=$(run run)
assert_eq "a usage_dir inside ~/.claude starts nothing" "$(recv_starts_seen)" "0"
assert_file_not_exists "...and nothing is created there" "$HOME/.claude/usage"
# And it is SAID. The refusal is right; a bare `return 0` was not — logging on,
# claude launched, and neither stream writing anything, for ever, with the one
# command that reports recording saying it was on.
assert_contains "...and the run says why nothing is being recorded" \
  "$out" "inside ~/.claude"
assert_contains "...naming the fix rather than only the refusal" \
  "$out" "Point usage_dir elsewhere"
recv_conf
out=$(run run)
assert_not_contains "a usable usage_dir carries no such warning" \
  "$out" "Point usage_dir elsewhere"

# --- a usage_dir that cannot be used ----------------------------------------
# The worst shape this feature has: telemetry is ON, the endpoint is exported,
# claude launches, every record goes to a closed port and Claude Code drops it
# in silence by design — for ever, because nothing about a directory nobody can
# write repairs itself on the next run. Every path that gives up on the data
# directory therefore says so on the run path.
if [ "$(id -u)" != 0 ]; then
recv_reset
RO_PARENT="$TMP/recv-ro"
rm -rf "$RO_PARENT"
mkdir -p "$RO_PARENT"
chmod 555 "$RO_PARENT"
printf 'otel=1\notel_protocol=http/json\notel_endpoint=http://localhost:4318\nusage_dir=%s\nreceiver=%s\n' \
  "$RO_PARENT/usage" "$RECV_STUB" > "$CONF"
out=$(run run)
assert_contains "a usage_dir that cannot be created is reported" "$out" "stream A will NOT be recorded"
assert_contains "...naming the directory" "$out" "$RO_PARENT/usage"
assert_contains "...and the launch still happens" "$out" '\[claude\]'
assert_eq "...and no receiver was started" "$(recv_starts_seen)" "0"

# The same again one step later: the directory exists and is not writable, so
# the pidfile, the lock and the capture all have nowhere to go.
recv_reset
mkdir -p "$RDIR"
chmod 500 "$RDIR"
recv_conf
out=$(run run)
assert_contains "a usage_dir that cannot be written is reported too" "$out" "stream A will NOT be recorded"
# Named for the fault, not for the first thing it broke: without the writability
# test this surfaces one step later as a lock that cannot be claimed, which
# reads like a race and sends the reader looking for another spawner.
assert_contains "...and diagnosed as the directory, not as a lock" "$out" "cannot write to"
assert_contains "...and the launch still happens" "$out" '\[claude\]'
chmod 700 "$RDIR"
chmod 755 "$RO_PARENT"
rm -rf "$RO_PARENT"
else
  printf "  (skipped 6 — running as root, where mode bits refuse nothing)\n"
fi

# A recv.lock that is not a lock. The shell cannot see errno, so a failed
# `mkdir` used to be read as "another spawner holds it" whatever went wrong —
# and a regular file of that name then stood every future spawn down silently
# and permanently: `find -mmin` matches nothing that was never a directory and
# `rmdir` cannot remove a file, so the staleness rule could never clear it.
recv_reset
mkdir -p "$RDIR"
: > "$RDIR/recv.lock"
recv_conf
out=$(run run)
recv_wait_starts 1 || true
assert_eq "a recv.lock that is not a directory is cleared, not obeyed" \
  "$(recv_starts_seen)" "1"

# The launch must never wait on the receiver. This is the dash trap the update
# refresh already carries a scar from: a redirected SIMPLE background command
# leaks the parent's stdout, so `out=$(claudio run …)` blocks until the child
# exits — which here would be until the receiver does, i.e. for the session.
recv_reset
RECV_MODE=slow
recv_conf
t0=$(date +%s)
out=$(run run)
t1=$(date +%s)
el=$((t1 - t0))
TESTS=$((TESTS + 1))
if [ "$el" -le 2 ]; then
  pass "run does not wait on the receiver (${el}s)"
else
  fail "run does not wait on the receiver (${el}s)" \
    "took ${el}s against a receiver that takes 3s; the braces around the background command are gone"
fi
RECV_MODE=flap

# The one diagnostic on this path. A receiver that is quietly not there is the
# silent loss this project exists to prevent, so the reason the last start
# failed is printed — on stderr, because cmd_env's stdout is meant to be eval'd
# and these two share a prologue.
recv_reset
mkdir -p "$RDIR"
printf '2026-01-01 00:00:00  cannot bind 127.0.0.1:4318: address already in use\n' > "$RDIR/recv.err"
recv_conf
out=$(run run)
assert_contains "run prints why the receiver last failed" "$out" "address already in use"

# ...and the other file, which is the fault of a receiver that was UP. Two
# files because they answer two questions and only one is a reason not to
# spawn: recv.err stands the supervisor down, recv.warn must not. Merging them
# cost a real bug — one undecodable payload wrote recv.err from a healthy
# receiver, and from then on no render would ever respawn a dead one.
recv_reset
mkdir -p "$RDIR"
printf '2026-01-01 00:00:00  undecodable OTLP body: Expecting value\n' > "$RDIR/recv.warn"
recv_conf
out=$(run run)
assert_contains "run prints what a running receiver reported" "$out" "undecodable OTLP body"
recv_wait_starts 1 || true
assert_eq "...and a runtime fault does not stand the spawn down" "$(recv_starts_seen)" "1"

# The shipper's two files, same contract and the same reason: a shipping
# failure nobody is told about is silent non-delivery. They are printed from
# _run_receiver ABOVE the protocol gate, because shipping does not depend on
# this machine's receiver — someone exporting to their own collector ships from
# the same ledger and never reaches _recv_ensure at all.
recv_reset
mkdir -p "$RDIR"
printf '2026-01-01 00:00:00  1 shipping failure(s); the offsets are unchanged: alpha/ledger: HTTP 503\n' > "$RDIR/ship.err"
printf '2026-01-01 00:00:00  2 ledger row(s) carry no account identity\n' > "$RDIR/ship.warn"
recv_conf
out=$(run run)
assert_contains "run prints why the last shipping pass failed" "$out" "HTTP 503"
assert_contains "...and says the offsets are unchanged, so it is a retry" \
  "$out" "offsets are unchanged"
assert_contains "run prints what a delivering pass could not place" \
  "$out" "no account identity"
rm -f "$RDIR/ship.err" "$RDIR/ship.warn"

# ...including when the export goes to somebody else's collector, which is the
# path that never reaches the receiver machinery at all.
recv_reset
mkdir -p "$RDIR"
printf '2026-01-01 00:00:00  1 shipping failure(s): alpha/ledger: HTTP 503\n' > "$RDIR/ship.err"
recv_conf grpc http://otel.corp:4317
out=$(run run)
assert_contains "a remote OTLP export still hears about a shipping failure" \
  "$out" "HTTP 503"
rm -f "$RDIR/ship.err"

# The same, on the render path: recv.warn is not the supervisor's gate.
recv_reset
mkdir -p "$RDIR"
printf '2026-01-01 00:00:00  could not write raw/api_request.jsonl: No space left\n' > "$RDIR/recv.warn"
printf 'statusline=sh %s\nusage_dir=%s\nreceiver=%s\n' "$SHIMBAR" "$RDIR" "$RECV_STUB" > "$CONF"
SHIMTEL=1; SHIMEP="http://localhost:4318"; SHIMPROTO="http/json"
out=$(shim "$PAY")
recv_wait_starts 1 || true
assert_contains "a render past a runtime fault still draws" "$out" "RENDER cksum="
assert_eq "...and still respawns a dead receiver" "$(recv_starts_seen)" "1"
SHIMTEL=""; SHIMEP=""; SHIMPROTO=""
cd "$RPROJ"

# A receiver claudio cannot find at all: said once in recv.err, said on stderr,
# and never retried in silence.
recv_reset
recv_conf http/json http://localhost:4318 "$TMP/no-such-receiver"
out=$(run run)
assert_contains "a missing receiver is reported by name" "$out" "not found at $TMP/no-such-receiver"
assert_contains "...with the key that names it" "$out" "receiver=<path>"
assert_file_exists "...and recorded for the shim to read" "$RDIR/recv.err"
# A PATH was configured and is simply wrong, so the remedy is the override and
# nothing else. Naming the install here would send someone to reinstall over a
# typo in their own conf.
assert_not_contains "...and a wrong receiver= path is not blamed on the install" \
  "$out" "make install"

# A BARE name is the other shape, and it is the one a single-file copy of
# `claudio` produces: the search fell through both layouts and landed on the
# last resort, a program nothing ever puts on $PATH. That is an install fault,
# not a configuration one, and it used to be reported as though the user had
# mis-set a key they had never heard of.
recv_reset
recv_conf http/json http://localhost:4318 otlp-recv
out=$(run run)
assert_contains "a bare receiver name names the install as the cause" \
  "$out" "installed without its Python"
assert_contains "...and the command that fixes it" "$out" "make install"
assert_contains "...while still offering the override" "$out" "receiver=<path>"

# --- liveness, and the two ways a pidfile lies -------------------------------
# A live receiver is not started a second time.
recv_reset
RECV_MODE=bind
recv_conf
out=$(run run)
recv_wait_starts 1 || true
wait_for_match "$RDIR/recv.pid" '[0-9]' || true
out=$(run run)
sleep 1
assert_eq "run leaves a live receiver alone" "$(recv_starts_seen)" "1"

# A stale pidfile — a SIGKILLed receiver leaves one, because only a graceful
# exit removes it. The pid is dead, so it is replaced.
recv_reset
RECV_MODE=flap
mkdir -p "$RDIR"
printf '%s\n%s\n%s\n%s\n' 999999 tok-dead 4318 1 > "$RDIR/recv.pid"
out=$(run run)
recv_wait_starts 1 || true
assert_eq "a stale pidfile is replaced, not believed" "$(recv_starts_seen)" "1"

# A RECYCLED pid: the file names a process that is alive and is not the
# receiver. `kill -0` says yes and is wrong, and believing it would leave this
# session exporting into nothing for its whole life — so the run path, which
# can afford one fork a session, asks what the process actually is.
recv_reset
mkdir -p "$RDIR"
sleep 20 &
RECV_IMPOSTOR=$!
printf '%s\n%s\n%s\n%s\n' "$RECV_IMPOSTOR" tok-recycled 4318 1 > "$RDIR/recv.pid"
out=$(run run)
recv_wait_starts 1 || true
assert_eq "a recycled pid does not pass for the receiver" "$(recv_starts_seen)" "1"
assert_file_not_exists "...and its pidfile is cleared, token matched first" "$RDIR/recv.pid"
kill "$RECV_IMPOSTOR" 2>/dev/null || true

# --- the spawn lock ----------------------------------------------------------
# Whoever loses the race walks away rather than waiting: the winner is about to
# bind the one port there is.
recv_reset
mkdir -p "$RDIR/recv.lock"
recv_conf
out=$(run run)
sleep 1
assert_eq "a held lock means this spawner stands down" "$(recv_starts_seen)" "0"

# ...but a lock nobody released must not stop every future spawn for ever. It
# is broken rather than obeyed, and that attempt still stands down, so breaking
# it and claiming it are never one racy step.
#
# The lock is re-established immediately before it is aged, and that is not
# belt and braces. The RECEIVER releases the spawn lock as it starts -- claudio
# deliberately does not, because a receiver alive for hours would be removing a
# lock some later spawner now holds -- so a stub receiver detached by an earlier
# scenario can still be on its way here. On a slow machine it lands between the
# `mkdir` above and the `touch` below, and then this `run` finds NO lock,
# claims it and spawns: `recv.lock` exists at the assertion and the spawn count
# is already 1, so both assertions in this block fail. That is a raced test over
# a correct claudio, and it is what failed on the CI runner while passing on
# macOS and in two Linux containers.
#
# Waiting for absence first is what makes it exact: the straggler's last act is
# releasing the lock, so an empty path means it has finished and the lock
# created next is ours.
recv_lock_i=0
while [ -e "$RDIR/recv.lock" ] && [ "$recv_lock_i" -lt 20 ]; do
  sleep 1
  recv_lock_i=$((recv_lock_i + 1))
done
mkdir -p "$RDIR/recv.lock"
touch -t 200001010000 "$RDIR/recv.lock"
out=$(run run)
assert_file_not_exists "a stale lock is broken" "$RDIR/recv.lock"
out=$(run run)
recv_wait_starts 1 || true
assert_eq "...and the next run spawns" "$(recv_starts_seen)" "1"

# --- the sessions directory --------------------------------------------------
# claudio is gone after exec and can never come back to tidy up, so the
# receiver's sweep owns removal. This is the case it cannot cover: with no
# receiver ever running, nothing else would remove an entry.
recv_reset
mkdir -p "$RDIR/sessions"
: > "$RDIR/sessions/999998"
sleep 20 &
RECV_LIVE_SESS=$!
: > "$RDIR/sessions/$RECV_LIVE_SESS"
: > "$RDIR/sessions/not-a-pid"
recv_conf
out=$(run run)
assert_file_not_exists "run sweeps a dead session entry" "$RDIR/sessions/999998"
assert_file_exists "...keeps a live one" "$RDIR/sessions/$RECV_LIVE_SESS"
assert_file_exists "...and does not touch what it cannot read as a pid" "$RDIR/sessions/not-a-pid"
kill "$RECV_LIVE_SESS" 2>/dev/null || true

# ...and the entry is written BEFORE the receiver is spawned, which is an
# ordering and not a style. The receiver's watchdog decides to exit from a
# snapshot of this directory taken on a tick, so with the spawn first `run`
# could be told the receiver was alive, register a moment later, and export
# into a process that had already counted zero sessions and gone. Reproduced
# against the real receiver: alive at t=5.98, registered at t=6.08, connection
# refused at t=8.08, with its own uptime record blaming "no live session".
#
# The stub reports what it saw, and the sweep is made slow enough for that
# reading to be decisive rather than lucky: every dead entry costs the parent
# an `rm` fork, so with the wrong order the stub reads the directory while
# hundreds are still waiting to be removed and cannot report 1.
recv_reset
mkdir -p "$RDIR/sessions"
i=0
while [ "$i" -lt 300 ]; do : > "$RDIR/sessions/$((999000 + i))"; i=$((i + 1)); done
recv_conf
out=$(run run)
recv_wait_starts 1 || true
assert_contains "the session is registered before the receiver is spawned" \
  "$(cat "$RECV_LOG")" "sessions=1$"

# And the directory it may now be the first to create is still 0700: it holds
# request metadata and an email address.
recv_reset
recv_conf
out=$(run run)
assert_eq "a usage dir created by the session registration is 0700" \
  "$(ls -ld "$RDIR" 2>/dev/null | cut -c1-10)" "drwx------"

# --- the detach form, statically ---------------------------------------------
# The elapsed-time test above catches this, but only on a machine slow enough
# to notice; the shape itself is the invariant, and it is one character away
# from the bug at all times.
assert_contains "the receiver is spawned as a BRACED background command" \
  "$(grep -c '{ _recv_start; } >/dev/null 2>&1 </dev/null &' "$CLAUDIO")" "^1$"

# The healthy path may not fork. `kill` is a builtin and the pidfile is read
# with `read`; a `ps`, a `cat` or a `$( )` in the shim's check would be a
# process several times a second, for ever.
recv_early=$(sed -n '/^_recv_alive()/,/^}/p' "$CLAUDIO")
assert_not_contains "the shim's liveness check forks nothing" "$recv_early" '\$('
assert_contains "...and asks with the kill builtin" "$recv_early" 'kill -0'

# Nothing above may have reached the developer's own recording.
assert_eq "the receiver never touched the real ~/.claudio-usage" \
  "$(_real_recv_state)" "$REAL_RECV_BEFORE"

for p in $(head -1 "$RDIR/recv.pid" 2>/dev/null) $RECV_ALIVE; do
  case $p in [0-9]*) kill "$p" 2>/dev/null || true ;; esac
done
RECV_MODE=flap
SHIMTEL=""; SHIMEP=""; SHIMPROTO=""
: > "$CONF"
rm -rf "$RDIR" 2>/dev/null || :
cd "$WORKDIR"

# ============================================================
printf "\n\033[1m=== migrate: the plan ===\033[0m\n"
# ============================================================
#
# claudio used to store its data inside ~/.claude, Claude Code's own directory.
# `migrate` moves it to ~/.claudio. Two things make it more than a `mv`:
# activation writes ABSOLUTE symlinks into projects, and on macOS the login is
# keyed to the account directory's path. Both are covered below.
#
# Every one of these runs goes through run_mig, which pins all four storage
# paths inside $TMP. Nothing here can see or touch a real installation.

cd "$TMP"
mk_legacy
MIGPROJ="$TMP/mig-project"
mk_legacy_project "$MIGPROJ"

out=$(run_mig migrate --dry-run --scan "$MIGPROJ")
assert_contains "migrate --dry-run names what it is doing" "$out" "move claudio's data out of Claude Code"
assert_contains "plan lists the profiles to move" "$out" "profiles/soc"
assert_contains "plan lists every profile" "$out" "profiles/hermetic-mig"
assert_contains "plan lists the accounts to move" "$out" "accounts/work"
assert_contains "plan lists the global conf" "$out" "claudio.conf"
assert_contains "plan lists the project to re-link" "$out" "$MIGPROJ"
assert_contains "plan counts the project's stale links" "$out" "5 links"
assert_contains "plan states nothing has changed yet" "$out" "Nothing has been changed yet"
assert_contains "dry run says it stopped" "$out" "Dry run"
assert_not_contains "dry run does not claim to have moved anything" "$out" "^Moved:"
# The stray file a real profiles directory accumulates is not claudio data.
assert_not_contains "plan ignores non-directories in the profiles dir" "$out" "DS_Store"

# The logins warning comes BEFORE anything moves — that is the whole point of it.
assert_contains "plan warns about logins before moving" "$out" "LOGINS:"

# Both platform branches, from either platform. THE PER-ACCOUNT LIST BELONGS TO
# THE macOS BRANCH and is asserted there rather than off the unfaked run above:
# `_mg_login_plan` returns before the list on every other platform, because the
# credential moves with the directory and there is nothing to report. Asserted
# unfaked, it passed on the author's Mac and failed on Linux -- a test that was
# really about the developer's laptop wearing the name of a general claim.
out=$(FAKE_UNAME_S=Darwin PATH="$UNAME_DIR:$PATH" run_mig migrate --dry-run)
assert_contains "on macOS the plan blames the Keychain" "$out" "Keychain"
assert_contains "on macOS the plan says a re-login is needed" "$out" "log in again"
assert_contains "on macOS the plan names the affected accounts" "$out" "work"
assert_contains "on macOS the plan reports each account's login state" "$out" "logged in"
out=$(FAKE_UNAME_S=Linux PATH="$UNAME_DIR:$PATH" run_mig migrate --dry-run)
assert_contains "elsewhere the plan says credentials move too" "$out" "credentials.json"
assert_contains "elsewhere the plan says no re-login" "$out" "No re-login needed"

# --dry-run really is dry.
assert_file_exists "dry run leaves the profile where it was" "$MIGOLD/profiles/soc/mcp.json"
assert_file_not_exists "dry run creates nothing at the destination" "$MIGNEW/profiles/soc"
assert_eq "dry run leaves the links alone" \
  "$(readlink "$MIGPROJ/.mcp.json")" "$MIGOLD/profiles/soc/mcp.json"

# ============================================================
printf "\n\033[1m=== migrate: confirmation ===\033[0m\n"
# ============================================================

rc=0
out=$(printf 'no\n' | run_mig migrate --scan "$MIGPROJ") || rc=$?
assert_contains "wrong confirmation aborts" "$out" "Aborted"
assert_exit_code "abort exits non-zero" "$rc" "1"
assert_file_exists "abort moves nothing" "$MIGOLD/profiles/soc/mcp.json"
assert_file_not_exists "abort creates nothing" "$MIGNEW/profiles/soc"

out=$(printf 'migrate\n' | run_mig migrate --scan "$MIGPROJ")
assert_contains "typing 'migrate' proceeds" "$out" "^Moved:"
assert_file_exists "confirmed run moves the profile" "$MIGNEW/profiles/soc/mcp.json"

# ============================================================
printf "\n\033[1m=== migrate: moving the data ===\033[0m\n"
# ============================================================

mk_legacy
mk_legacy_project "$MIGPROJ"
before_links=$(count_links "$MIGPROJ")
rc=0
# PINNED TO Darwin, because the result assertions below include the re-login
# advice and `_mg_login_result` returns immediately on every other platform.
# Everything else here -- the moves, the nesting guards, the stray file -- is
# platform-neutral and reads the same either way, so faking costs nothing and
# buys the same answer on a Mac and on a Linux CI box. Unpinned, "result tells
# you to log back in" failed on Linux against a claudio doing exactly what it
# documents. The Linux half of the same promise is asserted at the end of the
# migrate tests, where a fresh legacy tree can be built for it.
out=$(FAKE_UNAME_S=Darwin PATH="$UNAME_DIR:$PATH" run_mig migrate --yes --scan "$MIGPROJ") || rc=$?
assert_exit_code "a clean migration exits 0" "$rc" "0"

assert_file_exists "profile lands at the new path" "$MIGNEW/profiles/soc/mcp.json"
assert_eq "profile content survives the move" \
  "$(cat "$MIGNEW/profiles/soc/CLAUDE.md")" "soc instructions"
assert_file_exists "nested profile content survives" "$MIGNEW/profiles/soc/skills/a.md"
assert_file_exists "every profile moves" "$MIGNEW/profiles/hermetic-mig"
assert_file_exists "accounts move too" "$MIGNEW/accounts/work/.claude.json"
assert_file_exists "the logged-out account moves too" "$MIGNEW/accounts/spare"
assert_file_exists "the global conf moves" "$MIGNEW/claudio.conf"
assert_file_not_exists "the original is gone once the move succeeded" "$MIGOLD/profiles/soc"
assert_file_not_exists "the original account is gone too" "$MIGOLD/accounts/work"

# Per item, never the parent: `mv old/profiles new/profiles` does not fail when
# the destination exists, it nests into new/profiles/profiles.
assert_file_not_exists "the profiles dir is not nested into itself" "$MIGNEW/profiles/profiles"
assert_file_not_exists "the accounts dir is not nested into itself" "$MIGNEW/accounts/accounts"
assert_file_exists "a stray file is left behind, not carried over" "$MIGOLD/profiles/.DS_Store"

assert_contains "result reports what moved" "$out" "^Moved:"
assert_contains "result says where the data now lives" "$out" "$MIGNEW/profiles"
assert_contains "result tells you to log back in" "$out" "claudio account login work"
assert_not_contains "result does not nag about accounts that had no login" "$out" "account login spare"
assert_contains "result offers undo" "$out" "migrate --undo"

# ============================================================
printf "\n\033[1m=== migrate: the symlinks ===\033[0m\n"
# ============================================================
#
# The links are absolute and claudio recognises its own by target prefix, so
# after the move it does not merely dangle them, it disowns them: `clean`
# reports success and removes nothing. Re-running activation cannot repair
# that, so migrate rewrites the targets in place.

assert_eq "a CONFIG_MAP link is repointed" \
  "$(readlink "$MIGPROJ/.mcp.json")" "$MIGNEW/profiles/soc/mcp.json"
assert_eq "a link inside .claude/ is repointed" \
  "$(readlink "$MIGPROJ/.claude/skills")" "$MIGNEW/profiles/soc/skills"
# The one no CONFIG_MAP-driven fix could ever reach.
assert_eq "an off-CONFIG_MAP claudio link is repointed" \
  "$(readlink "$MIGPROJ/.mcp.json.bak")" "$MIGNEW/profiles/soc/mcp.json"
# Matching by target prefix is what protects this one.
assert_eq "a link that is not claudio's is left alone" \
  "$(readlink "$MIGPROJ/.git-profile")" "$TMP/outside-target"

assert_eq "no link is left dangling" "$(count_dangling "$MIGPROJ")" "0"
assert_eq "the project's link set is unchanged in size" "$(count_links "$MIGPROJ")" "$before_links"
assert_eq "the repointed link resolves to the real content" \
  "$(cat "$MIGPROJ/CLAUDE.md")" "soc instructions"
assert_contains "result reports the re-linked project" "$out" "$MIGPROJ"

# The marker names a profile, not a path, so it needs no migration at all.
assert_eq "the .claudio marker is untouched" "$(cat "$MIGPROJ/.claudio")" "soc"

# And claudio owns the project again: clean removes the links it just repointed.
out=$(cd "$MIGPROJ" && run_mig clean)
assert_contains "clean owns the repointed links again" "$out" "removed .mcp.json"
assert_not_contains "clean no longer reports an empty removal" "$out" "no claudio symlinks"

# ============================================================
printf "\n\033[1m=== migrate: idempotence ===\033[0m\n"
# ============================================================

mk_legacy
mk_legacy_project "$MIGPROJ"
run_mig migrate --yes --scan "$MIGPROJ" > /dev/null
rc=0
out=$(run_mig migrate --yes --scan "$MIGPROJ") || rc=$?
assert_exit_code "a second run exits 0" "$rc" "0"
assert_contains "a second run says there is nothing to do" "$out" "Nothing to migrate"
assert_contains "a second run says where the data is" "$out" "already lives in"
assert_eq "a second run leaves the links alone" \
  "$(readlink "$MIGPROJ/.mcp.json")" "$MIGNEW/profiles/soc/mcp.json"

# A half-finished migration completes rather than jamming.
mk_legacy
mkdir -p "$MIGNEW/profiles"
mv "$MIGOLD/profiles/hermetic-mig" "$MIGNEW/profiles/hermetic-mig"
rc=0
out=$(run_mig migrate --yes) || rc=$?
assert_exit_code "a half-finished migration completes" "$rc" "0"
assert_file_exists "the remaining profile moves" "$MIGNEW/profiles/soc/mcp.json"

# ============================================================
printf "\n\033[1m=== migrate: never destroys ===\033[0m\n"
# ============================================================

mk_legacy
mk_legacy_project "$MIGPROJ"
# An EMPTY destination directory is the dangerous case: mv silently replaces
# one, so "never overwrite" has to be an explicit test, not mv's own behaviour.
mkdir -p "$MIGNEW/profiles/soc"
rc=0
out=$(run_mig migrate --yes --scan "$MIGPROJ") || rc=$?
assert_exit_code "a collision exits non-zero" "$rc" "1"
assert_contains "a collision is reported by name" "$out" "SKIPPED profiles/soc"
assert_contains "a collision explains itself" "$out" "already exists"
assert_contains "a collision says what to do" "$out" "Rename or remove"
assert_file_exists "a collision leaves the original untouched" "$MIGOLD/profiles/soc/mcp.json"
assert_eq "a collision does not overwrite the destination" \
  "$(ls "$MIGNEW/profiles/soc" | wc -l | tr -d ' ')" "0"
# One item failing must not strand the others.
assert_file_exists "the other items still move" "$MIGNEW/profiles/hermetic-mig"
assert_file_exists "the accounts still move" "$MIGNEW/accounts/work/.claude.json"
# And the blocked profile's links must not be pointed at data that is not there.
assert_eq "a blocked profile's links keep pointing at the working copy" \
  "$(readlink "$MIGPROJ/.mcp.json")" "$MIGOLD/profiles/soc/mcp.json"
assert_contains "the untouched links are reported" "$out" "left alone"
assert_eq "nothing is left dangling after a collision" "$(count_dangling "$MIGPROJ")" "0"

# Resolving the collision and re-running finishes the job.
rmdir "$MIGNEW/profiles/soc" || true
rc=0
out=$(run_mig migrate --yes --scan "$MIGPROJ") || rc=$?
assert_exit_code "re-running after fixing the collision succeeds" "$rc" "0"
assert_file_exists "the blocked profile finally moves" "$MIGNEW/profiles/soc/mcp.json"
assert_eq "and its links are repointed" \
  "$(readlink "$MIGPROJ/.mcp.json")" "$MIGNEW/profiles/soc/mcp.json"

# ============================================================
printf "\n\033[1m=== migrate: the plan tells the truth ===\033[0m\n"
# ============================================================
#
# --dry-run exists so the user can see what will happen BEFORE typing
# 'migrate'. A plan that lists a move the act phase is certain to refuse is
# therefore not a cosmetic problem: it is the one output whose entire job is
# to be accurate. The collision is knowable up front — [ -e ] on the
# destination — so the plan has to say so.

mk_legacy
mkdir -p "$MIGNEW/profiles/soc"
out=$(run_mig migrate --dry-run)
assert_contains "the plan flags a destination that already exists" "$out" "CONFLICT"
assert_contains "the plan names the conflicting item" "$out" "profiles/soc *-> CONFLICT"
assert_contains "the plan counts the conflicts" "$out" "CONFLICTS: 1 of the items"
assert_contains "the plan says how to resolve it" "$out" "Rename or remove"
assert_contains "the plan says the rest still moves" "$out" "Everything else still moves"
# An item that CAN move must not be dressed up as a conflict.
assert_contains "a clean item is still shown as a plain move" "$out" \
  "profiles/hermetic-mig *-> $MIGNEW/profiles/hermetic-mig"
# And the plan must not act.
assert_file_exists "the plan changes nothing" "$MIGOLD/profiles/soc/mcp.json"

rm -rf "$MIGNEW/profiles/soc"
out=$(run_mig migrate --dry-run)
assert_not_contains "no conflict, no conflict banner" "$out" "CONFLICT"

# The summary must not claim a move that did not happen. With every
# destination occupied, stdout on its own used to read as a success — the
# contradiction was on stderr only, so `claudio migrate 2>/dev/null` sent the
# user away believing their data had moved when none of it had.
mk_legacy
mkdir -p "$MIGNEW/profiles/soc" "$MIGNEW/profiles/hermetic-mig" \
         "$MIGNEW/accounts/work" "$MIGNEW/accounts/spare"
printf 'account=other\n' > "$MIGNEW/claudio.conf"
rc=0
out=$(CLAUDIO_PROFILES="$MIGNEW/profiles" CLAUDIO_ACCOUNTS="$MIGNEW/accounts" \
      CLAUDIO_CONF="$MIGNEW/claudio.conf" CLAUDIO_LEGACY="$MIGOLD" \
      CLAUDIO_HOST=testhost CLAUDIO_DOCKER="$DOCKER_STUB" EDITOR=true \
      CLAUDIO_USER_SETTINGS="$USER_SETTINGS" \
      CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
      CLAUDIO_UPDATE=0 CLAUDIO_GIT="$GIT_STUB" \
      "$TEST_SH" "$CLAUDIO_UNDER_TEST" migrate --yes 2>/dev/null) || rc=$?
assert_exit_code "a wholly refused migration exits non-zero" "$rc" "1"
assert_contains "stdout alone says the migration is incomplete" "$out" "INCOMPLETE"
assert_contains "stdout alone says to run it again" "$out" "again to finish"
assert_not_contains "stdout does not claim the profiles moved" "$out" \
  "profiles now live in"
assert_not_contains "stdout does not claim the accounts moved" "$out" \
  "accounts now live in"
assert_file_exists "and nothing did move" "$MIGOLD/profiles/soc/mcp.json"

# ============================================================
printf "\n\033[1m=== migrate: paths with a tab in them ===\033[0m\n"
# ============================================================
#
# The plan is a tab-separated table and nearly every field in it is a path, so
# a tab in a path is not an exotic curiosity — it is a delimiter collision.
# Demonstrated damage, before this was fixed: a symlink named "notes<TAB>x.md"
# was read back as the link "notes", and the repoint did `rm -f notes` — it
# deleted an unrelated file of the user's, wrote a junk symlink over it, and
# exited 0. Every field that can hold a tab is now either the last field on
# its line (so it survives the split) or on a line of its own.

TABCH=$(printf '\t')

# (a) A tab in the LINK's own name, inside an ordinarily-named project. This
# is the destructive case: the link path was truncated at the tab and rm -f
# struck whatever sat at the shortened name.
mk_legacy
mk_legacy_project "$MIGPROJ"
printf 'do not delete me\n' > "$MIGPROJ/notes"
ln -s "$MIGOLD/profiles/soc/CLAUDE.md" "$MIGPROJ/notes${TABCH}link.md"
rc=0
out=$(run_mig migrate --yes --scan "$MIGPROJ") || rc=$?
assert_exit_code "a tab in a link name does not fail the run" "$rc" "0"
assert_eq "an unrelated file at the truncated name survives" \
  "$(cat "$MIGPROJ/notes" 2>/dev/null)" "do not delete me"
assert_eq "it is still a regular file, not a junk symlink" \
  "$(if [ -L "$MIGPROJ/notes" ]; then echo link; else echo file; fi)" "file"
assert_eq "the tab-named link is repointed, not skipped" \
  "$(readlink "$MIGPROJ/notes${TABCH}link.md")" "$MIGNEW/profiles/soc/CLAUDE.md"
assert_eq "nothing is left dangling after a tab-named link" \
  "$(count_dangling "$MIGPROJ")" "0"

# (b) A tab in the PROJECT DIRECTORY's name. This one was silently skipped:
# the whole project vanished from the work list and its links were left
# pointing at data that had moved, with a zero exit status and no warning.
mk_legacy
MIGTAB="$TMP/mig tab${TABCH}project"
mk_legacy_project "$MIGTAB"
rc=0
out=$(run_mig migrate --yes --scan "$MIGTAB") || rc=$?
assert_exit_code "a tab in a project path does not fail the run" "$rc" "0"
assert_eq "a project whose own path holds a tab is re-linked" \
  "$(readlink "$MIGTAB/.mcp.json")" "$MIGNEW/profiles/soc/mcp.json"
assert_eq "including the link CONFIG_MAP never mentions" \
  "$(readlink "$MIGTAB/.mcp.json.bak")" "$MIGNEW/profiles/soc/mcp.json"
assert_eq "nothing is left dangling in a tab-named project" \
  "$(count_dangling "$MIGTAB")" "0"
assert_eq "and the link that is not claudio's is untouched" \
  "$(readlink "$MIGTAB/.git-profile")" "$TMP/outside-target"
# The plan has to name it too, or --dry-run would under-report the work.
mk_legacy
mk_legacy_project "$MIGTAB"
out=$(run_mig migrate --dry-run --scan "$MIGTAB")
assert_contains "the plan lists a tab-named project" "$out" "mig tab"
assert_contains "the plan counts its links, not zero" "$out" "5 links"

# A profile directory whose NAME holds a tab has no safe encoding at all —
# every field of its plan line is a path. claudio can never create one
# (_validate_name rejects whitespace), so refusing is right; refusing
# SILENTLY is not, because the profile would sit in the old location for ever.
mk_legacy
mkdir -p "$MIGOLD/profiles/od${TABCH}d"
printf '{}\n' > "$MIGOLD/profiles/od${TABCH}d/mcp.json"
rc=0
out=$(run_mig migrate --yes) || rc=$?
assert_contains "a tab in a profile name is reported, not mangled" "$out" \
  "its name contains a tab"
assert_contains "and the fix is named" "$out" "rename it"
assert_file_exists "the tab-named profile is left where it was" \
  "$MIGOLD/profiles/od${TABCH}d/mcp.json"
assert_file_exists "the well-named profiles still move" "$MIGNEW/profiles/soc/mcp.json"
rm -rf "$MIGOLD/profiles/od${TABCH}d"

# A symlink whose name contains a NEWLINE cannot be handled at all: find
# reports it as fragments. Nothing can repoint it — but it must not be passed
# over without a word, which is the whole promise of this command.
mk_legacy
mk_legacy_project "$MIGPROJ"
ln -s "$MIGOLD/profiles/soc/CLAUDE.md" "$MIGPROJ/$(printf 'two\nlines').md"
out=$(run_mig migrate --dry-run --scan "$MIGPROJ")
assert_contains "a newline-named link is warned about, not skipped in silence" \
  "$out" "name contains a newline"
rm -f "$MIGPROJ/$(printf 'two\nlines').md"

# ============================================================
printf "\n\033[1m=== migrate: --undo ===\033[0m\n"
# ============================================================

mk_legacy
mk_legacy_project "$MIGPROJ"
run_mig migrate --yes --scan "$MIGPROJ" > /dev/null

# The payoff is real and non-obvious, so undo has to state it: the Keychain
# name is a pure function of the path, so moving the accounts back makes the
# logins migrate invalidated work again. Asserted while there is still
# something to undo — afterwards the plan is empty and says so instead.
out=$(FAKE_UNAME_S=Darwin PATH="$UNAME_DIR:$PATH" run_mig migrate --undo --dry-run)
assert_contains "undo explains that it restores the logins" "$out" "restores their original entry"

rc=0
out=$(run_mig migrate --undo --yes --scan "$MIGPROJ") || rc=$?
assert_exit_code "undo exits 0" "$rc" "0"
assert_file_exists "undo moves the profile back" "$MIGOLD/profiles/soc/mcp.json"
assert_file_exists "undo moves the accounts back" "$MIGOLD/accounts/work/.claude.json"
assert_file_exists "undo moves the conf back" "$MIGOLD/claudio.conf"
assert_file_not_exists "undo leaves nothing behind at the new path" "$MIGNEW/profiles/soc"
assert_eq "undo repoints the links back" \
  "$(readlink "$MIGPROJ/.mcp.json")" "$MIGOLD/profiles/soc/mcp.json"
assert_eq "undo leaves nothing dangling" "$(count_dangling "$MIGPROJ")" "0"
assert_contains "undo says how to keep the old locations" "$out" "CLAUDIO_PROFILES="

# ============================================================
printf "\n\033[1m=== migrate: edges ===\033[0m\n"
# ============================================================

mk_legacy
rc=0
out=$(run_mig migrate --bogus) || rc=$?
assert_contains "an unknown option is named" "$out" "Unknown option: --bogus"
assert_contains "an unknown option lists the valid ones" "$out" "--dry-run"
assert_exit_code "an unknown option exits 1" "$rc" "1"

out=$(run_mig migrate --dry-run --scan "$TMP/does-not-exist")
assert_contains "a missing scan root warns rather than failing" "$out" "scan root is not a directory"

# Nothing to migrate at all.
rm -rf "$MIGOLD"; mkdir -p "$MIGOLD"
rc=0
out=$(run_mig migrate --yes) || rc=$?
assert_exit_code "an empty old location exits 0" "$rc" "0"
assert_contains "an empty old location says so" "$out" "Nothing to migrate"

# Old and new configured to the same place: a no-op, not an infinite shuffle.
rc=0
out=$(CLAUDIO_PROFILES="$MIGOLD/profiles" CLAUDIO_ACCOUNTS="$MIGOLD/accounts" \
      CLAUDIO_CONF="$MIGOLD/claudio.conf" CLAUDIO_LEGACY="$MIGOLD" \
      CLAUDIO_USER_SETTINGS="$USER_SETTINGS" \
      CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
      CLAUDIO_UPDATE=0 CLAUDIO_GIT="$GIT_STUB" \
      "$TEST_SH" "$CLAUDIO_UNDER_TEST" migrate --yes 2>&1) || rc=$?
assert_exit_code "same source and destination exits 0" "$rc" "0"
assert_contains "same source and destination is refused politely" "$out" "same path"

# Hermeticity, stated as a test rather than trusted: the migration paths must
# reach nothing outside $TMP. hermetic-mig is a name only this suite uses.
mk_legacy
run_mig migrate --yes > /dev/null
assert_file_exists "migrate writes into the configured destination" "$MIGNEW/profiles/hermetic-mig"
assert_file_not_exists "migrate does not touch the real ~/.claudio" "$HOME/.claudio/profiles/hermetic-mig"
assert_file_not_exists "migrate does not touch the real ~/.claude" "$HOME/.claude/profiles/hermetic-mig"
assert_file_not_exists "migrate does not create a real accounts dir" "$HOME/.claudio/accounts/work"

# ============================================================
printf "\n\033[1m=== migrate: the hints that lead you here ===\033[0m\n"
# ============================================================
#
# After the move, every profile lookup fails and `clean` silently removes
# nothing. Those are the three moments the hint has to appear.

mk_legacy
out=$(run_mig list)
assert_contains "list reports no profiles at the new path" "$out" "No profiles found"
assert_contains "list points at the data in the old location" "$out" "Found claudio data in"
assert_contains "list names the command that fixes it" "$out" "claudio migrate"

out=$(run_mig use soc 2>&1 || true)
assert_contains "a missing profile is reported" "$out" "not found"
assert_contains "a missing profile hints at the migration" "$out" "claudio migrate"

# ...and it stops once the data has moved, rather than nagging forever. A
# per-item move leaves the old profiles/ directory behind, empty.
run_mig migrate --yes > /dev/null
assert_is_dir "the old directory is left in place, emptied" "$MIGOLD/profiles"
out=$(run_mig use nosuchprofile 2>&1 || true)
assert_not_contains "the hint stops once there is nothing left to move" "$out" "Found claudio data"

# clean, over a project whose links claudio no longer recognises.
mk_legacy
mk_legacy_project "$MIGPROJ"
run_mig migrate --yes > /dev/null      # no --scan: the links are left stale
out=$(cd "$MIGPROJ" && run_mig clean)
assert_contains "clean admits when it removed nothing" "$out" "no claudio symlinks found to remove"
assert_contains "clean names the stale links" "$out" ".mcp.json"
assert_contains "clean says how to repoint them" "$out" "migrate --scan"
# -L, not -e: after the move these links are dangling. That they are still
# present is the point — clean must not delete what it does not recognise.
assert_is_symlink "clean does not delete links it does not own" "$MIGPROJ/.mcp.json.bak"

# ...and the recovery it suggests works, even though clean deleted the marker.
out=$(cd "$MIGPROJ" && run_mig migrate --yes --scan .)
assert_eq "migrate --scan . repoints a project with no marker left" \
  "$(readlink "$MIGPROJ/.mcp.json")" "$MIGNEW/profiles/soc/mcp.json"
assert_eq "recovery leaves nothing dangling" "$(count_dangling "$MIGPROJ")" "0"

# ============================================================
printf "\n\033[1m=== migrate: the platform the login advice is for ===\033[0m\n"
# ============================================================
#
# CLAUDE.md is honest that the non-Darwin advice has never been run on Linux:
# the macOS half was observed, the other half is Claude Code's documented
# behaviour. The code path can still be exercised from here, and the half that
# had no assertion at all is the RESULT -- the plan's wording was pinned, but
# nothing checked that a migration on such a platform stays quiet about logins.
# It matters in the direction that costs the user something: being told to run
# `claudio account login` when the credential moved with the directory sends
# somebody to re-authenticate an account that never lost its session.
#
# This is the last of the migrate tests on purpose. `mk_legacy` clears both
# trees, so a fresh migration anywhere earlier would pull the ground out from
# under the section after it.
mk_legacy
MIGPROJ_LX="$TMP/mig-project-elsewhere"
mk_legacy_project "$MIGPROJ_LX"
rc=0
out=$(FAKE_UNAME_S=Linux PATH="$UNAME_DIR:$PATH" run_mig migrate --yes --scan "$MIGPROJ_LX") || rc=$?
assert_exit_code "a migration elsewhere exits 0" "$rc" "0"
assert_contains "a migration elsewhere still reports what moved" "$out" "^Moved:"
assert_file_exists "the account still moves" "$MIGNEW/accounts/work/.claude.json"
assert_not_contains "elsewhere the result never asks for a re-login" "$out" "account login"
assert_contains "...and the plan said why: the credential moved with it" "$out" "No re-login needed"
# The marker is macOS's alone: it is what `_mg_login_result` reads, so writing
# it on a platform that never reports one would be a file nothing ever removes.
assert_file_not_exists "elsewhere no re-login marker is written" \
  "$MIGNEW/accounts/work/.claudio-relogin"

# ============================================================
printf "\n\033[1m=== a trailing slash on a storage path ===\033[0m\n"
# ============================================================
#
# Every symlink test is a literal prefix match on readlink output, so an
# unnormalised trailing slash used to build links under /p/ and then compare
# against "/p//*" — `clean` reported success having removed nothing.

# The failure is ASYMMETRIC, which is what made it survive: activate with the
# path written one way, clean with it written the other, and clean matches
# nothing. One exported trailing slash in a shell profile is enough.
SLASHDIR="$TMP/slash-profiles"
rm -rf "$SLASHDIR" "$TMP/slash-project"
mkdir -p "$SLASHDIR" "$TMP/slash-project"

slashrun() {
  # $1 = the profiles dir exactly as written; the rest is the command.
  _sp=$1; shift
  CLAUDIO_PROFILES="$_sp" CLAUDIO_ACCOUNTS="$ACCOUNTS" CLAUDIO_CONF="$CONF" \
  CLAUDIO_LEGACY="$LEGACY" CLAUDIO_DOCKER="$DOCKER_STUB" CLAUDIO_HOST=testhost \
  CLAUDIO_USER_SETTINGS="$USER_SETTINGS" \
  CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
  CLAUDIO_UPDATE=0 CLAUDIO_GIT="$GIT_STUB" \
  "$TEST_SH" "$CLAUDIO_UNDER_TEST" "$@" 2>&1
}

slashrun "$SLASHDIR" new slashy --manual > /dev/null
cd "$TMP/slash-project"
slashrun "$SLASHDIR" use slashy > /dev/null
assert_is_symlink "activation without a trailing slash links" ".mcp.json"
out=$(slashrun "$SLASHDIR/" clean)
assert_contains "cleaning WITH a trailing slash still owns the links" "$out" "removed .mcp.json"
assert_not_contains "and does not report an empty removal" "$out" "no claudio symlinks"
assert_file_not_exists "a trailing slash does not strand the links" ".mcp.json"

# ...and the other way round.
slashrun "$SLASHDIR/" use slashy > /dev/null
assert_is_symlink "activation WITH a trailing slash links" ".mcp.json"
out=$(slashrun "$SLASHDIR" clean)
assert_contains "cleaning without one still owns those links" "$out" "removed .mcp.json"
assert_file_not_exists "no links stranded either way round" ".mcp.json"
cd "$TMP"

# ============================================================
printf "\n\033[1m=== update: version and the kill switch ===\033[0m\n"
# ============================================================
#
# Everything in this file except run_upd runs with CLAUDIO_UPDATE=0, so the
# 500-odd tests above cannot have their output changed by an update notice and
# cannot reach the network even if the stub were removed.

VER=$(sed -n 's/^VERSION=//p' "$CLAUDIO" | head -1)
stamp() { cat "$UPDSTAMP" 2>/dev/null || true; }

TESTS=$((TESTS + 1))
if [ -n "$VER" ]; then
  pass "claudio embeds a VERSION"
else
  fail "claudio embeds a VERSION" "no VERSION= line in $CLAUDIO"
fi

out=$(run --version)
assert_eq "--version prints the embedded version" "$out" "$VER"
out=$(run -V)
assert_eq "-V is the same flag" "$out" "$VER"

out=$(run update)
assert_contains "CLAUDIO_UPDATE=0 disables update entirely" "$out" "disabled (CLAUDIO_UPDATE=0)"

: > "$GIT_LOG"
out=$(cd "$UPDPROJ" && run run)
assert_eq "a disabled run never invokes git" "$(wc -c < "$GIT_LOG" | tr -d ' ')" "0"
assert_file_not_exists "a disabled run writes no stamp" "$CONF.update-stamp"
assert_file_not_exists "and none beside the shared conf either" "$TMP/update-stamp"

# ============================================================
printf "\n\033[1m=== update --check ===\033[0m\n"
# ============================================================

rm -f "$UPDSTAMP"; : > "$UPDCONF"; : > "$GIT_LOG"
printf 'v0.0.1\n' > "$GIT_TAGS"
out=$(run_upd update --check)
assert_contains "update reports the running version" "$out" "^claudio $VER"
assert_contains "an older newest tag means up to date" "$out" "Up to date"
assert_contains "update asks git for the tags" "$(cat "$GIT_LOG")" "ls-remote"
assert_file_not_exists "--check changes nothing on disk" "$UPDSTAMP"

# Newest wins, and the comparison is numeric per component rather than
# lexical: 0.10.0 is newer than 0.9.0, which a string compare gets backwards.
printf 'v0.9.0\nv0.10.0\nv0.2.0\nnightly\n' > "$GIT_TAGS"
out=$(run_upd update --check)
assert_contains "the newest tag is chosen numerically, not lexically" "$out" "v0.10.0 is available"
assert_not_contains "an older tag is not the one reported" "$out" "v0.9.0 is available"
assert_not_contains "a non-version tag is ignored" "$out" "nightly"
assert_contains "an available update says where to get it" "$out" "Get it from"

# The state the repo is actually in today: no tags at all.
: > "$GIT_TAGS"
out=$(run_upd update --check)
assert_contains "no tags is reported as no tags" "$out" "No releases are tagged yet"
assert_not_contains "no tags never claims you are up to date" "$out" "Up to date"

# A plain `update` is a real check, so it records one — and records that the
# answer has been seen, or `run` would repeat it at the next launch.
printf 'v9.9.9\n' > "$GIT_TAGS"
out=$(run_upd update)
assert_contains "a newer tag is announced" "$out" "v9.9.9 is available"
assert_file_exists "a plain update stamps the check" "$UPDSTAMP"
assert_contains "the stamp records what was found" "$(stamp)" "^latest=v9.9.9"
assert_contains "the stamp records that it has been said" "$(stamp)" "^notified=v9.9.9"
assert_contains "the stamp records when" "$(stamp)" "^checked=[0-9][0-9]*"

rc=0
out=$(run_upd update --nonsense) || rc=$?
assert_exit_code "an unknown update option exits 1" "$rc" "1"
assert_contains "an unknown update option names the valid one" "$out" "Valid: --check"

# ============================================================
printf "\n\033[1m=== update: the three states of update_check ===\033[0m\n"
# ============================================================

# Unset: offered once, to stderr, without prompting and without fetching.
rm -f "$UPDSTAMP"; : > "$UPDCONF"; : > "$GIT_LOG"
out=$(cd "$UPDPROJ" && run_upd run)
assert_contains "an un-offered user is offered the check" "$out" "claudio can check for a newer version"
assert_contains "the offer spells out yes" "$out" "update_check=1"
assert_contains "the offer spells out never" "$out" "update_check=0"
assert_eq "the offer never fetches" "$(wc -c < "$GIT_LOG" | tr -d ' ')" "0"
assert_contains "the offer is recorded in the stamp" "$(stamp)" "^offered=1"

out=$(cd "$UPDPROJ" && run_upd run)
assert_not_contains "the offer is made once, not once per run" "$out" "can check for a newer version"

# Declined: silent forever, and still no fetch.
printf 'update_check=0\n' > "$UPDCONF"
rm -f "$UPDSTAMP"; : > "$GIT_LOG"
out=$(cd "$UPDPROJ" && run_upd run)
assert_not_contains "update_check=0 is never mentioned again" "$out" "can check for a newer version"
assert_not_contains "update_check=0 shows no notice" "$out" "is available"
assert_eq "update_check=0 never fetches" "$(wc -c < "$GIT_LOG" | tr -d ' ')" "0"
assert_file_not_exists "update_check=0 writes no stamp" "$UPDSTAMP"

# Enabled, with a fresh stamp that already knows about a newer release. The
# notice comes from the stamp alone: `run` never waits on the network.
printf 'update_check=1\n' > "$UPDCONF"
NOW=$(date +%s)
printf 'checked=%s\nlatest=v9.9.9\nnotified=\noffered=1\n' "$NOW" > "$UPDSTAMP"
: > "$GIT_LOG"
out=$(cd "$UPDPROJ" && run_upd run)
assert_contains "a known newer version is announced at launch" "$out" "claudio v9.9.9 is available"
assert_contains "the notice says which version you have" "$out" "you have $VER"
assert_contains "the notice carries its own off switch" "$out" "update_check=0"
assert_eq "a fresh stamp means run does not fetch at all" "$(wc -c < "$GIT_LOG" | tr -d ' ')" "0"
assert_contains "the notice is recorded against that version" "$(stamp)" "^notified=v9.9.9"

out=$(cd "$UPDPROJ" && run_upd run)
assert_not_contains "the notice fires once per version, not once per run" "$out" "is available"

# ...and a tag that is not newer than what is installed says nothing at all.
printf 'checked=%s\nlatest=v0.0.1\nnotified=\noffered=1\n' "$NOW" > "$UPDSTAMP"
out=$(cd "$UPDPROJ" && run_upd run)
assert_not_contains "an older release is not announced" "$out" "is available"

# A corrupt stamp must not be able to break a launch: $((now - "soon")) is a
# fatal error in dash, not a zero.
printf 'checked=soon\nlatest=\nnotified=\noffered=1\n' > "$UPDSTAMP"
rc=0
out=$(cd "$UPDPROJ" && run_upd run) || rc=$?
assert_exit_code "a corrupt stamp does not break run" "$rc" "0"
assert_contains "a corrupt stamp still launches claude" "$out" "\[claude\]"

# ============================================================
printf "\n\033[1m=== update: the detached refresh ===\033[0m\n"
# ============================================================
#
# checked=0 is "never checked", so this run is due one. The refresh must land
# in the background: its output must not appear in the capture (a child that
# inherits stdout has its output swallowed into $( ) — and blocks it), and its
# write must complete after the parent has gone.

printf 'update_check=1\n' > "$UPDCONF"
printf 'v9.9.9\n' > "$GIT_TAGS"

# THE TRIGGER IS RETRIED, and the reason is one race further on than the
# `wait_for_match` comment above. A refresh is spawned only when the stamp is
# stale, so a straggler from an earlier section that lands between this block's
# `checked=0` and claudio's read of it makes the stamp fresh: this run then
# correctly declines to refresh, `wait_for_stamp` is satisfied by the
# straggler's own write, and the git log stays empty for ever. Waiting longer
# cannot fix that -- there is nothing left to wait for. Observed on Linux,
# where the whole suite runs fast enough for the overlap to be ordinary.
#
# Three attempts, each resetting both files. This hides no regression: a
# refresh that never runs fails the assertions below just the same, three times
# over instead of once.
_ref_try=0
while [ "$_ref_try" -lt 3 ]; do
  _ref_try=$((_ref_try + 1))
  printf 'checked=0\nlatest=\nnotified=\noffered=1\n' > "$UPDSTAMP"
  : > "$GIT_LOG"
  out=$(cd "$UPDPROJ" && run_upd run)
  if wait_for_match "$GIT_LOG" 'ls-remote'; then break; fi
done
assert_not_contains "the background refresh does not leak into run's output" "$out" "refs/tags"
assert_contains "run still launches claude" "$out" "\[claude\]"

TESTS=$((TESTS + 1))
if wait_for_stamp '^latest=v9.9.9'; then
  pass "a stale stamp triggers a refresh that lands after the parent execs"
else
  fail "a stale stamp triggers a refresh that lands after the parent execs" "stamp: $(stamp)"
fi
assert_contains "the refresh records when it ran" "$(stamp)" "^checked=[1-9]"
assert_contains "the refresh went through git" "$(cat "$GIT_LOG")" "ls-remote"

# ...and it does not make `run` wait. A git that takes three seconds must cost
# the launch nothing: if the fork were not fully redirected, $( ) would block
# on it and this would measure three seconds or more.
SLOWGIT="$TMP/git-slow"
cat > "$SLOWGIT" <<STUB
#!/bin/sh
sleep 3
exec "$GIT_STUB" "\$@"
STUB
chmod +x "$SLOWGIT"
printf 'checked=0\nlatest=\nnotified=\noffered=1\n' > "$UPDSTAMP"
T0=$(date +%s)
out=$(cd "$UPDPROJ" && CLAUDIO_PROFILES="$PROFILES" CLAUDIO_ACCOUNTS="$ACCOUNTS" \
      CLAUDIO_CONF="$UPDCONF" CLAUDIO_LEGACY="$LEGACY" CLAUDIO_HOST=testhost \
      CLAUDIO_USER_SETTINGS="$USER_SETTINGS" CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
      CLAUDIO_GIT="$SLOWGIT" CLAUDIO_DOCKER="$DOCKER_STUB" EDITOR=true \
      "$TEST_SH" "$CLAUDIO_UNDER_TEST" run 2>&1)
T1=$(date +%s)
assert_contains "the slow-git run still launches claude" "$out" "\[claude\]"
TESTS=$((TESTS + 1))
if [ "$((T1 - T0))" -lt 3 ]; then
  pass "run does not wait on the update check ($((T1 - T0))s against a 3s git)"
else
  fail "run does not wait on the update check" "took $((T1 - T0))s — the refresh is blocking"
fi
# Reap the slow child before the suite's temp dir is removed under it.
wait_for_stamp '^latest=v9.9.9' || true

# ============================================================
printf "\n\033[1m=== update: Homebrew installs defer to brew ===\033[0m\n"
# ============================================================
#
# Detected from the path, never by invoking brew: `brew --prefix` is Ruby and
# takes ~2.9s to start, against 0.007s to read the Cellar directory name.

BREWPFX="$TMP/brewprefix"
rm -rf "$BREWPFX"
mkdir -p "$BREWPFX/Cellar/claudio/1.2.0/bin" "$BREWPFX/bin"
cp "$CLAUDIO_UNDER_TEST" "$BREWPFX/Cellar/claudio/1.2.0/bin/claudio"
chmod +x "$BREWPFX/Cellar/claudio/1.2.0/bin/claudio"
ln -s ../Cellar/claudio/1.2.0/bin/claudio "$BREWPFX/bin/claudio"

brewrun() {
  # $1 = the claudio to execute; the rest is the command.
  _bb=$1; shift
  CLAUDIO_PROFILES="$PROFILES" CLAUDIO_ACCOUNTS="$ACCOUNTS" CLAUDIO_CONF="$UPDCONF" \
  CLAUDIO_LEGACY="$LEGACY" CLAUDIO_HOST=testhost \
  CLAUDIO_USER_SETTINGS="$USER_SETTINGS" CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
  CLAUDIO_GIT="$GIT_STUB" CLAUDIO_DOCKER="$DOCKER_STUB" EDITOR=true \
  "$TEST_SH" "$_bb" "$@" 2>&1
}

: > "$GIT_LOG"
out=$(brewrun "$BREWPFX/Cellar/claudio/1.2.0/bin/claudio" update --check)
assert_contains "a Cellar path is recognised as a Homebrew install" "$out" "Installed with Homebrew"
assert_contains "the installed version is read from the Cellar directory" "$out" "Cellar/claudio/1.2.0"
assert_eq "a Homebrew install never asks git for tags" "$(wc -c < "$GIT_LOG" | tr -d ' ')" "0"
assert_contains "with no tap readable it says so honestly" "$out" "Cannot read a claudio formula"
assert_contains "and names the command that would know" "$out" "brew update && brew upgrade claudio"

# A tap offering something newer.
mkdir -p "$BREWPFX/Library/Taps/gabrielbelli/homebrew-tap"
cat > "$BREWPFX/Library/Taps/gabrielbelli/homebrew-tap/claudio.rb" <<'RB'
class Claudio < Formula
  desc "Claude Code profile and account manager"
  homepage "https://github.com/gabrielbelli/claudio"
  url "https://github.com/gabrielbelli/claudio/archive/refs/tags/v1.4.0.tar.gz"
  version "1.4.0"
end
RB
out=$(brewrun "$BREWPFX/Cellar/claudio/1.2.0/bin/claudio" update --check)
assert_contains "the tap's version is read from the formula" "$out" "The tap offers 1.4.0"
assert_contains "and brew is named as the way to get it" "$out" "brew upgrade claudio"

# ...through the bin/ symlink too, which is how brew actually puts it on PATH.
# This is the readlink hop plus the /opt/homebrew/bin/.. that pwd -P collapses.
out=$(brewrun "$BREWPFX/bin/claudio" update --check)
assert_contains "the shim symlink resolves back to the Cellar" "$out" "Installed with Homebrew"
assert_contains "and reads the same tap version through it" "$out" "The tap offers 1.4.0"

# ------------------------------------------------------------
# ...and the status line registered THROUGH that symlink must not name the
# Cellar, which is where `_self_path` would land it.
#
# `/opt/homebrew/bin/claudio` is a symlink into
# `.../Cellar/claudio/<version>/bin/claudio`, so the resolved path carries a
# VERSION, and `brew upgrade` deletes that directory. Reproduced against the
# resolving version: after the upgrade the registered command was
# `sh: .../Cellar/claudio/0.1.0/bin/claudio: No such file or directory`, exit
# 127 -- Claude Code draws nothing and plan-usage recording stops for good,
# while `account list` still prints `shim`, because `_settings_has_shim`
# matches `statusline --shim` WITHOUT the path in front of it and must.
# Silent, permanent, and invisible on the one surface built to show drift.
#
# So: the entry point is registered and the target is not. Asserted both ways
# -- the Cellar must be absent as well as the link present -- because a
# contains-check on the link alone passes for a path that contains both.
# ------------------------------------------------------------
BREWACCT="$ACCOUNTS/brewshim"
rm -rf "$BREWACCT"; mkdir -p "$BREWACCT"
out=$(brewrun "$BREWPFX/bin/claudio" statusline --set brewshim)
assert_contains "the shim is registered when set through a brew symlink" \
  "$out" "statusline --shim"
brewcmd=$(sed -n 's/.*"command": *"\(.*\)".*/\1/p' "$BREWACCT/settings.json" | head -1)
# The PHYSICAL directory, because that is what claudio registers: `pwd -P`
# resolves symlinked directories, and on macOS `$TMPDIR` sits under `/var`,
# which is a symlink to `/private/var`. Nothing about the brew layout does
# this -- `/opt/homebrew/bin` and `/usr/local/bin` are real directories -- but
# the test's own fixture does, so compare against the same normalisation
# rather than against the path the harness happens to hold.
brewphys=$(cd "$BREWPFX/bin" && pwd -P)
assert_contains "...at the stable entry point, not the versioned Cellar path" \
  "$brewcmd" "^$brewphys/claudio statusline --shim$"
assert_not_contains "...so a brew upgrade cannot delete the path it names" \
  "$brewcmd" "Cellar"

# The whole point, as a behaviour: upgrade the Cellar out from under it and the
# registered command still runs. A path assertion alone would pass for a link
# that pointed nowhere.
mkdir -p "$BREWPFX/Cellar/claudio/1.3.0/bin"
cp "$CLAUDIO_UNDER_TEST" "$BREWPFX/Cellar/claudio/1.3.0/bin/claudio"
chmod +x "$BREWPFX/Cellar/claudio/1.3.0/bin/claudio"
rm -rf "$BREWPFX/Cellar/claudio/1.2.0"
rm -f "$BREWPFX/bin/claudio"
ln -s ../Cellar/claudio/1.3.0/bin/claudio "$BREWPFX/bin/claudio"
brew_rc=0
printf '{"session_id":"s"}' | CLAUDIO_PROFILES="$PROFILES" CLAUDIO_ACCOUNTS="$ACCOUNTS" \
  CLAUDIO_CONF="$UPDCONF" CLAUDIO_LEGACY="$LEGACY" CLAUDIO_UPDATE=0 \
  CLAUDIO_USER_SETTINGS="$USER_SETTINGS" CLAUDIO_MANAGED_SETTINGS="$MANAGED_SETTINGS" \
  "$TEST_SH" -c "$brewcmd" >/dev/null 2>&1 || brew_rc=$?
assert_exit_code "the registered shim still runs after a simulated brew upgrade" \
  "$brew_rc" 0
# Put the fixture back the way the rest of this section expects it.
mkdir -p "$BREWPFX/Cellar/claudio/1.2.0/bin"
cp "$CLAUDIO_UNDER_TEST" "$BREWPFX/Cellar/claudio/1.2.0/bin/claudio"
chmod +x "$BREWPFX/Cellar/claudio/1.2.0/bin/claudio"
rm -rf "$BREWPFX/Cellar/claudio/1.3.0"
rm -f "$BREWPFX/bin/claudio"
ln -s ../Cellar/claudio/1.2.0/bin/claudio "$BREWPFX/bin/claudio"
rm -rf "$BREWACCT"

# A tap that is not ahead must not tell you to upgrade.
cat > "$BREWPFX/Library/Taps/gabrielbelli/homebrew-tap/claudio.rb" <<'RB'
class Claudio < Formula
  version "1.2.0"
end
RB
out=$(brewrun "$BREWPFX/Cellar/claudio/1.2.0/bin/claudio" update --check)
assert_contains "a tap at the installed version reports it without a verdict" "$out" "The tap offers 1.2.0."
assert_not_contains "and does not tell you to upgrade" "$out" "Upgrade with"

# ...and a claudio outside a Cellar is not a brew install, however it is run.
: > "$GIT_TAGS"
out=$(run_upd update --check)
assert_not_contains "a plain install is not mistaken for Homebrew" "$out" "Installed with Homebrew"

# ============================================================
printf "\n\033[1m=== update: the network invariants ===\033[0m\n"
# ============================================================
#
# The layer that survives a refactor. A second fetch site added later would
# escape the stub and the kill switch alike, and the suite would still pass —
# unless it is asserted against the source that there is only one.

lsr=$(grep -n 'ls-remote' "$CLAUDIO" | grep -v '^[0-9]*: *#' || true)
assert_eq "claudio has exactly one ls-remote call site" \
  "$(printf '%s\n' "$lsr" | grep -c 'ls-remote' || true)" "1"
assert_contains "and it goes through the overridable git binary" "$lsr" 'GIT_BIN'

nets=$(grep -n -E '(^|[^-[:alnum:]_])(curl|wget|nc|ftp)[[:space:]]' "$CLAUDIO" | grep -v '^[0-9]*: *#' || true)
assert_eq "claudio reaches the network through git and nothing else" "$nets" ""

# Hermeticity, stated rather than trusted: the stamp follows CLAUDIO_CONF, so
# no test — and no background child of one — can write to the real ~/.claudio.
assert_file_exists "the stamp lands beside the configured conf" "$UPDSTAMP"
assert_eq "the update check never creates or touches the real ~/.claudio stamp" \
  "$(_real_stamp_state "$REAL_STAMP_CLAUDIO")" "$REAL_STAMP_CLAUDIO_BEFORE"
assert_eq "nor the one in the real ~/.claude" \
  "$(_real_stamp_state "$REAL_STAMP_CLAUDE")" "$REAL_STAMP_CLAUDE_BEFORE"

# ============================================================
printf "\n\033[1m=== usage mentions the new commands ===\033[0m\n"
# ============================================================

out=$(run --help)
assert_contains "usage leads with the first-run commands" "$out" "Start here:"
assert_contains "usage documents run" "$out" "run \[profile\]"
assert_contains "usage documents env" "$out" "env \[profile\]"
assert_contains "usage documents account" "$out" "account list"
assert_contains "usage documents default" "$out" "default \[--account <name>\]"
assert_not_contains "usage no longer advertises use -l" "$out" "\-\-launch"
assert_contains "usage documents --account" "$out" "--account <name>"
assert_contains "usage documents --tag" "$out" "--tag key=value"

# ------------------------------------------------------------
# `usage` and `recv`: the Python halves, reached as subcommands.
#
# They were `claude-usage` and `otlp-recv` on $PATH, which meant a single-file
# `sudo install` of claudio produced a tool that could not find either -- the
# receiver resolver fell through to a bare name that was never installed. One
# binary with subcommands removes the question: whatever finds `claudio` finds
# the rest, in a checkout, in libexec, or on $PATH.
# ------------------------------------------------------------
assert_contains "usage documents the usage subcommand" "$out" "usage \[args\]"
assert_contains "usage documents the recv subcommand" "$out" "recv \[args\]"

# Resolution, tested hermetically: the harness runs a COPY of claudio in a temp
# directory, so "beside the script" is a directory we control. Planting a stub
# there is the whole test -- it pins the order (`usage/<name>` first) and proves
# the verb execs what it found rather than a bare name from $PATH.
tool_dir=$(dirname "$CLAUDIO_UNDER_TEST")/usage
mkdir -p "$tool_dir/recv"
printf '#!/bin/sh\necho "[stub-usage]" "$@"\n' > "$tool_dir/claudio-usage"
printf '#!/bin/sh\necho "[stub-recv]" "$@"\n'  > "$tool_dir/recv/otlp-recv"
chmod 755 "$tool_dir/claudio-usage" "$tool_dir/recv/otlp-recv"

out2=$(run usage show --by account)
assert_contains "claudio usage execs the reader beside the script" "$out2" "\[stub-usage\]"
assert_contains "claudio usage passes its arguments through" "$out2" "show --by account"

# The two things `claudio usage` hands DOWN, and both were missing.
#
# CLAUDIO_SELF: `claudio usage doctor` asks claudio two questions -- `claudio
# env` for the stream A export and `claudio statusline` for whether recording
# is on -- and it asked them of a BARE NAME, i.e. of whichever claudio is first
# on $PATH. Resolution only runs the other way (claudio finds its Python and
# then handed nothing back), so a checkout's `./claudio usage doctor`
# interrogated /usr/local/bin/claudio: a different build reading a different
# config. Reproduced on this machine as `Recording plan usage: on` from one and
# `recording is OFF -- claudio reports nothing configured at all` from the
# other, naming the file that already held the line.
#
# CLAUDIO_USAGE_DIR: `usage_dir=` layers on this side and was read by the shim,
# by `_run_receiver` and by the receiver spawn -- and by nothing on the Python
# side, which looked only at the environment variable. So `usage_dir=/data`
# recorded into /data and `claudio usage show` read ~/.claudio-usage and
# printed "No requests recorded yet." over a full ledger.
printf '#!/bin/sh\necho "[stub-usage] SELF=$CLAUDIO_SELF DIR=$CLAUDIO_USAGE_DIR"\n' \
  > "$tool_dir/claudio-usage"
chmod 755 "$tool_dir/claudio-usage"
out2b=$(run usage doctor)
# An ABSOLUTE path to the script under test, matched by shape rather than by
# string: `_self_path` ends in `pwd -P`, so on macOS $TMP's /var/... becomes
# /private/var/... and an equality test would fail for the wrong reason. What
# is being pinned is that a path was handed down at all and that it names THIS
# claudio -- a bare name, or nothing, is the defect.
assert_contains "claudio usage hands down the claudio that is running" \
  "$out2b" "SELF=/.*/claudio-test\.sh"
# ...and with no usage_dir= anywhere, nothing is invented: both halves default
# to the same path, so an exported empty value would be a third answer.
assert_contains "with no usage_dir= configured, nothing is exported" \
  "$out2b" "DIR= *\$"

printf 'usage_dir=%s\n' "$TMP/elsewhere-usage" >> "$CONF"
out2c=$(run usage doctor)
assert_contains "claudio usage hands down the resolved usage_dir" \
  "$out2c" "DIR=$TMP/elsewhere-usage"
# The narrowest layer still wins: an explicit CLAUDIO_USAGE_DIR in the
# environment is not re-decided here, on either side.
out2d=$(CLAUDIO_USAGE_DIR="$TMP/from-env" run usage doctor)
assert_contains "an explicit CLAUDIO_USAGE_DIR is left alone" \
  "$out2d" "DIR=$TMP/from-env"
# usage_dir inside ~/.claude is refused however it is configured, and the
# refusal is SAID: claudio writes nothing there, so a reader pointed at it
# would report an empty machine and be believed.
sed -i.bak '/^usage_dir=/d' "$CONF"; rm -f "$CONF.bak"
printf 'usage_dir=%s\n' "$HOME/.claude/usage" >> "$CONF"
out2e=$(run usage doctor 2>&1)
assert_contains "a usage_dir inside ~/.claude is named, not passed on" \
  "$out2e" "inside ~/.claude"
assert_not_contains "...and is not exported" "$out2e" "DIR=$HOME/.claude"
sed -i.bak '/^usage_dir=/d' "$CONF"; rm -f "$CONF.bak"
printf '#!/bin/sh\necho "[stub-usage]" "$@"\n' > "$tool_dir/claudio-usage"
chmod 755 "$tool_dir/claudio-usage"

out3=$(run recv serve --port 1234)
assert_contains "claudio recv execs the receiver beside the script" "$out3" "\[stub-recv\]"
assert_contains "claudio recv passes its arguments through" "$out3" "serve --port 1234"

# `receiver=` in the global conf outranks the copy beside the script, because a
# packaged claudio and a checkout can both be present and the conf is the only
# statement of which one is meant.
printf '#!/bin/sh\necho "[stub-conf-recv]" "$@"\n' > "$TMP/conf-recv"
chmod 755 "$TMP/conf-recv"
printf 'receiver=%s\n' "$TMP/conf-recv" >> "$CONF"
out4=$(run recv serve)
assert_contains "receiver= in the conf beats the script's neighbour" "$out4" "\[stub-conf-recv\]"
# Two statements, not `a && b`: under `set -e` a trailing && as the last
# statement of a block returns its own status and aborts the suite.
sed -i.bak '/^receiver=/d' "$CONF"
rm -f "$CONF.bak"

# A tool that is not there is named, with a remedy. The last fallback is a bare
# name, and a bare name that is not installed is exactly what a single-file
# `sudo install` of claudio used to produce: a tool that could not find its own
# receiver, reported as an exec failure with no remedy in it.
rm -f "$tool_dir/recv/otlp-recv"
out5=$(run recv serve 2>&1) || :
assert_contains "a missing receiver is named" "$out5" "receiver not found"
assert_contains "a missing receiver names the remedy" "$out5" "receiver="
rm -rf "$tool_dir"
assert_contains "usage documents --no-telemetry" "$out" "--no-telemetry"
assert_contains "usage documents --" "$out" "passed to claude"
# ...and scopes it to `run`. The block header says "Options for run/env", but
# cmd_env launches nothing: it parses -- and then drops every token after it on
# the floor, exit 0, no diagnostic. The line used to promise "everything after
# this is passed to claude" under that header, which is simply false for env.
# Pinned on the -- line itself, not on the block, because the header is shared.
dashdoc=$(printf '%s\n' "$out" | grep '^  -- ' || true)
assert_contains "usage scopes -- to run, which is the only verb that launches" "$dashdoc" "run only"
# `default` shares _parse_run_opts with run/env, so it PARSES --no-telemetry and
# --, then ignores both: it only ever reads RUN_ACCOUNT and RUN_TAGS. The help
# text must not advertise a flag that silently does nothing, so the shared
# options block is scoped to run/env. Pinned because the drift is invisible from
# the outside — the flag is accepted, the exit status is 0, nothing is printed.
opts_hdr=$(printf '%s\n' "$out" | grep '^Options for ' || true)
assert_contains "usage scopes the shared options to run/env" "$opts_hdr" "run/env:"
assert_not_contains "usage does not offer default a flag it ignores" "$opts_hdr" "default"
assert_contains "usage documents --version" "$out" "^  --version"
# email= is the one automatic tag that is PII, so `claudio help` says so rather
# than leaving it to be discovered in an exported record.
assert_contains "usage names email= among the automatic tags" "$out" "email="
assert_contains "usage warns that email= is PII" "$out" "PII"
# The one key that turns recording on, named where a new user meets it. It
# used to say usage=1, which is the key being retired.
assert_contains "usage names the logging key, not the one it replaces" "$out" "logging=local"
# show grew the same [component] argument edit has, so the list covers both.
assert_contains "usage names the components for edit and show" "$out" "Components for edit/show:"

# Accounts before MCP, asserted because every other usage test is a
# contains-check and could not tell the difference. Accounts are the thing a
# new user needs second; MCP servers are the thing they need last.
acct_at=$(printf '%s\n' "$out" | grep -n '^Accounts (' | head -1 | cut -d: -f1 || true)
mcp_at=$(printf '%s\n' "$out" | grep -n '^MCP servers' | head -1 | cut -d: -f1 || true)
TESTS=$((TESTS + 1))
if [ -n "$acct_at" ] && [ -n "$mcp_at" ] && [ "$acct_at" -lt "$mcp_at" ]; then
  pass "usage puts Accounts above MCP servers"
else
  fail "usage puts Accounts above MCP servers" "Accounts at '$acct_at', MCP at '$mcp_at'"
fi

# Doc-drift guard, BOTH directions. Both lists are derived from the script and
# from the help text it prints — a hardcoded list here would just be a third
# place to forget, and the failure mode it is meant to catch is forgetting.
#
# Dispatch verbs: the `case $CMD in` block, one label per line, `a|b|c)` split
# on `|`. Flags are dropped (`-h`, `--version`) — the usage text lists those in
# their own sections and `--version` is deliberately a flag, not a verb — and
# `*)` never matches the letter class in the first place.
disp_verbs=$(sed -n '/^case \$CMD in/,/^esac/p' "$CLAUDIO" \
  | sed -n 's/^  \([a-z|-][a-z|-]*\)).*/\1/p' \
  | tr '|' '\n' | grep -v '^-' | sort -u)
# Usage verbs: lines indented exactly two spaces that start with a word, up to
# `Examples:` (whose lines start with `claudio ` and are not command listings).
# Continuation lines are indented far further, option lines start with `-`, and
# the component list is comma-separated, so none of them match.
usage_verbs=$(printf '%s\n' "$out" | sed -n '1,/^Examples:/p' \
  | sed -n 's/^  \([a-z][a-z-]*\) .*/\1/p' | sort -u)

# Guard the guards: an extraction that silently matched nothing would make both
# loops below run zero times and the section pass without testing anything.
assert_contains "the dispatch verb list was extracted" "$disp_verbs" "^run$"
assert_contains "the usage verb list was extracted" "$usage_verbs" "^run$"

# Forward: every command the dispatch table accepts has a line in the usage.
for c in $disp_verbs; do
  assert_contains "usage lists the '$c' command" "$out" "^  ${c}[ []"
done

# Reverse: every command the usage advertises is one the dispatch table takes.
# Without this, a renamed subcommand keeps its old name in the help text and
# `claudio <old>` answers "Unknown command" while the docs still promise it.
for c in $usage_verbs; do
  assert_contains "dispatch accepts the advertised '$c' command" "$disp_verbs" "^${c}\$"
done

# THIRD direction: readme.md's command reference has a heading for every verb.
# The two loops above pin the script against itself -- the help text and the
# dispatch table cannot disagree -- and both were green while `usage` and
# `recv` were documented in neither. A verb the help advertises and the
# reference never mentions is the shape a reader hits: `claudio help` names it,
# the manual has nothing to say about it, and there is no way to tell a verb
# that is undocumented from one that was removed.
#
# The trailing [ \`] is not decoration. Without it `claudio use` is satisfied by
# the heading for `claudio usage`, so deleting the `use` section would leave
# this green -- one verb hiding behind another verb's prefix, which is exactly
# the pair this repository now has.
# dirname "$CLAUDIO" and not $DOCS_DIR: that is set further down the file.
readme_ref="$(dirname "$CLAUDIO")/readme.md"
if [ -f "$readme_ref" ]; then
  ref_heads=$(sed -n 's/^### `claudio \([a-z][a-z-]*\)[ `].*/\1/p' "$readme_ref" | sort -u)
  assert_contains "the readme's command headings were extracted" "$ref_heads" "^run$"
  for c in $disp_verbs; do
    assert_contains "readme.md documents the '$c' command" "$ref_heads" "^${c}\$"
  done
else
  fail "readme.md is next to claudio" "not at $readme_ref"
fi

# ============================================================
printf "\n\033[1m=== packaging: an installed claudio finds its own Python ===\033[0m\n"
# ============================================================
# claudio stopped being one file the day `claudio usage` and `claudio recv`
# moved in beside it, and the documented install did not notice: `sudo install
# -m 755 claudio /usr/local/bin/claudio` copies the script and nothing else, so
# `_tool_path` fell through to a bare name that was never on $PATH. Both verbs
# then failed -- and the receiver is what stream A is exported to, so the
# symptom a user sees is an empty ledger, not an error.
#
# So the install is a Makefile target, and this section runs the REAL Makefile
# into a temporary DESTDIR and then runs the REAL Python out of the result.
# The file list is DERIVED from the tree rather than written out here: a
# hardcoded copy would be a second place to forget a new module, which is the
# same failure one layer up.
PKG_ROOT=$(dirname "$CLAUDIO")
if [ -f "$PKG_ROOT/Makefile" ] && command -v make >/dev/null 2>&1; then
  PKG_STAGE="$TMP/pkgstage"
  PKG_PREFIX=/opt/claudio-under-test
  rm -rf "$PKG_STAGE"
  pkg_out=$( (cd "$PKG_ROOT" && make install DESTDIR="$PKG_STAGE" PREFIX="$PKG_PREFIX") 2>&1 ) \
    || pkg_out="MAKE-INSTALL-FAILED $pkg_out"
  assert_not_contains "make install succeeds into a DESTDIR" "$pkg_out" "MAKE-INSTALL-FAILED"

  pkg_bin="$PKG_STAGE$PKG_PREFIX/bin/claudio"
  pkg_lib="$PKG_STAGE$PKG_PREFIX/libexec/claudio"
  TESTS=$((TESTS + 1))
  if [ -x "$pkg_bin" ]; then
    pass "make install writes an executable bin/claudio"
  else
    fail "make install writes an executable bin/claudio" "not at $pkg_bin"
  fi

  # DESTDIR must not leak into the tree: an install that wrote into $PREFIX for
  # real would have touched the developer's /opt on this very run.
  TESTS=$((TESTS + 1))
  if [ -e "$PKG_PREFIX" ]; then
    fail "make install respects DESTDIR" "$PKG_PREFIX exists outside the stage"
  else
    pass "make install respects DESTDIR"
  fi

  # Every runtime file under usage/ has to land, and the list is read off the
  # tree so a module added tomorrow is checked without anyone remembering to
  # add it here. README.md and __pycache__ are the only exclusions.
  pkg_seen=0
  pkg_missing=""
  for f in "$PKG_ROOT"/usage/claudio-usage "$PKG_ROOT"/usage/cu/*.py \
           "$PKG_ROOT"/usage/recv/*; do
    [ -f "$f" ] || continue
    case "$f" in *.md|*/__pycache__/*) continue ;; esac
    rel=${f#"$PKG_ROOT"/usage/}
    pkg_seen=$((pkg_seen + 1))
    [ -f "$pkg_lib/$rel" ] || pkg_missing="$pkg_missing $rel"
  done
  # Guard the guard: an empty file list would make the assertion below vacuous.
  TESTS=$((TESTS + 1))
  if [ "$pkg_seen" -gt 5 ]; then
    pass "the runtime file list was read off the tree ($pkg_seen files)"
  else
    fail "the runtime file list was read off the tree" "only $pkg_seen files"
  fi
  assert_eq "make install carries every runtime file under usage/" "$pkg_missing" ""

  # ...and the layout is the one `_tool_path` looks for: <bin>/../libexec/claudio.
  # Asserted as a path rather than inferred from the verbs working, because a
  # resolver that fell back to $PATH would make those pass on a machine that
  # happens to have an older claudio installed.
  assert_contains "the Python lands where _tool_path looks for it" \
    "$(printf '%s\n' "$pkg_lib/claudio-usage")" "/libexec/claudio/claudio-usage$"

  if command -v python3 >/dev/null 2>&1; then
    # The whole point: run the installed script and make it find its own
    # Python. The last resort in `_tool_path` is a BARE NAME on $PATH, so a
    # decoy of each name is planted at the front of $PATH: if resolution ever
    # falls through to it the assertion sees the decoy's marker instead of
    # argparse's banner, which is the difference between "it works" and "it
    # found somebody else's copy".
    mkdir -p "$TMP/pkgdecoy"
    printf '#!/bin/sh\necho "[decoy-on-PATH]"\n' > "$TMP/pkgdecoy/claudio-usage"
    printf '#!/bin/sh\necho "[decoy-on-PATH]"\n' > "$TMP/pkgdecoy/otlp-recv"
    chmod 755 "$TMP/pkgdecoy/claudio-usage" "$TMP/pkgdecoy/otlp-recv"
    pkg_env="PATH=$TMP/pkgdecoy:$PATH CLAUDIO_UPDATE=0 CLAUDIO_CONF=$TMP/pkg.conf"
    pkg_env="$pkg_env CLAUDIO_USAGE_DIR=$TMP/pkgdata"

    pkg_help=$(env $pkg_env "$TEST_SH" "$pkg_bin" usage --help 2>&1) \
      || pkg_help="EXEC-FAILED $pkg_help"
    assert_contains "an installed claudio runs 'claudio usage'" "$pkg_help" "claudio usage"
    assert_not_contains "...from libexec, not from a bare name on \$PATH" \
      "$pkg_help" "decoy-on-PATH"

    pkg_help2=$(env $pkg_env "$TEST_SH" "$pkg_bin" recv --help 2>&1) \
      || pkg_help2="EXEC-FAILED $pkg_help2"
    assert_contains "an installed claudio runs 'claudio recv'" "$pkg_help2" "claudio recv"
    assert_not_contains "...from libexec, not from a bare name on \$PATH " \
      "$pkg_help2" "decoy-on-PATH"

    # The Homebrew shape: /opt/homebrew/bin/claudio is a SYMLINK into the
    # Cellar, so resolution has to start from the resolved path. `_self_path`
    # does that; this pins it, because a `dirname $0` regression would look
    # perfect in a checkout and break every brew install.
    mkdir -p "$TMP/pkgshim"
    ln -sf "$pkg_bin" "$TMP/pkgshim/claudio"
    pkg_help3=$(env $pkg_env "$TEST_SH" "$TMP/pkgshim/claudio" usage --help 2>&1) \
      || pkg_help3="EXEC-FAILED $pkg_help3"
    assert_contains "a symlinked claudio still finds its Python (the brew shape)" \
      "$pkg_help3" "claudio usage"
    assert_not_contains "...and not the decoy either" "$pkg_help3" "decoy-on-PATH"
  else
    printf "  (skipped — no python3, so the installed verbs cannot be run)\n"
  fi

  # The Homebrew formula rewrites the shebang of each Python entry point by
  # name, so every path it names must be one `make install` really produced.
  # Extracted from the formula rather than listed here: renaming a file would
  # otherwise leave brew rewriting a path that no longer exists, which fails at
  # `brew install` time on somebody else's machine and nowhere in this suite.
  if [ -f "$PKG_ROOT/Formula/claudio.rb" ]; then
    brew_paths=$(sed -n 's|.*libexec/"\([^"]*\)".*|\1|p' \
      "$PKG_ROOT/Formula/claudio.rb")
    TESTS=$((TESTS + 1))
    if [ -n "$brew_paths" ]; then
      pass "the formula's libexec paths were extracted"
    else
      fail "the formula's libexec paths were extracted" "none found"
    fi
    brew_missing=""
    for bp in $brew_paths; do
      # The formula's paths are rooted at libexec/, the Makefile's at
      # libexec/claudio/ -- so strip the leading component the formula supplies.
      [ -f "$PKG_STAGE$PKG_PREFIX/libexec/$bp" ] || brew_missing="$brew_missing $bp"
    done
    assert_eq "every path the brew formula rewrites was really installed" \
      "$brew_missing" ""
  fi

  # uninstall removes exactly what install wrote, and nothing above it.
  #
  # A SECOND STAGE, deliberately, and it is what the two directory assertions
  # below need: this one gets a file of somebody else's beside claudio, so the
  # "removed if empty" rule is asked BOTH of its questions. A single stage can
  # only ever ask one, and the answer it happens to give reads as the rule.
  PKG_STAGE2="$TMP/pkgstage2"
  rm -rf "$PKG_STAGE2"
  (cd "$PKG_ROOT" && make install DESTDIR="$PKG_STAGE2" PREFIX="$PKG_PREFIX") >/dev/null 2>&1 \
    || :
  : > "$PKG_STAGE2$PKG_PREFIX/bin/not-ours"
  mkdir -p "$PKG_STAGE2$PKG_PREFIX/libexec/someone-else"
  pkg_un2=$( (cd "$PKG_ROOT" && make uninstall DESTDIR="$PKG_STAGE2" PREFIX="$PKG_PREFIX") 2>&1 ) \
    || pkg_un2="MAKE-UNINSTALL-FAILED $pkg_un2"
  assert_not_contains "make uninstall succeeds over a shared prefix" \
    "$pkg_un2" "MAKE-UNINSTALL-FAILED"
  TESTS=$((TESTS + 1))
  if [ -f "$PKG_STAGE2$PKG_PREFIX/bin/not-ours" ] \
     && [ -d "$PKG_STAGE2$PKG_PREFIX/libexec/someone-else" ]; then
    pass "make uninstall leaves a shared bin/ and libexec/ alone"
  else
    fail "make uninstall leaves a shared bin/ and libexec/ alone" \
      "removed a directory that was not empty"
  fi
  rm -rf "$PKG_STAGE2"

  pkg_un=$( (cd "$PKG_ROOT" && make uninstall DESTDIR="$PKG_STAGE" PREFIX="$PKG_PREFIX") 2>&1 ) \
    || pkg_un="MAKE-UNINSTALL-FAILED $pkg_un"
  assert_not_contains "make uninstall succeeds" "$pkg_un" "MAKE-UNINSTALL-FAILED"
  TESTS=$((TESTS + 1))
  if [ ! -e "$pkg_bin" ] && [ ! -d "$pkg_lib" ]; then
    pass "make uninstall removes both halves"
  else
    fail "make uninstall removes both halves" "left $pkg_bin / $pkg_lib"
  fi

  # ...and leaves no empty directory behind that it created itself. On a real
  # /usr/local this is invisible, because bin/ is never empty -- but staging
  # into a DESTDIR is how a package is built, and an orphan there is shipped
  # inside the package. $PREFIX itself is NOT asserted gone: nothing can tell
  # which of a prefix's ancestors it created, and removing them on a guess is
  # the worse bug.
  TESTS=$((TESTS + 1))
  pkg_orphans=""
  for pkg_d in "$PKG_STAGE$PKG_PREFIX/bin" "$PKG_STAGE$PKG_PREFIX/libexec"; do
    if [ -d "$pkg_d" ]; then
      pkg_orphans="$pkg_orphans $pkg_d"
    fi
  done
  if [ -z "$pkg_orphans" ]; then
    pass "make uninstall leaves no empty directory it created"
  else
    fail "make uninstall leaves no empty directory it created" "left$pkg_orphans"
  fi
  rm -rf "$PKG_STAGE"

  # A LIVE PREFIX, no DESTDIR, with a bin/ the user made first. This is the
  # `make install PREFIX="$HOME/.local"` path readme.md recommends by name,
  # and it is the case neither assertion above can see: the shared-prefix one
  # only ever asks a NON-EMPTY directory, which `rmdir` refuses anyway, and the
  # stage one asks a directory `install -d` really did create.
  #
  # Unguarded, `rmdir "$(BINDIR)"` removed it: `install -d` does not create a
  # directory that already exists, so "remove exactly what install created"
  # cannot be decided at uninstall time, and both ~/.local/bin and
  # ~/.local/libexec went with the uninstall. A directory on somebody's $PATH
  # is not ours to delete, so the rmdir pair is scoped to DESTDIR.
  PKG_LIVE="$TMP/pkglive"
  rm -rf "$PKG_LIVE"
  mkdir -p "$PKG_LIVE/bin"
  : > "$PKG_LIVE/bin/.made-by-the-user"
  (cd "$PKG_ROOT" && make install PREFIX="$PKG_LIVE") >/dev/null 2>&1 || :
  rm -f "$PKG_LIVE/bin/.made-by-the-user"
  pkg_unl=$( (cd "$PKG_ROOT" && make uninstall PREFIX="$PKG_LIVE") 2>&1 ) \
    || pkg_unl="MAKE-UNINSTALL-FAILED $pkg_unl"
  assert_not_contains "make uninstall succeeds over a live prefix" \
    "$pkg_unl" "MAKE-UNINSTALL-FAILED"
  TESTS=$((TESTS + 1))
  if [ ! -e "$PKG_LIVE/bin/claudio" ]; then
    pass "make uninstall over a live prefix removes claudio"
  else
    fail "make uninstall over a live prefix removes claudio" \
      "left $PKG_LIVE/bin/claudio"
  fi
  TESTS=$((TESTS + 1))
  if [ -d "$PKG_LIVE/bin" ]; then
    pass "...and leaves \$PREFIX/bin, which is the user's and may be on \$PATH"
  else
    fail "...and leaves \$PREFIX/bin, which is the user's and may be on \$PATH" \
      "removed $PKG_LIVE/bin"
  fi
  TESTS=$((TESTS + 1))
  if [ -d "$PKG_LIVE/libexec" ]; then
    pass "...and leaves \$PREFIX/libexec for the same reason"
  else
    fail "...and leaves \$PREFIX/libexec for the same reason" \
      "removed $PKG_LIVE/libexec"
  fi
  rm -rf "$PKG_LIVE"
else
  # NOT a silent skip. The Makefile is a file this repository owns and the
  # packaging guard is the release gate; a missing one is a failure by name,
  # and only `make` itself is a genuine environment fact.
  TESTS=$((TESTS + 1))
  if [ -f "$PKG_ROOT/Makefile" ]; then
    pass "Makefile is where the packaging guard looks"
  else
    fail "Makefile is where the packaging guard looks" "not at $PKG_ROOT/Makefile"
  fi
  printf "  (skipped — no make on this machine)\n"
fi

# `make check` is the documented way to run the suites, and what it must never
# become is a target that runs one shell. /bin/sh on macOS is bash 3.2, so a
# `check` that dropped the dash line would keep printing a green wall while
# every no-bash-ism regression walked through it -- the suite quietly halving
# itself, which is the failure this project is most careful about.
if [ -f "$PKG_ROOT/Makefile" ]; then
  mk=$(cat "$PKG_ROOT/Makefile")
  mk_targets=$(printf '%s\n' "$mk" | sed -n 's/^\([a-z][a-z-]*\):.*/\1/p' | sort -u)
  assert_contains "the Makefile's target list was extracted" "$mk_targets" "^install\$"
  assert_contains "make check exists" "$mk_targets" "^check\$"
  assert_contains "make uninstall exists" "$mk_targets" "^uninstall\$"
  # The body of `check`, up to the next blank line: both shells and both
  # Python suites, or it is not running the suites.
  mk_check=$(printf '%s\n' "$mk" | sed -n '/^check:/,/^$/p')
  assert_contains "make check runs the suite under bash" "$mk_check" "bash test.sh"
  assert_contains "make check runs the suite under dash" "$mk_check" "dash test.sh"
  assert_contains "make check runs the usage suite" "$mk_check" "usage/tests/test_all.py"
  assert_contains "make check runs the server suite" "$mk_check" "server/tests/test_all.py"
  # ...and passes it NOTHING that would weaken it. That suite refuses a run in
  # which it self-skipped -- 44 of 192 test functions, 918 assertions, which
  # used to be announced in a banner above an exit 0 -- and the escape hatch is
  # its own `--allow-skips`. A `check` target that carried that word would be a
  # release gate opting itself out.
  #
  # BOTH PLACES, because the recipe line names a variable and the opt-out fits
  # in either: the first draft of this guard checked the `check:` body alone,
  # and a `SERVER_SUITE_ARGS ?= --allow-skips` fifty lines above it left the
  # suite fully green. The declaration is asserted to exist before its value is
  # read, so an extraction that matched nothing cannot pass as an empty default
  # -- that is the shape that once deleted fifteen shipping-key assertions and
  # printed a reason that was untrue.
  assert_contains "the Makefile declares how the server suite is invoked" \
    "$mk" "^SERVER_SUITE_ARGS[[:space:]]*?="
  mk_ssa=$(printf '%s\n' "$mk" \
    | sed -n 's/^SERVER_SUITE_ARGS[[:space:]]*?=[[:space:]]*//p')
  assert_eq "...and its default weakens nothing" "$mk_ssa" ""
  assert_not_contains "make check does not opt the server suite out of its own engine" \
    "$mk_check" "allow-skips"
  # server/ is NOT installed: it is the only part of the project with a
  # non-stdlib dependency, and an install target that carried it would hand
  # every claudio user a dependency claudio itself gains nothing from.
  mk_install=$(printf '%s\n' "$mk" | sed -n '/^install:/,/^$/p')
  assert_not_contains "make install never installs server/" "$mk_install" "server/"
else
  fail "Makefile is where the make-check guard looks" "not at $PKG_ROOT/Makefile"
  TESTS=$((TESTS + 1))
fi

# The Homebrew formula installs through the same Makefile rather than listing
# files of its own. Two lists of what claudio consists of is how a new module
# reaches a checkout, a `make install` and not a brew install -- with the brew
# half broken in exactly the way this whole section exists to prevent.
if [ -f "$PKG_ROOT/Formula/claudio.rb" ]; then
  brewf=$(cat "$PKG_ROOT/Formula/claudio.rb")
  assert_contains "the brew formula installs via the Makefile" \
    "$brewf" 'system "make", "install"'

  # The dependency list, extracted rather than read. `claude-statusline` is
  # what renders the status line claudio's shim forwards to, and the docs tell
  # people to write `statusline=claude-statusline` -- a formula that does not
  # bring it names a command the reader then has to go and find. It is fully
  # qualified because this repository is itself tappable (a top-level
  # `Formula/` is all Homebrew needs), and a bare name does not resolve there.
  brew_deps=$(printf '%s\n' "$brewf" \
    | sed -n 's/^[[:space:]]*depends_on[[:space:]]*"\([^"]*\)".*/\1/p' | sort -u)
  assert_contains "the formula's dependency list was extracted" \
    "$brew_deps" "jq"
  assert_contains "the formula brings the status-line renderer" \
    "$brew_deps" "claude-statusline"
  assert_contains "...named by its tap, so it resolves from either tap" \
    "$brew_deps" "^gabrielbelli/tap/claude-statusline\$"
  # Python is declared and not assumed. The alternative -- the system
  # interpreter -- is a guess about every machine, and the failure it produces
  # is the quiet one: `claudio run` still launches and stream A stops being
  # recorded.
  assert_contains "the formula declares the Python it runs on" \
    "$brew_deps" "^python@"
  # `claudio update` parses a tap formula with three sed patterns over the
  # WHOLE file, comments included. This one is read back with claudio's own
  # patterns, because an example of a pattern written inside a comment is
  # matched by it -- which is not hypothetical: an early draft of this file
  # spelled one out and the update check reported the tap as offering "x".
  tapv=$(sed -n 's/^[[:space:]]*version[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$PKG_ROOT/Formula/claudio.rb" | head -1)
  [ -n "$tapv" ] || tapv=$(sed -n 's/.*tag:[[:space:]]*"v\{0,1\}\([^"]*\)".*/\1/p' \
    "$PKG_ROOT/Formula/claudio.rb" | head -1)
  [ -n "$tapv" ] || tapv=$(sed -n 's|.*/tags/v\{0,1\}\([0-9][^"/]*\)\.tar\.gz.*|\1|p' \
    "$PKG_ROOT/Formula/claudio.rb" | head -1)
  script_ver=$(sed -n 's/^VERSION=\(.*\)/\1/p' "$CLAUDIO" | head -1)
  assert_eq "the version claudio update reads out of the formula is this build's" \
    "$tapv" "$script_ver"
else
  # Mutation-proven: with Formula/claudio.rb moved aside, this branch reported
  # 1171/1171 green and exit 0 while eight assertions -- the whole dependency
  # list, the `make install` call, and the version `claudio update` parses out
  # of a tap -- vanished in silence, on the run that is the release gate. A
  # file this repository owns is not an environment fact.
  fail "Formula/claudio.rb is where the packaging guard looks" \
    "not at $PKG_ROOT/Formula/claudio.rb"
  TESTS=$((TESTS + 1))
fi

# ============================================================
printf "\n\033[1m=== the environment tables agree with the script ===\033[0m\n"
# ============================================================
# Three tables document the same variables — claudio's own header comment,
# readme.md and CLAUDE.md — and the script is the only source of truth for what
# it actually reads. Compared as sets, so this catches a variable added to the
# script and documented nowhere, AND a table row left behind by a removal.
# EDITOR is in all three tables too but is not a CLAUDIO_ name; it is left out
# of the comparison rather than special-cased, and asserted separately below.
#
# `CLAUDIO_[A-Z][A-Z_]*` requires at least one character after the underscore,
# so prose like "the CLAUDIO_* paths" contributes no phantom entry.
DOCS_DIR=$(dirname "$CLAUDIO")
env_used=$(grep -o 'CLAUDIO_[A-Z][A-Z_]*' "$CLAUDIO" | sort -u)
assert_contains "the variable list was extracted from the script" "$env_used" "^CLAUDIO_PROFILES$"

# The header comment's block only, not the whole file: every one of these names
# obviously appears in the code as well, which would make the check vacuous.
hdr_vars=$(sed -n '/^# Environment:/,/^$/p' "$CLAUDIO" \
  | grep -o 'CLAUDIO_[A-Z][A-Z_]*' | sort -u)
assert_eq "claudio's header comment documents exactly the variables it reads" \
  "$hdr_vars" "$env_used"

# Table rows only (`| \`CLAUDIO_X\` | … |`), so a variable merely mentioned in
# prose does not count as documented.
for f in readme.md CLAUDE.md; do
  if [ -f "$DOCS_DIR/$f" ]; then
    doc_vars=$(sed -n 's/^| `\(CLAUDIO_[A-Z][A-Z_]*\)`.*/\1/p' "$DOCS_DIR/$f" | sort -u)
    assert_eq "$f's variable table matches the variables claudio reads" \
      "$doc_vars" "$env_used"
    assert_contains "$f's variable table documents EDITOR" \
      "$(sed -n 's/^| `\([A-Z_][A-Z_]*\)`.*/\1/p' "$DOCS_DIR/$f")" "^EDITOR$"
  else
    printf "  (skipped — %s not next to claudio)\n" "$f"
  fi
done
assert_contains "claudio's header comment documents EDITOR" \
  "$(sed -n '/^# Environment:/,/^$/p' "$CLAUDIO")" "EDITOR"

# The config-file keys are documented in the same two files, and `logging=`
# replaced two of them. A deprecation written down in one doc and not the other
# is how a reader ends up following the stale half — so the list of retired
# keys is DERIVED from the script's own LOGGING_LEGACY line rather than copied
# here, which would only be a third place to forget.
legacy_keys=$(sed -n 's/^LOGGING_LEGACY="\(.*\)"$/\1/p' "$CLAUDIO")
assert_contains "the deprecated-key list was extracted from the script" "$legacy_keys" "otel"
assert_contains "...and has more than one entry in it" "$legacy_keys" "usage"
for f in readme.md CLAUDE.md; do
  if [ -f "$DOCS_DIR/$f" ]; then
    assert_contains "$f documents the logging= key and its values" \
      "$(cat "$DOCS_DIR/$f")" 'logging=none|local|remote'
    for k in $legacy_keys; do
      assert_contains "$f marks ${k}= deprecated on the line that names it" \
        "$(grep -i 'deprecat' "$DOCS_DIR/$f" || :)" "${k}="
    done
  else
    printf "  (skipped — %s not next to claudio)\n" "$f"
  fi
done

# The shipping keys, DERIVED from the shipper's own lookups rather than listed
# here. claudio itself only reads `ship_url` (`_ship_conf`, for the diagnostic);
# `ship_token` is the shipper's alone, so a list taken from the script would
# document one key and forget the other — and a key nobody documents is a key
# nobody sets, on the one feature whose failure mode is an employer quietly
# receiving nothing.
#
# The gate is the FILE and nothing else, which is the only condition the skip
# message claims. It used to be `[ -n "$ship_keys" ]`, so an extraction that
# found the file and matched nothing was indistinguishable from a missing file:
# rewriting ship.py's two lookups from `conf.get("account.%s.ship_url" % name)`
# to `conf.get("account." + name + ".ship_url")` — a change with no behavioural
# effect whatsoever — silently deleted fifteen assertions, printed a message
# that was factually untrue, and exited 0 green. The extraction assertions
# therefore sit OUTSIDE the emptiness check, exactly as the `LOGGING_LEGACY`
# guard twenty lines above has them, so a broken extraction is a red test.
if [ -f "$DOCS_DIR/usage/cu/ship.py" ]; then
  ship_keys=$(sed -n 's/.*account\.[^"]*\.\(ship_[a-z_]*\)".*/\1/p' \
    "$DOCS_DIR/usage/cu/ship.py" | sort -u)
  assert_contains "the shipping-key list was extracted from the shipper" \
    "$ship_keys" "^ship_url$"
  assert_contains "...and has more than one entry in it" "$ship_keys" "^ship_token$"
  for f in readme.md CLAUDE.md; do
    if [ -f "$DOCS_DIR/$f" ]; then
      for k in $ship_keys; do
        # Anchored to the LINE that documents the key, never to a blob of
        # every line in the file containing the phrase. The blob form asked
        # only that the key and the phrase co-occur *somewhere*, with no
        # relationship asserted between them — one sentence naming both keys
        # satisfied it single-handedly, so the table rows were unguarded and
        # three mutations survived, including a row rewritten to assert the
        # exact opposite ("unlike the global conf keys it may be set in any
        # layer"). The row is extracted first and its absence is its own
        # failure, so a deleted row fails as a missing row rather than as a
        # passing grep.
        rows=$(grep -F "account.<name>.${k}" "$DOCS_DIR/$f" || :)
        assert_contains "$f has a line documenting account.<name>.${k}" \
          "$rows" "${k}"
        stray=$(printf '%s\n' "$rows" \
          | grep -v -E '[Gg]lobal[- ]conf[- ]only' || :)
        assert_eq "$f: every line naming account.<name>.${k} says global conf only" \
          "$stray" ""
      done
    fi
  done

  # The shipper exists, so no document may still say it does not. `remote`
  # reading as enabled while nothing shipped was the last version of exactly
  # this failure, and its correction was written in three files at once — a
  # fourth still carrying the retired claim is the repo saying both things,
  # with the stale half in the most believable place.
  for f in readme.md CLAUDE.md usage/README.md; do
    if [ -f "$DOCS_DIR/$f" ]; then
      for claim in "shipping is not implemented" "there is no uploader" \
                   "not shipping yet"; do
        assert_not_contains "$f no longer claims the shipper is unbuilt: '$claim'" \
          "$(cat "$DOCS_DIR/$f")" "$claim"
      done
    fi
  done
else
  printf "  (skipped — usage/cu/ship.py not next to claudio)\n"
fi

# The install line is written in two places that must not drift apart: readme's
# Installation section, and the upgrade instruction `claudio update` prints to
# someone who did not install with brew. Two different one-liners for the same
# job read as one of them being stale, and the reader cannot tell which. Derived
# from the script, so the readme is checked against what users are actually told.
if [ -f "$DOCS_DIR/readme.md" ]; then
  inst_line=$(sed -n 's/^ *echo "  Then: *\([^"]*\)"/\1/p' "$CLAUDIO" | head -1)
  assert_contains "claudio's upgrade instruction was extracted" "$inst_line" "make install"
  assert_contains "readme documents the install line claudio tells you to run" \
    "$(cat "$DOCS_DIR/readme.md")" "$inst_line"
else
  printf "  (skipped — readme.md not next to claudio)\n"
fi

# claudio stopped being one file, and the sentence that said otherwise was
# written in four places at once — readme.md's `update` section, CLAUDE.md's
# command-surface note, the comment above `cmd_update`, and server/README.md's
# dependency-boundary paragraph. Nothing compared them, so `_update_how` was
# corrected to print `cd claudio && sudo make install` while the comment three
# screens below it still explained that claudio is one file, and CLAUDE.md
# still named a remedy — "the repo plus the documented `cp`" — that no document
# has documented since the Makefile landed.
#
# It is the same shape as the shipper guard above and it is here for the same
# reason: a refuted claim standing in the most believable place is worse than
# no claim, and the boundary paragraph in server/README.md is the one a
# packager reads while deciding whether to ship the script alone — which is
# exactly the mistake `make install` exists to prevent.
#
# The phrases are the FALSE spellings only. "no longer one file" and "NOT one
# file any more" are the corrections and must keep passing, so nothing here
# matches a bare "one file".
for f in readme.md CLAUDE.md server/README.md usage/README.md \
         packaging/README.md claudio Makefile; do
  if [ -f "$DOCS_DIR/$f" ]; then
    for claim in "claudio is one file" "one file copied into place" \
                 "one file that was copied" "single POSIX \`sh\` file" \
                 "single POSIX sh file"; do
      assert_not_contains "$f no longer claims claudio is one file: '$claim'" \
        "$(cat "$DOCS_DIR/$f")" "$claim"
    done
  fi
done
# ...and the guard can only say that because it is reading real files: a typo
# in the list above would make every assertion above vacuously true.
TESTS=$((TESTS + 1))
if [ -f "$DOCS_DIR/readme.md" ] && [ -f "$DOCS_DIR/server/README.md" ] \
   && [ -f "$DOCS_DIR/Makefile" ]; then
  pass "the one-file guard is reading files that exist"
else
  fail "the one-file guard is reading files that exist" \
    "one of readme.md / server/README.md / Makefile is not next to claudio"
fi

# ============================================================
# Results
# ============================================================

# The run reached the end under its own steam; cleanup can trust the counters.
SUITE_COMPLETED=1

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
