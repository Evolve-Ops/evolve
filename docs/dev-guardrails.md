# Dev guardrails — pre-PR review + Stop verify + code-quality monitor

Four guardrails wired together to catch bugs *before* they merge and surface
the workflow patterns that let them through. Diagnosed from a two-month
retrospective showing fix(:feat( ≈ 739:617 — fixes outpacing features, with
many fix-on-feat clusters within hours of merge.

## What's in place

| # | Layer | Mechanism | Catches |
|---|---|---|---|
| 1 | Pre-PR gate | Claude Code `PreToolUse` hook on `gh pr create` runs an adversarial review via headless `claude -p` and **blocks the PR** on findings | Bugs that slip past the build session and would land in main |
| 2 | In-session verifier | Claude Code `Stop` hook runs a "try to refute this" review on the working-tree diff and blocks the turn end on findings | Bugs caught mid-session so Claude fixes them before the commit |
| 3 | Process metrics | `code_quality_monitor` daemon — daily; emits Signals for revert rate, fix-heavy scopes, same-day fix-on-feat | Tells you whether 1+2 are *working*; surfaces verification-weak surfaces |
| 4 | Worktree GC | `.claude/hooks/worktree-gc.sh` — prunes stale locked `agent-*` worktrees | Disk bloat (the project had 201 worktrees, ~103 stale) |

## Files

- `.claude/settings.json` — wires up `PreToolUse` + `Stop` hooks
- `.claude/hooks/lib/review.sh` — shared `claude -p` invocation + diff streaming
- `.claude/hooks/pre-pr-review.sh` — guardrail #1
- `.claude/hooks/stop-verify.sh` — guardrail #2
- `.claude/hooks/worktree-gc.sh` — guardrail #4
- `packages/analyzer/code_quality_monitor.py` — guardrail #3
- `packages/admin/evolve_admin/deploy.py` — installs the launchd plist (`_install_launchd_code_quality_monitor`)

## How review hooks work

Both `pre-pr-review.sh` and `stop-verify.sh` call `review_diff_blocking()`
from `lib/review.sh`. That function:

1. Streams a `git diff` into `claude -p --model claude-sonnet-4-6`.
2. Asks the model for an **adversarial** review (find BLOCKING bugs only;
   prefer false negatives over false positives).
3. Requires the model to end its output with `VERDICT: PASS` or
   `VERDICT: BLOCK`.
4. Returns rc=2 on `VERDICT: BLOCK`.

The hook scripts translate rc=2 into a Claude-Code-visible block:

- **`pre-pr-review.sh`** exits 2 → Claude Code refuses the `gh pr create`
  call and shows the review to the model so it can fix and retry.
- **`stop-verify.sh`** emits JSON `{"decision": "block", "reason": "..."}`
  → Claude Code continues the turn with the reason as a continuation
  instruction, so the model fixes the issue before stopping.

### Skip conditions (pre-PR)

- `EVOLVE_SKIP_PR_REVIEW=1` in env
- `[skip-review]` tag in any commit message in the PR
- All commits are `chore(`, `docs(`, `style(`, or `test(`
- Diff exceeds 8000 lines (sweep change — pre-PR gate gives up; rely on
  the Signal monitor and post-merge reviews instead)

### Skip conditions (Stop)

- `EVOLVE_SKIP_STOP_VERIFY=1` in env
- Working tree is clean
- All changes are docs/markdown only
- Already inside a continuation (`stop_hook_active=true`) — avoid loops
- Diff exceeds 4000 lines (in-session changes shouldn't be that big anyway)

### Override model

`EVOLVE_REVIEW_MODEL=claude-opus-4-7 claude` to use a smarter (slower)
reviewer. Default is `claude-sonnet-4-6`.

## Code-quality monitor — what it watches

Daily daemon over the last 30 days of `main`:

| KPI | Threshold | Severity |
|---|---|---|
| **Revert rate** | ≥ 1.5% → warn; ≥ 3% → alert | scales |
| **Fix-heavy scope** | fix:feat ≥ 2× AND ≥ 8 commits in scope | warn per scope |
| **Same-day fix-on-feat** | ≥ 40% of feats had a same-author related fix within 24h | warn |

Auto-resolves via `sweep_resolve` when the metric drops below threshold.
Signals show up on the Alerts page like any other pod-scope Signal.

### Baseline at install (2026-05-30)

- **Revert rate: 0.13%** (2 of 1554) — under threshold; no Signal
- **Fix-heavy scopes (top 5):**
  - `plugin` 12.0× (24 fix / 2 feat)
  - `deploy` 6.6× (33 fix / 5 feat)
  - `audit` 2.1× (17 fix / 8 feat)
- **Same-day fix-on-feat: 74.8%** (366 of 489) — well above threshold

The same-day metric is high enough today that the monitor fires immediately
on install. That's the point — the monitor is a measurement layer to confirm
that hooks 1+2 are working. If the rate drops to <40% in coming weeks, the
Signal auto-resolves; that's how we'll know the hooks are doing their job.

## Worktree GC

Cleans up `.claude/worktrees/agent-*` worktrees that are:

- **Orphaned** (registered with git but the dir is gone) — always remove
- **Merged** (branch's HEAD reachable from origin/main) — always remove
- **Stale** (mtime > 7 days AND clean working tree) — remove

Skips anything with uncommitted work or commits not yet on main.

```sh
# Dry-run (default):
.claude/hooks/worktree-gc.sh

# Apply:
.claude/hooks/worktree-gc.sh --apply

# Tune age cutoff:
.claude/hooks/worktree-gc.sh --apply --max-age-days 14
```

## Tuning + iteration

After running for a couple of weeks, check:

1. **How often is `pre-pr-review.sh` blocking?** If it blocks too often on
   non-bugs, tighten the prompt (`lib/review.sh`) or move noisy false
   positives to the `NOTE:` (advisory) tier.
2. **Are the same-scope hits on `code_quality_monitor` actually the
   places you'd want pre-review attention?** If not, tune
   `THRESH_FIX_FEAT_MIN_COMMITS` / `THRESH_FIX_FEAT_RATIO_WARN` in
   `code_quality_monitor.py`.
3. **Cost** — `claude -p` per PR runs ~30-60s, ~5-10K tokens at sonnet
   tier. Stop-time verifier fires per Claude session that touched files,
   so 5-50× per day. Watch the usage tile; if it spikes, switch the model
   default to haiku or add more skip conditions.

## Disabling

- Edit `.claude/settings.json` and remove the `hooks` block.
- Or set the env vars: `EVOLVE_SKIP_PR_REVIEW=1 EVOLVE_SKIP_STOP_VERIFY=1`.
- To remove the daemon: `sudo launchctl bootout system/ai.openclaw.evolve.code_quality_monitor` on the mini (deploy.py will reinstall it on the next `install-infra-jobs`; remove from `expected_plist_labels` in deploy.py to retire permanently).
