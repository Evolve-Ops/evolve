<!--
Thanks for opening a PR. A few notes before submitting:

1. Tests: run the relevant package's suite locally before pushing.
   `cd packages/admin && python3 -m pytest tests/` for admin changes,
   same shape for analyzer / plugin.

2. CI invariants: scrub-guard, launchd-scope, and gitleaks checks
   run on this PR. The most common failure is scrub-guard catching
   a personal/bot identifier — see docs/PLACEHOLDER_NAMING.md if it
   fires.
-->

## Summary
<!-- 1-3 sentences. What changed and why. -->

## Type
<!-- Pick one. -->
- [ ] **feat** — new user-facing behavior
- [ ] **fix** — fixes existing behavior
- [ ] **refactor** — no behavior change
- [ ] **docs** — documentation only
- [ ] **chore** — build, CI, dependency, or repo-metadata change
- [ ] **test** — adding or fixing tests only

## Test plan
<!-- How to verify locally. Commands a reviewer can paste. -->

## Risk and rollback
<!-- What can go wrong; how to undo. For non-trivial changes only. -->

## Related
<!-- Linked issues, prior PRs, specs, design discussions. -->
