#!/bin/bash
# Regression guard for tools/hooks/guard-out-of-cwd-write.sh — the PreToolUse(Write|Edit|
# MultiEdit) hook that DENIES a write into ~/.claude/meta-{dispatch,reconcile,coherence}/**
# from a META session and names the granted verb instead.
#
# The hook intercepts EVERY Write/Edit, so the cardinal sin is a FALSE DENY: a chip editing
# a file in its own checkout, or any session with no META marker, must sail through
# untouched. This test therefore pins the passthrough surface at least as hard as the deny
# surface — including the aspect ledgers under ~/.claude/projects/, which are deliberately
# NOT guarded because no granted writer exists for them yet.
#
#   bash tools/hooks/test_guard_out_of_cwd_write.sh
#
# Exits 0 only if every assertion passes; non-zero (and prints which cases failed)
# otherwise. Requires `jq` and `shasum`/`sha256sum` (the hook's own deps).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/guard-out-of-cwd-write.sh"
MARKER_HELPER="$SCRIPT_DIR/meta-active-aspect.sh"

for f in "$HOOK" "$MARKER_HELPER"; do
  if [ ! -f "$f" ]; then
    echo "FATAL: not found: $f" >&2
    exit 2
  fi
done
if ! command -v jq >/dev/null 2>&1; then
  echo "FATAL: required dependency 'jq' not on PATH" >&2
  exit 2
fi

# --- an isolated HOME, so the test never reads or writes the operator's real markers ----
# The hook resolves both the marker dir AND the guarded ~/.claude/meta-<lane>/ prefixes
# from $HOME, so overriding it moves the whole surface into the sandbox at once.
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/guard-oocw.XXXXXX")"
cleanup() { rm -rf "$TMPROOT"; }
trap cleanup EXIT
TMPROOT="$(cd "$TMPROOT" && pwd -P)"     # macOS /tmp -> /private/tmp

export HOME="$TMPROOT/home"
mkdir -p "$HOME/.claude"
META_CWD="$TMPROOT/checkout"             # a session WITH a META active-aspect marker
PLAIN_CWD="$TMPROOT/plain"               # a session WITHOUT one
mkdir -p "$META_CWD/internal" "$PLAIN_CWD"
META_CWD="$(cd "$META_CWD" && pwd -P)"
PLAIN_CWD="$(cd "$PLAIN_CWD" && pwd -P)"

# Written via the REAL helper, so a drift between its key algorithm and the copy inlined
# in the hook fails this test rather than silently disabling the guard in production.
bash "$MARKER_HELPER" write substrate "$META_CWD"

PASS=0
FAIL=0
FAILED_LABELS=()

fail() { FAIL=$((FAIL + 1)); FAILED_LABELS+=("$1"); printf 'FAIL  %s\n      %s\n' "$1" "$2"; }
ok()   { PASS=$((PASS + 1)); printf 'ok    %s\n' "$1"; }

# run_hook <tool> <file_path> <cwd>
run_hook() {
  jq -n --arg t "$1" --arg f "$2" --arg d "$3" \
    '{tool_name:$t, tool_input:{file_path:$f}, cwd:$d}' | bash "$HOOK"
}
decision_of() {
  printf '%s' "$1" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null
}
reason_of() {
  printf '%s' "$1" | jq -r '.hookSpecificOutput.permissionDecisionReason // empty' 2>/dev/null
}

# assert_deny <label> <tool> <path> <cwd> [<substring the remedy must name>]
assert_deny() {
  local label="$1" out dec
  out="$(run_hook "$2" "$3" "$4")"
  dec="$(decision_of "$out")"
  if [ "$dec" != "deny" ]; then
    fail "$label" "expected deny, got: ${out:-<passthrough>}"
    return
  fi
  if [ "$#" -ge 5 ] && ! printf '%s' "$(reason_of "$out")" | grep -qF -- "$5"; then
    fail "$label" "deny reason does not name the remedy '$5'"
    return
  fi
  ok "$label"
}

# assert_pass <label> <tool> <path> <cwd>
assert_pass() {
  local label="$1" out
  out="$(run_hook "$2" "$3" "$4")"
  if [ -n "$out" ]; then
    fail "$label" "expected passthrough, got: $out"
  else
    ok "$label  (passthrough)"
  fi
}

echo "--- DENY: the guarded lane dirs, from a META session -------------------------------"
assert_deny "D-dispatch-state"  Write "$HOME/.claude/meta-dispatch/last-seen.json" "$META_CWD" \
            "state --lane dispatch"
assert_deny "D-reconcile-state" Edit  "$HOME/.claude/meta-reconcile/last-seen.json" "$META_CWD" \
            "state --lane reconcile"
assert_deny "D-coherence-state" Write "$HOME/.claude/meta-coherence/last-seen.json" "$META_CWD" \
            "state --lane coherence"
