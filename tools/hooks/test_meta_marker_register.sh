#!/bin/bash
# Regression guard for tools/hooks/meta-marker-register-on-start.sh — the SessionStart hook
# that makes a [META:<id>]-titled session self-register its active-aspect marker, so the
# prefix propagates down the whole spawn tree (spec-substrate-2026-06-15 §14, Initiative 9).
#
#   bash tools/hooks/test_meta_marker_register.sh
#
# Exits 0 only if every assertion passes; non-zero (and prints which cases failed) otherwise.
# Requires `jq` and `shasum`/`sha256sum` (the hook's own deps).
#
# ISOLATION: every invocation runs with HOME pointed at a throwaway temp dir, so the test
# NEVER reads or writes the operator's real ~/.claude/meta-state.
#
# THE CENTRAL GUARD (why this test also exercises the OTHER two scripts): the whole point of
# the hook is that the marker it writes is the SAME file prepend-meta-prefix.sh later reads.
# So the REGISTER cases assert the marker landed at the key the REAL helper
# (meta-active-aspect.sh) computes, AND then feed prepend-meta-prefix.sh a spawn payload with
# the same cwd and prove it now prefixes — an end-to-end round-trip across all three scripts.
# Drift in any of the three key algorithms makes that round-trip fail here.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/meta-marker-register-on-start.sh"
HELPER="$SCRIPT_DIR/meta-active-aspect.sh"
PREFIX_HOOK="$SCRIPT_DIR/prepend-meta-prefix.sh"

if [ ! -x "$HOOK" ]; then
  echo "FATAL: hook not found or not executable: $HOOK" >&2
  exit 2
fi
if [ ! -x "$HELPER" ]; then
  echo "FATAL: marker helper not found or not executable: $HELPER" >&2
  exit 2
fi
if [ ! -x "$PREFIX_HOOK" ]; then
  echo "FATAL: prefix hook not found or not executable: $PREFIX_HOOK" >&2
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
WORK="$(mktemp -d)"        # a real dir the markers get keyed against
PWNED="/tmp/maa_register_pwned_$$"   # injection canary (must never be created)
cleanup() { rm -rf "$TEST_HOME" "$WORK"; rm -f "$PWNED"; }
trap cleanup EXIT

MARKER_DIR="$TEST_HOME/.claude/meta-state/active-aspect"

PASS=0
FAIL=0
FAILED_LABELS=()
fail() { FAIL=$((FAIL + 1)); FAILED_LABELS+=("$1"); printf 'FAIL  %s\n      %s\n' "$1" "$2"; }
ok()   { PASS=$((PASS + 1)); printf 'ok    %s\n' "$1"; }

clean_markers() { rm -rf "$MARKER_DIR" 2>/dev/null || true; }
# Count regular files under the marker dir (0 if the dir doesn't exist). A clean register
# leaves exactly one file (the key) and no leftover .tmp; a no-register leaves zero.
marker_count() { find "$MARKER_DIR" -type f 2>/dev/null | wc -l | tr -d ' '; }
# The key the REAL helper computes for $WORK (the byte-match the hook must hit).
helper_key() { HOME="$TEST_HOME" bash "$HELPER" key "$WORK" 2>/dev/null; }
# Content of the marker at the helper's key for $WORK (empty if absent).
marker_at_helper_key() { cat "$MARKER_DIR/$(helper_key)" 2>/dev/null; }

# Build a SessionStart payload. Pass title + cwd; either may be the literal "<OMIT>" to drop
# that field entirely (for the missing-field cases).
payload() {
  local t="$1" c="$2"
  if [ "$t" = "<OMIT>" ] && [ "$c" = "<OMIT>" ]; then
    jq -n '{source:"startup"}'
  elif [ "$t" = "<OMIT>" ]; then
    jq -n --arg c "$c" '{source:"startup",cwd:$c}'
  elif [ "$c" = "<OMIT>" ]; then
    jq -n --arg t "$t" '{source:"startup",session_title:$t}'
  else
    jq -n --arg t "$t" --arg c "$c" '{source:"startup",session_title:$t,cwd:$c}'
  fi
}

