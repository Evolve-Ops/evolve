---
title: "Help: Cost Optimization Page"
slug: cost-optimization
audience: public
last_reviewed: 2026-06-06
concepts:
  - cost
  - spend
  - budget
  - daily-cap
  - efficiency-score
  - context-pruning
  - tier-downgrade
  - cost-caps-matrix
  - graduated-remediation
  - l2-breaker
  - cascade-toggle
  - pod-defaults
ui_surface: admin.cost-measures
related_specs:
  - docs/spec-cost-caps-2026-06-05.md
---

# Help: Cost Optimization Page

The Cost Optimization page (in the **Improve** bucket) is the control center for spend governance — the cap ladder, the per-bot context and session settings, the behavioral efficiency score, and the audit tools. Use this page to understand how each bot is spending and to set the guardrails that catch runaway costs before you find them on a bill.

**Ask evo to apply a cap or optimization.** Evo has chat tools for the whole cap ladder — *"set team-bot-a's daily warn at $5"*, *"trip the tier-downgrade on team-bot-a"*, *"what cost optimizations are open?"*. The matrix on this page and the chat tools both write through the same backend, so changes show up either way.

---

## POD vs. per-bot

The page has two views, picked by the bot-tab row near the top:

- **POD tab** (first, default) — shows pod-wide content: the pod-default Cost & Caps matrix (every threshold that bots inherit unless they override it) and an aggregate of every open cost proposal across the pod.
- **Per-bot tab** (one per bot) — shows that bot's Cost Efficiency Score, its Cost & Caps matrix with inherited-vs-explicit indicators, its Context & Session Settings matrix, and the runaway / audit / spike sections below.

The per-bot tile rail above the tabs is a faster picker — click a tile to jump straight to that bot's tab. The tiles also show a grade letter (A–F), today's spend, and a chip row for any active cost issues.

---

## Per-bot tile rail

One tile per bot at the top, primary-first then alphabetical. Each tile shows:

- **Grade circle (A–F)** — the bot's current Cost Efficiency Score.
- **Today's spend + delta** — vs. yesterday, with the trend arrow.
- **Tier-colored model-mix bar** — share of spend going to tier1/2/3.
- **Chip row** — up to 3 active cost issues (e.g. `cost_burst`, `l1_tripped`, `cascade-live` indicator if the bot is in the cascade-toggle treatment group).

Click a tile to switch to that bot's tab. Empty days collapse the spend headline to "no spend"; cascade-live is only shown on bots where the toggle is on (see Cascade toggle below).

---

## Cost & Caps matrix (per-bot + POD)

The headline control on this page. One row per cost threshold, organized into three groups:

### Alerts · notify only

These send a Signal to your alert channel when crossed. No enforcement.

| Row | What it does |
|-----|--------------|
| **Monthly warn** | Notifies when month-to-date spend crosses this. Alert-only. |
| **Weekly warn** | Notifies when rolling 7-day spend crosses this. Deduped once per ISO week. |
| **Daily warn** | Notifies when today's spend crosses this. Deduped once per day, pod local TZ. |
| **Pod-total weekly warn** *(POD tab only)* | Notifies when the SUM of every bot's rolling 7-day spend crosses this. Pod aggregate; no per-bot equivalent. |

### Remediation · graduated enforcement

The graduated ladder shipped May–June 2026 ([spec](../spec-cost-caps-2026-06-05.md)) replaces the old single-cap model with three independent levers, each more disruptive than the last. Each fires on today's spend in pod local TZ.

| Row | What it does | Recovery |
|-----|--------------|----------|
| **Tier downgrade (daily)** | Auto-switches the bot's primary model to tier 3 for the rest of the day when crossed. | Auto-reverts at midnight (pod local TZ). |
| **L1 circuit breaker (daily) — stop auto sessions** | Trips L1: heartbeat + background sessions pause; user chat keeps working. A "this bot is over its daily budget; resumes at midnight" sentinel replaces background turns. | Auto-resets after 24h. |
| **L2 circuit breaker (daily) — shut down gateway** | Stops the bot's gateway entirely. No chat, no background turns. | Manual reset required; **no auto-revert.** |

Pick the smallest hammer that solves your problem. Tier downgrade is reversible at midnight and barely felt; L1 keeps you reachable but kills cron / heartbeats; L2 is the "I need this bot offline today" lever.

### Sentinel · in-flight session ceiling

| Row | What it does |
|-----|--------------|
| **Per-session cap** | Rejects the next turn in any session whose accumulated cost crosses this. Catches looping prompts and runaway tool use before they burn through the daily budget. |

### How the matrix reads

Each cell shows the active value or `—` if unset. On the per-bot tab, three column states:

