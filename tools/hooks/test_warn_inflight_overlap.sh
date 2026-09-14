#!/bin/bash
# Regression guard for tools/hooks/warn-inflight-overlap.sh — the PreToolUse(Bash) hook that
# WARNS (never blocks) when a branch-create overlaps work already in flight. It is the READ
# side of the dispatch-claim layer: record-dispatch-claim.sh makes self-work VISIBLE, this
# makes a self-working session LOOK.
#
# Two properties matter most and are pinned hardest:
#   1. It NEVER blocks. The emitted JSON must carry additionalContext and NOTHING else — no
#      permissionDecision, no updatedInput — or it would silently become a gate, contradicting
#      the operator's recorded warn-only policy (spec §17.8).
#   2. It is QUIET unless work is actually in flight. An early build warned on 19 overlaps,
#      18 of them backlog and two already shipped; a warning that cries wolf gets ignored.
#
# HOME is redirected to a temp dir so the marker, claim registry, and ledger dir are isolated;
# cwd stays the REAL checkout so `git rev-parse --show-toplevel` and tools/meta-inflight resolve.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/warn-inflight-overlap.sh"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd -P)"

for dep in jq python3 git shasum; do
  command -v "$dep" >/dev/null 2>&1 || { echo "FATAL: missing dependency '$dep'" >&2; exit 2; }
done
[ -x "$HOOK" ] || { echo "FATAL: hook not executable: $HOOK" >&2; exit 2; }

TEST_HOME="$(mktemp -d)"
trap 'rm -rf "$TEST_HOME"' EXIT

CLAIMS="$TEST_HOME/.claude/meta-state/claims"
MARKERS="$TEST_HOME/.claude/meta-state/active-aspect"
# meta-inflight resolves its ledger dir as $HOME/.claude/projects/*evolve*/memory/meta-state
LEDGERS="$TEST_HOME/.claude/projects/-Users-test-evolve/memory/meta-state"
mkdir -p "$CLAIMS" "$MARKERS" "$LEDGERS"

KEY="$(printf '%s' "$REPO" | shasum -a 256 | awk '{print $1}')"

PASS=0; FAIL=0; FAILED=()
ok()   { PASS=$((PASS+1)); printf 'ok    %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); printf 'FAIL  %s\n      %s\n' "$1" "$2"; }

set_marker()   { printf '%s\n' "$1" > "$MARKERS/$KEY"; }
clear_marker() { rm -f "$MARKERS/$KEY"; }
reset_claims() { rm -f "$CLAIMS"/*.json 2>/dev/null; }

write_claim() {  # <aspect> <title>
  python3 - "$CLAIMS" "$1" "$2" <<'PY'
import json, sys, time, os
d, aspect, title = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({"aspect": aspect, "title": title, "time": int(time.time()), "ttl_seconds": 14400},
          open(os.path.join(d, "c-%d.json" % len(os.listdir(d))), "w"))
PY
}

payload() {  # <tool> <command>
  jq -n --arg t "$1" --arg c "$REPO" --arg cmd "$2" \
    '{tool_name:$t,cwd:$c,tool_input:{command:$cmd}}'
}

run_hook() { OUT="$(printf '%s' "$1" | HOME="$TEST_HOME" bash "$HOOK" 2>/dev/null)"; RC=$?; }

# assert_silent <label> <payload>
assert_silent() {
  run_hook "$2"
  if [ "$RC" -ne 0 ]; then fail "$1" "expected exit 0, got $RC"; return; fi
  if [ -n "$OUT" ]; then fail "$1" "expected NO output, got: ${OUT:0:160}"; return; fi
  ok "$1  (silent, exit 0)"
}

# assert_warns <label> <payload> <substring that must appear>
assert_warns() {
  run_hook "$2"
  if [ "$RC" -ne 0 ]; then fail "$1" "expected exit 0, got $RC"; return; fi
  if [ -z "$OUT" ]; then fail "$1" "expected a warning, got nothing"; return; fi
  local ctx
  ctx="$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null)"
  if [ -z "$ctx" ]; then fail "$1" "no additionalContext in: ${OUT:0:160}"; return; fi
  case "$ctx" in *"$3"*) ;; *) fail "$1" "context missing '$3'"; return;; esac
  # THE contract: warn, never gate.
  local pd ui
  pd="$(printf '%s' "$OUT" | jq -r '.. | objects | .permissionDecision? // empty' 2>/dev/null)"
  ui="$(printf '%s' "$OUT" | jq -r '.. | objects | .updatedInput? // empty' 2>/dev/null)"
  if [ -n "$pd" ]; then fail "$1" "emitted a permissionDecision ('$pd') — this hook must never gate"; return; fi
  if [ -n "$ui" ]; then fail "$1" "emitted updatedInput — this hook must never rewrite a command"; return; fi
  ok "$1  (warns, no permissionDecision/updatedInput, exit 0)"
}

BRANCH_CMD="git checkout -b meta-model-tiers/tile-metrics-clock-seam"

echo "== WARN cases =="
set_marker "model-tiers"; reset_claims
write_claim "model-tiers" "[META:model-tiers] fix the clock-coupled tile_metrics tests (FLEET-BLOCKING)"
assert_warns "W1 sibling claim in flight surfaces (the 2026-08-27 incident)" \
  "$(payload Bash "$BRANCH_CMD")" "IN-FLIGHT OVERLAP"
assert_warns "W2 the sibling's title is named, not just a count" \
  "$(payload Bash "$BRANCH_CMD")" "clock-coupled"
assert_warns "W3 warning states it is advisory, not a stop" \
  "$(payload Bash "$BRANCH_CMD")" "ADVISORY"
assert_warns "W4 git -C form also checked" \
  "$(payload Bash "git -C $REPO switch -c meta-model-tiers/tile-metrics-clock-seam")" "IN-FLIGHT OVERLAP"

echo
echo "== SILENT cases (noise control + fail-safe) =="
reset_claims
assert_silent "S1 nothing in flight — no backlog/shipped noise" \
  "$(payload Bash "$BRANCH_CMD")"

# The hook must not report the claim that record-dispatch-claim.sh writes for THIS very
# branch-create. Exclusion is by VALUE (title match), never by hook ordering.
reset_claims
write_claim "model-tiers" "branch meta-model-tiers/tile-metrics-clock-seam (meta model tiers tile metrics clock seam)"
assert_silent "S2 our OWN just-written claim is not an overlap" \
  "$(payload Bash "$BRANCH_CMD")"

reset_claims
write_claim "model-tiers" "[META:model-tiers] fix the clock-coupled tile_metrics tests (FLEET-BLOCKING)"
assert_silent "S3 ordinary command (hot path — never runs meta-inflight)" \
  "$(payload Bash "git status --short")"
assert_silent "S4 checkout of an EXISTING branch" \
  "$(payload Bash "git checkout main")"
assert_silent "S5 branch-create with no keyword overlap" \
  "$(payload Bash "git checkout -b zzz/quuxfrobnicate")"
assert_silent "S6 non-Bash tool" \
  "$(payload Agent "$BRANCH_CMD")"
assert_silent "S7 malformed JSON" "{not json"
assert_silent "S8 empty stdin" ""

clear_marker
assert_silent "S9 no marker — a non-META session is never warned" \
  "$(payload Bash "$BRANCH_CMD")"

printf 'bad id\n' > "$MARKERS/$KEY"
assert_silent "S10 invalid aspect id in marker" \
  "$(payload Bash "$BRANCH_CMD")"
clear_marker

echo
echo "==================================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then echo "FAILED: ${FAILED[*]}"; exit 1; fi
echo "ALL ASSERTIONS PASSED"
