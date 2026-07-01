#!/usr/bin/env bash
# PreToolUse hook: gate `gh pr create` on an adversarial review of the diff.
#
# Wiring (in .claude/settings.json):
#   {
#     "hooks": {
#       "PreToolUse": [
#         { "matcher": "Bash",
#           "hooks": [{ "type": "command",
#                       "command": ".claude/hooks/pre-pr-review.sh" }] }
#       ]
#     }
#   }
#
# The hook receives JSON on stdin (Claude Code's tool-call payload). It exits:
#   0   — allow PR creation (review passed, or no review needed)
#   2   — BLOCK PR creation; stderr is shown to the model so it can fix and retry
#
# Skip conditions (exit 0):
#   - command does not start with `gh pr create`
#   - all commits in PR are chore(/docs(/style( — mechanical sweeps
#   - any commit message contains [skip-review]
#   - EVOLVE_SKIP_PR_REVIEW=1 in env
#
# Escape hatch for emergencies:
#   EVOLVE_SKIP_PR_REVIEW=1 claude

set -u

# Recursion guard: if this hook fired from a `claude -p` invocation made by
# another hook (or by code that already vetted its diff), short-circuit.
if [[ "${EVOLVE_HOOK_NESTED:-0}" == "1" ]]; then
  exit 0
fi

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=.claude/hooks/lib/review.sh
source "${HOOK_DIR}/lib/review.sh"

# Read stdin payload once; hook_command consumes it.
INPUT="$(cat)"
CMD="$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try:
  d = json.load(sys.stdin)
  print(d.get("tool_input", {}).get("command", ""))
except Exception:
  print("")
')"

# Not a PR creation? pass through silently.
case "$CMD" in
  *"gh pr create"*) ;;
  *) exit 0 ;;
esac

if [[ "${EVOLVE_SKIP_PR_REVIEW:-0}" == "1" ]]; then
  echo "[pre-pr-review] EVOLVE_SKIP_PR_REVIEW=1 set; skipping" >&2
  exit 0
fi

# Determine base ref. Default to origin/main; fall back to main.
BASE="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || echo '')"
if [[ -z "$BASE" || "$BASE" == "HEAD" ]]; then
  if git rev-parse --verify origin/main >/dev/null 2>&1; then
    BASE="origin/main"
  else
    BASE="main"
  fi
fi

# Range of commits in this PR.
RANGE="${BASE}...HEAD"

# Skip-by-commit-types: if every commit on this branch is chore/docs/style/test, allow.
MSGS="$(git log --format='%s' "${RANGE}" 2>/dev/null || echo '')"
if [[ -z "$MSGS" ]]; then
  echo "[pre-pr-review] no commits vs ${BASE}; allowing" >&2
  exit 0
fi

# [skip-review] escape hatch anywhere in commit messages.
if grep -q '\[skip-review\]' <<<"$MSGS"; then
  echo "[pre-pr-review] [skip-review] tag found in commit; skipping" >&2
  exit 0
fi

# All commits are mechanical? Skip.
NON_MECH="$(grep -vE '^(chore|docs|style|test)(\(|:)' <<<"$MSGS" || true)"
if [[ -z "$NON_MECH" ]]; then
  echo "[pre-pr-review] all commits are chore/docs/style/test; skipping" >&2
  exit 0
fi

# Run the review.
echo "[pre-pr-review] reviewing ${RANGE} ..." >&2
REVIEW_OUTPUT="$(review_diff_blocking "git diff ${RANGE}" 8000 "PR change")"
RC=$?

if (( RC == 2 )); then
  {
    echo "═══════════════════════════════════════════════════════════════════"
    echo "PR creation BLOCKED by adversarial review."
    echo "═══════════════════════════════════════════════════════════════════"
    echo
    echo "$REVIEW_OUTPUT"
    echo
    echo "To override (use sparingly):"
    echo "  - add [skip-review] to a commit message, OR"
    echo "  - set EVOLVE_SKIP_PR_REVIEW=1 in env"
    echo "═══════════════════════════════════════════════════════════════════"
  } >&2
  exit 2
fi

if (( RC != 0 )); then
  echo "[pre-pr-review] review returned rc=${RC}; allowing (fail-open)" >&2
fi

# Surface advisories to stderr (visible to Claude but not blocking).
NOTES="$(printf '%s\n' "$REVIEW_OUTPUT" | grep -E '^NOTE:' || true)"
if [[ -n "$NOTES" ]]; then
  {
    echo "[pre-pr-review] advisory findings (non-blocking):"
    echo "$NOTES"
  } >&2
fi

exit 0
