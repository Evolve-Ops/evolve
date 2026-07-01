# Decision/Record: the optimizer loop closes (Budget Hawk)

**Status:** demonstrated · **Date:** 2026-06-09 · **Roadmap:** Phase 1 (1.1 + 1.2 + 1.4)

## The question this answers

The 2026-06-09 diligence review's single most important finding was that Evolve's
headline claim — *recursive self-improvement* — could not close on an **optimizer**
generator. The *guardian* half worked (a config-drift / gateway-down claim verifies
trivially: "did the error clear?"), but no generator that tries to make a bot
**better** (cheaper, more efficient) could ever measure whether it succeeded. The
load-bearing case was **Budget Hawk**, whose `TierAdjustment` proposals claim
`cost.daily_usd` should drop.

The reviewer's killer question: *"Show me one optimizer proposal where the verify
daemon resolved the claimed metric against real data at the horizon, and the outcome
moved a generator's authority."*

Before this work, the honest answer was **no** — for two concrete reasons.

## What was broken

1. **`cost.daily_usd` was never registered.** Budget Hawk's three claims targeted it,
   but no resolver declared it, so the verify daemon raised `UnknownMetricError` →
   `failed_flagged` every time. The claim was never tested.
2. **The baseline was never filled.** The claims shipped `baseline=0.0` with a comment
   promising "Budget Hawk fills this from current reading at apply time" — but no such
   code existed. Even had the metric resolved, it would have compared against a
   meaningless `0.0`.

## The fix (shipped in PR #2518)

- **1.1** — registered `cost.daily_usd` as a noise-robust **trailing-7-day average
  $/day** resolver (`metrics/resolvers/cost_metrics.py`). A single calendar day is too
  volatile to verify against; low confidence on no-data so a missing window is never
  mistaken for a real `$0/day`.
- **1.2** — added an explicit `Claim.baseline_at_apply` opt-in. When set, the applier
  resolves the metric at apply time and overwrites the placeholder with the live
  pre-change reading (`arbiter/apply.py`). The metric is trailing-historical, so the
  just-applied change hasn't perturbed it. Generators that author a real creation-time
  baseline (Efficiency Hawk, Cache TTL Tuner) leave the flag `False` and are untouched.
- Both the apply-time baseline and the verify-time read go through the **same**
  resolver, so the pre- and post-change windows are symmetric by construction.

## The proof artifact

`packages/analyzer/tests/test_optimizer_loop_closes_e2e.py` drives one Budget Hawk
proposal through the **entire** loop with the **real** components — real resolver,
real apply-time baseline fill, real verify daemon, real `Claim` evaluation, real
authority bump:

```
apply  (baseline filled from a live $2.00/day reading — not the 0.0 placeholder)
  → horizon reached (apply_time + window_days)
  → verify daemon resolves the REAL cost.daily_usd metric → $1.00/day
  → claim "down by $0.50/day" holds  ($2.00 − $1.00 = $1.00 ≥ $0.50)
  → proposal transitions to SUCCEEDED, file moves applied/ → succeeded/
  → Budget Hawk's GeneratorRecord.proposals_verified_success increments
```

A **control** test runs the identical setup but holds spend flat at $2/day: the claim
fails, the proposal does **not** succeed, and authority is **not** credited — proving
the success path measures something real rather than rubber-stamping.

This is the artifact the founder can put in front of the reviewer.

## Honest scope — what this proof does and does not establish

- **Does establish:** the optimizer loop is *mechanically complete and correct* — every
  link from apply → baseline → horizon → real-metric resolution → claim evaluation →
  status transition → authority adjustment works end to end for a dollar-denominated
  optimizer claim.
- **Does NOT establish:** that it has closed on a *live pod with real production cost
  events*. The e2e injects cost data at the `cost_ledger.read_events` boundary (so the
  test is deterministic and runnable in CI). The remaining confirmation is to let one
  real Budget Hawk `TierAdjustment` apply on the mini and watch the verify daemon close
  it against actual spend — recommended as the final sign-off before leaning on the
  claim externally.

## What's still open in Phase 1

- **1.3** — the resolver calling convention still can't pass `(noun, verb)` cluster
  context, so **Efficiency Hawk's** `cluster.engagement_trend` claim remains
  structurally unreachable. That's the next optimizer to close.
- **1.5 / 1.6** — the competition mechanic (`competitive_group`) is still dormant, and
  the objective-aware synthesis layer is still unbuilt. Both remain decisions, not code.

Budget Hawk closing is the proof the *machinery* works; 1.3/1.5/1.6 extend it.
