# Sessions Redesign — Spec (2026-05-11)

**Status:** draft for review
**Supersedes:** the existing "Session Quality" page (productive/maintenance/corrections/efficiency frame)

## Problem

The current Session Quality page is built on unreliable inference. Every load-bearing metric is broken or misleading:

- **Productive vs Maintenance classification** is keyword-matched on conversation text. A 67-turn Admin-Bot session productively debugging gateway-selfheal gets tagged "maintenance" because the words `gateway`, `selfheal`, and `debug` appear. The classifier cannot distinguish productive debugging from struggling debugging — they use identical vocabulary.
- **Corrections/Session is always 0** because the detector substring-matches literal phrases (`"no, i meant"`, `"you misunderstood"`). Real user corrections use varied natural language and never match.
- **Efficiency flags never fire** because they require high-turn + low-complexity, and high-turn sessions are rare.
- **`applications_invoked` capability tagging is also keyword-matched** on conversation text, not on actual tool calls. A session that mentions "calendar" gets tagged `calendar` regardless of whether anything calendar-y happened.
- The denominators are also wrong: ~76% of sessions are "ambi" (automated heartbeats, cron-driven turns) and dilute every ratio.

Beyond accuracy, the metrics are not decision-supporting. A high maintenance ratio doesn't tell the operator what to do. The page describes; it doesn't help.

## Reframe

Three shifts:

1. **Observation, not inference.** Replace classifier-derived metrics with ground-truth facts: timestamps, tokens, costs, cache state, tool calls.
2. **Deltas, not absolutes.** Aggregate stats are boring; *what changed this week* is interesting and actionable.
3. **Curation, not aggregation.** With 2,000+ sessions per period, the operator cannot read them all. The page should produce a shortlist of which to read.

Rename the page from **Session Quality** to **Sessions**. The "quality" framing implied judgement the system cannot reliably render.

## Audience

The pod operator deciding:
- Is the pod healthy and being used?
- Should I touch the cost-tab knobs (TTL, history limits, model routing)?
- Which sessions deserve my attention this week?

## Three pillars

The redesign has three coordinated pillars:

1. **Operator UI** — the rebuilt Sessions page (humans read this)
2. **Instrumentation roadmap** — the new data plumbing required
3. **RSI integration** — same data feeds Signals, generators, and verification (system learns from this)

The same telemetry serves both audiences. The page is a *view* onto the substrate; the RSI loop is another view onto the same substrate.

---

## Pillar 1 — Operator UI

### Section 1: Vital signs strip

Headline numbers, each with delta vs prior period. Period selector (7d/30d/90d) and per-bot filter retained.

| Metric | Source |
|---|---|
| Session count | session_summary records |
| Total cost | cost_event aggregation |
| Cache hit rate | `cache_read / (cache_read + cache_write + input)` per turn, averaged |
| Invalidated cache % | share of cost_events with `cache_state == "invalidated"` |
| p95 model latency | per-turn model latency (model-side only until gateway pairing exists) |
| Active users | distinct users with ≥1 human turn |
| Non-responses | count of `unanswered_message` Signals firing in the window (when monitor exists) |

**Replaces:** Productive %, Maintenance %, Corrections/Session, Efficiency Issues.

### Section 2: Anomaly strip

Auto-generated cards showing *deltas* relative to trailing baselines. 3-6 cards per render. Each card is a Signal from the `session_economics` monitor (see Pillar 3). If nothing is anomalous, render an "all stable" tile — itself a meaningful signal.

Card examples:
- `Team-Bot-C's session count up 35% vs trailing 4w`
- `Team-Bot-B unused for 6 days — longest gap in 90d`
- `Evolve's invalidated-cache rate jumped to 22% (was 8%) — consider bumping TTL`
- `Admin-Bot had its longest session ever yesterday (47 turns, $4.20)`
- `New user "Sara" — first session with Team-Bot-C`

### Section 3: Activity rhythm

- **Time-of-day heatmap per bot** (hour × day-of-week) — reveals each bot's natural rhythm.
- **Daily session count per bot, 90d trend** — growth, decline, abandonment.
- **Inter-turn gap distribution per bot** — direct input to the TTL question.

All three use timestamps and counts. Zero inference.

### Section 4: Cache & cost economics

The decision-support panel. Operators land here before touching the cost tab.

- **Cache health stacked area per bot** — `warm` / `invalidated` / `fresh` / `unknown` over time. Field already exists in `cost_event` records.
- **Cost decomposition** for the period: cache_read $, cache_write $, fresh input $, output $. High cache_write with low cache_read = TTL too short. High fresh-input slice = caching not engaging.
- **Cost-per-session histogram** (not average) per bot, with the **top 10 most expensive sessions** linked out.
- **Context-size trajectory** — avg context tokens per turn over time, with outlier sessions flagged.

This panel directly answers: *Are sessions too costly? Are we dropping too much context? Should I move the TTL?*

### Section 5: Sessions to read (curation)

5-10 individual sessions selected by ground-truth rules. Refreshes per period.

