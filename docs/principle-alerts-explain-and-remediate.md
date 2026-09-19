# Principle: Alerts Must Explain and Remediate

**Status:** load-bearing design principle (not a soft guideline).
**Adopted:** 2026-05-31, after a member-bot tile "pushback" chip was identified as a dead-end (no explanation, no link, no action).

---

## The principle, in two clauses

1. **Every alert-shaped UI element must explain itself.** A chip, banner, toast, notification, badge, or warning indicator must answer four questions through UI affordance (popover, tooltip, expandable detail, side panel):
   1. **What is it?** — plain-English name of the condition
   2. **What triggered it?** — the specific numbers or event that fired it now
   3. **What's the impact?** — what the operator experiences differently because of this
   4. **How serious is it?** — severity, in operator terms, not internal severity strings

   "Decorative-only" chips with no way to learn more are a bug. The operator should never have to read source code or ask Evolve in chat what a UI signal means.

2. **Every alert-shaped UI element should be actionable — or honest that it isn't.** Default to providing at least one concrete next step:
   - A **deep-link** to the page where the issue can be addressed (preferred — always grounded), or
   - A **verified remediation** with evidence that it helps for this specific condition (good when we have the data), or
   - An **investigation surface** — "see which apps / sessions contributed" — when we don't yet know the right fix (honest, still useful).

   If no action is possible, say so explicitly ("Informational — no action needed"). If we don't yet have a grounded remediation, ship investigation deep-links rather than fabricating advice. **Hallucinated guidance is a bug.** Telling an operator to "try a different model" without evidence that a different model helps trains them to distrust our advice.

   An alert may ship without a remediation IF the chip/alert definition documents why none exists (`remediation: null` with a brief reason). The bar is "did we think about it and decide there isn't one" — not "we forgot to add one."

## What this implies in code

Practical translation across the codebase:

### Chip data structure carries explainer fields

Today's chip in `packages/analyzer/tile_metrics.py` is:

```python
{"id": "...", "severity": "...", "label": "...", "detail": "...", "nav": "..."}
```

Principled shape adds:

```python
{
  "id": "...",
  "severity": "...",
  "label": "...",
  "detail": "...",                        # the trigger, in plain English (already exists)
  "why": "...",                           # one sentence on what this means
  "impact": "...",                        # what the operator experiences differently
  "remediations": [                       # zero or more; null OK with rationale
    {"label": "See which apps contributed", "kind": "deep_link", "nav": "apps?bot=<id>&filter=high_pushback"},
    {"label": "Review recent pushback turns", "kind": "deep_link", "nav": "sessions?bot=<id>&filter=user_pushback_score:high"},
  ],
}
```

A single React popover component renders this metadata uniformly. The same shape applies to banners and notifications — they all carry `why` + `impact` + `remediations[]`.

### Catalog alerts already partially comply

Catalog events in `packages/admin/evolve_admin/alerts/catalog.py` carry `body_template` + `ActionOffer`, which together cover #1 (explain) and partly cover #2 (remediate). Enforcement lives in `tests/test_alerts_catalog.py`. Extending that test to assert every catalog event has either an `ActionOffer` or an explicit `"informational"` flag is the chat-message expression of this principle.

### Chip explainers are CI-enforced

A unit test asserts every `chip_id` appearing in `tile_metrics.py` has a corresponding entry in a chip-metadata registry with `why`, `impact`, and `remediations` (or a documented null). New chip-ids without explainers fail CI. This mirrors the `test_body_templates_start_with_approved_emoji` pattern that already keeps `catalog.py` honest.

### Investigation > prescription when grounded data is missing

When a detector fires but we don't know what the operator should do about it, the principled response is a deep-link to where they can investigate, not a guess at the cause. For the "pushback" chip: link to the bot's settings so the operator can review model / system prompt / app config. Do not recommend "try a different model" until we have data showing that a different model reduces pushback for that bot.

## Anti-patterns to grep for

These are violations:

- Chips, banners, badges with no popover / tooltip / explainer (current state of all tile chips)
- Tooltips that restate the label without adding `why` or `impact`
- Remediations not backed by data ("try changing the model" without evidence)
- Generic "see Settings" without a specific tab / section deep-link
- "Click here for more" without saying what "more" is
- Status icons (red dot, yellow triangle) with no on-hover explanation of what the color means in context

## What this principle is NOT

- **Not a demand for elaborate remediation flows.** A deep-link to the relevant page is sufficient. We don't need to embed a guided wizard inside every popover.
- **Not a ban on informational alerts.** Informational signals are fine; they just have to be labeled informational and explain why no action is needed.
- **Not a demand to retrofit every existing alert immediately.** New code complies. Existing chips and alerts are migrated incrementally — but the chip explainer registry and CI test should land before the next new chip is added, so we stop accumulating debt.
- **Not a demand for the LLM to compose remediations on the fly.** Remediations are authored as part of the chip/alert definition, reviewed once, deep-linked precisely. LLM-composed advice is per-instance and not grounded — that's exactly the hallucination risk this principle exists to prevent.

## Why this matters

Alerts without explanation train operators to either ignore them or to ask Evolve in chat what they mean — both are friction. Alerts without remediation pretend to inform but actually offload the problem to the operator with no path forward. The combination is worse than no alert at all: it adds visual noise, claims attention, and pays the operator back with nothing.

Marcus (see [principle-plex-test.md](principle-plex-test.md)) reads the dashboard on his phone between client meetings. Every chip he sees is making a claim on his attention. Our job is to make that claim worth honoring — by explaining, and by helping him act.

## References

- [principle-plex-test.md](principle-plex-test.md) — the audience constraint this principle serves
- [operator-message-style.md](operator-message-style.md) — the chat-message expression of "explain + concrete next step" (CI-enforced via `tests/test_alerts_catalog.py`)
- `packages/analyzer/tile_metrics.py` — tile chip definitions (the surface that motivated this principle)
- `packages/admin/evolve_admin/alerts/catalog.py` — alert body templates and ActionOffers
- `packages/admin/evolve_admin/evo/tools/pod_state_bots.py` — current chip exposure layer (strips `nav` before model exposure; the explainer fields would route similarly)
