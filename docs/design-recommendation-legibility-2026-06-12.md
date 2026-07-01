# Design: Recommendation Legibility

**Status:** active (Bites 1 + 3 shipped)
**Date:** 2026-06-12
**Lineage:** extends [docs/spec-recommendations-rework-2026-06-02.md](spec-recommendations-rework-2026-06-02.md)
(the coalesce + humanize phase that introduced `coalesce_key` / `human_title` /
`sub_findings`); this doc names the *contract* those mechanics serve so future
generators inherit the standard instead of re-litigating it.

---

## Problem

The RSI **Recommendations / Improve** surface must be **digestible**. The unit an
operator can actually act on is roughly **one card carrying ~45 plain words**, with
the heavy detail one click away in the proposal/report drill-down — *not* N cards
each carrying hundreds of words of maintainer-language prose.

This is a difference in **kind, not degree**: an indigestible recommendation isn't
"a bit worse," it gets **ignored**. A page that shows seven near-identical fat cards
trains the operator to skip the whole surface. Brevity here is load-bearing.

The concrete trigger: when a provider ships several new models, `model_discovery`
emitted **one fat Proposal per model**. The live pod showed ~7 separate "New model
available from xai" cards, each with a ~250-word `problem` body. The operator wants
**one** card — "New models available from xAI" — with the individual models behind a
drill-down. (Operator-endorsed 2026-06-12.)

---

## The recommendation-legibility contract

Four ordered properties a generator's operator-facing output must satisfy. Ordered
because each is cheap relative to the next and the early ones do most of the work:

1. **Cardinality** — coalesce sibling findings. Findings that share a root cause get
   one card, not N. Mechanism: set the same non-empty `coalesce_key` on each
   Proposal; `arbiter.store` folds the 2nd..Nth into the first as `sub_findings`
   (deduped by `trigger_observations[0]`). Pick the *grain* deliberately — for
   `model_discovery` it's per-provider (`model_discovery:{provider}`), so an xAI drop
   and an Anthropic drop stay separate cards.

2. **Register** — plain language up front. The card shows a terse `human_title`;
   heavy maintainer detail (evidence dumps, slugs, rationale) lives in the
   drill-down body, never the headline. The `human_title` is **count-agnostic** — the
   UI's sub-findings badge supplies the live count, so the title stays correct as
   findings fold in or get adopted out.

3. **Decision-grounding** — **cite-or-don't** *(shipped, Bite 3)*. Any value or cost
   claim on the card must be backed by a real, cited source. If there's no source, say
   nothing rather than fabricate a number. (Already practiced in `model_discovery`'s
   cost-band citation: `pricing` / `family-map` / `heuristic` each say exactly what
   backs the band, and the unpriced case says so plainly.) Bite 3 extends this from
   the band to a **pod-grounded value line** — see the Roadmap entry.

4. **Ranking** — when several digestible cards remain, order them so the
   highest-leverage one is first. (Lowest priority; only matters once 1–3 hold.)

---

## Decision

- **Pilot the contract on `model_discovery`** (Bite 1, this change), then **ratchet**
  it to other generators incrementally as each is touched. `app_audit` and
  `app_permission_review` already satisfy cardinality + register and serve as the
  reference implementation.

- **Explicitly NOT building a general LLM "editor brain"** over the proposal stream.
  That approach was tried and rejected — it produced ~138 low-value items (see
  [docs/decision-rsi-synthesis-layer-2026-06-09.md](decision-rsi-synthesis-layer-2026-06-09.md)).
  Legibility is achieved by **each generator emitting digestible output at the
  source** (cheap, deterministic, per-generator), not by a synthesis layer
  rewriting the stream after the fact.

---

## Roadmap

- **Bite 1 — coalesce + humanize (this change).** `model_discovery` sets
  `coalesce_key = "model_discovery:{provider}"` and a count-agnostic
  `human_title = "New models available from {provider}"`. Each per-model `AdoptModel`
  action, its evidence, and motivating signals are unchanged — every model stays
  individually adoptable from the drill-down. Satisfies cardinality + the title half
  of register.

- **Bite 2 — actionable coalesced group.** Restore per-model adoptability on the
  coalesced card and add a single "adopt all as dormant" batch action that fans out
  across the group. This is the bite that reconciles coalescing with `AdoptModel`'s
  N-independently-actionable shape (see "Known limitations after Bite 1" below) —
  both the per-sub-finding adopt control and the sweep/coalesce interaction fix live
  here, since both touch the parent↔child relationship the `sub_findings` projection
  introduces.

