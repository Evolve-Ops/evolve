# Decision/Record: the optimizer loop closed in production

**Status:** DECIDED — closed · **Date:** 2026-06-11 · **Roadmap:** Phase 1 (1.7)

## The question this answers

The 2026-06-09 diligence review's single most important demand was a live
artifact for Evolve's headline claim — *recursive self-improvement applied to
applications*. The 1.4 proof
([decision-optimizer-loop-closed-2026-06-09.md](decision-optimizer-loop-closed-2026-06-09.md))
drove an optimizer proposal through the entire loop in a deterministic CI test,
and was deliberately honest about its limit: it injects cost data at the ledger
boundary so it can run in CI, and it *"does NOT establish that it has closed on
a live pod with real production cost events."* Roadmap 1.7 is exactly that
remaining confirmation — let the optimizers run on the live pod until one
proposal closes the full loop against real data.

The exit criterion is the killer diligence question, verbatim:

> *"show me one optimizer proposal where the verify daemon resolved the claimed
> metric against real data and moved a coach's authority."*

As of 2026-06-11 the answer is **yes, here it is** — not in a test, on the pod.

## The artifact

One `efficiency_hawk` (OPTIMIZER) proposal,
`46e9caec-1c4a-478e-82f3-fcadb9dddf3d`, targeting a production team bot, walked
the full loop end to end. Re-pulled read-only from the live pod 2026-06-11; the
proposal JSON, its verification result, and the generator's track record all
agree.

**Generate.** `efficiency_hawk.tier_misrouting` observed that 100% of the bot's
maintenance-class spend — **$106.46 of $106.72 over 14 days, across 292
maintenance sessions** — was running on a high-tier model rather than a cheaper
one suited to routine upkeep. It emitted a `TierAdjustment` proposal to route
maintenance sessions to `haiku`, with a verifiable claim attached:

| Field | Value |
|---|---|
| `metric` | `cost.maintenance_high_tier_share` |
| `direction` | `down` |
| `magnitude` | `0.4` (share must fall by ≥ 0.4) |
| `baseline` | `0.997559` (the ~100% share, authored at creation time) |
| `window_days` | `14` |
| `fallback` | `revert` (auto-reversible; blast radius = single bot) |

**Approve.** A human operator clicked *Act* on 2026-05-12 (`actor: user`,
`reason: user clicked Act`). This was a real approval of a real routing change,
not an auto-applied lane.

**Apply.** One second later the applier executed the routing change on the live
bot config (`actor: user`, `reason: set routing.maintenanceTier='tier3'`).
Maintenance sessions began routing to the cheaper tier from that point.

**Verify.** Fourteen days later, on 2026-05-26, the verify daemon resolved
`cost.maintenance_high_tier_share` against the bot's **real cost ledger** over
the post-change window and evaluated the claim:

```
baseline       0.997559   (~100% of maintenance spend on high-tier, pre-change)
observed       0.438255   (resolved from the real ledger at the 14-day horizon)
delta         -0.559304   (claim required ≥ 0.4 down — held with room to spare)
direction      down  ✓
confidence     1.0        (full data in the window)
→ outcome      succeeded
```

The proposal transitioned `applied → succeeded`
(`actor: verify_daemon`, `reason: claim held: …delta=-0.559304…`).

**Authority bump.** The success credited the generator's track record.
`efficiency_hawk`'s `GeneratorRecord` now reads
`proposals_verified_success = 1`, `proposals_applied = 2`,
`proposals_succeeded_first_shot = 1`, `authority_score = 1.0` — a verified
optimizer success on production data moved a coach's standing.

Every dollar, every date, every status transition above is from the on-disk
artifact on the live pod. The bot's identity is anonymized here per the repo's
scrub convention; in the store it is a real, named, in-use team bot.

## Why this is production-genuine, not test-seeded

The 1.4 CI proof established that the machinery is *mechanically complete and
correct*. What 1.7 adds is that it ran **in the world**, on every axis the test
deliberately stubbed:

- **Real approval.** A human operator clicked *Act*. No auto-apply lane, no
  fixture.
- **Real applier execution.** The proposal mutated a live bot's
  `routing.maintenanceTier` — an actual config change that altered how a running
  bot routed its sessions for two weeks.
- **Real cost-ledger data.** The baseline and the 14-day-horizon reading both
  came from the bot's genuine spend events. The test injected synthetic values
  at the `cost_ledger.read_events` boundary; here the resolver read the ledger
  the bot actually wrote.
