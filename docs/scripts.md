# Evolve — Script Reference

Every Python script in `packages/analyzer/`, what it does, who runs it, and when.

---

## Shared directory layout

All runtime data lives under the shared directory (`/Users/Shared/evolve/` by default):

```
{sharedDir}/
├── network.json                             # Network-wide config
├── {botId}/
│   ├── turns/
│   │   └── turns-YYYY-MM-DD.jsonl          # Written by plugin (TurnObserver)
│   └── tiers.json                          # Model routing config (admin UI)
├── annotations/
│   └── {botId}/
│       └── YYYY-MM-DD.jsonl                # Written by plugin (turn_annotation + session_summary)
├── metrics/
│   └── YYYY-MM-DD/
│       └── {botId}.json                    # Written by measure.py
├── calibration/
│   ├── classifier.json                     # TierClassifier keyword overrides
│   ├── measure.json                        # measure.py threshold overrides
│   └── outcomes.json                       # outcome.py check-in timing
├── feedback/
│   ├── pending-outcomes.jsonl              # Proposals awaiting 7-day outcome check
│   ├── outcomes.jsonl                      # Completed outcome feedback
│   └── rejections.jsonl                    # Proposal rejection history
├── proposals/
│   ├── pending/                            # {proposal_id}.json — awaiting review
│   ├── approved/                           # Approved, not yet applied
│   ├── apply-results/                      # apply.py success/failure records
│   ├── validation-results/                 # validate.py results
│   └── rejected/                           # Rejected proposals
├── reviews/
│   └── YYYY-MM-DD.md                       # weekly_review.py RSI health reports
├── scoreboard/
│   └── network-YYYY-MM-DD.json             # scoreboard.py output
├── status/
│   └── {botId}.json                        # Gateway liveness (heal.py)
└── kaizen/
    └── *.md                                # Weekly kaizen scan results
```

---

## Script execution model

```
root (via evolve-admin deploy)
  → copies scripts to /Users/{bot}/.openclaw/workspace/evolve/
  → installs plist files in /Library/LaunchDaemons/

/Users/{bot} (bot's own user) — measure.py, apply.py, heal.py
  launchd runs these as each bot's own user
  reads/writes sharedDir/ (1777 sticky — cross-user OK)
  reads/writes /Users/{bot}/ (own workspace — no sudo)
  NEVER writes to other bots' home directories

evolve user — all other analyzer scripts
  analyze.py, outcome.py, weekly_review.py,
  scoreboard.py, expansion.py, task_runner.py, slack_signals.py, spend_alert.py,
  cron_alert.py, pod_report.py, backup.py
  runs as the 'evolve' system user via launchd
  reads bot data via ACL grants and sudo /bin/cat fallback
```

---

## The plugin (TurnObserver) — written in TypeScript, not Python

**File:** `packages/plugin/src/observer/TurnObserver.ts`  
**Runs as:** each bot's gateway process (same user as the bot)  
**Triggered by:** openclaw hook system on every agent turn

The plugin is the entry point for all usage data. It writes two files per bot:

1. **Annotation file** — `{sharedDir}/annotations/{botId}/{YYYY-MM-DD}.jsonl`  
   Contains `turn_annotation` and `session_summary` records. Used by `measure.py` and the session browser.

2. **Turns file** — `{sharedDir}/{botId}/turns/turns-YYYY-MM-DD.jsonl`  
   Contains one record per turn with raw token counts, model, channel, source. Used by `usage_analytics.py` and the Usage & Cost page.

**Hooks registered:**

| Hook | Action |
|------|--------|
| `session_start` | Injects pod conduct + pending tasks into system prompt via `session_surface.py` |
| `llm_output` | Accumulates token counts + model metadata into `sessionLlmData` Map for this session |
| `agent_end` | Main turn handler — polls up to 500ms (50ms intervals) for `llm_output` data, classifies session, writes both files |
| `session_end` | Runs `SessionSummarizer` (writes session_summary), runs `TaskExtractor`, cleans up session Maps |
| `before_model_resolve` | `ModelRouter` can override model and auth profile based on session class |

