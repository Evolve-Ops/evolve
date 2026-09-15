#!/bin/bash
# Regression guard for tools/hooks/guard-cross-checkout-mutation.sh — the PreToolUse(Bash)
# hook that BLOCKS a mutating git/shell op targeting a git checkout OTHER than the
# session's own worktree, while passing everything else through untouched.
#
# The hook intercepts EVERY Bash command, so a behavior change is high-blast-radius —
# and the cardinal sin is a FALSE BLOCK of a chip working in its OWN tree. This test
# pins both arms: the true blocks (foreign mutation) and, more importantly, the wide
# passthrough surface (own-worktree ops, read-only foreign ops, unresolvable/junk input).
#
#   bash tools/hooks/test_guard_cross_checkout_mutation.sh
#
# Exits 0 only if every assertion passes; non-zero (and prints which cases failed)
# otherwise. Requires `jq`, `python3`, and `git` (the hook's own deps).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/guard-cross-checkout-mutation.sh"

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

# --- two real, distinct git checkouts so `rev-parse --show-toplevel` resolves -----------
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/guard-xcheckout.XXXXXX")"
cleanup() { rm -rf "$TMPROOT"; }
trap cleanup EXIT

mk_repo() {
  local d="$1"
  mkdir -p "$d"
  git -C "$d" init -q
  git -C "$d" config user.email t@t.t
  git -C "$d" config user.name t
  mkdir -p "$d/sub"
  : > "$d/sub/f.txt"
  git -C "$d" add -A
  git -C "$d" commit -qm init
}
OWN="$TMPROOT/own"        # this session's worktree
FOREIGN="$TMPROOT/foreign" # the shared/other checkout the guard must protect
mk_repo "$OWN"
mk_repo "$FOREIGN"
# Canonicalize (macOS /tmp -> /private/tmp) so string compares match the hook's output.
OWN="$(cd "$OWN" && pwd -P)"
FOREIGN="$(cd "$FOREIGN" && pwd -P)"

PASS=0
FAIL=0
FAILED_LABELS=()

# Feed a PreToolUse-shaped JSON ({tool_input:{command}, cwd}) to the hook; echo raw stdout
# (empty == passthrough, JSON == a deny/block).
run_hook() {
  local cmd="$1" cwd="$2"
  jq -n --arg c "$cmd" --arg d "$cwd" '{tool_input:{command:$c}, cwd:$d}' | "$HOOK"
}
decision_of() {
  printf '%s' "$1" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null
}

fail() {
  FAIL=$((FAIL + 1)); FAILED_LABELS+=("$1")
  printf 'FAIL  %s\n      %s\n' "$1" "$2"
}
ok() { PASS=$((PASS + 1)); printf 'ok    %s\n' "$1"; }

# assert_block <label> <cmd> <cwd>
#   Asserts the hook emits a permissionDecision:"deny".
assert_block() {
  local label="$1" cmd="$2" cwd="$3"
  local out dec
  out="$(run_hook "$cmd" "$cwd")"
  if [ -z "$out" ]; then
    fail "$label" "expected a BLOCK (deny) but hook emitted nothing (passthrough)"; return
  fi
  dec="$(decision_of "$out")"
  if [ "$dec" != "deny" ]; then
    fail "$label" "expected permissionDecision=deny, got '$dec' in: $out"; return
  fi
  ok "$label  (deny)"
}

# assert_pass <label> <cmd> <cwd>
#   Asserts the hook emits NOTHING (command proceeds unchanged).
assert_pass() {
  local label="$1" cmd="$2" cwd="$3"
  local out
  out="$(run_hook "$cmd" "$cwd")"
  if [ -n "$out" ]; then
    fail "$label" "expected PASSTHROUGH (empty) but hook returned: $out"; return
  fi
  ok "$label  (passthrough)"
}

echo "== BLOCK cases (mutating op against a FOREIGN checkout) =="

# Detector B — leading `cd <foreign>` then a mutating git verb.
assert_block "B-reset-hard"   "cd $FOREIGN && git reset --hard"           "$OWN"
assert_block "B-checkout"     "cd $FOREIGN && git checkout main"          "$OWN"
assert_block "B-commit"       "cd $FOREIGN && git commit -m x"            "$OWN"
assert_block "B-stash-pop"    "cd $FOREIGN && git stash pop"              "$OWN"
assert_block "B-branch-D"     "cd $FOREIGN && git branch -D feature"      "$OWN"
assert_block "B-clean"        "cd $FOREIGN && git clean -fd"              "$OWN"
assert_block "B-mixed-ro-then-mut" "cd $FOREIGN && git log && git reset --hard" "$OWN"
# Detector B — shell mutation with a RELATIVE operand (resolves under the foreign root).
assert_block "B-rm-relative"  "cd $FOREIGN && rm -rf build"              "$OWN"
assert_block "B-redirect-rel" "cd $FOREIGN && echo hi > note.txt"        "$OWN"

