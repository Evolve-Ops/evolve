# Spec: Session Quality — Full System

*Author: Evolve · Date: 2026-04-15 · Status: Active*

---

## Purpose

Session Quality is Evolve's answer to the question: **is the bot network actually working?**
Cost and uptime are necessary but not sufficient. A bot can be online, cheap, and completely useless — spending all its sessions on config troubleshooting rather than user work. Session Quality tracks the difference.

The system has two jobs:

1. **Measurement** — annotate every turn as it happens, aggregate into daily metrics per bot
2. **Surface** — present those metrics to the operator in a way that generates understanding and recommends action

This spec covers the complete system: pipeline, data model, measurement, and UI.

---

## Scope

| Component | Location |
|-----------|----------|
| Plugin hooks (annotation writer) | `packages/plugin/src/observer/TurnObserver.ts` |
| Session classifier | `packages/plugin/src/observer/TierClassifier.ts` |
| LLM classifier (ambiguous sessions) | `packages/plugin/src/observer/LLMTierClassifier.ts` |
| Session summarizer | `packages/plugin/src/observer/SessionSummarizer.ts` |
| Daily metrics computation | `packages/analyzer/measure.py` |
| Admin API | `packages/admin/evolve_admin/web/server.py` |
| Admin UI | `packages/admin/evolve_admin/web/index.html` (Session Quality page) |
| Help doc | `docs/help/monitoring.md` |

**Bot coverage:** The plugin is installed on every bot in the network, **including the `evolve` infrastructure bot**. Evolve has its own conversations (setup wizard runs, config changes, operator queries) and its session data is tracked the same as member bots.

---

## Pipeline Overview

```
Bot session (all bots, including evolve)
    │
    ▼
[OC plugin hooks — TurnObserver.ts]
    before_model_resolve → capture selected model
    llm_output           → accumulate token counts per session
    agent_end            → write turn_annotation + turns record
    session_end          → write session_summary record
    │
    ├─────────────────────────────────────────────────┐
    ▼                                                 ▼
{sharedDir}/annotations/{botId}/                {sharedDir}/{botId}/turns/
    {YYYY-MM-DD}.jsonl                              turns-{YYYY-MM-DD}.jsonl
    (session quality: turn_annotation,              (usage/cost: per-turn model,
     session_summary records)                        token counts, cost)
    │                                                 │
    ▼                                                 ▼
[measure.py — runs daily at 01:00 via launchd]   [usage_analytics.py — weekly roll-up]
    reads annotation JSONL
    groups by session_id
    computes session/application/cost metrics
    │
    ▼
{sharedDir}/metrics/{YYYY-MM-DD}/{botId}.json
    │
    ▼
[server.py /api/analytics/metrics]
[server.py /api/analytics/sessions]   ← new
    │
    ▼
Admin UI — Session Quality page
```

---

## Annotation Records

TurnObserver writes to two separate JSONL streams per bot per day.

### Annotation file (session quality)

```
{sharedDir}/annotations/{botId}/{YYYY-MM-DD}.jsonl
```

Contains `turn_annotation` and `session_summary` records. Read by `measure.py` (daily metrics) and `/api/analytics/sessions`.

### Turns file (usage / cost)

```
{sharedDir}/{botId}/turns/turns-{YYYY-MM-DD}.jsonl
```

Contains one record per turn with model, token counts, and cost. Read by `usage_analytics.py` for weekly spend roll-ups and the Usage & Cost dashboard. The `spend_alert.py` script also reads this file to fire Slack alerts when daily spend exceeds threshold.

The date in both filenames comes from the session start time (not the wall clock at write time), so cross-midnight sessions land in a single file.

### turn_annotation

Written by `TurnObserver.handleTurn()` after each `agent_end` event.