**OC timing note:** openclaw fires `agent_end` before `llm_output` internally (observed ~13ms gap). The handler polls up to 500ms in 50ms increments; in practice data arrives within one or two polls. If not arrived after 500ms, tokens default to 0 for that turn (session-level totals in `session_summary` are always accurate).

**Config fields (injected into `openclaw.json` by `ensure_plugin_config()`):**

| Field | Purpose |
|-------|---------|
| `botId` | Which bot this is |
| `role` | `"primary"` or `"member"` — affects `dashboardEnabled` |
| `networkId` | Network identifier |
| `sharedDir` | Path to shared analytics directory |
| `classifierModel` | Model for LLM tier classification (default: `anthropic/claude-haiku-4-5`) |
| `reportingEnabled` | Whether to write to sharedDir |
| `dashboardEnabled` | Whether to include in admin UI dashboards |

**All bots run the plugin, including `evolve`.** The evolve bot was previously excluded by a design decision that turned out to be wrong — it has conversations and its turns should be tracked. Corrected as of 2026-04-15.

---

## Scripts deployed to ALL bots (runs as bot's own user)

### `measure.py`
**When:** Daily at 01:00 (launchd `ai.openclaw.evolve.measure.{botId}`)  
**Runs as:** each bot's own user  
**What:** Reads that bot's annotation JSONL for the target date. Groups records by `session_id`. For each session: majority-votes `session_class` from `turn_annotation` records, reads resolution from `session_summary`. Computes `maintenance_ratio`, `correction_count`, `application_usage`, and status (`ok/warning/critical`). Writes atomically via `.tmp` rename.  
**Inputs:** `{sharedDir}/annotations/{botId}/{date}.jsonl`  
**Outputs:** `{sharedDir}/metrics/{date}/{botId}.json`  
**Calibration:** `{sharedDir}/calibration/measure.json` (threshold overrides)

---

### `apply.py`
**When:** Every 5 minutes (launchd `ai.openclaw.evolve.apply.{botId}`)  
**Runs as:** each bot's own user  
**What:** Polls `proposals/approved/` for proposals targeting this bot. Requires a passing validation result before applying (except `investigation` type). Backs up config before applying. Health-checks after config changes. Auto-rollbacks on failure. Appends to `feedback/pending-outcomes.jsonl` on success (for 7-day follow-up by `outcome.py`).  
**Inputs:** `proposals/approved/`, `proposals/validation-results/` (with read fallback to `proposals/forge-results/` on un-migrated pods)  
**Outputs:** `proposals/apply-results/{proposal_id}.json`, `feedback/pending-outcomes.jsonl`  
**Alert on:** failure, rollback

---

### `heal.py`
**When:** Every 5 minutes (launchd `ai.openclaw.evolve.heal.{botId}`, RunAtLoad=true)  
**Runs as:** primary bot's user  
**What:** HTTP health-checks all network members. On failure: records incident, attempts restart via `launchctl kickstart`, respects restart cooldown (default 10min). On 3+ failures in 24h: generates `investigation` proposal. Detects config drift. Writes liveness status to `{sharedDir}/status/{botId}.json`.  
**Inputs:** `network.json` members, each bot's gateway  
**Outputs:** `{sharedDir}/status/{botId}.json`, `proposals/pending/`  
**Alert on:** restart failure (immediate Telegram), recurring failures (proposal)

---

### `test_runner.py` — removed 2026-06-08
Ran weekly application tests from approved manifests. Removed with the
app-test surface (`decision-app-tests-2026-06-08.md`);
coverage moved to the Tier 2 structural audit and the coherence passes.

---

## Scripts deployed to the evolve user (infrastructure jobs)

These run as the `evolve` system user — they read across all bots and write to shared analytics directories.