Rules (all observation-based, zero inference):
- Top cost outlier
- Top turn-count outlier
- Longest-context outlier
- First session for any new user
- First invocation of any new tool *(needs real tool tagging)*
- Sessions with errors
- Long user-message sessions (>300 words)
- Sessions where a user returned after a long gap

This is likely the most valuable artifact on the page. Operators have thousands of sessions and zero time; a curated 10 is what they actually need.

### Section 6: Session browser (preserved, cleaned up)

**Drop:**
- Class column (ambi/prod/maint chips)
- Capabilities column based on keyword matching

**Add:**
- `cache_state` chip (warm / invalidated / fresh)
- Cost
- Context size
- Real tools-called list *(needs new tagging)*

**Better filters:**
- Bot, integration, user
- "Multi-turn only" (≥2 turns)
- "Errors only"
- "Cache invalidated only"
- Date range

### What is dropped entirely

- Productive / Maintenance / Ambiguous classification (unreliable; misclassifies productive debugging as maintenance)
- Corrections detection (broken; always 0)
- Efficiency flags (never fires)
- The Insights bullet list ("X performing well — 0 corrections across N sessions" — meaningless when corrections is always 0)
- Keyword-based `applications_invoked` tagging

The TierClassifier code stays in the plugin (other systems consume it) but nothing user-facing renders its output.

---

## Pillar 2 — Instrumentation roadmap

What's already captured vs what needs new plumbing.

### Already captured (no new instrumentation)

Each `cost_event` record (`packages/plugin/src/observer/CostLedger.ts:136`) includes:

- `ts` — ISO timestamp (per-turn; inter-turn gaps are computable)
- `bot_id`, `session_id`, `model`, `provider`
- `input_tokens`, `output_tokens`
- `cache_read_tokens`, `cache_write_tokens`
- `cost_usd`
- `cache_state` — already classified as `warm` / `invalidated` / `fresh` / `unknown` (`CostLedger.ts:75-97`)
- `trigger_kind` — `user_turn` / `heartbeat` / `summarizer` / etc.

Each session_summary record includes turn count, outcome text, token totals.

Everything in Section 1, 3, 4 (except non-responses and end-to-end latency) and Section 5 (except the new-tool rule) is buildable from this data today.

### New instrumentation required

**1. Real tool/app tagging (medium plumbing)**

Replace the keyword-matched `applications_invoked` with extraction from OpenClaw turn records — the actual tool calls made during the session. Unlocks the Section 6 capabilities column and the "first invocation of any new tool" curation rule. Implementation in `SessionSummarizer.ts`.

**2. Gateway non-response monitor (new Signal producer)**

The gateway is the only layer that knows "message M received at time T from user U, no reply sent." A new `gateway_responsiveness` monitor pairs inbound message IDs with outbound reply IDs and emits `unanswered_message` Signals when M exceeds a threshold without R.

- Threshold per integration shape (DM-style ~5min, email longer)
- Auto-resolves if a reply eventually arrives
- Sweep-resolves at end of run if backlog clears
- Surfaces in Alerts page; powers Section 1's non-responses metric

This is operationally valuable on its own, independent of the Sessions page.

**3. Gateway-paired end-to-end response time (small plumbing)**

Pair gateway inbound timestamp with first-token timestamp for true user-perceived latency. Until this exists, Section 1 shows model-side latency only.

### TTL change-log (small, for honest verification)

Whenever the operator changes a cost-tab knob (TTL, history limit, model routing), record `{ts, bot_id, field, before, after}` to a small audit log under `{shared_dir}`. This lets the verify daemon and the cache_ttl_tuner generator do honest before/after comparisons rather than guessing which TTL was in effect for any given turn.

---

## Pillar 3 — RSI integration

The same Sessions telemetry that powers the operator UI is a substrate for the RSI loop. Three threads:

### 3a. New monitor: `session_economics`

Daily sweep over `cost_event` and `session_summary` records. Writes Signals to the existing Signal store (per `spec-alerts-signal-store-2026-05-07.md`). Pure Python, no LLM.

Signal types it produces:

- `cache_invalidation_elevated` — firing when bot's invalidated % > threshold over 7d
- `cache_hit_rate_low` — firing when realized hit rate < threshold over 7d
- `cost_per_session_drift` — firing when 7d avg drifts >25% from trailing 28d
- `context_bloat` — firing when avg context per turn grows >X% week-over-week
- `bot_unused` — firing when a bot has zero human turns in N days
- `cost_outlier_pattern` — firing when N+ sessions in a week exceed the trailing p95 by Y×
- `unanswered_message` — emitted by the gateway monitor (Pillar 2 item 2), consumed alongside the rest

**The anomaly strip in Section 2 renders directly from these Signals filtered by `producer == session_economics`.** Same Signal store, same lifecycle (`firing → snoozed → resolved`), same operator workflow. No parallel system.

### 3b. Three new generators

Each follows the standard charter pattern: observes Signals (via `motivating_signals[]`), proposes a bounded change, verify daemon checks the downstream metric. All pure Python — threshold-and-arithmetic, not LLM-judging.