- **Default** — inheriting the pod default for this threshold. Cell shows the inherited value with a grey "inherited" affordance.
- **Off** — explicitly disabled on this bot. Cell shows "Off."
- **Custom $___** — explicit per-bot override. Cell shows the dollar amount in the active accent color.

Click a row to expand its description; click a cell to switch its state. Setting any cell to a numeric value writes through `/api/arbiter/bot-setup/<bot>`; "Default" deletes the override so the row inherits again. On the POD tab, the matrix is simpler — every row is the pod default, and changes propagate to inheriting bots immediately.

**Pod conduct rule for transparency.** When a remediation tier fires on a bot, the bot itself receives a `RUNTIME_NOTES.md` injection at the next session_start telling it which remediation is active and why. This means a user who hits a tier-downgraded bot gets an honest "I'm running on a slower model right now because we crossed today's spend threshold" rather than silent degradation.

---

## Cost Efficiency Score (per-bot)

The per-bot grade card at the top of the per-bot view. Replaces the old composite 0–100 score with a behavioral measurement: did the bot actually spend efficiently over the last 7 days?

The grade circle shows the letter (A–F) and the numeric score. Below the badge, a "components" grid lists each measured behavior with a pass/fail icon, score, one-line detail, and — for failing components — a fix hint plus clickable jump links that scroll the Cost & Caps or Context & Session matrix row into view and flash it briefly.

Common components:

- **Heartbeat behavior** — are idle pings running light (small context, tier3) instead of full sessions?
- **Maintenance routing** — are debug-the-gateway sessions hitting tier3 as intended?
- **Cache discipline** — what fraction of input tokens are cache hits vs. re-paid?
- **Audience mix** — are autonomous (non-Human) audiences pulling premium models?
- **Per-session ceiling fit** — is the per-session cap catching outliers without false positives?
- **Cap headroom** — is the bot well within its daily warn / L1 thresholds, or running close to the edge?

The window is "last 7 days" by default; the panel text states it explicitly. The score is a diagnostic — fix the matrix rows below to move it.

---

## Context & Session Settings (matrix)

Per-bot only. A matrix view of every cost-relevant `openclaw.json` setting. Rows are individual fields grouped by purpose (heartbeat, context pruning, compaction, session start). Columns are four profiles plus an editable Custom column:

| Column | What it means |
|--------|---------------|
| **Conservative** | Maximum cost savings, smallest context. Good for high-volume short-session bots. |
| **Balanced** | Recommended default for normal work. |
| **Performance** | Maximum continuity, largest context. Use for long-form research bots. |
| **Other** | Either a saved custom profile (dropdown picker) or live-editable Custom values for any integer field. |

Click a **column header** to apply that entire profile to the bot. Click a single **cell** to set just that one field to the profile's value. The active value is highlighted with a bold accent border; unset values show as "—".

To save the current per-bot state as a reusable profile, click **+ Save current as profile** in the header. Saved profiles appear in the Other column's dropdown and can be applied to any bot. Custom profiles can be deleted from the dropdown's trash button.

After changing settings the gateway needs a kickstart — go to **Maintenance → Status** and restart the bot. The page banner reminds you.

**Setting groups, briefly:**

- **Heartbeat** — session_isolation and light_context_mode together cut idle-ping cost by 97%+ when both are on. Recommended: both on.
- **Context pruning** — drops idle session data on a TTL. Cache-TTL mode with a 1-hour TTL is the sweet spot for most bots.
- **Compaction** — what happens when the context window fills. Safeguard mode preserves the most history; Aggressive compacts early for cheaper turns.
- **Session start** — bootstrap_max_chars caps total injection; idle_reset_minutes ends fully idle sessions.
- **Prompt cache TTL** — added 2026-05-30; controls how long OpenClaw keeps Anthropic ephemeral cache breakpoints alive. Longer = more cache hits when conversations are slow; shorter = lower cache-write overhead for very chatty bots. Default is the OpenClaw default. Deep-link in the row jumps to the Anthropic billing console row that this affects.

---

## Cascade toggle

A toggle on each bot's row in the AI Optimization → Tier Definitions section, mirrored as a chip on the per-bot tile here. When **cascade is on**, the model router walks down the tier ladder on tier-2 failures or "I need help" markers from the bot — tier2 → tier3 if tier2 errors, or tier1 → tier2 if a tier1 turn is taking too long. When **off**, a failed tier-2 call fails the session instead of cascading.

Cascade is off by default. It's a treatment-group experiment shipped 2026-06-05 ([PR 2291](https://github.com/evolve-ops/evolve/pull/2291)) — bots in the treatment group get the chip and the live cascading behavior; bots outside don't. Once the per-bot effect on spend and quality is well-characterized this will graduate from experiment to default.