- **Multi-week wall-clock window.** The verify daemon waited the full 14 calendar
  days between apply (2026-05-12) and verification (2026-05-26) before resolving.
  The CI test fast-forwards the clock; this elapsed in real time.
- **A real, measurable improvement.** The bot's high-tier maintenance share fell
  from ~100% to ~44% — a change the operator can see on the bill, not a contrived
  pass.

This is the difference 1.7 was created to close: 1.4 proved the loop *can*
close; this proves it *did*, against production cost data, on a bot a person
relies on.

## The companion: the loop discriminates, it does not rubber-stamp

A success on its own invites the obvious skepticism — does the daemon credit
*anything* that gets applied? The negative case answers it.

`budget_hawk` proposal `7ef418aa-c583-4a1a-991e-c00f72a85142`, on the same bot,
claimed `cost.daily_usd` should drop and was applied 2026-05-08. At its 7-day
horizon on 2026-05-15 the verify daemon tried to resolve the metric, **failed
three times** (`metric resolver failed 3 times: 'cost.daily_usd'`), exhausted
its retries, and forced the proposal to `failed_flagged` — recording
`proposals_verified_failed = 1`, `proposals_verified_success = 0`, **no authority
credit**.

That failure is, here, the load-bearing evidence: the daemon will not mark a
proposal succeeded unless it can actually resolve the claimed metric against real
data and the claim holds. When it cannot, it records a failure and withholds
authority. A loop that credits successes and failures by the same rule is a loop
that *measures*; one that only ever credits successes is a rubber stamp. This one
measures.

(`budget_hawk`'s failure was a missing resolver — the pre-1.1-era
`cost.daily_usd` gap, since fixed. The point it proves is structural, not that
the proposal was bad: even an applied, human-approved optimizer proposal does not
earn authority if the daemon can't verify it.)

## Honest scope — what one verified proposal proves, and what it doesn't

**It proves:** the headline claim is now demonstrated *in production*, not only in
test. An optimizer generated a verifiable improvement, a human approved it, the
applier changed a live bot, and 14 days later the verify daemon resolved the
claimed metric against real cost-ledger data, found it held, transitioned the
proposal to `succeeded`, and moved the coach's authority. The diligence answer
upgrades from "proven in a CI test" to "here is the live artifact."

**It does not prove:**

- **A track record.** This is **n = 1** verified optimizer success on the pod.
  One proposal closing is a demonstration, not a distribution. Authority math,
  intra-dimension competition, and any claim about *how often* the optimizers are
  right all need a larger sample. The soak continues precisely to build it.
- **That every optimizer metric resolves.** `cost.daily_usd` and the
  three `cost.maintenance_*` / `cost.background_*` metrics are now registered, but
  `cache_invalidation_ratio` (claimed by `cache_ttl_tuner`) still has **no**
  registered resolver, so those claims continue to force-flag. Tracked under
  roadmap **1.1** — that gap is not closed by this artifact.
- **That the cluster-scoped claim path is live.** `efficiency_hawk`'s
  `cluster.engagement_trend` claim still has no live closure through the daemon
  (roadmap **1.3**). The proposal closed here used a *cost*-scoped metric, which
  resolves through the standard `resolve(metric, bot_id, as_of)` signature.

In one line: **one real optimizer proposal has closed the full loop against
production data and moved a coach's authority** — enough to retire the "can it
close live?" question, not enough to lean on the optimizers' aggregate judgment.
The soak stays running as the safety net and the sample-builder.

## Decision

**1.7 is closed.** The live-pod soak has produced its proof artifact: a real
optimizer proposal demonstrated the full generate → approve → apply → verify →
authority-bump loop against production cost data. Evolve's headline claim —
*recursive self-improvement applied to applications* — is **demonstrated in
production**, with the honest caveats above on file.

## Proof artifact (1.7)

The roadmap asks for *"a `GeneratorRecord` with non-zero
`proposals_verified_success` from production data + a dated decision doc."* The
record exists on the live pod (`efficiency_hawk.proposals_verified_success = 1`,
`authority_score = 1.0`); this is that dated decision doc; and the closing
transcript — metric, baseline `0.997559`, observed `0.438255`, delta
`-0.559304`, the human approval, the applier change, the 14-day window, the
`succeeded` transition — is captured above, re-verified against a fresh read-only
pull on 2026-06-11.