### `analyze.py`
**When:** Weekly Sunday at 02:00 (launchd `ai.openclaw.evolve.analyze.evolve`)  
**What:** Reads 7–30 days of metrics for all bots. Runs detectors: zero activity, unexpected billing mode, high maintenance ratio, declining resolution rate, and others (see `analyze.py` for the current set). Deduplicates proposals by `pattern_key`. Calls `scoreboard.py` as part of its run.  
**Inputs:** `{sharedDir}/metrics/`  
**Outputs:** `proposals/pending/{id}.json`  
**Alert on:** new proposals generated

---

### `report.py` — removed
Generated the twice-daily network health report. Removed; pod-level reporting
now comes from `pod_report.py` (below) and the admin UI.

---

### `weekly_review.py`
**When:** Weekly Sunday at 03:00 (launchd `ai.evolve.evolve.weekly-review`)  
**What:** RSI (Recursive Self-Improvement) pipeline health report. Covers: proposal velocity (this week vs 4-week avg), 30-day funnel (generated→approved→applied→measured), rejection reasons, RSI Cycle Health Score (velocity 25%, approval rate 35%, measurement rate 25%, backlog freshness 15%). Synthesizes decisions + recommended actions via one Haiku LLM call. Sends via `openclaw message send`.  
**Inputs:** `proposals/{pending,approved,deployed,rejected,apply-results}/`, `{sharedDir}/kaizen/`  
**Outputs:** `{sharedDir}/reviews/{date}.md`, channel message

---

### `outcome.py`
**When:** Daily at 09:00 (launchd `ai.openclaw.evolve.outcome.evolve`)  
**What:** Post-apply outcome tracking. 7 days after a proposal is applied, sends a Telegram message asking the operator (👍/👎). Records responses to `outcomes.jsonl` for calibrating detector thresholds over time.  
**Inputs:** `{sharedDir}/feedback/pending-outcomes.jsonl`  
**Outputs:** `{sharedDir}/feedback/outcomes.jsonl`, channel message

---

### `scoreboard.py`
**When:** Called by `analyze.py` (not independently scheduled)  
**What:** Computes rolling 30-day health score per bot (0–100). Weights: maintenance ratio, resolution rate, auth health, data presence. Written to `scoreboard/` and read by the admin UI status endpoint.  
**Inputs:** `{sharedDir}/metrics/`  
**Outputs:** `{sharedDir}/scoreboard/network-{date}.json`

---

### `usage_analytics.py`
**Module, not a script.** Imported by `server.py` for the Usage & Cost page.  
Provides `load_turns(bot_id, days, ...)` and `compute_summary(turns)`. Reads from `{sharedDir}/{botId}/turns/turns-YYYY-MM-DD.jsonl` (primary), falls back to `{workspace}/memory/turns-YYYY-MM-DD.jsonl` (via sudo /bin/cat). Returns structured dicts for JSON serialization: `total_turns`, `total_cost`, `by_date`, `by_model`, `by_channel`, `by_source`, `billing`.

---

### `expansion.py`
**When:** Scheduled via launchd (runs as evolve user)  
**What:** Network expansion — monitors bot usage patterns and generates proposals to add new applications or bots based on observed gaps.  
**Inputs:** `{sharedDir}/metrics/`, `{sharedDir}/applications/`  
**Outputs:** `proposals/pending/`

---

### `slack_signals.py`
**When:** Scheduled via launchd (runs as evolve user)  
**What:** Monitors Slack for signals relevant to the pod (mentions, action items, calendar events). Posts summaries to bots via Telegram.  
**Inputs:** Slack API  
**Outputs:** channel messages

---

### `spend_alert.py`
**When:** Scheduled via launchd (runs as evolve user)  
**What:** Checks API spend across all bots against configured caps. Sends alert if any bot exceeds its daily or monthly limit. Also enforced at model-call time by `ModelRouter` in the plugin.  
**Inputs:** `{sharedDir}/{botId}/turns/` (recent), `{sharedDir}/calibration/`  
**Outputs:** Telegram alert

---

### `cron_alert.py`
**When:** Scheduled via launchd (runs as evolve user)  
**What:** Checks that all expected cron jobs ran recently. Alerts if any cron job has been silent for longer than its expected interval.  
**Inputs:** `{sharedDir}/status/`, launchd plist list  
**Outputs:** Telegram alert