```json
{
  "type":                "turn_annotation",
  "schema_version":      2,
  "turn_id":             "string",
  "session_id":          "string",
  "ts":                  "ISO-8601",
  "bot_id":              "string",
  "session_class":       "productive | maintenance | ambiguous",
  "class_signals":       ["string"],
  "class_confidence":    0.0,
  "model_tier":          "tier2 | tier3",
  "model_selected":      "anthropic/claude-sonnet-…",
  "provider":            "anthropic | openai | …",
  "auth_mode":           "unknown",
  "resolution_turn":     1,
  "correction_detected": false,
  "task_id":             "string",
  "input_tokens":        0,
  "output_tokens":       0,
  "cache_write_tokens":  0,
  "cache_read_tokens":   0,
  "cost_estimated":      0.0
}
```

**Classification strategy:**
1. Keyword classifier runs immediately (< 1ms, no I/O)
2. If confidence > 0.75 → done
3. If ambiguous on turn 1 → fire LLM classifier async (Haiku, ~$0.0001); result cached for all subsequent turns in this session
4. Subsequent turns use cached LLM result if available, else keyword result

**Token counts:** OC fires `agent_end` before `llm_output` (typically ~13ms gap). TurnObserver polls up to 500ms in 50ms increments waiting for LLM data to arrive before writing the record. If data still hasn't arrived after 500ms, tokens are recorded as 0 for that turn (session-level totals remain correct via SessionSummarizer).

### session_summary

Written by `SessionSummarizer.summarize()` at `session_end`.

```json
{
  "type":                    "session_summary",
  "schema_version":          2,
  "session_id":              "string",
  "ts":                      "ISO-8601",
  "bot_id":                  "string",
  "turn_count":              3,
  "session_class":           "productive | maintenance | ambiguous",
  "tier":                    "productive | …",
  "tier_confidence":         0.85,
  "first_response_resolution": false,
  "outcome":                 "User asked for help organizing travel itinerary…",
  "complexity":              "low | medium | high",
  "applications_invoked":    ["calendar", "travel"],
  "promises_made":           ["I'll follow up on the flight options"],
  "correction_count":        1,
  "efficiency_flag":         false,
  "total_input_tokens":      4800,
  "total_output_tokens":     1200
}
```

**LLM outcome extraction:** One Haiku call per session, last 2 turns only (capped at 300 chars each). Produces a ≤ 120-char English sentence. Disabled via `enableLLMSummarization: false` in config.

**`first_response_resolution`:** `turns.length === 1`. The user got what they needed in a single exchange — the strongest signal of bot effectiveness.

**Complexity classification:**
- `low` — ≤ 2 turns
- `medium` — 3–6 turns
- `high` — > 6 turns

**Efficiency flag:** true when turns significantly exceed expected for complexity:
- low complexity > 3 turns
- medium complexity > 9 turns
- high complexity > 18 turns

---

## Classification: Productive vs Maintenance

**Productive** — moves user objectives forward:
research, writing, analysis, planning, calendar, health, travel, creative work, project coordination, information lookup.

**Maintenance** — maintains the bot system itself:
config errors, permission issues, gateway restarts, debugging bot behavior, fixing broken scripts, long error-report-and-retry loops.

**Ambiguous** — genuinely unclear, or a mix of both. Excluded from the maintenance_ratio numerator but counted in the denominator.

### Calibration

The keyword lists are calibrated at plugin startup from `{sharedDir}/calibration/classifier.json`:

```json
{
  "classifier": {
    "productive_keywords_add": ["acme-project", "weekly standup"],
    "productive_keywords_remove": [],
    "maintenance_keywords_add": [],
    "maintenance_keywords_remove": ["document"],
    "correction_patterns_add": [],
    "correction_patterns_remove": [],
    "confidence_params": {
      "base": 0.5,
      "per_signal": 0.1,
      "max": 0.9
    }
  }
}
```

Deployment-specific productive keywords (project names, team members, domain vocabulary) should be added here rather than in the plugin source.

---

## Daily Metrics (measure.py)

Runs daily at 01:00 via launchd. Reads one day's annotation JSONL and writes:

