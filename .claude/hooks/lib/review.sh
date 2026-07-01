#!/usr/bin/env bash
# Shared review primitives for .claude/hooks/.
#
# review_diff_blocking <diff-source-cmd> <max-loc> <context-label>
#   Streams diff from <diff-source-cmd> into a headless claude review.
#   Echoes the review to stdout. Returns 0 if PASS, 2 if BLOCK, 1 on error.
#
# All hooks share one model + prompt shape so findings are consistent across
# entry points (pre-PR gate, Stop-time verify, ad-hoc).

set -u

# Model: fast tier by default; override with EVOLVE_REVIEW_MODEL.
REVIEW_MODEL="${EVOLVE_REVIEW_MODEL:-claude-sonnet-4-6}"

# Headless invocation. claude -p prints the assistant response and exits.
# --output-format text keeps parsing trivial.
review_diff_blocking() {
  local diff_cmd="$1"
  local max_loc="${2:-8000}"
  local context_label="${3:-change}"

  if ! command -v claude >/dev/null 2>&1; then
    echo "[review] claude CLI not on PATH; skipping" >&2
    return 0
  fi

  local diff
  diff="$(eval "$diff_cmd")"
  if [[ -z "$diff" ]]; then
    echo "[review] empty diff, nothing to review" >&2
    return 0
  fi

  local loc
  loc="$(printf '%s\n' "$diff" | wc -l | tr -d ' ')"
  if (( loc > max_loc )); then
    echo "[review] diff is ${loc} lines (>${max_loc}); skipping deep review for sweep-shaped change" >&2
    echo "VERDICT: PASS (skipped: diff too large for deep review)"
    return 0
  fi

  local prompt
  prompt="You are an adversarial code reviewer. Your job is to find BLOCKING bugs in this ${context_label} — defects that would break functionality, corrupt state, leak credentials, deadlock, or regress existing behavior.

Rules:
- Flag only what you can verify from the diff itself. Do not speculate about code you cannot see.
- Ignore style, naming, minor performance, and missing tests unless they cause a bug.
- A finding is BLOCKING only if a reasonable reviewer would say 'do not ship this until fixed'.
- Prefer false negatives over false positives. When in doubt, do not flag.

Output format:
1. For each BLOCKING finding: a single line starting with 'BLOCK:' then file:line and one-sentence explanation.
2. For each advisory (nice-to-fix-later): a single line starting with 'NOTE:' then file:line and explanation.
3. Final line MUST be exactly one of:
   VERDICT: PASS
   VERDICT: BLOCK

Diff follows:

\`\`\`diff
${diff}
\`\`\`"

  local out
  # EVOLVE_HOOK_NESTED=1 is read by both hook scripts at the top and short-
  # circuits them — without it, claude -p would fire Stop hooks of its own
  # and recurse. The 4-minute timeout is a hard upper bound; claude -p
  # normally completes in 30-60s for diffs under the size cap.
  out="$(printf '%s' "$prompt" | EVOLVE_HOOK_NESTED=1 claude -p --model "$REVIEW_MODEL" 2>&1)" || {
    echo "[review] claude -p failed; allowing through" >&2
    return 0
  }

  printf '%s\n' "$out"

  local verdict
  verdict="$(printf '%s\n' "$out" | grep -E '^VERDICT:' | tail -n1)"
  if [[ "$verdict" == *"BLOCK"* ]]; then
    return 2
  fi
  return 0
}

# Read tool_input.command from hook stdin JSON.
hook_command() {
  local stdin
  stdin="$(cat)"
  printf '%s' "$stdin" | python3 -c 'import json,sys
try:
  d = json.load(sys.stdin)
  print(d.get("tool_input", {}).get("command", ""))
except Exception:
  print("")
'
}
