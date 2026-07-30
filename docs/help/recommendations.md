---
title: "Help: Recommendations Page"
slug: recommendations
audience: public
last_reviewed: 2026-06-06
concepts:
  - recommendations
  - proposals
  - arbiter
  - coaches
  - approval
  - snooze
ui_surface: admin.self-improvement
related_specs: []
---

# Help: Recommendations Page

The Recommendations page (in the **Improve** bucket) is the unified suggestion queue — Better Engine's output, ranked and filterable. Every change a coach wants to make to your pod surfaces here for you to approve, snooze, dismiss, or reject.

**You don't need to handle proposals from this page.** Open the chat widget
and tell evo what you want — *"apply the cron-caps proposal"*, *"snooze the
improvement proposals for a week"*, *"reject the model-switch proposal — it's
not a fit"*. Evo runs the same applier this page's inline buttons drive, then
verifies the change landed. The Recommendations page is the visual inventory;
chat is the fast lane to action.

When chatting from this page, evo gets the current pending-proposals context
automatically. Try:

- `what proposals are pending?`
- `apply the cron-caps proposal`
- `snooze improvement proposals for a week`
- `mark complete the morning-brief investigation` *(for deferred-completion kinds)*

---

## How suggestions come to be

```
A coach notices a pattern in observations or system state
  ↓
It self-checks the suggestion against its charter's invariants
  ↓
The suggestion lands here for you to decide on
  ↓
You approve, reject, snooze, or dismiss
  ↓
For approved + testable changes: validation runs
  ↓
Apply — automatically for config edits, manually for hand-rolled instructions
  ↓
For suggestions carrying a falsifiable claim: the check-in verifies 7 days later
  → if the claim fails: revert / flag / escalate per the suggestion's revert plan
```

Your decision is the gate. No suggestion touches a production bot until you act on it.

---

## The "How it works" pipeline strip

Across the top of the page is a six-step illustration: Spot → Self-check → You decide → Test → Apply → Check-in. The "You decide" step is highlighted because that's what this page is for. Below the strip, the **Active coaches** row lists every charter currently producing suggestions, pulled live from `/api/arbiter/generators`.

---

## What a suggestion is

Every entry in the queue is a structured **Suggestion** with these fields:

- **dimension** — which quality the suggestion affects. One of: `substrate_health`, `cost`, `safety`, `utility`, `capability_growth`, `efficiency`, `voice_fit`, `hygiene`, `meta_health`.
- **urgency** — how soon it wants attention. `security_critical` → `operational_urgent` → `cost_alert` → `substrate_warn` → `improvement` → `hygiene` → `whimsy`.
- **generator_id** — which coach emitted it (e.g., `sysadmin_watchdog`, `budget_hawk`, `adjacency_explorer`).
- **action kind** — what clicking *Act* would do (`ConfigPatch`, `AgentsAppend`, `InstallApp`, `Investigation`, …).
- **approval audience** — who's supposed to respond (`pod_operator`, `bot_primary_user`, `both`).
- **claim** (optional) — the falsifiable promise it makes ("this reduces gateway restarts by ≥30% over 7 days"). The check-in verifies it later.
- **revert plan** (optional) — what rollback looks like if the claim fails.
- **guardian annotations** — other coaches flagging concerns ("Security Warden: this touches auth scopes").
- **adjacency** — for exploration suggestions, how close to current behavior this is (`near` / `medium` / `far`).
- **conflicts_with** — related suggestions that touch the same thing; they're grouped on-screen.

---

## The card

Each card shows everything above as small badges with the urgency color on the left edge:

- The ⏳ / ✓ / ✗ at top-left is the **check-in status** — pending, confirmed, refuted, or unknown. This is the claim-follow-up signal.
- The colored badge next to the title is the **urgency**. Red for `security_critical`, orange for `operational_urgent`, down through blue for `improvement`, grey for `hygiene`.
- The dimension pill, action-kind pill, and approval audience pill are self-describing.
- An "adjacency: near" (or medium/far) chip appears when the suggestion comes from an Adjacency Explorer; hover to see the underlying type.
- Guardian warnings appear as "⚠ security_warden · high" mini-badges.
- The bottom line shows bot, coach, age, plus a **rank #N · score X.XX ▾** chip. Click the chip to expand the scoring breakdown: `urgency × track record + tiebreak = score`. This is how the referee ordered the queue.

