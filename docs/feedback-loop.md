# Evolve — The Feedback Loop

How Evolve actually improves bots over time. This document traces the full path from a session to an improvement being applied and verified.

---

## The loop at a glance

```
Session happens
    │
    ▼ (session end, in-process)
TurnObserver annotates every turn
SessionSummarizer generates session summary
    │
    ▼ (daily, 01:00)
measure.py aggregates turn annotations → daily metrics
    │
    ▼ (15-min better_engine_refresh tick + signal-subscriber dispatch;
       analyze.py's legacy detectors still run Sunday 02:00)
generators read metrics + summaries + Signals → proposals
    │
    ▼ (at proposal creation)
arbiter routing: autonomous-eligible? → approved_auto, else pending
    │
    ▼ (human action, non-autonomous proposals only)
Pod-admin approves or rejects in admin UI
    │
    ▼ (in-process, on approval)
admin server validates the proposal → validation result
    │
    ▼ (every 5 min, on target bot)
apply.py applies change → backup → health check → rollback if needed
    │
    ▼ (every 5 min)
verify daemon checks the proposal's claim → succeeded / failed_*
    │
    ▼ (7 days later)
outcome.py asks the admin: "did this help?" → outcomes.jsonl
    │
    ▼ (feeds back into)
generator track records + detector/classifier calibration
```

---

## What gets measured

Every bot turn produces an annotation. Every annotation feeds metrics. Here's what's tracked:

### Per-turn (annotation JSONL)

| Field | What it captures |
|-------|-----------------|
| `session_class` | `productive` or `maintenance` — what kind of work |
| `class_confidence` | 0-1 confidence in session class |
| `model_tier` | Which tier was selected for routing |
| `model_selected` | Actual model string used |
| `correction_detected` | Did the human correct the bot this turn? |
| `task_complete` | Did this turn complete the current task? |
| `input_tokens` / `output_tokens` | Token counts |
| `auth_mode` | `token` (MAX, $0) or `api_key` (costs money) |
| `cost_estimated` | Estimated dollar cost |

### Per-session (session summary)

| Field | What it captures |
|-------|-----------------|
| `outcome` | LLM-extracted description of what was accomplished |
| `complexity` | `low` / `medium` / `high` |
| `applications_invoked` | Which application domains appeared in this session |
| `promises_made` | Deferred commitments the bot made |
| `correction_count` | Total corrections this session |
| `efficiency_flag` | True if turn count was disproportionate to complexity |
| `session_class` | Dominant class across all turns |

### Per-day (daily metrics)

```json
{
  "date": "2026-04-05",
  "bot_id": "<bot-id>",
  "session_count": 8,
  "productive_sessions": 6,
  "maintenance_sessions": 2,
  "maintenance_ratio": 0.25,
  "tier1_session_count": 1,
  "avg_resolution_rate": 0.83,
  "auth_health": 1.0,
  "application_usage": {
    "health-tracking": {
      "sessions": 3,
      "unresolved": 0,
      "corrections": 1,
      "efficiency_flags": 0,
      "promises": 2
    }
  }
}
```

---

## The detectors

`analyze.py` runs its detector suite on Sunday at 02:00 (see `analyze.py` for the current set). Each detector reads metrics and session summaries, looking for patterns.

### Core detectors (always have data after week 1)

**`detect_high_maintenance_ratio`**
- Signal: maintenance_ratio > 0.30 over 7+ days
- What it means: >30% of sessions are bot upkeep, not user value
- What it generates: investigation proposal to find root cause

**`detect_api_key_fallback`**
- Signal: `auth_health` < 1.0 (any paid API calls)
- What it means: MAX subscription failing; tokens costing real money
- What it generates: immediate Telegram alert + config_change proposal to fix auth order

**`detect_declining_resolution_rate`**
- Signal: resolution rate declining over 14+ days, now < 0.70
- What it means: bot is completing fewer tasks per session over time
- What it generates: investigation proposal

**`detect_zero_activity`**
- Signal: no metrics files for 3+ consecutive days
- What it means: bot may be down, disconnected, or heal.py isn't running
- What it generates: immediate Telegram alert

### Session-summary detectors (need 2+ weeks of data)

