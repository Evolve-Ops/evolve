# Principle: Instrument Outcomes Before Optimization Machinery

**Status:** load-bearing build-process principle (governs HOW we ship optimization features, not what they do).
**Adopted:** 2026-06-07, after a five-PR cascade-controller arc that built elaborate detection machinery on top of an outcome signal that didn't work.

---

## The principle, in three clauses

1. **An outcome signal must demonstrably fire before its associated optimization machinery is worth building.** "Optimization machinery" means anything that READS the outcome to make decisions (escalation, demotion, RSI tuning, automated routing). If the outcome signal can't tell apart good outcomes from bad ones — at a rate higher than the background noise — then any optimization that grades against it is unfalsifiable.

2. **Validate the premise before scaling the optimizer.** Before phase-planning a multi-PR optimization rollout, confirm via direct evidence (not first-principles reasoning) that the intervention has user-outcome value. "Cheaper tier on trivial turns saves cost" is intuitive but the empirical question is "do you have any tier-divergent turns to measure?"

3. **When a detector doesn't fire on production data, the design might be wrong (not just the detector).** Sometimes the detector is correct and the underlying usage shape doesn't support the design (e.g., post-hoc cascade requires multi-turn sessions; most chat sessions are single-turn). Detection-side fixes can't rescue a design that doesn't fit the workload.

## What this implies in practice

### Order of construction

When building an observation→decision→optimization pipeline:

1. **Outcome signal first.** Before the decision layer, before the optimization layer, before any threshold tuning: prove the signal produces ≥1% true-positive rate on real production data, with ≥5% precision. If you can't, the rest is theater.
2. **Decision layer second.** Wire a decision-maker that READS the signal; verify it can actually decide (i.e., the signal has enough resolution to discriminate).
3. **Optimization machinery last.** Now you can ship escalation/demotion/tuning logic, because each decision is gradeable against the outcome signal.

### Signal validation has its own audit

A signal that "runs on every turn but returns 0" is indistinguishable from a broken signal. Two specific guards:

- **Trace the inputs.** When the signal returns 0, distinguish "saw nothing" from "saw something but couldn't measure" (see [Tri-State Status](principle-tri-state-status.md)). A `null` payload-drift code is the signal that *your inputs are stale*, not your conclusions.
- **Sample positives by hand.** Find sessions where you're confident the outcome was bad. Did your signal fire on them? If not, the signal is missing real positives — fix the extractor before scaling.

### Investment gating

Before committing to a multi-phase rollout of an optimization feature:

- **Phase 0 — outcome instrumentation that demonstrably works.** Ship + verify on the pod.
- **Phase 1 — single-shot decision layer using the signal.** Confirm the decision actually changes routing on real turns.
- **Phase 2+ — escalating sophistication** (rules, ML classifier, RSI tuning).

If Phase 0 doesn't produce a working signal in real production data, stop. Either the signal model is wrong (different proxy needed) or the premise is wrong (the intervention doesn't matter as much as theory suggests).

## Anti-patterns

These should ring alarms during design review:

- **"We'll wire it now and tune later."** Shipping an optimization layer whose threshold will be tuned from data that doesn't yet exist.
- **"The detector will improve when we fix X, Y, Z."** If three independent fixes are needed before the detector produces actionable data, the detector design itself may be at fault.
- **"We see no firings because the bots are quiet."** Maybe — but verify by running positive-case examples through the detector by hand. If those don't fire either, "quiet bots" is masking a broken signal.
- **"We measured the metric, the rate is 0.5%, that's our baseline."** A 0.5% event rate over thousands of turns gives you no statistical power to slice by tier, complexity, or any other dimension. Either the events are genuinely that rare (different design needed) or the detector is under-triggering (audit it).
- **"We'll know if it works once it ships."** Optimization machinery whose effect can only be observed downstream of its own decisions is unfalsifiable by construction. There must be an independent outcome signal.

## What this is NOT

