---
title: "Help: Model Economics Page"
slug: model-economics
audience: public
last_reviewed: 2026-06-19
concepts:
  - model-economics
  - model-cost
  - cost-per-turn
  - effective-cost
  - provider-comparison
  - pricing-band
ui_surface: admin.model-economics
related_specs:
  - spec-model-economics-page-2026-06-13.md
---

# Help: Model Economics Page

The Model Economics page is a pod-wide, **model-centric** cost leaderboard — one
row (or bar) per model identity, summed across every bot in the pod. It is the
*transpose* of the bot-centric Usage / Cost page: where Usage answers "what is
each bot spending?", Model Economics answers "what does each model cost me, pod-wide?"
— so you can tune model **choice** by comparing within a pricing band or across providers.

It reads the same underlying data the Usage page consumes (the per-day `by_model`
metrics), assembled model-first.

**Ask evo for the model comparison.** Same numbers, conversational answer:

- *"which model is cheapest per turn right now?"*
- *"compare Sonnet and Opus on effective cost"*
- *"what are we spending on each provider this month?"*

Evo answers from the same `pod_state.usage` model metrics this page renders, so
the numbers match what you see here.

---

## What the page shows

**Bars by default, table on toggle.** The default view is a horizontal bar chart,
one bar per model, sorted descending by the selected metric. A toggle flips to a
dense multi-stat table for side-by-side reading.

**Metric switcher.** Re-sort and re-scale the whole view by:

- **$/turn** — average spend per turn on that model
- **Eff. cost/1k** — effective cost per 1,000 tokens, *cache-aware* (this is a cost
  number, not an effectiveness score)
- **spend** — total spend over the selected window
- **share** — that model's share of pod spend
- **turns** — how many turns ran on it

**Filter chips.** Narrow the view by **provider**, **pricing band**, **bot**, or
**audience**. Selections are multi-select within a facet and combine (AND) across
facets — e.g. "Anthropic" + "the standard band" + "two specific bots".

**Aggregate rollup strip.** A summary strip rolls models up by **band** or **role**
(toggle between the two) and shows the blended $/turn for each group — useful for
"is my standard tier as cheap as it should be?" at a glance.

**Confidence.** Low-confidence rows render dimmed rather than hidden, and keep their
confidence badge, so a thin-data model never silently disappears from the comparison.

**Tier column.** The tier shown is the **pricing cost-band** the model falls in.
`configured`, `off-catalog`, and `unexpected-billing` are badges on a row, not
separate rows.

---

## How it relates to the other cost surfaces

- **Usage** — bot-first spend (what each *bot* costs). Model Economics is the
  model-first transpose of the same data.
- **AI Optimization** — where you *change* model choice and tier routing. Model
  Economics is the evidence you bring to that page: it shows which models are
  worth keeping in the catalog and which band each really lands in.
- **Cost Optimization** — caps and remediation (the brakes). Model Economics is
  about steering, not braking.
