#!/usr/bin/env bash
# Stop hook: when Claude finishes a turn that mutated files, run an
# adversarial "try to refute this" review and surface findings BEFORE the
# turn ends. The model sees the findings via stderr and gets a chance to fix
# in-session rather than letting the bug ship to a later PR.
#
# Wiring (in .claude/settings.json):
#   {
#     "hooks": {
#       "Stop": [
#         { "hooks": [{ "type": "command",
#                       "command": ".claude/hooks/stop-verify.sh" }] }
#       ]
#     }
#   }
#
# Exit codes (Claude Code Stop hook semantics):
#   0   — allow stop (normal end-of-turn)
#   2   — block stop; stderr is fed back to the model as a continuation
#         instruction. Used here to force fix-up of BLOCKING findings.
#
# Skip conditions (exit 0):
#   - working tree is clean (no changes this turn)
#   - stop_hook_active=true (already in a continuation, don't loop)
#   - EVOLVE_SKIP_STOP_VERIFY=1 in env
#   - diff is purely additive docs/markdown
#   - diff exceeds size cap (sweep change — leave it to pre-PR gate)

set -u

# Recursion guard: short-circuit when this hook fired inside a `claude -p`
# launched by another hook. Without this, an inner review session would
# fire its own Stop hook and try to review its own (empty) diff.
if [[ "${EVOLVE_HOOK_NESTED:-0}" == "1" ]]; then
  exit 0
fi

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=.claude/hooks/lib/review.sh
source "${HOOK_DIR}/lib/review.sh"

INPUT="$(cat)"

# Avoid infinite continuation: if Claude Code is already running this hook
# inside a continuation, just allow the stop.
STOP_ACTIVE="$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try:
  d = json.load(sys.stdin)
  print(str(d.get("stop_hook_active", False)).lower())
except Exception:
  print("false")
')"
if [[ "$STOP_ACTIVE" == "true" ]]; then
  exit 0
fi

if [[ "${EVOLVE_SKIP_STOP_VERIFY:-0}" == "1" ]]; then
  exit 0
fi

# Working tree changes only — both staged + unstaged.
if [[ -z "$(git status --porcelain 2>/dev/null)" ]]; then
  exit 0
fi

# Compose diff: tracked changes (staged + unstaged) PLUS brand-new untracked
# files surfaced via `git diff --no-index /dev/null <file>`. Without the
# untracked sweep the reviewer can't see files Claude created this turn.
DIFF_CMD='{ git diff HEAD --; while IFS= read -r f; do [ -z "$f" ] || git diff --no-index --no-color /dev/null "$f" 2>/dev/null || true; done <<< "$(git ls-files --others --exclude-standard)"; }'

# Names list (tracked + untracked) for the docs-only short-circuit.
ALL_CHANGED="$(
  git diff HEAD --name-only
  git ls-files --others --exclude-standard
)"
NON_DOC="$(printf '%s\n' "$ALL_CHANGED" | grep -vE '\.(md|txt|rst)$' | grep -vE '^docs/' || true)"
if [[ -z "$NON_DOC" ]]; then
  exit 0
fi

echo "[stop-verify] reviewing working-tree changes ..." >&2
REVIEW_OUTPUT="$(review_diff_blocking "$DIFF_CMD" 4000 "in-session change")"
RC=$?

if (( RC == 2 )); then
  # Stop hook: writing JSON to stdout with decision=block tells Claude Code
  # to continue the turn with `reason` injected. This is the documented path
  # for Stop hooks (exit 2 also works but the JSON form is clearer).
  REASON="Adversarial verifier flagged blocking issues in this turn's changes. Address them before finishing.

${REVIEW_OUTPUT}

To override, set EVOLVE_SKIP_STOP_VERIFY=1 (use sparingly)."
  python3 -c "
import json, sys
print(json.dumps({'decision': 'block', 'reason': sys.argv[1]}))
" "$REASON"
  exit 0
fi

# Pass — surface advisories quietly.
NOTES="$(printf '%s\n' "$REVIEW_OUTPUT" | grep -E '^NOTE:' || true)"
if [[ -n "$NOTES" ]]; then
  echo "[stop-verify] advisory:" >&2
  echo "$NOTES" >&2
fi

exit 0
