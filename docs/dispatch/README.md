# Dispatch specs — fan-out work for the 80→100 roadmap

Each file here is a **self-contained work order** for one roadmap item, written so a
fresh session on the named tier can execute it with no other context. The parent
plan is [docs/roadmap-80-to-100-2026-06-09.md](../roadmap-80-to-100-2026-06-09.md).

## How to dispatch

1. `git worktree add ../wt-<item> main` (parallel pieces don't collide).
2. Open a fresh session in that worktree, set the model (`/model` or `/fast`).
3. Paste: **"Execute `docs/dispatch/<item>.md`. Follow the acceptance criteria and
   guardrails exactly. Produce the proof artifact before reporting done."**
4. When it reports, the quarterback (Fable) session reviews + integrates.

## Tier rule

Each spec names a tier. The principle: **the spec carries the judgment, the session
carries the execution.** A spec tight enough — acceptance criteria + exact files +
proof artifact + "don't do X" — lets a lower tier succeed at work that would
otherwise need Fable. If a session hits a genuine design fork the spec didn't cover,
it should STOP and surface it to the quarterback, not improvise.

## Gotcha every session must respect

**No real names in tracked files.** The `test_public_launch_scrub.py` CI gate blocks a
set of reserved real bot/person names anywhere in committed files — including docs,
test fixtures, and comments. (The reserved list lives in that test and the mapping in
`docs/PLACEHOLDER_NAMING.md`; this note deliberately does not repeat the names, since
doing so would itself trip the gate.) Use placeholders — `team_bot_a`, `team-bot-b`,
`pod-admin-user`, … — per `docs/PLACEHOLDER_NAMING.md`. Run
`python -m pytest packages/admin/tests/test_public_launch_scrub.py` before pushing; it
fails fast and names the offending line.

## First batch (parallelizable now — independent, no shared files)

| Spec | Item | Tier | Notes |
|------|------|------|-------|
| [0.2-stale-docs.md](0.2-stale-docs.md) | Update architecture.md + calibration-schema.md | Sonnet | Mechanical reconcile-against-code |
| [0.7-ci-gate.md](0.7-ci-gate.md) | CI runs the full suite w/ quarantine allowlist | Sonnet | Do NOT touch sudoers |
| [3.3-ui-smoke.md](3.3-ui-smoke.md) | Extend `tests/browser/` smoke (theme parity + flows) | Sonnet | Existing harness — extend, don't rebuild |
| [5.1-kill-stubs.md](5.1-kill-stubs.md) | Remove "coming soon" stubs in apps.js | Sonnet | Wire-or-hide, never fake |

**Stays on Fable (the quarterback session):** Phase 1 (prove the optimizer loop),
plus the design of 2.1 / 2.3 / 4.3 and the 0.7 sudoers decision.
