#!/bin/bash
# Regression guard for tools/hooks/guard-empty-pr.sh — the PreToolUse(Bash) hook
# that BLOCKS `gh pr create` when the head branch has an EMPTY diff vs its base
# (the empty-scaffold-merge root cause; spec §17, Initiative 12 / bite B2).
#
# The hook intercepts EVERY Bash command, so a behavior change is high-blast-radius:
# a false block would deny a legitimate PR. This test pins BOTH directions —
# it BLOCKS a genuinely empty diff and PASSES a create with real changes — plus
# the fail-safe passthroughs (unresolved base, --head, --repo, cd, detached, not a
# git repo, not a gh-pr-create). Run it after any edit to the hook.
#
#   bash tools/hooks/test_guard_empty_pr.sh
#
# Exits 0 only if every assertion passes. Requires `jq`, `python3`, and `git`.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/guard-empty-pr.sh"

if [ ! -x "$HOOK" ]; then
  echo "FATAL: hook not found or not executable: $HOOK" >&2
  exit 2
fi
for dep in jq python3 git; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    echo "FATAL: required dependency '$dep' not on PATH" >&2
    exit 2
  fi
done

PASS=0
FAIL=0
FAILED_LABELS=()

fail() {
  FAIL=$((FAIL + 1))
  FAILED_LABELS+=("$1")
  printf 'FAIL  %s\n      %s\n' "$1" "$2"
}
ok() {
  PASS=$((PASS + 1))
  printf 'ok    %s\n' "$1"
}

# ── git fixture: a real clone with an origin/HEAD → origin/main ──────────────
TMP="$(mktemp -d 2>/dev/null || mktemp -d -t guardpr)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

export GIT_CONFIG_GLOBAL="$TMP/gitconfig"   # isolate from the operator's config/hooks
export GIT_CONFIG_SYSTEM=/dev/null
git config --file "$GIT_CONFIG_GLOBAL" user.email test@example.com
git config --file "$GIT_CONFIG_GLOBAL" user.name  "Test Guard"
git config --file "$GIT_CONFIG_GLOBAL" init.defaultBranch main
git config --file "$GIT_CONFIG_GLOBAL" commit.gpgsign false

( set -e
  cd "$TMP"
  git init -q -b main seed
  cd seed
  printf 'seed\n' > base.txt
  git add base.txt
  git commit -qm "init"
  git init -q --bare "$TMP/origin.git"
  git remote add origin "$TMP/origin.git"
  git push -qu origin main
  cd "$TMP"
  git clone -q origin.git work
  cd work
  # Sanity: clone sets origin/HEAD → origin/main (the guard's default-base source).
  git symbolic-ref -q refs/remotes/origin/HEAD >/dev/null
  # empty-branch: no new commits vs main → empty diff vs origin/main → must BLOCK.
  git checkout -q -b empty-branch main
  # real-branch: one real file change → non-empty diff → must PASS.
  git checkout -q -b real-branch main
  printf 'new feature\n' > feature.txt
  git add feature.txt
  git commit -qm "add feature"
  git checkout -q empty-branch
) || { echo "FATAL: git fixture setup failed" >&2; exit 2; }

REPO="$TMP/work"

on_branch() { git -C "$REPO" checkout -q "$1"; }

# Feed a PreToolUse-shaped payload ({command, cwd}) and echo the hook's stdout.
run_hook() {
  local cmd="$1" cwd="${2:-$REPO}"
  jq -n --arg c "$cmd" --arg w "$cwd" '{tool_input:{command:$c}, cwd:$w}' | "$HOOK"
}
decision_of() {
  printf '%s' "$1" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null
}

# assert_block <label> <cmd> [branch]
assert_block() {
  local label="$1" cmd="$2" branch="${3:-empty-branch}"
  on_branch "$branch"
  local out dec
  out="$(run_hook "$cmd")"
  if [ -z "$out" ]; then
    fail "$label" "expected BLOCK (deny) but hook emitted nothing (passthrough)"
    return
  fi
  dec="$(decision_of "$out")"
  if [ "$dec" != "deny" ]; then
    fail "$label" "expected permissionDecision=deny, got '$dec' in: $out"
    return
  fi
  ok "$label  (blocked)"
}

# assert_pass <label> <cmd> [branch] [cwd]
assert_pass() {
  local label="$1" cmd="$2" branch="${3:-empty-branch}" cwd="${4:-$REPO}"
  [ "$branch" != "-" ] && on_branch "$branch"
  local out
  out="$(run_hook "$cmd" "$cwd")"
  if [ -n "$out" ]; then
    fail "$label" "expected PASSTHROUGH (empty) but hook returned: $out"
    return
  fi
  ok "$label  (passthrough)"
}

echo "== BLOCK cases (positively-empty diff vs base → deny) =="
assert_block "B1 default-base empty"     'gh pr create --fill'                 empty-branch
assert_block "B2 explicit --base empty"  'gh pr create --base main --fill'     empty-branch
assert_block "B3 in a && chain, empty"   'git add -A && git commit -m x && gh pr create --fill' empty-branch
assert_block "B4 body flags, empty"      'gh pr create --title t --body-file /dev/null' empty-branch

echo
echo "== PASSTHROUGH cases (real changes or any uncertainty → emit nothing) =="
# Real changes — the create the guard must NEVER block.
assert_pass  "P1 real changes default"   'gh pr create --fill'                 real-branch
assert_pass  "P2 real changes --base"    'gh pr create --base main --fill'     real-branch
assert_pass  "P3 real changes in chain"  'git add -A && git commit -m x && gh pr create --fill' real-branch
# Not a gh-pr-create at all.
assert_pass  "P4 not gh pr create"       'git status && git log'               empty-branch
assert_pass  "P5 gh pr view"             'gh pr view 123'                      empty-branch
# Uncertainty → fail-safe passthrough (even on the empty branch).
assert_pass  "P6 --head explicit"        'gh pr create --head other --fill'    empty-branch
assert_pass  "P7 --repo other"           'gh pr create --repo owner/x --fill'  empty-branch
assert_pass  "P8 -R other (short)"       'gh pr create -R owner/x --fill'      empty-branch
assert_pass  "P9 cross-fork base"        'gh pr create --base up:main --fill'  empty-branch
assert_pass  "P10 leading cd"            'cd /tmp && gh pr create --fill'      empty-branch
assert_pass  "P11 base resolves nowhere" 'gh pr create --base nonesuch --fill' empty-branch
# Detached HEAD → HEAD is not a branch → passthrough.
DET_SHA="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" checkout -q "$DET_SHA"
assert_pass  "P12 detached HEAD"         'gh pr create --fill'                 -
git -C "$REPO" checkout -q empty-branch
# cwd is not a git repo → git fails → passthrough.
assert_pass  "P13 cwd not a git repo"    'gh pr create --fill'                 empty-branch "$TMP"
# cwd field ABSENT from the payload entirely → no dir to run git in → passthrough.
on_branch empty-branch
P14_OUT="$(jq -n --arg c 'gh pr create --fill' '{tool_input:{command:$c}}' | "$HOOK")"
if [ -n "$P14_OUT" ]; then
  fail "P14 no cwd field" "expected PASSTHROUGH (empty) but hook returned: $P14_OUT"
else
  ok "P14 no cwd field  (passthrough)"
fi

echo
echo "==================================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: ${FAILED_LABELS[*]}"
  exit 1
fi
echo "ALL ASSERTIONS PASSED"