```
{sharedDir}/metrics/{YYYY-MM-DD}/{botId}.json
```

### Output schema (v2)

```json
{
  "schema_version":           2,
  "bot_id":                   "team-bot-a",
  "date":                     "2026-04-15",
  "generated_at":             "ISO-8601",
  "session_count":            42,
  "turn_count":               187,
  "productive_sessions":      30,
  "maintenance_sessions":     8,
  "ambiguous_sessions":       4,
  "maintenance_ratio":        0.190,
  "first_response_resolutions": 18,
  "correction_count":         11,
  "total_cost_estimated":     0.2341,
  "provider_cost":            { "anthropic": 0.2341 },
  "unexpected_billing_turns": 0,
  "status":                   "ok | warning | critical",
  "application_usage": {
    "calendar": {
      "sessions":             12,
      "correction_sessions":  1,
      "efficiency_sessions":  0,
      "promise_sessions":     3,
      "unresolved_sessions":  1
    }
  }
}
```

**`maintenance_ratio`** = `maintenance_sessions / session_count`. Includes all sessions (productive + maintenance + ambiguous) in the denominator. Excluding ambiguous would inflate the ratio in deployments with many ambiguous sessions.

**Status thresholds** (calibration-aware):
- `critical` → maintenance_ratio > 0.50
- `warning` → maintenance_ratio > 0.20, or any `unexpected_billing_turns` > 0
- `ok` → otherwise

---

## Admin API

### GET /api/analytics/metrics

**Params:** `bot` (optional, blank = all), `days` (default 30)

**Returns:** `{ [botId]: DayRow[] }` where each `DayRow` is:
```json
{
  "date":                "2026-04-14",
  "session_count":       42,
  "productive_sessions": 30,
  "maintenance_sessions": 8,
  "maintenance_ratio":   0.190,
  "resolution_rate":     0.429
}
```

### GET /api/analytics/sessions *(new — to be built)*

Returns recent `session_summary` records from annotation files, for the session browser.

**Params:** `bot` (optional), `days` (default 7), `class` (optional: productive/maintenance/ambiguous), `corrections` (optional: true), `efficiency` (optional: true), `limit` (default 50), `offset` (default 0)

**Returns:**
```json
{
  "sessions": [
    {
      "session_id":            "abc123…",
      "date":                  "2026-04-14",
      "bot_id":                "team-bot-a",
      "session_class":         "productive",
      "tier_confidence":       0.85,
      "turn_count":            3,
      "complexity":            "medium",
      "correction_count":      0,
      "efficiency_flag":       false,
      "first_response_resolution": false,
      "applications_invoked":  ["calendar", "travel"],
      "promises_made":         [],
      "outcome":               "User planned a 3-day Tokyo itinerary with hotel and flights.",
      "total_input_tokens":    4800,
      "total_output_tokens":   1200,
      "ts":                    "ISO-8601"
    }
  ],
  "total": 142,
  "offset": 0,
  "limit": 50
}
```

**Implementation:** reads `session_summary` records (type == "session_summary") from annotation JSONL files in date range, applies filters, returns paginated slice. Reads directly from annotation files (not metrics JSON) so results are available same-day without waiting for measure.py.

---

## Admin UI — Session Quality Page

### Design Principles

