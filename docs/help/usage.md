---
title: "Help: Usage Page"
slug: usage
audience: public
last_reviewed: 2026-06-05
concepts:
  - usage
  - billing
  - model-spend
  - cost-rollup
  - export
ui_surface: admin.cost
related_specs: []
---

# Help: Usage Page

The Usage page shows AI API spend broken down by model, channel, and source — with billing summaries and exportable data. Select a bot and time range to drill into any account.

**Ask evo for the cost rollup.** Same numbers, conversational answer:

- *"how much are we spending this week?"* / *"...this month?"*
- *"which bot costs most?"*
- *"show the projected month-end"*

Behind the scenes evo calls `pod_state(query="usage")` (the same daily metric
files this page reads), so the numbers match what you see here.

---

## Controls

- **Bot tabs** — switch between individual bots or view the full pod
- **Time range** — Today, 7d, 30d, or custom date range
- **↻ Refresh** — reload usage data from session annotation files
- **⬇ Export CSV** — download the full usage table as a spreadsheet
- **⬆ Import** — import usage data from an exported file

---

## What Each Section Shows

### Usage Summary
Billing overview for the selected bot and time range: total estimated cost, token counts, and per-model breakdown.

**Important:** Anthropic ended MAX subscription coverage for third-party tools on April 4, 2026. Every OpenClaw call now bills at API rates regardless of whether you have a MAX subscription. Set per-bot daily caps on Cost Optimization (they auto-trip an L1 cost breaker) and set provider-side caps at console.anthropic.com as a backstop.

### Activity Timeline — Turns by Model
Stacked bar chart showing turn volume per model over time. Taller bars = more activity. Color-coded by model to make tier utilization visible at a glance.

### By Model
Breakdown of spend and turn count per model. This shows whether your tier routing is working — in a healthy pod you should see:
- Most turns on tier2 (Sonnet) for productive sessions
- Maintenance/background turns on tier 3 (your lowest-cost model)
- Minimal tier1 (Opus) use — reserved for explicit power requests

### By Channel
How much activity came via each messaging channel (Telegram, Slack, Discord, etc.). Useful for understanding which channels are driving cost.

### By Source
What triggered each API call: `user` (human message), `cron` (scheduled job), `agent` (CE task dispatch), `background` (internal analysis). High `cron` or `background` cost suggests analysis jobs may be over-running.

### By User (top 10)
For bots with multiple users (e.g., a Slack bot used by a team), shows which users are driving the most activity and cost.

### Context Health
Context efficiency metrics: average context window utilization, compaction events, and whether sessions are running into context limits. High context usage with many compaction events suggests sessions are growing bloated — consider adjusting compaction settings in AI Optimization.

---

## Common Questions

**Why is my cost higher than expected?**
Most common causes:
1. **Maintenance sessions not routing to tier3** — go to AI Optimization and verify tier routing is configured and the gateway has been restarted after any config changes
2. **Cron / heartbeat jobs using tier2** — background jobs should use tier3; check the "By Source" breakdown or the Cost Optimization → Model × Audience table
3. **Context bloat** — large context windows cost more per turn; check Context Health
4. **A user override pinning the bot to `power`** — check Users → per-bot panel for per-user tier overrides

**What's the difference between "by model" and "by tier"?**
Models are the specific strings (e.g., `claude-sonnet-4-6`). Tiers are Evolve's routing categories (tier1/2/3). One model can be assigned to a tier. The "By Model" view shows what actually ran; tiers show Evolve's routing intent.

**How accurate is the cost estimate?**
The cost is estimated from token counts and published pricing at the time Evolve was last updated. Actual billing may differ slightly due to pricing changes or batch discounts. Always verify against your provider's billing dashboard.

**Can I see cost across all bots together?**
Yes — select "All bots" from the bot tab selector. The summary shows aggregate spend, but per-model and per-channel breakdowns are pod-wide.

**What does a spike in the activity timeline mean?**
A sudden spike usually means a cron job ran, a batch analysis completed, or a session with an unusually large context was active. Cross-reference with the "By Source" breakdown to identify the cause.

---

## Sessions tab

The Sessions tab shows how well each bot is performing over time — productive vs. maintenance ratios, correction trends, a session browser for drill-down, and the overall RSI loop health.

### What the metrics mean

#### Productive vs. Maintenance Sessions