- **It's not "no progress until perfect data."** Diagnostic + instrumentation work can ship in parallel with optimization work, AS LONG AS the optimization doesn't make routing decisions whose outcomes go ungraded. A shadow-mode optimization that produces telemetry-only is fine.
- **It's not "outcomes have to be quantitative."** A `success: true/false` flag is an outcome signal; a `cascade.success` span attribute is an outcome signal. Operator thumbs-up/down is an outcome signal. The bar is "discriminates good from bad above noise," not "produces a continuous variable."
- **It's not "wait for years of data."** A working outcome signal at 5% event rate gives statistically meaningful slices within a week on a busy bot. The bar is *signal works*, not *signal has accumulated*.

## Why it matters

Optimization machinery is expensive — both in build cost (each phase is a PR + tests + spec + UI surface) and in operational risk (wrong routing wastes money or degrades user experience). If the signal grading those decisions is broken, the machinery makes decisions you can neither validate nor improve.

Worse, the machinery LOOKS productive — it runs, emits telemetry, ships releases. The absence of an outcome signal is silent; nothing crashes, nothing alerts. You can ship five phases of an optimization feature and still not know whether any of it helps. That's the failure mode this principle exists to prevent.

## Case study: the cascade controller arc (2026-06-06 to 2026-06-07)

**What we built:**
- A four-phase cascade controller that observes per-turn "struggle features" (tool error counts, rephrase markers, retry rates) and escalates to a higher tier when struggle is detected.
- A pre-flight intent router (four more phases) that decides tier UP FRONT from the user's prompt.
- Disagreement detector that grades both against outcomes.

**What we discovered, in order:**
1. Cascade had never fired across 744 spans / 9 days / all 9 bots. Zero escalations.
2. The detection bug: three of five struggle features didn't fire on real OC payload shapes.
3. After fixing the detection, the threshold was still unreachable because the `success=false` floor capped unmeasured failure at 0.5 < the 0.65 threshold.
4. Stepping back: **even if cascade fired, we couldn't grade it.** The outcome signal (user pushback) was firing on only 14 turns out of 6,594 — 0.21% rate, below noise.
5. Stepping back further: **the outcome signal itself was broken.** `extractMessages` was zeroing the prior turn's text 98% of the time, so pushback detection effectively never ran on multi-turn sessions.
6. Stepping back even further: **the cascade design assumed multi-turn sessions that don't dominate real usage.** Most sessions in this install are single-turn (1.27 avg). Post-hoc escalation has nothing to escalate to.

**What we should have done first:** validated that pushback detection produced ≥1% event rate on real traffic before building the cascade controller that grades against it. Then noticed that single-turn sessions don't support post-hoc escalation, and pivoted to pre-flight intent routing earlier.

**What we did instead:** shipped five PRs of cascade-detection improvements before stepping back to ask whether the underlying outcome signal even worked. Then shipped four more PRs of pre-flight routing (a better design for the dominant session shape) plus its disagreement-detector + UI.

**Net:** the pre-flight router is the right primary mechanism for this install. The cascade controller is now a useful secondary mechanism for the rare multi-turn power-user case. The arc cost ~10 PRs of work, half of which would have been spent on a different design if we'd validated the outcome signal at the start.

## References

- `internal/spec-tier-cascade-2026-05-26.md` — the original cascade design (now superseded as primary by pre-flight)
- `internal/spec-preflight-intent-router-2026-06-06.md` — the pre-flight router that emerged from this reframe (to be written)
- [`packages/analyzer/cascade/preflight_audit.py`](../packages/analyzer/cascade/preflight_audit.py) — the disagreement detector, which IS the outcome signal for pre-flight decisions
- [Tri-State Status — `null` ≠ `0`](principle-tri-state-status.md) — sibling principle: detectors must distinguish "couldn't measure" from "measured: 0." Closely related; this principle generalizes it to "your downstream optimization can't validate what your upstream signal can't measure."

## Sibling principles

- [Tri-State Status — `null` ≠ `0`](principle-tri-state-status.md)
- [Alerts Must Explain and Remediate](principle-alerts-explain-and-remediate.md) — same family: if you can't explain what an optimization is doing AND what to do about it, you shouldn't ship the optimization yet.