**`detect_low_satisfaction_application`**
- Signal: satisfaction_score ≤ 3 in manifest AND session summaries show corrections/failures for that application's domain
- What it generates: improvement proposal with specific evidence (correction count, efficiency flags, outcomes, known issues from manifest)
- This is the key closed-loop detector — manifests and session data talk to each other

**`detect_promise_breach`**
- Signal: >40% of sessions have promises_made with unresolved outcomes
- What it means: bot is making commitments it isn't keeping
- What it generates: workflow_change proposal + Continuity Engine suggestion

**`detect_efficiency_problems`**
- Signal: >25% of sessions efficiency-flagged for a specific application
- What it means: too many turns for the complexity involved in this domain
- What it generates: investigation proposal with application breakdown

**`detect_application_abandonment`**
- Signal: application used in <20% of sessions compared to 30 days prior, with no corresponding improvement in outcomes
- What it means: a feature is being used less — could be fixed, could be unnecessary
- What it generates: investigation proposal

**`detect_promise_resolution`**
- Signal: ≥1.5 promises/session average for an application
- What it means: the bot keeps deferring work in this domain — a task queue would help
- What it generates: investigation + Continuity Engine setup suggestion

### Multi-user detectors (Team-bot-a only — requires Slack signals)

**`detect_slack_quality_drop`**
- Signal: average reaction quality < 0.45 over ≥3 signals for an application
- What it means: the team is reacting negatively to Team-bot-a's messages in this area
- What it generates: investigation proposal with reaction breakdown

### Meta-detectors

**`detect_detector_staleness`**
- Signal: any detector with ≥80% rejection rate over ≥5 proposals
- What it means: a detector is generating proposals Pod-admin keeps rejecting — it's miscalibrated
- What it generates: investigation proposal to recalibrate or retire that detector

---

## The proposal pipeline

Every improvement travels through five stages. None can be skipped.

### Stage 1: Generation

Any generator (run by `generator_runner` on the 15-minute `better_engine_refresh` tick, or dispatched by the signal-subscriber daemon when a subscribed Signal fires) or legacy `analyze.py` detector writes a proposal to `proposals/pending/`. Each proposal has:
- A unique `pattern_key` — prevents duplicate proposals for the same issue
- Confidence score (0-1)
- Risk level (`low`, `medium`, `high`, `critical`)
- Proposal type (`config_change`, `script_change`, `workflow_change`, `investigation`)

### Stage 2: Routing (arbiter)

Every proposal is routed by the arbiter when it is written. The autonomy gate (`arbiter.routing.is_autonomous_eligible`) requires ALL of:
- `reversibility == "auto"` — the applier can undo it via the paired RevertPlan
- `blast_radius ∈ {local, bot}` — no platform-wide effect
- `touches` disjoint from the irreversibility surfaces (`auth`, `tools`, `channel_config`, `gateway_core`, `app_install`, `app_removal`, `bot_specialization`) — this is what keeps auth, channel, and gateway-core changes off the autonomous path
- a verifiable claim and an attached revert plan
- not an autonomy-ladder promotion (upward autonomy changes are always human-gated)
- a clean pass through the folded security screen (`arbiter/security_screen.py`) — deny-rule hits or a missing AST layer fail closed to human review

Eligible proposals become `approved_auto` and skip Stage 3 — they still pass validation, apply-time health checks, and the post-apply verify daemon. Everything else stays `pending` for human approval.

