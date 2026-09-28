#!/bin/bash
# Regression guard for tools/hooks/rewrite-cd-git.sh — the PreToolUse(Bash) hook that
# rewrites `cd <dir> && git <readonly...>` into `git -C <dir> <readonly...>` to dodge
# Claude Code's non-grantable "untrusted git hooks" guardrail.
#
# The hook intercepts EVERY Bash command, so a behavior change is high-blast-radius:
# this test is the regression guard. Run it after any edit to the hook.
#
#   bash tools/hooks/test_rewrite_cd_git.sh
#
# Exits 0 only if every assertion passes; non-zero (and prints which cases failed)
# otherwise. Requires `jq` and `python3` (the hook's own deps).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/rewrite-cd-git.sh"

if [ ! -x "$HOOK" ]; then
  echo "FATAL: hook not found or not executable: $HOOK" >&2
  exit 2
fi
for dep in jq python3; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    echo "FATAL: required dependency '$dep' not on PATH" >&2
    exit 2
  fi
done

PASS=0
FAIL=0
FAILED_LABELS=()

# Feed a PreToolUse-shaped JSON ({"tool_input":{"command":...}}) to the hook and
# echo its raw stdout (empty == passthrough, JSON == rewrite).
run_hook() {
  local cmd="$1"
  jq -n --arg c "$cmd" '{tool_input:{command:$c}}' | "$HOOK"
}

# Extract the rewritten command from the hook's JSON output.
rewritten_of() {
  printf '%s' "$1" | jq -r '.hookSpecificOutput.updatedInput.command // empty' 2>/dev/null
}

fail() {
  local label="$1" detail="$2"
  FAIL=$((FAIL + 1))
  FAILED_LABELS+=("$label")
  printf 'FAIL  %s\n      %s\n' "$label" "$detail"
}
ok() {
  PASS=$((PASS + 1))
  printf 'ok    %s\n' "$1"
}

# assert_rewrite <label> <cmd> -- <must-contain-substr> [<must-contain-substr> ...]
#   Asserts: output is non-empty JSON, the rewritten command contains each given
#   substring (use this to pin EACH `git -C <dir>` and the preserved separators/pipes),
#   and the rewritten command no longer contains a `cd `.
assert_rewrite() {
  local label="$1" cmd="$2"
  shift 2
  [ "$1" = "--" ] && shift
  local out rw
  out="$(run_hook "$cmd")"
  if [ -z "$out" ]; then
    fail "$label" "expected a REWRITE but hook emitted nothing (passthrough)"
    return
  fi
  rw="$(rewritten_of "$out")"
  if [ -z "$rw" ]; then
    fail "$label" "hook output was not valid rewrite JSON: $out"
    return
  fi
  local missing=()
  local needle
  for needle in "$@"; do
    if ! printf '%s' "$rw" | grep -qF -- "$needle"; then
      missing+=("$needle")
    fi
  done
  if [ "${#missing[@]}" -ne 0 ]; then
    fail "$label" "rewritten='$rw' missing expected substring(s): ${missing[*]}"
    return
  fi
  # The whole point is to remove the `cd`; a surviving `cd ` is a defect.
  if printf '%s' "$rw" | grep -qE '(^|[^-[:alnum:]])cd[[:space:]]'; then
    fail "$label" "rewritten command still contains a 'cd ': $rw"
    return
  fi
  ok "$label  ->  $rw"
}

# assert_passthrough <label> <cmd>
#   Asserts the hook emits NOTHING (command proceeds unchanged).
assert_passthrough() {
  local label="$1" cmd="$2"
  local out
  out="$(run_hook "$cmd")"
  if [ -n "$out" ]; then
    fail "$label" "expected PASSTHROUGH (empty) but hook rewrote to: $(rewritten_of "$out")"
    return
  fi
  ok "$label  (passthrough)"
}

# assert_git_c_count <label> <rewritten-cmd> <expected-count>
#   Pins the number of `git -C <dir>` rewrites (one per git in the chain).
assert_git_c_count() {
  local label="$1" cmd="$2" want="$3"
  local rw got
  rw="$(rewritten_of "$(run_hook "$cmd")")"
  got="$(printf '%s' "$rw" | grep -oF 'git -C /w/x' | wc -l | tr -d ' ')"
  if [ "$got" != "$want" ]; then
    fail "$label" "expected $want 'git -C /w/x' occurrences, got $got in: $rw"
    return
  fi
  ok "$label  ($got x 'git -C /w/x')"
}

echo "== REWRITE cases (each git in the chain gets its own -C; separators/pipes preserved) =="