1. **Signal before data** — the page should tell you something actionable within 3 seconds, not make you interpret charts
2. **Three zones** — Signal (what's the health), Insight (why, what changed), Explore (individual sessions)
3. **Recommendations, not just metrics** — every anomaly should come with a suggested next step
4. **Ad hoc application** — the session browser with filters should let operators answer their own questions without writing queries

### Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Session Quality                               [↻ Refresh]  │
│  [All] [Team-bot-a] [Admin-bot] [Security-bot] [Evolve]                      │
│  [7d]  [30d] [90d]                                          │
├─────────────────────────────────────────────────────────────┤
│  SIGNAL                                                     │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌────────┐ │
│  │ 847  │ │  72% │ │  28% │ │  61% │ │   8% │ │   14   │ │
│  │ sess │ │ prod │ │ maint│ │ 1st  │ │ corr │ │ effic  │ │
│  │      │ │      │ │      │ │ resp │ │ rate │ │ flags  │ │
│  └──────┘ └──────┘ └──────┘ └──────┘ └──────┘ └────────┘ │
├─────────────────────────────────────────────────────────────┤
│  INSIGHTS                                                   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ ⚠ Maintenance ratio elevated (28% vs 14% last period)│   │
│  │   Top signals: error: (18), permission denied (9)   │   │
│  │   → View Infra Jobs                                 │   │
│  │                                                     │   │
│  │ ℹ Admin-bot: 0 productive sessions this week            │   │
│  │                                                     │   │
│  │ ⚠ Correction rate up 3× vs last period (8% → 25%) │   │
│  │   Affected applications: calendar, task-management  │   │
│  │                                                     │   │
│  │ ✓ Travel application performing well — 94% 1st resp  │   │
│  └─────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│  CHARTS                                                     │
│  ┌──────────────────────┐  ┌──────────────────────────┐    │
│  │ Sessions by Class    │  │ Correction Rate Trend    │    │
│  │ (stacked line chart) │  │ (line chart)             │    │
│  └──────────────────────┘  └──────────────────────────┘    │
├─────────────────────────────────────────────────────────────┤
│  APPLICATIONS                                               │
│  ┌────────────────────────────────────────────────────┐    │
│  │ Application usage — horizontal bar, class-colored   │    │
│  │ calendar ████████████████████░░░░░░ 34 sessions   │    │
│  │ email    ███████████░░░░░ 18 sessions              │    │
│  │ travel   █████████ 14 sessions                    │    │
│  └────────────────────────────────────────────────────┘    │
├─────────────────────────────────────────────────────────────┤
│  SESSION BROWSER                                            │
│  [All classes ▾] [Any bot ▾] [Corrections ☐] [Flags ☐]    │
│  ┌────────────────────────────────────────────────────┐    │
│  │ Date  Bot   Class   Turns Caps         Outcome     │    │
│  │ 04-14 team-bot-a ● prod   3     calendar,… Planned trip… │    │
│  │ 04-14 team-bot-a ● maint  8     evolve-sys  Fixed gatew… │    │
│  │ 04-13 admin-bot ● ambi 2     —           Unclear requ…│    │
│  │ …                                                  │    │
│  └────────────────────────────────────────────────────┘    │
│  [← Prev]  Showing 1–25 of 142   [Next →]                 │
└─────────────────────────────────────────────────────────────┘
```

---

### Zone 1 — Signal (stat blocks)

Six stat blocks, replacing the current four. Computed from the fetched metrics data.

| Block | Value | Color logic |
|-------|-------|-------------|
| Total Sessions | count | neutral |
| Productive % | productive / total | green ≥ 80%, yellow ≥ 60%, red < 60% |
| Maintenance % | maintenance / total | green < 20%, yellow < 35%, red ≥ 35% |
| 1st Response Rate | first_response_resolutions / sessions | green ≥ 70%, yellow ≥ 50%, red < 50% |
| Correction Rate | correction_count / turn_count | green < 10%, yellow < 20%, red ≥ 20% |
| Efficiency Issues | sum of sessions with efficiency_flag *(from session browser data)* | green = 0, yellow < 5, red ≥ 5 |

Each block shows a ▲▼ trend delta vs the previous equivalent period (computed client-side by splitting the fetched time series in half and comparing).

---

### Zone 2 — Insights Panel

A card generated client-side from data already in memory. Renders a list of observations sorted by severity (⚠ first, then ℹ, then ✓). Collapses to just the attention-level items if there are > 5 observations.

**Observation rules (in priority order):**

| Condition | Severity | Message | Action link |
|-----------|----------|---------|-------------|
| maintenance_ratio > 0.35 | ⚠ warning | "Maintenance ratio elevated (N% vs N% last period)" + top 3 class_signals | View Infra Jobs |
| maintenance_ratio > 0.20 | ℹ info | "Maintenance ratio above target (N%)" | View Infra Jobs |
| any bot has 0 productive sessions in range | ⚠ warning | "{bot}: no productive sessions this period" | switch to bot tab |
| correction_rate this period > 2× previous period | ⚠ warning | "Correction rate up {N}× vs last period — check recent sessions" | filter session browser to corrections=true |
| correction_rate > 0.15 | ℹ info | "Correction rate elevated ({N}%)" | — |
| any application correction_sessions / sessions > 0.30 | ℹ info | "{application} has high correction rate ({N}%)" | filter browser by application |
| efficiency_flag count > 5 | ℹ info | "{N} sessions flagged for efficiency" | filter browser to efficiency=true |
| any application with 0 corrections and ≥ 5 sessions | ✓ good | "{application} performing well — {N}% 1st response" | — |
| first_response_resolutions / sessions > 0.80 | ✓ good | "Strong first-response resolution ({N}%)" | — |
| no data in range | ℹ info | "No metrics yet — measure.py runs at 01:00 AM" | View Infra Jobs |

Insight generation is a pure client-side function over the already-fetched metrics data. No additional API calls.

**Class signals surface:** The `class_signals` field is in `turn_annotation` records, not in the daily metrics JSON. To surface the top maintenance signals cheaply, the session browser API endpoint (`/api/analytics/sessions`) should include an aggregate `top_signals` field in the response root when returning maintenance-classified sessions — the top 5 signals by frequency across the result set.

---

### Zone 3a — Application Chart

A horizontal bar chart below the main charts. One bar per application that appears in `application_usage` across the fetched date range. Bars are color-split by productive/maintenance/ambiguous proportion.

Data comes from the `application_usage` field already present in the daily metrics JSON — no new API call needed.

**Sort:** by total session count descending.  
**Hover tooltip:** sessions, correction_sessions, efficiency_sessions, unresolved_sessions for that application.

If no application data exists (first week), hide the section with a subtle empty state.

---

### Zone 3b — Session Browser

A paginated table of `session_summary` records, fetched from the new `/api/analytics/sessions` endpoint on page load (default: last 7 days, all classes, limit 25).

**Columns:**

| Column | Content |
|--------|---------|
| Date | MM-DD |
| Bot | bot_id pill |
| Class | colored badge: ● productive (green) / ● maintenance (red) / ● ambiguous (gray) |
| Turns | number, dim if 1 |
| Complexity | low / med / high badge |
| Corr | ✗ if correction_count > 0 |
| Flags | ⚡ if efficiency_flag |
| Applications | comma-joined tags, truncated |
| Outcome | outcome text, truncated to ~60 chars |

**Row expand:** clicking a row expands it inline to show:
- Full outcome text
- All applications with their class_signals
- Promises made (if any)
- Token counts and cost
- Session ID (truncated, copyable)

**Filter controls** (above table):
- Class dropdown: All / Productive / Maintenance / Ambiguous
- Bot dropdown (if multiple bots)
- Corrections checkbox: only sessions with correction_count > 0
- Efficiency checkbox: only sessions with efficiency_flag = true
- Changing any filter reloads from the API

**Pagination:** 25 rows per page, Prev/Next, "Showing N–M of total" label.

---

### Empty / Warming-Up States

| State | Display |
|-------|---------|
| `last_metric_date` is null (measure.py never run) | "Not configured" + "View Infra Jobs →" button |
| `last_metric_date` set but < 7 days of data | "Collecting data" (yellow) — no insights panel, no browser |
| 7+ days of data | Full UI as above |
| API returns no sessions for current filter | "No sessions match this filter" empty state in table |

---

### Dashboard Tile (Overview page)

The existing tile shows: Total Sessions (14d), Resolution Rate, Productive %. Update to show:

- Total Sessions (14d)
- Productive %
- Maintenance % (colored by threshold)
- Correction Rate (if > 10%, shown in yellow/red)

Attention level: `warning` if maintenance > 0.20 **or** correction_rate > 0.15. `critical` if maintenance > 0.35.

---

## Performance & Cost

| Component | Cost | Latency |
|-----------|------|---------|
| Keyword classifier | ~0 | < 1ms, in-memory |
| LLM tier classifier | ~$0.0001/session | async, doesn't block user |
| LLM outcome extraction | ~$0.0002/session | fires at session_end, after user interaction |
| annotation write | ~0 | 2× appendFileSync, < 2ms total in steady state (dirs cached) |
| measure.py (daily) | ~0 | runs at 01:00, no user impact |
| `/api/analytics/sessions` endpoint | ~0 | reads JSONL files, typically < 100ms |

**Memory:** per-session Maps capped at 500 entries; excess entries are evicted with a warning log. Session state is deleted on `session_end`.

**File I/O optimization:** `mkdirSync` and `chmodSync` are called once per directory per process lifetime via `_initializedDirs` Set. Subsequent turns use only `appendFileSync`.

---

## Open Questions

1. **Top maintenance signals in insights panel** — currently `class_signals` lives in `turn_annotation` records, not in daily metrics JSON. Options:
   - (a) Add a `top_maintenance_signals` aggregation to `measure.py` output — cleaner, requires measure.py change
   - (b) Surface via new sessions endpoint aggregate field — works today but requires an extra API call
   - Recommendation: (a), add to measure.py in the next analytics pass

2. **Correction Rate stat block denominator** — using `turn_count` means a 1-turn session with a correction has a 100% correction rate. Using `session_count` (correction_sessions / total) is more stable. Recommend: `correction_count / session_count` (corrections per session, not per turn) labeled as "Corrections/Session" rather than "Correction Rate."

3. **Session browser date range** — default is 7d (annotation files are available immediately). The 30d and 90d time range tabs apply to the charts and stat blocks but the session browser should independently default to 7d with its own range control. Keep them separate or link them?
   - Recommendation: separate — charts need longer history, browser is for recent investigation.

4. **`efficiency_flag` count in stat blocks** — efficiency data lives in session_summary records (annotation files), not in daily metrics JSON. The Efficiency Issues stat block either needs:
   - (a) measure.py to add `efficiency_flag_count` to the daily metrics JSON, or
   - (b) a separate API call to count from annotation files
   - Recommendation: (a), add to measure.py. Until then, omit from stat blocks and show only in session browser.

---

## Implementation Plan

### Phase 1 — Stat blocks + Insights panel (client-side only)

Changes: `index.html` only. No new API endpoints.

- Replace 4 stat blocks with 6 (add Correction Rate using `correction_count` from metrics, remove "Days with Data")
- Add ▲▼ period-over-period deltas
- Add Insights panel with client-side observation rules
- Update dashboard tile logic

### Phase 2 — Session Browser

Changes: `server.py` (new endpoint), `index.html` (browser UI).

- Implement `/api/analytics/sessions` endpoint
- Add session browser table with row expand
- Add filter controls
- Wire to correct empty states

### Phase 3 — Application Chart

Changes: `index.html` only. Data already in metrics JSON.

- Horizontal bar chart for application breakdown
- Hover tooltips
- Sort by session count

### Phase 4 — measure.py additions (for Phase 1 completion)

Changes: `measure.py`.

- Add `top_maintenance_signals` aggregation (top 5 class_signals across maintenance turn_annotations)
- Add `efficiency_flag_count` to daily metrics output
- These unlock the full Insights panel and Efficiency Issues stat block

---

## Help Doc Updates (docs/help/monitoring.md)

Update to reflect:
- New stat blocks (Correction Rate, 1st Response Rate, Efficiency Issues)
- Insights panel — how observations are generated
- Session Browser — what it shows and how to use filters
- Application chart — what applications are and where they come from
- All existing content to be kept, updated where needed
