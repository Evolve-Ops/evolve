#!/bin/bash
# Regression guard for tools/hooks/prepend-meta-prefix.sh — the PreToolUse(Agent,
# mcp__ccd_session__spawn_task) hook that prepends "[META:<id>] " to spawned chip /
# subagent titles when an active-aspect marker exists for the spawning session's cwd.
#
# The hook intercepts EVERY Agent + spawn_task call, so a behavior change is
# high-blast-radius: this test is the regression guard. Run it after any edit to the
# hook OR to tools/hooks/meta-active-aspect.sh (the marker writer).
#
#   bash tools/hooks/test_prepend_meta_prefix.sh
#
# Exits 0 only if every assertion passes; non-zero (and prints which cases failed)
# otherwise. Requires `jq` and `shasum`/`sha256sum` (the hook's own deps).
#
# ISOLATION: every invocation runs with HOME pointed at a throwaway temp dir, so the
# test NEVER reads or writes the operator's real ~/.claude/meta-state. Markers are
# written via the REAL helper (meta-active-aspect.sh) and read by the hook — so the
# PREFIX assertions double as the end-to-end guard that the helper's maa_key and the
# hook's inline key computation still agree (drift would make every marker "missing").

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/prepend-meta-prefix.sh"
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

# Isolated HOME (marker store) + a couple of real dirs to key markers against.
TEST_HOME="$(mktemp -d)"
WORK="$(mktemp -d)"        # the META coordinator's working dir (gets a marker)
NOMARK="$(mktemp -d)"      # a dir that NEVER gets a marker
cleanup() { rm -rf "$TEST_HOME" "$WORK" "$NOMARK"; }
trap cleanup EXIT

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

# Write a marker for $WORK with the given id, via the REAL helper, isolated HOME.
write_marker() { HOME="$TEST_HOME" bash "$HELPER" write "$1" "$WORK" 2>/dev/null; }
clear_marker() { HOME="$TEST_HOME" bash "$HELPER" clear "$WORK" 2>/dev/null; }
# Force a raw marker value the helper would refuse to write (for bad-id / empty cases).
force_marker_raw() {
  local raw="$1"
  local key m
  key="$(HOME="$TEST_HOME" bash "$HELPER" key "$WORK")"
  m="$TEST_HOME/.claude/meta-state/active-aspect/$key"
  mkdir -p "$(dirname "$m")"
  printf '%s' "$raw" > "$m"
}

# Run the hook with isolated HOME; echo raw stdout (empty == passthrough).
run_hook() { printf '%s' "$1" | HOME="$TEST_HOME" bash "$HOOK"; }

field_of() { printf '%s' "$1" | jq -r ".hookSpecificOutput.updatedInput.$2 // empty" 2>/dev/null; }

# assert_prefix <label> <tool> <field> <input-json> <expected-field-value>
#   Asserts: hook REWROTE (non-empty), the named display field equals the expected
#   prefixed value, NO permissionDecision is present (must NOT auto-approve), and every
#   OTHER tool_input field is preserved value-for-value.
assert_prefix() {
  local label="$1" tool="$2" fld="$3" in_json="$4" expect="$5"
  local out got perm orig_rest new_rest
  out="$(run_hook "$in_json")"
  if [ -z "$out" ]; then
    fail "$label" "expected a REWRITE but hook emitted nothing (passthrough)"
    return
  fi
  if ! printf '%s' "$out" | jq -e '.hookSpecificOutput.updatedInput' >/dev/null 2>&1; then
    fail "$label" "output was not valid updatedInput JSON: $out"
    return
  fi
  # CRITICAL safety assertion: the hook must NOT set a permissionDecision (no auto-approve).
  perm="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "ABSENT"' 2>/dev/null)"
  if [ "$perm" != "ABSENT" ]; then
    fail "$label" "hook set permissionDecision='$perm' — MUST be absent (no auto-approve)"
    return
  fi
  got="$(field_of "$out" "$fld")"
  if [ "$got" != "$expect" ]; then
    fail "$label" "field .$fld = '$got', expected '$expect'"
    return
  fi
  # Every field EXCEPT the title must be byte-identical to the original tool_input.
  orig_rest="$(printf '%s' "$in_json" | jq -S "del(.tool_input.$fld) | .tool_input" 2>/dev/null)"
  new_rest="$(printf '%s' "$out" | jq -S "del(.hookSpecificOutput.updatedInput.$fld) | .hookSpecificOutput.updatedInput" 2>/dev/null)"
  if [ "$orig_rest" != "$new_rest" ]; then
    fail "$label" "non-title fields changed:\n      orig=$orig_rest\n      new =$new_rest"
    return
  fi
  ok "$label  ->  .$fld='$got' (other fields preserved, no permissionDecision)"
}

# assert_passthrough <label> <input-json>
#   Asserts the hook emits NOTHING and exits 0 (spawn proceeds unchanged).
assert_passthrough() {
  local label="$1" in_json="$2" out rc
  out="$(run_hook "$in_json")"; rc=$?
  if [ "$rc" -ne 0 ]; then
    fail "$label" "expected exit 0, got $rc"
    return
  fi
  if [ -n "$out" ]; then
    fail "$label" "expected PASSTHROUGH (empty) but hook emitted: $out"
    return
  fi
  ok "$label  (passthrough, exit 0)"
}

