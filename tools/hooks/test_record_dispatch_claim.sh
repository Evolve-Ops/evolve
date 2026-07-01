#!/bin/bash
# Regression guard for tools/hooks/record-dispatch-claim.sh — the PreToolUse(Agent,
# mcp__ccd_session__spawn_task) hook that RECORDS a dispatch claim to
# ~/.claude/meta-state/claims/<id>.json whenever a chip/subagent is spawned from a
# session with an active-aspect marker (META:substrate spec §17 / Initiative 12, B4).
#
# The hook intercepts EVERY Agent + spawn_task call, so a behavior change is
# high-blast-radius: this test is the regression guard. Run it after any edit to the
# hook OR to tools/hooks/meta-active-aspect.sh (the marker writer whose key algorithm
# the hook mirrors).
#
#   bash tools/hooks/test_record_dispatch_claim.sh
#
# The two CRITICAL safety properties it proves:
#   (1) the hook NEVER emits anything on stdout — no updatedInput, no permissionDecision —
#       so it can NEVER alter or block a spawn (record-side-effect only), and
#   (2) a claim is recorded ONLY for a real, validated active-aspect marker, and is
#       attributed to the marker's aspect (never mis-attributed / never junk).
#
# ISOLATION: every invocation runs with HOME pointed at a throwaway temp dir, so the
# test NEVER reads or writes the operator's real ~/.claude/meta-state. Markers are
# written via the REAL helper (meta-active-aspect.sh) — so the RECORD assertions double
# as the end-to-end guard that the helper's maa_key and the hook's inline key computation
# still agree (drift would make every marker "missing" and record nothing).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/record-dispatch-claim.sh"
HELPER="$SCRIPT_DIR/meta-active-aspect.sh"

if [ ! -x "$HOOK" ]; then
  echo "FATAL: hook not found or not executable: $HOOK" >&2
  exit 2
fi
if [ ! -x "$HELPER" ]; then
  echo "FATAL: marker helper not found or not executable: $HELPER" >&2
  exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "FATAL: required dependency 'jq' not on PATH" >&2
  exit 2
fi
if ! command -v shasum >/dev/null 2>&1 && ! command -v sha256sum >/dev/null 2>&1; then
  echo "FATAL: need 'shasum' or 'sha256sum' on PATH" >&2
  exit 2
fi

TEST_HOME="$(mktemp -d)"
WORK="$(mktemp -d)"        # the META coordinator's working dir (gets a marker)
NOMARK="$(mktemp -d)"      # a dir that NEVER gets a marker
cleanup() { rm -rf "$TEST_HOME" "$WORK" "$NOMARK"; }
trap cleanup EXIT

CLAIMS_DIR="$TEST_HOME/.claude/meta-state/claims"

PASS=0
FAIL=0
FAILED_LABELS=()

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

write_marker() { HOME="$TEST_HOME" bash "$HELPER" write "$1" "$WORK" 2>/dev/null; }
clear_marker() { HOME="$TEST_HOME" bash "$HELPER" clear "$WORK" 2>/dev/null; }
force_marker_raw() {
  local raw="$1" key m
  key="$(HOME="$TEST_HOME" bash "$HELPER" key "$WORK")"
  m="$TEST_HOME/.claude/meta-state/active-aspect/$key"
  mkdir -p "$(dirname "$m")"
  printf '%s' "$raw" > "$m"
}

