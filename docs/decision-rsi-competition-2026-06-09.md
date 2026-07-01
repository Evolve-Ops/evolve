# Decision/Record: intra-dimension competition — built, deliberately unarmed

**Status:** deferred (engine shipped; arming gated on a written trigger) · **Date:** 2026-06-09 · **Roadmap:** Phase 1 (1.5)

## The question this answers

The 2026-06-09 diligence review flagged that Evolve *implies* its coaches
compete — that when two generators target the same objective, authority (and
thus influence over the bot) should flow to whichever one's proposals actually
verify. The reviewer's concern: is that a real, running mechanism, or a
marketing line the code can't back?

Roadmap item 1.5 forces the call: **arm competition on the generators that
genuinely compete, or explicitly scope-defer it and stop implying it runs.**

## What actually exists

The competition engine is **built and tested**, not vapor:

- `registry/competition.py` — `resolve_groups` (buckets generators by
  `(dimension, competitive_group)`), `compute_group_weights` (authority-weighted
  split with a per-member floor), `_is_in_grace_period` (a 4-week shield for new
  deployments), `rebalance` / `apply_weights_to_records` (weekly in-place
  reallocation). Spec §8.
- `tests/test_rsi_competition.py` — 10 tests covering multi-member resolution,
  authority-based split, equal-split-under-grace, floor enforcement, mixed
  grace/established membership, and in-place rebalance.

It is, today, a **no-op** — for exactly one reason: **no charter declares a
`competitive_group`.** The field defaults to `None`
(`schema/generator.py:392`), and `resolve_groups` skips any record without one
(`competition.py:66`). So the machinery runs every week over zero groups.

## Why not arm it now

Arming is a behavior change, and arming *meaningfully* requires two conditions
that don't hold yet:

1. **Genuine rivals.** Two-or-more generators in the same dimension that propose
   *alternative fixes to the same objective* — not merely "both touch cost." The
   two optimizers that have closed the loop so far (Budget Hawk, Efficiency Hawk)
   target **different levers**: Budget Hawk caps spend (`TierAdjustment` on the
   daily-$ ceiling), Efficiency Hawk restructures the workload (tier-misrouting,
   cache TTL, automation balance). They are complementary, not competing —
   reallocating authority *between* them would punish one for the other's domain.
2. **Authority to reallocate on.** `compute_group_weights` splits weight by
   `authority_score`, which is derived from verified-proposal track record. That
   record is **nascent** — the optimizer loop only closed on 2026-06-09 (see
   [decision-optimizer-loop-closed](decision-optimizer-loop-closed-2026-06-09.md)).
   Splitting authority on ~zero verified history is noise, not signal — the same
   premature-optimization the 4-week grace period exists to prevent, applied one
   level up.

Arming today would reallocate real influence over a user's bot based on no
evidence. That is the opposite of the "verify-or-don't-ship" discipline the
product holds its own proposals to.

## Decision

**Defer arming. Keep the engine. Write down the trigger.**

Competition stays built, tested, and unarmed until a single dimension actually
contains **≥2 generators that have each independently closed the loop**
(`proposals_verified_success > 0`) on *genuinely-alternative* fixes to the
*same* objective. When that condition holds, arming is mechanical: set a shared
`competitive_group` on those charters; the weekly `rebalance` and the 4-week
grace period do the rest, and `test_rsi_competition` already guards the math.

Candidate future groups to watch (none qualify yet — listed so the next person
knows where to look first):

- **Engagement** — `persona_tuner` / `engagement_amplifier` / `session_quality`
  (three distinct theories of "make the bot more engaging"; the most likely
  first real rivalry).
- **Capability** — `app_suggester` / `pod_capability_lift` /
  `app_birth_detector` (competing routes to "the bot can do a new thing").

## Proof artifact (1.5)

Per the roadmap, 1.5 closes on *either* armed reallocation *or* "a decision doc
+ README already softened." This is that decision doc; the README carries **no**
live competition claim (grep-verified clean, 2026-06-09). Claim and code now
agree: the mechanism exists, it is honestly described as not-yet-active, and the
exact condition for activating it is recorded here.
