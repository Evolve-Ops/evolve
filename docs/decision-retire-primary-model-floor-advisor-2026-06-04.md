# Decision: retire `primary_model_floor_advisor` (2026-06-04)

## TL;DR

The `primary_model_floor_advisor` generator was retired because its
core premise was obsoleted by the L1–L10 tier-routing audit work. Its
proposals — "lower `agents.defaults.model.primary` to the floor tier
so cost-leak paths land cheaper" — became misleading once the trigger
anchor closed the dominant leak path. The remaining work that still
falls through to `primary` is intentional Sonnet usage for human
chat; lowering primary degrades that chat (the exact PR #1765
regression PR #1774 reverted).

## What the generator was trying to do

Look at a bot's session-class distribution over a 14-day window. If
≥70% of sessions classified as `maintenance` / `background` /
`ambiguous` (tier3-shaped work), propose lowering
`agents.defaults.model.primary` to the floor tier (tier3) "so the
leak path lands cheaper without changing routed behavior for any
specific session class."

The "leak path" was the set of OC code paths that fell through to
`primary` instead of an explicit tier override:

- Heartbeat sessions that the classifier mis-identified (the
  "heartbeat-override leak")
- OC's bundled fallback chain walk
- Unclassified follow-up turns inside a longer session

In May 2026, those leaks were real and primary was the right knob.

## Why the premise no longer holds

Three pieces of audit work changed the floor under the generator:

1. **PR #1737 / PR #1764** — pre-classification trigger anchor.
   Heartbeats / crons / subagents / summarizers / classifiers /
   task_extractors / fallbacks are now pinned to
   `routing.backgroundTier` (= tier3) **before** the content
   classifier runs and **before** `primary` is consulted. The
   "heartbeat-override leak" no longer exists — those sessions don't
   touch primary at all.

2. **PR #1774** — revert of role-aware primary derivation. Background
   work was already routing to tier3 via the anchor; flipping
   member-bot primary to tier3 just silently degraded human chat on
   Slack / Telegram / Discord with no in-channel escalation path.
   The principle that came out of the revert: **on member bots, human
   chat stays on tier2 (Sonnet) — primary is the human-chat default,
   not a cost-leak destination.**

3. **PR #1786** — Phase A `userTierOverride.defaultTier` picker. The
   operator's explicit "default tier for user-facing turns" knob now
   has its own first-class field on `evolve-tiers.json`, surfaced via
   the AI Optimization page dropdown. That's the supported primitive
   for "I want this bot's chat to be cheaper" — separate from
   `primary`, which OC consumes for its own fallback resolution.

After all three landed, the generator's premise — "primary is the
unused-by-default leak destination" — was wrong on every active bot.
Every spot where it would have proposed a primary downgrade was
either (a) already routing to tier3 via the anchor, or (b)
intentional human-chat usage the operator wanted on Sonnet.

## Concrete evidence that triggered retirement

On 2026-06-04 the generator surfaced three proposals in the
admin-UI Improvements tab (one per affected bot role). Per
docs/PLACEHOLDER_NAMING.md the rows below use role placeholders
rather than the live pod's bot names:

| Bot | Generator claim | Reality |
|---|---|---|
| team-bot-b | 91% of 238 sessions classify to tier3-or-cheaper | True — but the 9% that don't are user_turn sessions on Sonnet (intentional). |
| team-bot-a | 89% of 211 sessions classify to tier3-or-cheaper | Same shape. Live spans showed 9 user_turn → tier2 via `default` driver in a single day. |
| security-bot | 86% of 100 sessions classify to tier3-or-cheaper | Same. |

The proposal body's claim "doesn't change routed behavior for any
specific session class" was **false** in all three cases — lowering
primary would have changed routing for the productive class
(human chat), which is what the PR #1774 revert exists to protect.

The three proposals were dismissed on the pod with the verdict
`bad_signal` and a note pointing at the obsolete premise.

## What replaces the generator

Two things — together they cover the cost-optimization intent without
the misleading framing:

1. **`userTierOverride.defaultTier` (Phase A, PR #1786)** —
   operator-controlled per-bot default for user-facing turns. The AI
   Optimization page exposes a dropdown (Auto / Fast / Standard /
   Power). Operators who want a bot's chat to be cheaper set
   `defaultTier=fast`; the plugin reads it on every turn and routes
   user-turn / productive / ambiguous sessions to tier3 without
   touching the OC-level `primary` field. **This is the correct
   primitive for "make this bot cheaper for chat."**

2. **`cost_watchdog.config_drift`** — reactive forensics for any
   change to `agents.defaults.model.primary` that lands outside an
   audited path. Catches accidental writes (deploy regressions, hand
   edits) and surfaces them as Signals on the Alerts page. Existed
   before this generator was retired; not affected by the retirement.

## What changed in this PR

- Deleted `packages/analyzer/generators/primary_model_floor_advisor/`
- Deleted `packages/analyzer/tests/test_primary_model_floor_advisor.py`
- Removed factory + registry entry in
  `packages/analyzer/generator_runner.py` (with a comment explaining
  the retirement and pointing at this doc)
- Removed `detect_primary_off_floor_chip` from
  `packages/analyzer/cost_opt_tiles.py` (the chip detector that
  consumed the generator's proposals)
- Updated 3 downstream tests that referenced the generator:
  - `test_charter_surface_field.py` — dropped the floor-advisor
    routing entry, swapped the YAML-roundtrip witness to
    `bloat_investigator`
  - `test_proposal_title_humanization.py` — dropped the 4 title-shape
    tests, replaced with a single retirement-witness assertion
  - `test_cost_opt_tiles.py` — dropped the 2 chip-detector tests,
    replaced with an absence assertion
- **NEW** lock-in:
  `packages/analyzer/tests/test_no_primary_floor_advisor_regenerates.py`
  Asserts the directory is gone, the import path doesn't resolve, the
  registry doesn't reference it, the chip detector is gone, no test
  fixture lingers, and this decision doc exists.

The lock-in test deliberately makes re-introducing the same name
require deleting tests AND writing a follow-up decision doc — so the
next time someone reaches for "lower primary to save cost," they
have to engage with the audit trail that established why it doesn't
work.

## What re-introduction would need to look like

If a future need arises for an automated "this bot's chat could be
cheaper" suggester, it should:

1. **Propose `userTierOverride.defaultTier`** (Phase A's primitive),
   not `agents.defaults.model.primary`. The primitive that exposes
   tier choice for user turns IS the operator-default knob.
2. **State its impact honestly** — "users on this bot would see
   tier3-quality replies by default" — not the misleading "doesn't
   change routed behavior."
3. **Honor the per-bot opt-out** — a bot where
   `userTierOverride.enabled = false` should be excluded entirely.
4. **Not collapse session classes** — productive sessions (human
   chat) and ambiguous sessions are different work; collapsing them
   under one threshold is what got us here.

A generator that does all four belongs in a new module with its own
name and charter, with the design conversation captured in a
follow-up decision doc.