# Run the hook with isolated HOME; sets globals OUT (stdout) and RC (exit code).
run_hook() {
  OUT="$(printf '%s' "$1" | HOME="$TEST_HOME" bash "$HOOK")"; RC=$?
}

# assert_register <label> <title> <expected-id>
#   Asserts: hook exit 0, SILENT on stdout, exactly one marker written, and that marker sits
#   at the REAL helper's key with the expected id as content.
assert_register() {
  local label="$1" title="$2" want="$3"
  clean_markers
  run_hook "$(payload "$title" "$WORK")"
  if [ "$RC" -ne 0 ]; then fail "$label" "expected exit 0, got $RC"; return; fi
  if [ -n "$OUT" ]; then fail "$label" "SessionStart hook MUST be silent on stdout; emitted: $OUT"; return; fi
  if [ "$(marker_count)" != "1" ]; then fail "$label" "expected exactly 1 marker file, found $(marker_count)"; return; fi
  local got; got="$(marker_at_helper_key)"
  if [ "$got" != "$want" ]; then fail "$label" "marker at helper-key = '$got', expected '$want' (key mismatch or wrong id)"; return; fi
  ok "$label  ->  marker '$want' at helper key (silent, exit 0)"
}

# assert_no_register <label> <payload-json>
#   Asserts: hook exit 0, SILENT on stdout, and NO marker file written anywhere.
assert_no_register() {
  local label="$1" pl="$2"
  clean_markers
  run_hook "$pl"
  if [ "$RC" -ne 0 ]; then fail "$label" "expected exit 0, got $RC"; return; fi
  if [ -n "$OUT" ]; then fail "$label" "expected SILENCE, hook emitted: $OUT"; return; fi
  if [ "$(marker_count)" != "0" ]; then fail "$label" "expected NO marker, found $(marker_count): $(ls -A "$MARKER_DIR" 2>/dev/null)"; return; fi
  ok "$label  (no marker, silent, exit 0)"
}

echo "== REGISTER cases (title carries the tag => marker self-written at the reader's key) =="

assert_register "R1 chip title"        "[META:rsi] Coalesce the thing"   "rsi"
assert_register "R2 coordinator title" "META substrate"                  "substrate"
# `model-tiers` (11 chars) is the longest REAL aspect id and exceeds the carve protocol's
# nominal <=10 — regression guard that the {0,30} bound covers every real aspect.
assert_register "R3 longest real id"   "[META:model-tiers] Audit tiers"  "model-tiers"
# Numeric-leading + hyphenated kebab is valid.
assert_register "R4 hyphen+digit id"   "[META:user-value] x"             "user-value"

echo
echo "== END-TO-END: the marker this hook writes is the one prepend-meta-prefix.sh reads =="
# Register via the hook, then feed the PREFIX hook a spawn from the SAME cwd and assert it now
# prefixes. This is the byte-for-byte key-agreement guard across all three scripts.
clean_markers
run_hook "$(payload "[META:demo] x" "$WORK")"
spawn_in="$(jq -n --arg c "$WORK" '{tool_name:"Agent",cwd:$c,tool_input:{description:"Do a thing",prompt:"body"}}')"
prefixed="$(printf '%s' "$spawn_in" | HOME="$TEST_HOME" bash "$PREFIX_HOOK" \
            | jq -r '.hookSpecificOutput.updatedInput.description // empty' 2>/dev/null)"
if [ "$prefixed" = "[META:demo] Do a thing" ]; then
  ok "E1 prepend-meta-prefix.sh prefixed using the hook-written marker -> '$prefixed'"