Every session is classified by the Evolve plugin:

- **Productive** — real user work (research, writing, coding, planning, answering questions). These are the sessions that deliver value.
- **Maintenance** — bot upkeep sessions (debugging gateway issues, fixing config errors, troubleshooting). These cost the same money but deliver no user value.
- **Ambiguous** — sessions where the classifier couldn't clearly determine the type.

**Maintenance ratio** = maintenance sessions / total sessions.

A healthy pod has a maintenance ratio below 20%. Above 30% consistently is a signal that something is consistently broken — a recurring API error, a context bloat issue, or a misconfigured tool. Coaches in the analysis layer emit proposals when this ratio stays high.

#### Correction rate

What fraction of productive sessions had at least one user correction (the user telling the bot to redo, retry, or fix what it just did). Lower is better.

A rising correction rate over several weeks is one of the main signals the analysis engine uses to generate improvement proposals about the bot's effectiveness.

#### RSI loop health

Status block at the bottom of the tab covering whether the substrate is producing the data the engine needs:
- **Metrics current** — `measure.py` ran successfully recently.
- **Analysis current** — `analyze.py` ran successfully in the past 7 days.
- **Proposals flowing** — proposals are being generated and reaching the queue.
- **Outcomes tracked** — verify-daemon and v1 outcome check-ins are being processed.

If any RSI loop component is broken, the quality metrics will still show but improvement proposals will stop flowing. Check the Modules tab in Settings or `evolve-admin status` to investigate.

### Controls and ranges

- **Bot tabs** — view one bot or all bots aggregated.
- **Range tabs** — 7d, 30d, 90d.
- **⚡ Run metrics now** — compute today's metrics immediately instead of waiting for the 01:00 cron.
- **↻ Refresh** — reloads from session annotation files.

### Sections (top to bottom)

**Stat blocks**
Six blocks summarizing the window: total sessions, productive ratio, maintenance ratio, ambiguous ratio, correction rate, and a session-quality summary. Click any block for filter-down behavior on the session browser.

**Insights**
Auto-generated summary of notable patterns — for example, "correction rate up 18% week-over-week on team-bot-a" or "no productive sessions in the last 3 days for admin-bot." When the analyzer has nothing flag-worthy, this section is brief or empty.

**Charts**
- **Productive vs Maintenance Sessions** — bar chart, stacked by class.
- **Correction Rate Trend** — line chart over the selected window.

**Capabilities**
A breakdown of which apps each bot is actively exercising in the window — useful for spotting an app that's installed but unused (a deprecation candidate) or a noun cluster the pod isn't covering yet.

**Session Browser**
Lookup table for individual sessions in the past 7 days, filtered by class, bot, corrections-only, or efficiency-flagged. Click a row to open the full session detail.

### Common questions — Sessions tab

**My maintenance ratio is suddenly very high — what happened?**
High maintenance ratio usually means:
1. A recurring API error or auth failure that keeps creating troubleshooting sessions
2. A context configuration issue causing sessions to start with error messages
3. A recently broken tool or integration that's generating repeated fix attempts

Check Plugins for any failing channels or API keys. Check Gateway Logs in Maintenance for errors. If the gateway is crashing frequently, that will generate many maintenance sessions.

**Why is my correction rate rising even though the bot seems to be working?**
A user correction is detected from session text patterns (the user telling the bot "no, do it this way" or "you got that wrong"). A rising rate can mean:
- The bot is taking more turns to accomplish tasks (efficiency issue)
- Tasks are being left partially complete more often
- The detection criteria have drifted

Cross-reference with the Recommendations page to see if a coach (often `efficiency_hawk`) has surfaced a related proposal.

**What's the difference between Sessions and Cost Optimization?**
Sessions is about *effectiveness* — are sessions doing useful work? Cost Optimization is about *efficiency* — what does that work cost? A bot can be effective but expensive (high productive ratio, high cost) or cheap but ineffective (low cost, low productive ratio). Both matter.

**How does the classifier decide if a session is productive or maintenance?**
First, keyword matching: productive signals include topic words like "research", "plan", "write", "calendar", etc. Maintenance signals include "openclaw.json", "gateway", "restart", "api key error", etc. If keyword confidence is above a threshold, classification is immediate. If ambiguous, a single tier3 LLM call classifies the first user message.