---

## Actions

- **Act** — marks the suggestion `approved_human` and runs the applier. For `Investigation`-kind suggestions, nothing is applied; the status just advances to `applied` so it drops from the queue.
- **Snooze 1w** — moves the suggestion to the snoozed subdir until a week from now, then the snooze-wake daemon promotes it back. Use this when something is real but not right now.
- **Dismiss** — silently drops it. No learning signal.
- **Reject** — drops it and records "not a fit" in the coach's track record. Use this when the pattern is real but the suggested fix is wrong. Over time, the coach's track record drops, which lowers its rank on the next cycle.

Conflict groups (two suggestions touching the same thing with opposite intent) are bordered and labeled "Conflict group — pick one." Acting on one of them doesn't automatically resolve the others; they remain in the queue until you handle them.

---

## Filters and the rate-limit banner

The top banner shows **N of 7 surfaced this week · M surfaceable now · K held**.

- The 7/week cap is a rate limit so the queue doesn't drown you. `security_critical` and `operational_urgent` bypass it — they always surface.
- "Held" suggestions exist but aren't displayed this week; they surface next week (or immediately when you act on or dismiss enough current ones).

Filter by dimension, urgency, audience, or coach to narrow the list. **Include snoozed** shows deferred suggestions alongside active ones.

---

## What happens after Act

The arbiter snapshots the "before" state and routes the suggestion based on its action kind:

- **Config patches** apply automatically through `apply.py` — backup, write, gateway restart, health check, auto-rollback if the health check fails.
- **Heavy changes** (`InstallApp`, `SoulEdit`, anything touching auth/tools/channel_config) are flagged by Security Warden and routed to the Forge bot for validation first; you'll see a Forge job appear on the Apps → Forge Jobs tab.
- **Investigations** carry no executable change. Acting on them just acknowledges the pattern.

A day or a week later (depending on the claim's `window_days`), the check-in verifies whether the metric moved in the claimed direction. If not: the fallback fires per the revert plan — revert the change, flag for review, or escalate to a watchdog event.

---

## Common questions

**Why did a low-urgency suggestion outrank a high-urgency one?**
The score is `urgency × track record + tiebreak`. If a coach has a low track record (more verified failures than successes), its suggestions sink. A high-urgency suggestion from a coach with track record 0.5 can lose to a lower-urgency suggestion from a coach with track record 1.5 if the urgencies are close enough.

**What's "Rank #1 · score 840 ▾"?**
Click the chip — it expands the breakdown. Useful when a rank surprises you.

**Can I see what a suggestion will change before I click Act?**
The card shows the action kind and, when present, the claim and revert-plan summary. For heavy changes the arbiter routes to Forge validation by default — there is no autonomous apply for those.

**A guardian annotation is flagging something I don't understand.**
The annotation's severity and reason are shown on the badge. They come from other coaches running their own rules on this suggestion. Treat medium/high as "read carefully before acting"; critical means a guardian actively objects and your Act may still succeed but you're overriding its veto.

**The page is empty — should I be worried?**
Probably not. New pods or pods with little session activity won't have enough observations to feed the coaches. Check the Coaches page: if you see zero or only a handful of "Emitted" counts, the substrate is just quiet. Coaches run on staggered cadences (some on_demand, some daily, some weekly), so the queue can populate gradually.

**How do I respond to a 7-day check-in outcome message?**
Check-in outcomes show up automatically in the queue with `verify_status` set to `confirmed` or `refuted`. Refuted suggestions trigger their revert plan automatically — you don't need to act unless escalation routes back to you. The legacy v1 Telegram-based check-in flow (`outcome.py` 7-day ping) was retired in the Better Engine pipeline unification (May 2026); everything now runs through the verify daemon on the unified pipeline.