- **Bite 3 — pod-grounded value line. SHIPPED.** A terse, cited value line on the
  card: join the discovered model × this pod's observed tier usage
  (`{shared}/cost/tier-usage/<bot>/<YYYY-MM-DD>.jsonl`) × `model_pricing` /
  `model_cost_bands`, e.g. *"Your `power` role ran 10 calls in the last 7d on
  `claude-opus-4-8`; this model is ~20% cheaper ($12.00/MTok vs $15.00/MTok input)."*
  Computed deterministically — **no LLM** — in
  `generators/model_discovery/value_line.py`, attached to a new optional
  `Proposal.value_line` field, rendered under the card title in
  `self-improvement.js`, with the full derivation woven into the proposal body.

  **As-built precision (cite-or-don't, property 3):** the tier-usage records are
  *counts-only* (`{ts, tier(role), model, context, bot_id}` — no tokens/$), so the
  value line grounds **call-volume per role** from real usage and expresses the $
  dimension as the **cited per-MTok input-price delta** from the pricing catalog —
  not a reconstructed weekly-$ total it can't back. The load-bearing invariant: a
  price/% number appears **only** when BOTH a real cited price for the model AND
  real same-band pod usage are present. An **unpriced** model (xAI/grok today) says
  *"can't price yet"* and surfaces usage honestly — **never** a fabricated savings %.
  The comparison cohort is the discovered model's cost **band** (a high-band model is
  compared against what currently serves the pod's high band); the band can come from
  the family map for an unpriced frontier model (it only picks the cohort), while the
  price number is gated strictly on a pricing-catalog hit.

---

## Known limitations after Bite 1

The `coalesce_key` / `sub_findings` machinery was built for **investigate-once**
findings (`app_audit`, `app_permission_review`), where a coalesced group is one root
cause investigated as a unit and the folded sub-findings are correctly display-only.
`model_discovery` is the first user where the folded items are **N independently
actionable** `AdoptModel` proposals, which exposes two interactions the existing
machinery doesn't yet handle. Both are accepted for Bite 1 (the 7→1 digestibility win
is intact and dominant) and are explicit **Bite 2** scope:

1. **Per-model adoptability.** Only the parent proposal carries an adopt control
   (`_adoptModelPicker`); the folded sub-findings render as display-only text
   (`_renderSubFindingsBlock`). Interim, an operator adopts roughly one model per
   provider per discovery cycle (adopting the parent promotes the next sibling to
   parent on the following run). Bite 2 restores per-model + batch adoption.
   *(The "bulk actions fan out across a coalesced group" affordance in
   `self-improvement.js` is the separate client-side `_propGroupSimilar` visual
   grouping, not this server-side `sub_findings` path.)*

2. **Sweep/coalesce flicker.** `arbiter.store.sweep_resolve_proposals` keys on the
   parent's own per-model fingerprint (`compute_fingerprint` → `trigger_observations`).
   If the parent's model goes silent (adopted into a rung, or delisted by the provider)
   while sibling models still fire, the parent — and its still-live sub-findings — is
   archived as `resolved_externally` for one cycle, then re-created fresh next run. A
   one-cycle flicker that also drops card-level snooze/dismiss state. Narrow trigger,
   self-healing, no permanent data loss. The fix (sweep preserves a parent while any
   sub-finding's model is still emitted) lands in Bite 2 alongside the actionability
   rework, since both touch the parent↔child relationship.

## Verification note (UI render, observed 2026-06-12)

The admin SPA already renders the coalesced shape: the card title prefers
`human_title`, `_subFindingsBadge` shows the live count, and the per-model detail
sits behind `openProposalDetail` with bulk actions fanning out across the group
(`packages/admin/evolve_admin/web/static/js/pages/self-improvement.js`). One nuance:
the card's secondary line (`self-improvement.js:1191`) renders the **parent's own
`problem` body inline** whenever it differs from `admin_surface_summary`, so the
surviving card still shows one model's heavy body. The dominant win — 7 cards → 1 —
is delivered by coalescing; trimming that inline secondary to a terse line (or
clamping it) is a shared-UI follow-up under property 2 (register), out of scope for
this generator-only bite.