else
  fail "E1 round-trip" "prefix hook produced '$prefixed', expected '[META:demo] Do a thing' (writer/reader key mismatch?)"
fi
# Same for the coordinator-title path: "META <id>" registration must also drive the prefix.
clean_markers
run_hook "$(payload "META edr" "$WORK")"
prefixed="$(printf '%s' "$spawn_in" | HOME="$TEST_HOME" bash "$PREFIX_HOOK" \
            | jq -r '.hookSpecificOutput.updatedInput.description // empty' 2>/dev/null)"
if [ "$prefixed" = "[META:edr] Do a thing" ]; then
  ok "E2 coordinator-title registration also drives the prefix -> '$prefixed'"
else
  fail "E2 round-trip" "prefix hook produced '$prefixed', expected '[META:edr] Do a thing'"
fi

echo
echo "== IDEMPOTENT / overwrite (last-writer-wins for the same cwd) =="
clean_markers
run_hook "$(payload "[META:rsi] first" "$WORK")"
run_hook "$(payload "[META:edr] second" "$WORK")"
if [ "$(marker_count)" = "1" ] && [ "$(marker_at_helper_key)" = "edr" ]; then
  ok "I1 second register overwrites (one file, content 'edr')"
else
  fail "I1 overwrite" "count=$(marker_count) content='$(marker_at_helper_key)' (expected 1 file, 'edr')"
fi

echo
echo "== NO-REGISTER cases (must write NOTHING + stay silent + exit 0) =="

# Plain, non-META titles are the common case and must never register.
assert_no_register "N1 plain title"            "$(payload "Fix the widget"        "$WORK")"
assert_no_register "N2 empty title"            "$(payload ""                       "$WORK")"
# Malformed ids inside a [META:...] tag must be REJECTED, never written.
assert_no_register "N3 internal space"         "$(payload "[META:Bad Id] x"        "$WORK")"
assert_no_register "N4 path traversal"         "$(payload "[META:../etc] x"        "$WORK")"
assert_no_register "N5 uppercase"              "$(payload "[META:RSI] x"           "$WORK")"
assert_no_register "N6 empty id"               "$(payload "[META:] x"              "$WORK")"
assert_no_register "N7 too-long id (>31)"      "$(payload "[META:$(printf 'a%.0s' {1..40})] x" "$WORK")"
# Coordinator form: only a single clean kebab token after "META " counts.
assert_no_register "N8 coordinator multi-word" "$(payload "META not a kebab"       "$WORK")"
assert_no_register "N9 coordinator bare"       "$(payload "META"                   "$WORK")"
assert_no_register "N10 META-prefixed word"    "$(payload "METADATA refresh"       "$WORK")"
# Missing payload fields / bad stdin.
assert_no_register "N11 missing session_title" "$(payload "<OMIT>"                 "$WORK")"
assert_no_register "N12 missing cwd"           "$(payload "[META:rsi] x"           "<OMIT>")"
assert_no_register "N13 missing both"          "$(payload "<OMIT>"                 "<OMIT>")"
assert_no_register "N14 malformed JSON"        'this is not json {'
assert_no_register "N15 empty stdin"           ''

echo
echo "== ANTI-INJECTION: a command-substitution tag must fail validation, run nothing ======"
clean_markers
rm -f "$PWNED"
run_hook "$(payload "[META:a\$(touch $PWNED)] x" "$WORK")"
if [ "$RC" -eq 0 ] && [ -z "$OUT" ] && [ "$(marker_count)" = "0" ] && [ ! -e "$PWNED" ]; then
  ok "J1 injection tag rejected (no marker, canary not created)"
else
  fail "J1 injection" "rc=$RC out='$OUT' count=$(marker_count) canary-exists=$([ -e "$PWNED" ] && echo yes || echo no)"
fi

echo
echo "==================================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: ${FAILED_LABELS[*]}"
  exit 1
fi
echo "ALL ASSERTIONS PASSED"