# Detector A — `git -C <foreign> <mutating>` (explicit target, no cd needed).
assert_block "A-checkout"     "git -C $FOREIGN checkout main"            "$OWN"
assert_block "A-reset"        "git -C $FOREIGN reset --hard HEAD~1"      "$OWN"
assert_block "A-stash-push"   "git -C $FOREIGN stash push -m wip"        "$OWN"

echo
echo "== PASSTHROUGH cases (must emit nothing) =="

# --- own-worktree ops: the FALSE-BLOCK surface the contract forbids ---------------------
assert_pass "P-own-commit"       "git commit -m x"                       "$OWN"
assert_pass "P-own-reset"        "git reset --hard"                      "$OWN"
assert_pass "P-own-checkout"     "git checkout main"                     "$OWN"
assert_pass "P-own-cd-sub"       "cd $OWN/sub && git commit -m x"        "$OWN"   # subdir == same toplevel
assert_pass "P-own-cd-root"      "cd $OWN && git reset --hard"           "$OWN"
assert_pass "P-own-gitC-sub"     "git -C $OWN/sub reset --hard"          "$OWN"
assert_pass "P-own-gitC-relsub"  "git -C sub reset --hard"               "$OWN"   # relative -C, own
# Chained -C nets back to own (git chdir's sub, then `..` back to toplevel). The hook
# can't resolve a chain against cwd alone, so it BAILS to passthrough — never false-blocks.
# (Regression for the multi-`-C` false block the adversarial review caught.)
assert_pass "P-own-gitC-chain-backto-own" "git -C sub -C .. reset --hard" "$OWN"
# Same reason, inverse direction: a chained -C could climb OUT to foreign, but multi-`-C`
# is a passthrough bail by design (missing an inverse is fail-safe; false-blocking is not).
assert_pass "P-gitC-chain-foreign" "git -C $FOREIGN -C . reset --hard"   "$OWN"
assert_pass "P-own-rm"           "rm -rf build"                          "$OWN"   # no cd -> own cwd
assert_pass "P-cwd-is-foreign"   "git commit -m x"                       "$FOREIGN" # session lives there -> own

# --- read-only git against foreign: NOT this hook's job (rewrite-cd-git owns it) ---------
assert_pass "P-foreign-log"      "cd $FOREIGN && git log"                "$OWN"
assert_pass "P-foreign-status"   "cd $FOREIGN && git status"             "$OWN"
assert_pass "P-foreign-branch-list" "cd $FOREIGN && git branch --list"   "$OWN"
assert_pass "P-gitC-foreign-diff" "git -C $FOREIGN diff"                 "$OWN"

# --- unresolvable / junk / ambiguous: uncertainty -> passthrough ------------------------
assert_pass "P-nonexistent-cd"   "cd /nonexistent/xyz && git reset --hard" "$OWN"
assert_pass "P-gitC-nonexistent" "git -C /nonexistent/xyz checkout main"   "$OWN"
assert_pass "P-junk"             "wat is this even"                      "$OWN"
assert_pass "P-empty"            ""                                      "$OWN"
assert_pass "P-cmd-subst-dir"    'cd $(pwd)/foreign && git reset --hard' "$OWN"
assert_pass "P-glob-dir"         "cd $TMPROOT/for* && git reset --hard"  "$OWN"

# --- foreign cd but a NON-mutating / absolute-target shell op: not confidently harmful --
assert_pass "P-foreign-echo"     "cd $FOREIGN && echo hi"                "$OWN"
assert_pass "P-foreign-rm-abs"   "cd $FOREIGN && rm -rf /tmp/scratch-xyz" "$OWN"  # absolute -> unconfirmable
assert_pass "P-foreign-redir-abs" "cd $FOREIGN && echo hi > /tmp/n.txt"  "$OWN"

# --- no cwd in payload: cannot resolve own -> passthrough -------------------------------
out_nocwd="$(jq -n --arg c "cd $FOREIGN && git reset --hard" '{tool_input:{command:$c}}' | "$HOOK")"
if [ -n "$out_nocwd" ]; then
  fail "P-no-cwd" "expected passthrough with no cwd, got: $out_nocwd"
else
  ok "P-no-cwd  (passthrough)"
fi

echo
echo "==================================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: ${FAILED_LABELS[*]}"
  exit 1
fi
echo "ALL ASSERTIONS PASSED"