---

### `pod_report.py`
**When:** Scheduled twice daily via launchd (runs as evolve user)  
**What:** Generates a structured daily pod snapshot including liveness, cost, security posture, task queue, and RSI health. Combines data from multiple sources into a single report.  
**Inputs:** `{sharedDir}/metrics/`, `{sharedDir}/status/`, `proposals/`  
**Outputs:** Telegram/channel message, `{sharedDir}/reports/`

---

### `backup.py`
**When:** Scheduled via launchd (runs as evolve user)  
**What:** Backs up bot workspace repos and config files to configured git remotes. Rotates old backups.  
**Inputs:** bot workspace directories, `network.json` backup config  
**Outputs:** git commits to backup repos

---

### `task_runner.py`
**When:** Scheduled via launchd (runs as evolve user)  
**What:** Polls `{sharedDir}/tasks/pending/` for tasks extracted by `task_extractor.py`. Dispatches tasks to appropriate bots via their gateways.  
**Inputs:** `{sharedDir}/tasks/pending/`  
**Outputs:** `{sharedDir}/tasks/completed/`, gateway API calls

---

## Helper modules (not standalone scripts)

### `evolve_config.py`
Resolves `network.json` from `--network` flag → `/Users/Shared/evolve/network.json` → local fallback. Provides `load_config()`, `get_shared_dir()`, `get_members()`, `get_primary()`, `get_alerts()`.

### `models.py`
Tier-based model registry. `resolve_tier("tier3", config)` → actual model string. Per-bot overrides, fallback chains, policy enforcement.

### `oc_cli.py`
Wrapper for running `openclaw` CLI commands as a bot user via `sudo -u {bot}`. Used by scripts that need to interact with a bot's gateway.

### `oc_model.py` / `oc_keys.py`
Read model configuration and API key presence from bot openclaw.json files. Called by the admin server via sudoers grants.

### `session_surface.py`
Called by TurnObserver's `session_start` hook. Injects pod conduct docs and pending tasks into the bot's system prompt for each new session.

### `task_extractor.py`
Called by TurnObserver's `session_end` hook. Scans session messages for action items and writes them to `{sharedDir}/tasks/pending/`.

### `cost_profiles.py`
Provides per-model cost tables for `cost_estimated` calculations in TurnObserver and `measure.py`.

### `calibration.py`
Loads calibration files from `{sharedDir}/calibration/` and provides merge helpers for overriding default thresholds.

---

## Launchd job naming conventions

| Pattern | User | Purpose |
|---------|------|---------|
| `ai.openclaw.evolve.measure.{botId}` | `{botId}` | Daily metrics computation per bot |
| `ai.openclaw.evolve.apply.{botId}` | `{botId}` | Proposal apply loop per bot |
| `ai.openclaw.evolve.heal.{botId}` | primary bot | Gateway health monitor |
| `ai.openclaw.evolve.analyze.evolve` | `evolve` | Weekly pattern detector |
| `ai.openclaw.evolve.report.evolve` | `evolve` | Daily health report |
| `ai.openclaw.evolve.outcome.evolve` | `evolve` | Post-apply outcome check-in |
| `ai.evolve.evolve.weekly-review` | `evolve` | Weekly RSI pipeline review |
| `ai.openclaw.{botId}-gateway` | `{botId}` | Bot OC gateway (system daemon) |
| `ai.evolve.evolve.admin-ui` | `evolve` | Admin web server |

All plist files are in `/Library/LaunchDaemons/`. Installed by `deploy.py` during `evolve-admin deploy` and `evolve-admin install-infra-jobs`.

---

## Adding a new detector to analyze.py

1. Write `detect_your_pattern(bot_id, history, ...) -> dict | None`
2. Return `None` if no issue; return a proposal dict if issue found
3. Follow the proposal schema (see `docs/configuration.md`)
4. Set a unique `pattern_key`: `"{bot_id}:your_detector_name"`
5. Add to the `detectors` list in `run_detectors()`
6. Deduplication is automatic via `pattern_key` check in `write_proposal()`