echo "== PREFIX cases (marker present; also proves helper/hook key algorithms agree) =="

write_marker "rsi"
assert_prefix "A1 Agent.description" "Agent" "description" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"Fix the thing",prompt:"body",subagent_type:"general-purpose",model:"sonnet"}}')" \
  "[META:rsi] Fix the thing"

assert_prefix "A2 spawn_task.title" "mcp__ccd_session__spawn_task" "title" \
  "$(jq -n --arg c "$WORK" '{tool_name:"mcp__ccd_session__spawn_task",cwd:$c,tool_input:{title:"Remove dead config",prompt:"do it",tldr:"cleanup",cwd:"/some/inner/path"}}')" \
  "[META:rsi] Remove dead config"

# Different valid id round-trips verbatim. `model-tiers` is the longest REAL aspect id
# (11 chars) and exceeds the carve protocol's nominal <=10 — this case is the regression
# guard that the validator's length bound covers every real aspect (a literal {0,9} would
# have silently never prefixed this whole aspect).
clear_marker; write_marker "model-tiers"
assert_prefix "A3 longest-real-id (model-tiers, 11 chars)" "Agent" "description" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"Audit tiers"}}')" \
  "[META:model-tiers] Audit tiers"

echo
echo "== PASSTHROUGH cases (must emit nothing + exit 0; spawn unchanged) =="

# Idempotent: already-[META:-prefixed titles are never touched (even a DIFFERENT aspect).
clear_marker; write_marker "rsi"
assert_passthrough "P1 already-prefixed Agent (same id)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"[META:rsi] Already done"}}')"
assert_passthrough "P2 already-prefixed spawn_task (other id)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"mcp__ccd_session__spawn_task",cwd:$c,tool_input:{title:"[META:edr] Other aspect"}}')"

# No marker for this cwd => non-META session => never touched.
assert_passthrough "P3 no marker (different cwd)" \
  "$(jq -n --arg c "$NOMARK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"untouched"}}')"

# Marker keying is per-exact-dir: a SUBDIR of the coordinator dir has no marker.
mkdir -p "$WORK/sub"
assert_passthrough "P3b subdir of marked dir (different key)" \
  "$(jq -n --arg c "$WORK/sub" '{tool_name:"Agent",cwd:$c,tool_input:{description:"untouched"}}')"

# Bad marker content => never inject junk into a title.
force_marker_raw "RSI"          # uppercase: fails ^[a-z0-9]...
assert_passthrough "P4 bad-id uppercase" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"
force_marker_raw "$(printf 'a%.0s' {1..40})"   # 40 chars > 31-char bound
assert_passthrough "P5 bad-id too-long (>31)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"
force_marker_raw "bad id"       # internal space: must NOT be sanitized to "badid"
assert_passthrough "P6 bad-id internal-space (no silent sanitize)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"
force_marker_raw ""             # empty marker file
assert_passthrough "P7 empty marker" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"

# Anti-injection: a tampered marker carrying shell / jq metacharacters must be rejected
# by the strict-kebab validator, never spliced into the title or any command.
force_marker_raw 'a$(touch /tmp/maa_pwned)'
assert_passthrough "P7b injection cmd-subst" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"
force_marker_raw '../../etc/passwd'
assert_passthrough "P7c injection path-traversal" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"
force_marker_raw 'a";evil'
assert_passthrough "P7d injection quote-break" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"x"}}')"

# Missing / empty display field => nothing to prefix.
clear_marker; write_marker "rsi"
assert_passthrough "P8 missing description" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{prompt:"only a prompt"}}')"
assert_passthrough "P9 empty title" \
  "$(jq -n --arg c "$WORK" '{tool_name:"mcp__ccd_session__spawn_task",cwd:$c,tool_input:{title:""}}')"
# Non-string title (number / array / null) => explicit type-guard passthrough.
assert_passthrough "P9b number title" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:12345}}')"
assert_passthrough "P9c array title" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:["a","b"]}}')"
assert_passthrough "P9d null tool_input" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:null}')"

# Missing cwd => can't key a marker => passthrough.
assert_passthrough "P10 missing cwd" \
  '{"tool_name":"Agent","tool_input":{"description":"no cwd"}}'

# Unrecognized tool => not our surface => passthrough (defense in depth vs. the matcher).
assert_passthrough "P11 wrong tool (Bash)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Bash",cwd:$c,tool_input:{command:"ls"}}')"
assert_passthrough "P12 wrong tool (Write)" \
  "$(jq -n --arg c "$WORK" '{tool_name:"Write",cwd:$c,tool_input:{file_path:"/x",content:"y"}}')"

# Malformed JSON on stdin => exit 0, no output.
assert_passthrough "P13 malformed JSON" 'this is not json {'
assert_passthrough "P14 empty stdin" ''

echo
echo "==================================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: ${FAILED_LABELS[*]}"
  exit 1
fi
echo "ALL ASSERTIONS PASSED"
