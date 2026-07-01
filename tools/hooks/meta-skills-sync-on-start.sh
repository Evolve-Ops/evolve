#!/bin/bash
# SessionStart hook — auto-refresh the operator-global ~/.claude/skills mirror from
# origin/main on every session start, so the cross-branch / cross-account skill copies
# never silently rot. The mirror's ONLY other refresh path is a manual
# `git show origin/main:.claude/skills/<n>/SKILL.md > ~/.claude/skills/<n>/SKILL.md` loop;
# when that is forgotten the mirror goes stale (proven: 8 days, 2026-06-22 — long enough
# that a merged mechanism living only in the stale skills became a silent no-op). This hook
# delegates the work to tools/meta-skills-sync (which holds the contract + the fast path).
#
# FAIL-SAFE CONTRACT (worst case == today's behavior: a possibly-stale mirror; NEVER a
# blocked, delayed, or altered session start):
#   - `trap 'exit 0' EXIT` + `set -euo pipefail`: ANY error / unset var / pipe failure /
#     signal lands on the trap and exits 0. The hook cannot fail loudly.
#   - Emits NOTHING on stdout. Per the Claude Code hook docs, a SessionStart hook's stdout
#     is INJECTED INTO THE MODEL CONTEXT — so the sync tool's summary is swallowed
#     (>/dev/null) and this hook is silent by construction; it never pollutes the session.
#     (SessionStart also cannot block the session — its exit code is ignored — but silence
#     + speed are still required so it never *alters* or *delays* startup.)
#   - No network: meta-skills-sync reads the LOCAL origin/main ref and never fetches.
#   - Bounded time: meta-skills-sync's HEAD-unchanged fast path makes the common case
#     ~two git calls; the `timeout` on the settings.json entry is the hard backstop. The
#     session waits synchronously for SessionStart hooks, so this speed is the safety knob.
#   - No-ops silently outside an Evolve checkout: if no runnable copy of the sync tool is
#     found near the session's cwd (or in the default checkouts), the hook just exits 0.
#
# Skills load at session START, so a refresh here takes effect on the NEXT session —
# identical semantics to the manual loop ("restart to pick up a change"). The win is that
# the mirror self-heals every session instead of drifting for days.
#
# Maintained by META:substrate. To disable: remove the SessionStart block that points at
# this script from hooks.SessionStart in ~/.claude/settings.json.

# Belt-and-suspenders: force a clean exit on EVERY code path, before set -e can bite.
trap 'exit 0' EXIT
set -euo pipefail

input="$(cat 2>/dev/null || true)"

# Best-effort: the session's cwd (a SessionStart payload field) seeds repo discovery.
cwd=""
if [ -n "$input" ] && command -v jq >/dev/null 2>&1; then
  # Wrap the WHOLE pipeline's stderr (not just jq's): on a large malformed stdin, jq exits
  # and closes the pipe, so the `printf` builtin would otherwise emit a broken-pipe
  # diagnostic to stderr. Keep every stream clean.
  cwd="$( { printf '%s' "$input" | jq -r '.cwd // empty'; } 2>/dev/null || true)"
fi
[ -n "$cwd" ] || cwd="$PWD"

# The session's git toplevel is the first place the sync tool can live (handles worktrees,
# where the tool is in the worktree's own tree). We only need to LOCATE a runnable copy of
# the tool here; the tool itself does all the real validation (origin/main resolvable, skills
# present) and no-ops cleanly if its discovered repo turns out unusable.
top=""
if command -v git >/dev/null 2>&1; then
  top="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || true)"
fi

# Candidate copies, freshest first: the session's checkout, then the default clones (so even
# a NON-Evolve session keeps the global mirror current from ~/GitHub/evolve), then the
# operator-local install (the always-present fallback when no checkout has the file on disk).
tool=""
for cand in \
    "${top:+$top/tools/meta-skills-sync}" \
    "${EVOLVE_REPO:+$EVOLVE_REPO/tools/meta-skills-sync}" \
    "${HOME:-}/GitHub/evolve/tools/meta-skills-sync" \
    "/Users/Shared/evolve-repo/tools/meta-skills-sync" \
    "${HOME:-}/.claude/hooks/meta-skills-sync" ; do
  [ -n "$cand" ] || continue
  if [ -x "$cand" ]; then tool="$cand"; break; fi
done

# No runnable copy found => not near an Evolve checkout => silent no-op.
[ -n "$tool" ] || exit 0

# Run the sync. The tool self-discovers the repo from --cwd (+ the same defaults). Swallow
# ALL output (SessionStart stdout is model context); the EXIT trap also guarantees exit 0.
"$tool" --cwd "$cwd" >/dev/null 2>&1 || true

exit 0