assert_rewrite "R1 two-gits-and-echo" \
  'cd /w/x && git status -sb && echo "---" && git diff' \
  -- 'git -C /w/x status -sb' 'git -C /w/x diff' 'echo "---"' ' && '
assert_git_c_count "R1 count" 'cd /w/x && git status -sb && echo "---" && git diff' 2

assert_rewrite "R2 log-show-pipe-head" \
  'cd /w/x && git log --oneline -3 && echo "---" && git show --stat HEAD | head -20' \
  -- 'git -C /w/x log --oneline -3' 'git -C /w/x show --stat HEAD' '| head -20' 'echo "---"'
assert_git_c_count "R2 count" 'cd /w/x && git log --oneline -3 && echo "---" && git show --stat HEAD | head -20' 2

assert_rewrite "R3 single-log" \
  'cd /w/x && git log --oneline -5' \
  -- 'git -C /w/x log --oneline -5'
assert_git_c_count "R3 count" 'cd /w/x && git log --oneline -5' 1

assert_rewrite "R4 branch-list (read-only listing form)" \
  'cd /w/x && git branch --list' \
  -- 'git -C /w/x branch --list'
assert_git_c_count "R4 count" 'cd /w/x && git branch --list' 1

assert_rewrite "R5 non-git-first" \
  'cd /w/x && echo hi && git status' \
  -- 'echo hi' 'git -C /w/x status' ' && '
assert_git_c_count "R5 count" 'cd /w/x && echo hi && git status' 1

echo
echo "== PASSTHROUGH cases (must emit nothing; command runs unchanged -> at worst today's prompt) =="

# Writes / mutating git — must never be rewritten.
assert_passthrough "P-push"        'cd /w/x && git push'
assert_passthrough "P-commit"      'cd /w/x && git commit -m x'
assert_passthrough "P-reset-hard"  'cd /w/x && git reset --hard'
assert_passthrough "P-checkout"    'cd /w/x && git checkout main'
assert_passthrough "P-branch-D"    'cd /w/x && git branch -D foo'
assert_passthrough "P-stash-push"  'cd /w/x && git stash push -m wip'

# cwd-sensitive / destructive non-git segments mixed in — must not be rewritten,
# because dropping the `cd` would silently move where the rm/etc runs.
assert_passthrough "P-log-then-rm" 'cd /w/x && git log && rm -rf foo'
assert_passthrough "P-bare-rm"     'cd /w/x && rm -rf foo'

# Structural bail-outs.
assert_passthrough "P-ls"          'cd /w/x && ls -la'
assert_passthrough "P-nested-cd"   'cd /w/x && git log && cd /w/y && git log'
assert_passthrough "P-cmd-subst"   'cd /w/x && git log $(whoami)'
assert_passthrough "P-non-cd-start" 'ls -la && git status'

# Boundary kinds the &&/||/;/| splitter does NOT see — a trailing command would ride
# on the read-only-git segment and be rewritten with the `cd` dropped. Must passthrough.
# (META:substrate hardening follow-up to #2946.)
#   1. Embedded newline (DESTRUCTIVE-capable): the `rm` would otherwise run in the
#      original cwd, not /w/x. Use a literal newline via ANSI-C quoting.
assert_passthrough "P-newline-rm"  $'cd /w/x && git log\nrm -rf foo'
#   2. Relative-path redirect: `rel.txt` would otherwise be truncated in the original cwd.
assert_passthrough "P-redirect"    'cd /w/x && git log > rel.txt'

# Relocated-READ faithfulness gaps (non-destructive) surfaced by the two-pass review of
# #2946/#2950 — each falls back to passthrough (today's prompt). (META:substrate.)
#   3. Benign command with a relative-path file operand: `head rel.txt` would read from
#      the original cwd, not /w/x (head/tail/wc are cwd-insensitive only on stdin).
assert_passthrough "P-head-relpath"  'cd /w/x && git log && head rel.txt'
#   4. Benign-PREFIXED name: `echo-foo` is a different command, not `echo`.
assert_passthrough "P-benign-prefix" 'cd /w/x && echo-foo && git status'
#   5. Glob in a benign arg expands against the original cwd.
assert_passthrough "P-glob-benign"   'cd /w/x && wc * && git status'
#   6. Glob in a read-only git arg expands against the original cwd too.
assert_passthrough "P-glob-git"      'cd /w/x && git diff *.py'

echo
echo "==================================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: ${FAILED_LABELS[*]}"
  exit 1
fi
echo "ALL ASSERTIONS PASSED"