# Empty the claims dir between cases so each asserts on its own writes.
reset_claims() { rm -rf "$CLAIMS_DIR" 2>/dev/null; }
claim_count() { ls -1 "$CLAIMS_DIR"/*.json 2>/dev/null | wc -l | tr -d ' '; }
# The single claim's JSON (assumes exactly one present).
only_claim() { cat "$CLAIMS_DIR"/*.json 2>/dev/null; }

# Run the hook with isolated HOME; echo raw stdout (empty == passthrough), capture rc.
LAST_RC=0
run_hook() { local out; out="$(printf '%s' "$1" | HOME="$TEST_HOME" bash "$HOOK")"; LAST_RC=$?; printf '%s' "$out"; }

# assert_records <label> <input-json> <expected-aspect> <expected-title>
#   Asserts: stdout EMPTY (passthrough — no updatedInput/permissionDecision), exit 0,
#   EXACTLY ONE claim written, with the expected aspect + title and a positive ttl/time.
assert_records() {
  local label="$1" in_json="$2" exp_aspect="$3" exp_title="$4"
  reset_claims
  local out; out="$(run_hook "$in_json")"
  if [ "$LAST_RC" -ne 0 ]; then fail "$label" "expected exit 0, got $LAST_RC"; return; fi
  if [ -n "$out" ]; then fail "$label" "hook emitted stdout (must be side-effect-only): $out"; return; fi
  local n; n="$(claim_count)"
  if [ "$n" != "1" ]; then fail "$label" "expected exactly 1 claim, found $n"; return; fi
  local j; j="$(only_claim)"
  local got_aspect got_title got_ttl got_time
  got_aspect="$(printf '%s' "$j" | jq -r '.aspect // ""')"
  got_title="$(printf '%s' "$j" | jq -r '.title // ""')"
  got_ttl="$(printf '%s' "$j" | jq -r '.ttl_seconds // 0')"
  got_time="$(printf '%s' "$j" | jq -r '.time // 0')"
  if [ "$got_aspect" != "$exp_aspect" ]; then fail "$label" "aspect='$got_aspect', expected '$exp_aspect'"; return; fi
  if [ "$got_title" != "$exp_title" ]; then fail "$label" "title='$got_title', expected '$exp_title'"; return; fi
  if ! [[ "$got_ttl" =~ ^[1-9][0-9]*$ ]]; then fail "$label" "ttl_seconds not a positive int: '$got_ttl'"; return; fi
  if ! [[ "$got_time" =~ ^[1-9][0-9]*$ ]]; then fail "$label" "time not a positive epoch int: '$got_time'"; return; fi
  ok "$label  ->  aspect='$got_aspect' title='$got_title' ttl=$got_ttl (stdout empty, exit 0)"
}

# assert_no_claim <label> <input-json>
#   Asserts the hook emits NOTHING, exits 0, and records NO claim (passthrough).
assert_no_claim() {
  local label="$1" in_json="$2"
  reset_claims
  local out; out="$(run_hook "$in_json")"
  if [ "$LAST_RC" -ne 0 ]; then fail "$label" "expected exit 0, got $LAST_RC"; return; fi
  if [ -n "$out" ]; then fail "$label" "expected passthrough (empty) but hook emitted: $out"; return; fi
  local n; n="$(claim_count)"
  if [ "$n" != "0" ]; then fail "$label" "expected 0 claims, found $n"; return; fi
  ok "$label  (no claim, passthrough, exit 0)"
}

echo "== RECORD cases (marker present; proves helper/hook key algorithms agree) =="

write_marker "substrate"
assert_records "R1 Agent.description" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,session_id:"s1",tool_input:{description:"Fix the thing",prompt:"body",subagent_type:"general-purpose"}}')" \
  "substrate" "Fix the thing"

assert_records "R2 spawn_task.title" \
  "$(jq -n --arg c "$WORK" '{tool_name:"mcp__ccd_session__spawn_task",cwd:$c,tool_input:{title:"Remove dead config",prompt:"do it",tldr:"cleanup"}}')" \
  "substrate" "Remove dead config"

# Longest real aspect id round-trips (model-tiers, 11 chars > carve nominal <=10).
clear_marker; write_marker "model-tiers"
assert_records "R3 longest-real-id (model-tiers)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"Audit tiers"}}')" \
  "model-tiers" "Audit tiers"

# A title carrying quotes / braces / jq-metacharacters must be SAFELY encoded, not break
# the JSON write (jq --arg, never string-concat) — and must round-trip byte-for-byte.
clear_marker; write_marker "substrate"
TRICKY='weird "title" {with} $(meta) & chars'
assert_records "R4 title with quotes/braces/metachars" \
  "$(jq -n --arg c "$WORK" --arg t "$TRICKY" '{tool_name:"Agent",cwd:$c,tool_input:{description:$t}}')" \
  "substrate" "$TRICKY"

# An already-[META:]-prefixed title is STILL recorded (unlike the prefix hook, which is
# idempotent on the title). The claim just carries the full prefixed title verbatim.
assert_records "R5 already-prefixed title still records" \
  "$(jq -n --arg c "$WORK" '{tool_name:"mcp__ccd_session__spawn_task",cwd:$c,tool_input:{title:"[META:substrate] already tagged"}}')" \
  "substrate" "[META:substrate] already tagged"

echo
echo "== NO-CLAIM cases (must record nothing + emit nothing + exit 0; spawn unchanged) =="

# No marker for this cwd => non-META session => never recorded.
assert_no_claim "N1 no marker (different cwd)" \
  "$(jq -n --arg c "$NOMARK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"untouched"}}')"

# Subdir of the marked dir has a different key => no marker => no claim.
mkdir -p "$WORK/sub"
assert_no_claim "N1b subdir of marked dir (different key)" \
  "$(jq -n --arg c "$WORK/sub" '{tool_name:"Agent",cwd:$c,tool_input:{description:"untouched"}}')"

# Bad marker content => never record a claim with a junk / injected aspect.
force_marker_raw "SUBSTRATE"      # uppercase: fails ^[a-z0-9]...
assert_no_claim "N2 bad-id uppercase" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"
force_marker_raw "$(printf 'a%.0s' {1..40})"   # 40 chars > 31-char bound
assert_no_claim "N3 bad-id too-long" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"
force_marker_raw "bad id"         # internal space: must NOT be coerced to "badid"
assert_no_claim "N4 bad-id internal-space" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"
force_marker_raw 'a$(touch /tmp/rdc_pwned)'    # cmd-substitution injection
assert_no_claim "N5 injection cmd-subst" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"
[ -e /tmp/rdc_pwned ] && { fail "N5-side-effect" "injection marker EXECUTED — /tmp/rdc_pwned exists"; rm -f /tmp/rdc_pwned; }
force_marker_raw ""               # empty marker file
assert_no_claim "N6 empty marker" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"

# Missing / empty / non-string display field => nothing to record.
clear_marker; write_marker "substrate"
assert_no_claim "N7 missing description" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{prompt:"only a prompt"}}')"
assert_no_claim "N8 empty title" \
  "$(jq -n --arg c "$WORK" '{tool_name:"mcp__ccd_session__spawn_task",cwd:$c,tool_input:{title:""}}')"
assert_no_claim "N9 number title (non-string)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:12345}}')"
assert_no_claim "N9b array title (non-string)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:["a","b"]}}')"
assert_no_claim "N9c null tool_input" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:null}')"

# Missing cwd => can't key a marker => passthrough.
assert_no_claim "N10 missing cwd" \
  '{"tool_name":"Agent","tool_input":{"description":"no cwd"}}'

# Unrecognized tool => not our surface => passthrough (defense in depth vs. the matcher).
assert_no_claim "N11 wrong tool (Bash)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Bash",cwd:$c,tool_input:{command:"ls"}}')"
assert_no_claim "N12 wrong tool (Write)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Write",cwd:$c,tool_input:{file_path:"/x",content:"y"}}')"

# Malformed / empty stdin => exit 0, no output, no claim.
assert_no_claim "N13 malformed JSON" 'this is not json {'
assert_no_claim "N14 empty stdin" ''

echo
echo "== TTL override =="
clear_marker; write_marker "substrate"
reset_claims
OUT="$(printf '%s' "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"ttl test"}}')" | HOME="$TEST_HOME" META_CLAIM_TTL_SECONDS=60 bash "$HOOK")"
TTL="$(only_claim | jq -r '.ttl_seconds // 0')"
if [ "$TTL" = "60" ]; then ok "T1 META_CLAIM_TTL_SECONDS honored (ttl=60)"; else fail "T1 ttl override" "ttl=$TTL, expected 60"; fi
# A bogus TTL falls back to the 14400 default (never records a non-positive/garbage ttl).
reset_claims
OUT="$(printf '%s' "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"ttl bad"}}')" | HOME="$TEST_HOME" META_CLAIM_TTL_SECONDS=nonsense bash "$HOOK")"
TTL="$(only_claim | jq -r '.ttl_seconds // 0')"
if [ "$TTL" = "14400" ]; then ok "T2 bogus TTL falls back to 14400"; else fail "T2 ttl fallback" "ttl=$TTL, expected 14400"; fi

echo
echo "==================================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: ${FAILED_LABELS[*]}"
  exit 1
fi
echo "ALL ASSERTIONS PASSED"
