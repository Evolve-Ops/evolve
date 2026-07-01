# Decision/Record: the synthesis layer is the Effectiveness Layer

**Status:** reconceived + in build (not deferred-indefinitely) · **Date:** 2026-06-09 · **Roadmap:** Phase 1 (1.6)

## The question this answers

The 2026-06-09 diligence review noted that Evolve's most ambitious idea — a brain
that mines *patterns* in how a bot is actually used and synthesizes
*objective-aware* proposals to make it better — is, by the team's own spec,
**unbuilt**. The proposal-eligibility spec says so plainly
(`spec-rsi-proposal-eligibility-2026-06-05.md`, §"It does not build"):

> It does **not** build the synthesis layer (pattern miner over observation
> tuples → objective-aware proposal). That's larger work, tracked separately as
> the pattern-mining workstream.

Roadmap item 1.6 forces the call: **build a minimal v1, or formally defer — and
either way, stop letting a marketing surface imply it exists.**

## The decision

**Neither "build the old design as written" nor "defer indefinitely." The
synthesis layer has been *reconceived* as the Effectiveness Layer, and that
layer is actively shipping.**

The 2026-06-05 spec sketched the future synthesizer (§"Next"): *walk observation
tuples per bot → cluster friction into structured findings → an objective-aware
synthesizer that reads bot persona / purpose / user profile, matches pattern
findings against capability gaps, and proposes.*

That is — almost line for line — the **Effectiveness Layer**
([spec-effectiveness-layer-2026-06-09](spec-effectiveness-layer-2026-06-09.md),
#2534), now in build:

| 2026-06-05 synthesizer sketch | Effectiveness Layer realization | Status |
|---|---|---|
| "reads bot persona / **purpose**" | the per-bot **purpose anchor** — declared archetype + one-line mission | Phase B shipped (#2537), UI (#2538) |
| "pattern miner over observation tuples" | the Fit Reviewer's periodic **in-bot LLM reflection** over real usage | Phase C (next build) |
| "matches pattern findings against capability gaps → proposes" | the **app-fit classifier** (retire / modify / surface / thriving) + `EffectivenessSuggestion` | speced (#2534) |
| "objective-aware" | grounded against the declared purpose, with a cite-or-don't grounding rule | speced (#2534) |

## Why the reconception is the *better* answer, not a dodge

The original framing — **unsupervised pattern-mining over raw observation
tuples** — is what produced the **138 low-value items** sitting in Reports →
Proposals today. Mining tuples with no anchor surfaces patterns that are
statistically real but operationally trivial ("this bot uses Sonnet at 10:30")
and dresses them as recommendations. That is the exact failure both the review
and the operator called out.

The Effectiveness Layer fixes the root cause two ways:

1. **It anchors synthesis on an explicit declared purpose** instead of mining in
   a vacuum. "Is this bot serving *what it's for*?" is a question with a usable
   answer; "what patterns exist in the tuples?" is not.
2. **It makes the LLM reflection a sanctioned, rare, per-bot step** rather than a
   cheap-but-noisy tuple scan — consistent with the standing principle that *RSI
   must be cheap; LLM is escalation, not default.* The Effectiveness Layer is the
   one place expensive synthesis is allowed, precisely because it is bounded and
   purpose-anchored.

Same ambition. Grounded.

## What this means concretely

- The "pattern-mining workstream" as a separate, raw-tuple-mining effort is
  **retired** — superseded, not parked-for-someday.
- The synthesis layer's status is **in build**, not unbuilt: spec merged
  (#2534), observation triage merged (Phase A, #2535), purpose anchor + UI
  merged/open (Phase B, #2537 / #2538); the Fit Reviewer (Phase C) is the next
  build.
- No marketing surface should describe an unsupervised "pattern-miner." The
  honest description is the Effectiveness Layer: *"a periodic, purpose-anchored
  review that suggests ways each bot could serve its owner better."* The README
  carries no unbuilt-synthesizer claim (grep-verified clean, 2026-06-09).

## Proof artifact (1.6)

The roadmap asks for "a dated decision memo; if deferred, no marketing surface
implies it exists." This is that memo. The decision is **reconceive-and-build**;
the realization is traceable to merged PRs (#2534 / #2535 / #2537 / #2538); and
the README is clean of any unbuilt-synthesizer claim.