**`cache_ttl_tuner`** *(Phase A — highest confidence)*
- Observes: `cache_invalidation_elevated`, inter-turn gap distribution, current TTL
- Proposes: Bump bot X TTL (e.g. 5min → 1hr), or lower it when hit rate is consistently >95% and write cost dominates
- Verifies: invalidated % drops, total cost drops or holds, within 7d
- Audience: `pod_operator`
- Why it's the cleanest play: single numeric knob, unambiguous signal, measurable response

**`context_truncation_tuner`** *(Phase B)*
- Observes: `context_bloat`, session-length distribution, current truncation policy
- Proposes: Cap history at N turns for bot X
- Verifies: context size flattens AND multi-turn engagement holds
- Audience: `pod_operator`

**`model_router_tuner`** *(Phase C — most subtle)*
- Observes: cost-per-session distribution by session shape (turn count, context size, tools used) and model
- Proposes: Routing rule changes — e.g. "1-turn factual lookups for bot X → Sonnet, not Opus"
- Verifies: cost dropped AND engagement counter-metric held (next-day return rate, multi-turn rate)
- Audience: `pod_operator`

### 3c. Verification substrate for existing generators

Beyond producing its own Signals, Sessions data is the **measurement layer for verifying any generator's proposals.**

Verify daemon currently checks if things didn't break. With Sessions instrumentation it can check whether things actually improved:

- Cost-reducing proposals → measured against cost-per-session distribution
- Routing changes → measured against engagement counter-metric (multi-turn rate, return rate)
- Prompt changes → measured against multi-turn rate, return rate, cost
- Tool additions → measured against tool-call frequency

The verify daemon gains a "sessions delta" check that any generator can opt into, comparing pre/post Sessions metrics over the proposal's window.

### What stays out of RSI (operator-only)

Not everything interesting to humans is actionable by the system. These remain page-only:

- Time-of-day rhythm — informative, no clean system action
- New user appearances — operator should notice; system has nothing to propose
- Per-bot engagement decline — surfaced as a Signal but no automated remediation
- "Sessions to read" curation — for humans, not for proposals

The dividing line: does the observation map to a *bounded config change with a verifiable downstream effect?* If yes, generator material. If no, anomaly callout only.

### Cost discipline

Per the RSI cost rule (memory: `RSI infrastructure must be cheap`):

- `session_economics` monitor: pure Python aggregation over existing JSONL, daily — cheap
- All three generators: threshold-and-arithmetic, no LLM — cheap
- Verification: piggybacks on the existing daily measure run — free

Nothing in Pillar 3 requires LLM escalation. The whole RSI loop on Sessions data can run on cheap CPU.

---

## Build order

### Phase 1 — free now, no new instrumentation

- Sessions page scaffold + rename
- Vital signs strip (without non-responses and end-to-end latency)
- Cache & cost economics panel (Section 4) — biggest standalone win
- Activity rhythm panel (Section 3)
- Curation list with rules that don't require tool tagging
- Session browser with `cache_state` added, broken columns removed
- `session_economics` monitor + Signals (anomaly strip renders from these)
- `cache_ttl_tuner` generator (Pillar 3 Phase A)

This phase alone replaces a misleading page with a useful one *and* delivers the first RSI win on Sessions data.

### Phase 2 — instrumentation unlocks

- Gateway non-response monitor + Signal type
- Real tool tagging from OpenClaw turn records
- TTL change-log for honest verification
- `context_truncation_tuner` generator (Pillar 3 Phase B)
- Curation rules that depend on tool tagging
- Section 6 enriched with real tool list

### Phase 3 — depth

- Gateway-paired end-to-end latency
- `model_router_tuner` generator (Pillar 3 Phase C)
- Per-user activity curves
- Verification substrate hooked into existing generators
- Optional LLM-augmented weekly digest (escalation, not default)

---

## What success looks like

- The page becomes one operators open weekly, not a vanity panel
- The anomaly strip surfaces something genuinely actionable most weeks
- The cache_ttl_tuner produces its first approved-and-verified proposal within a month of Phase 1
- Operators stop guessing when bumping the cost-tab knobs because the economics panel shows whether the current setting is right
- The verify daemon gains a measurable "did it actually help" signal for any cost- or engagement-affecting proposal

## What this replaces

Once Phase 1 ships, the productive/maintenance/corrections/efficiency frame is retired from the UI. The TierClassifier remains in code as long as anything consumes it; if nothing does after Phase 2, retire it from the plugin too.

## Open questions

- Should the existing `efficiency_hawk` generator be folded into `model_router_tuner`, or kept distinct? Both touch cost/model decisions.
- Should `bot_unused` produce a Proposal (e.g. "consider retiring or repurposing bot X") or stay Signal-only? Leaning Signal-only — retirement is a human decision.
- The engagement counter-metric (return rate, multi-turn rate) needs a clear definition before generators that depend on it ship. Worth a small spec section once Phase 2 starts.