> The legacy standing security reviewer (`review.py` on a 2-minute schedule, driven by `security_rules.json`) was retired and **executed** 2026-08-14 (#3641): both files are deleted; the deny mandate is folded into the autonomy gate as `arbiter/security_screen.py`, where deny-rule hits and a missing AST layer both fail closed to human review.

### Stage 3: Human approval

`pending` proposals appear in the admin UI (and as notifications). Pod-admin reviews each one and approves, snoozes, or rejects. This is a mandatory gate for every proposal that failed the autonomy gate — nothing auto-promotes out of `pending`. Approved proposals become `approved_human`; rejected ones are archived with the rejection reason.

### Stage 4: Validation

The admin server validates every approved proposal in-process and writes a result to `proposals/validation-results/`. For `config_change` proposals: dry-run the patch against a copy of the target's gateway config and run schema/safety checks. For `script_change`: syntax and safety checks. For `investigation`: pass-through.

Validation result: `pass` / `fail` / `needs-human` / `error`. `apply.py` reads the validation result before applying — non-`pass` results are skipped (investigation and workflow_change proposals bypass the check).

### Stage 5: Application

`apply.py` on the target bot applies the change:
1. Backs up current config/file (1-hour retention)
2. Applies the patch
3. Restarts gateway (for config_change)
4. Waits 3 seconds
5. Health check (GET /evolve/status)
6. If unhealthy: rollback + restart + post-rollback health check
7. If post-rollback also unhealthy: `rollback_unhealthy` status — manual intervention required, backup preserved
8. Logs result, sends Telegram summary

---

## Outcome tracking

The loop isn't closed until we know whether a proposal actually helped.

### How it works

When `apply.py` successfully applies a proposal, it calls `_register_outcome()` which writes a pending outcome record to `feedback/outcomes.jsonl`:
```json
{
  "proposal_id": "prop-...",
  "target_bot": "<bot-id>",
  "pattern_key": "<bot-id>:high_maintenance_ratio",
  "applied_at": "2026-04-05T14:00:00Z",
  "check_at": "2026-04-12T14:00:00Z",
  "status": "pending"
}
```

### The 7-day check-in

`outcome.py` runs daily at 09:00. It finds any pending outcomes where `check_at` has passed and sends a Telegram message:

```
📊 Outcome check: admin-bot — high maintenance ratio

Applied 7 days ago: Increased context window to reduce re-orientation turns.

Did this help? Your answer helps Evolve calibrate future proposals.

👍 Yes  👎 No  🤷 Can't tell
```

### How to respond

Reply to the check-in with:
- `Y` or `yes` or 👍 — it helped
- `N` or `no` or 👎 — it didn't help
- `?` or 🤷 — unclear / too soon to tell

The outcome is recorded in `feedback/outcomes.jsonl`:
```json
{
  "proposal_id": "prop-...",
  "status": "resolved",
  "outcome": "positive",
  "responded_at": "2026-04-12T09:30:00Z"
}
```

### What outcomes feed

Outcomes feed back into:
- `analyze.py` threshold calibration — if proposals for a pattern consistently get `negative` outcomes, the detector's threshold is too sensitive
- `detect_detector_staleness()` — alongside rejection history, negative outcomes signal a miscalibrated detector

---

## The expansion engine

Monthly (first Sunday, 04:00), `expansion.py` looks for application gaps — things the bot does repeatedly that aren't in any manifest.

**How it works:**
1. Clusters session outcome text into recurring topics
2. Filters against known application IDs
3. Scores gaps by: session_count × (1 + correction_rate×0.5 + efficiency_rate×0.3)
4. Uses a tier3 LLM call to enrich raw topics into specific application suggestions
5. Generates `investigation` proposals for the top gaps

**What it generates:**
```
💡 Application gap detected: ranch-management

Seen in 8 sessions over 30 days. Pattern: ranch projects, irrigation, 
property maintenance. Correction rate: 25%. Efficiency-flagged: 37%.

Suggestion: Consider creating a 'ranch-management' application manifest
to formalize this recurring domain. Structured support could reduce
the correction rate and provide better context continuity.
```

**These are suggestions only.** Expansion never generates `config_change` or `script_change` proposals. Pod-admin decides whether to act on them.

---

## How to read the weekly report

> **Note:** `report.py` was removed; pod-level reporting now comes from
> `pod_report.py` and the admin UI. The sample below is the historical
> `report.py` format, kept to illustrate what the metrics mean.

```
📊 Evolve Weekly Report — 2026-04-05

Network: 3 bots, all healthy

admin-bot: ⭐85/100
  • 42 sessions (34 productive, 8 maintenance)
  • Maintenance ratio: 19% ✅ (target: <20%)
  • Resolution rate: 88% ✅
  • Auth: MAX (no API cost)
  • Application health: 4/5 passing

team-bot-a: ⭐78/100  
  • 67 sessions (58 productive, 9 maintenance)
  • Maintenance ratio: 13% ✅
  • Slack quality: 0.61 (good)
  • 2 application tests failing [feature priority]

team-bot-c: ⭐91/100
  • 12 sessions (11 productive)

Proposals: 2 pending approval, 0 in review, 1 being applied
```

A score of 100 means: zero maintenance, perfect resolution rate, all application tests passing, no cost anomalies, no incidents.

Scores below 60 → warning. Below 40 → critical alert.