assert_deny "D-multiedit"       MultiEdit "$HOME/.claude/meta-dispatch/last-seen.json" "$META_CWD"
assert_deny "D-run-log"         Write "$HOME/.claude/meta-reconcile/log/2026-09-07.jsonl" "$META_CWD" \
            "heartbeat --log-dir"
assert_deny "D-tilde-form"      Write "~/.claude/meta-dispatch/last-seen.json" "$META_CWD" \
            "state --lane dispatch"
# `..` must not walk around the prefix test — the collapse is textual, so it holds for a
# file that does not exist yet (which is every first Write).
assert_deny "D-dotdot-walk"     Write "$HOME/.claude/meta-dispatch/x/../last-seen.json" "$META_CWD"
assert_deny "D-other-file"      Write "$HOME/.claude/meta-dispatch/scratch.json" "$META_CWD" \
            "meta-dispatch-move"
# The fourth scheduled writer, and the one whose dir carries no `meta-` prefix — so this
# case fails the moment someone "simplifies" the dir list into ~/.claude/meta-<lane>/.
assert_deny "D-pm-landing"      Write "$HOME/.claude/pm-landing/last-seen.json" "$META_CWD" \
            "state --lane pm-landing"

echo
echo "--- PASS: the wide surface a false deny would break --------------------------------"
assert_pass "P-in-cwd-relative"  Write "internal/dispatch/inflight/x.md" "$META_CWD"
assert_pass "P-in-cwd-absolute"  Edit  "$META_CWD/internal/x.md"         "$META_CWD"
# The aspect ledgers: same stall class, but NO granted writer exists, so guarding them
# would trade a probabilistic stall for a certain failure. Deliberately passthrough.
assert_pass "P-aspect-ledger"    Write "$HOME/.claude/projects/p/memory/meta-state/substrate.json" "$META_CWD"
assert_pass "P-claude-settings"  Edit  "$HOME/.claude/settings.json"     "$META_CWD"
assert_pass "P-pm-landing-look" Write "$HOME/.claude/pm-landing-scratch/x.json" "$META_CWD"
assert_pass "P-lookalike-prefix" Write "$HOME/.claude/meta-dispatcher/last-seen.json" "$META_CWD"
assert_pass "P-non-meta-session" Write "$HOME/.claude/meta-dispatch/last-seen.json" "$PLAIN_CWD"
assert_pass "P-other-tool"       Read  "$HOME/.claude/meta-dispatch/last-seen.json" "$META_CWD"
assert_pass "P-bash-tool"        Bash  "$HOME/.claude/meta-dispatch/last-seen.json" "$META_CWD"

echo
echo "--- PASS: uncertainty of every shape -----------------------------------------------"
assert_pass "P-empty-path"       Write ""                                "$META_CWD"
assert_pass "P-cmd-subst"        Write '$(echo ~)/.claude/meta-dispatch/last-seen.json' "$META_CWD"
assert_pass "P-glob"             Write "$HOME/.claude/meta-*/last-seen.json" "$META_CWD"
assert_pass "P-junk-cwd"         Write "$HOME/.claude/meta-dispatch/last-seen.json" "/nonexistent/xyz"

out_nocwd="$(jq -n '{tool_name:"Write",tool_input:{file_path:"~/.claude/meta-dispatch/last-seen.json"}}' | bash "$HOOK")"
if [ -n "$out_nocwd" ]; then
  fail "P-no-cwd" "expected passthrough with no cwd, got: $out_nocwd"
else
  ok "P-no-cwd  (passthrough)"
fi

out_junk="$(printf 'not json at all' | bash "$HOOK" 2>/dev/null)"
if [ -n "$out_junk" ]; then
  fail "P-malformed-payload" "expected passthrough on junk input, got: $out_junk"
else
  ok "P-malformed-payload  (passthrough)"
fi

out_nonstr="$(jq -n --arg d "$META_CWD" '{tool_name:"Write",tool_input:{file_path:42},cwd:$d}' | bash "$HOOK")"
if [ -n "$out_nonstr" ]; then
  fail "P-non-string-path" "expected passthrough on a non-string file_path, got: $out_nonstr"
else
  ok "P-non-string-path  (passthrough)"
fi

out_nohome="$(env -u HOME jq -n --arg f "$HOME/.claude/meta-dispatch/last-seen.json" --arg d "$META_CWD" \
  '{tool_name:"Write",tool_input:{file_path:$f},cwd:$d}' | env -u HOME bash "$HOOK")"
if [ -n "$out_nohome" ]; then
  fail "P-unset-home" "expected passthrough with HOME unset, got: $out_nohome"
else
  ok "P-unset-home  (passthrough)"
fi

echo
echo "==================================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: ${FAILED_LABELS[*]}"
  exit 1
fi
echo "ALL ASSERTIONS PASSED"