The deprecated **Configure** button on the cost tile was removed in the same PR — every tile-level cost action now lives in the Cost & Caps matrix below or in the AI Optimization page.

---

## Runaway Session Monitor

Detects sessions consuming disproportionate cost — a sign of a looping prompt, runaway tool use, or a compromised key.

**↻ Refresh** checks for active outlier sessions right now.

A session is flagged as runaway when its cost is significantly above the bot's rolling average session cost. Flagged sessions appear with the session ID, estimated cost, turn count, and time active.

Pair this with the per-session cap row in the matrix above — a per-session cap rejects the next turn in any flagged session, so combining "ceiling on per-session spend" with "alert me when sessions look runaway" gives you both prevention and detection.

---

## Turn Audit

An audit table of the most expensive individual turns across all sessions. Use this to identify what's actually driving spend — a single expensive turn with a huge tool call often explains a cost spike better than aggregate charts.

Sort by **Most recent** or **Highest cost**, and pick a time range (Today / 7d / 30d). Each row links to the session it belongs to.

---

## Spike Explorer (Budget Hawk v2)

One row per billable LLM call within the selected window, tagged with `trigger_kind` (cron / heartbeat / message / tool) and `cache_state` (cache miss / hit / partial). This is the lineage-backed view that powers Budget Hawk v2's forensics detectors — it answers "why did this cost what it did" in a way aggregate charts can't.

The rollup at the top groups spend by trigger and by app so you can spot which subsystem is producing the long tail. When a cost-burst Signal fires, this is the section you scroll to.

---

## Common questions

**My efficiency score is failing on "Cache discipline" — what do I do?**
Click the fix-link on that component. It jumps you to the Prompt cache TTL row in the Context & Session matrix above and flashes it. Increase the TTL if conversations are slow (more hits when the cache lives longer), or check the heartbeat-light-context row — if heartbeats are running heavy, they invalidate the cache on every ping.

**A bot is L1-tripped but I need it to keep doing background work — can I unstick it?**
L1 auto-resets after 24h. If you need to override before that, raise the bot's L1 threshold so today's spend no longer exceeds it (matrix → L1 row → Custom $___ above the current value). The breaker re-checks against today's spend on the next pricing tick. Alternatively, leave the threshold and accept the daily pause.

**I tripped L2 by mistake — how do I get the gateway back?**
L2 doesn't auto-revert. Raise the L2 threshold so the current spend is back under it, then redeploy or restart the gateway (Maintenance → Status). Set L2 to **Off** if you don't want that lever active.

**I set a Daily warn at $5 but never got an alert — what's wrong?**
Three things to check: (1) the alert channel for cost is configured in Reports → Alerts; (2) the producer `budget_hawk` is in the alert allowlist (it should be by default); (3) today's spend actually crossed $5 in pod local TZ — the dedupe says "once per day" so a tiny over-crossing followed by re-crossing on the same day fires once.

**Which order should I climb the ladder?**
Top-down: alerts first (warns at $X), then tier downgrade (auto-reversible at midnight), then L1 (24h pause on background), then L2 (operator-only reset). Pick the smallest hammer that solves your problem. Most pods only need warns + tier downgrade.

**The score card says "behavioral measurement over last 7 days" — what changed?**
The composite 0–100 score in the previous incarnation was a weighted average of static checks (is tier3 in use, is light-context on, etc.). The new score measures actual behavior: did the bot's spend pattern over the last 7 days look efficient, given its workload? Failing components carry jump links to the matrix row that drives them, so the diagnostic is one click from the fix.

**Where did the old Spending Caps section go?**
Merged into the Cost & Caps matrix above. The old single `daily_cap_usd` is now the **L1 circuit breaker** row; everything else (warns, tier downgrade, L2, per-session cap) was net-new and didn't exist on the page before. Pod-wide defaults moved to the POD tab.

**Important:** Spending caps are enforced by Evolve's cost-cap ladder, not by the API provider. Set caps at your provider too (console.anthropic.com) as a backstop in case the cap mechanism itself fails — defense in depth.

**The runaway session monitor flagged a session — what should I do?**
Check the session in the bot's OC interface. If it looks legitimate (a complex long-running task), you can dismiss it. If it looks like it's looping or unusually verbose, end the session from the OC admin. For recurring issues, set or lower the per-session cap row above so the ceiling catches the next one automatically.

**Note on MAX subscriptions.** Anthropic ended MAX coverage for third-party tools on April 4, 2026. Every OpenClaw call bills at API rates regardless of MAX status. The Auth-mode score factor that used to surface this has been retired; the cap ladder is the durable answer.
