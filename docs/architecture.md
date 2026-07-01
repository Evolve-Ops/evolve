# Evolve — Architecture

## Overview

Evolve is the **Better Engine** for OpenClaw pods — a bounded pod-wide adaptation system. It measures bot behavior, detects patterns, proposes improvements, and applies them autonomously — all with mandatory human approval gates and rollback safety. RSI (recursive self-improvement) is the optimizer-style component inside Better Engine; operations, security, and cost sit in the same engine.

See [docs/local-deployment-architecture.md](local-deployment-architecture.md) for the ownership model, account structure, and file paths specific to a local macOS deployment.

```
┌─────────────────────────────────────────────────────────────────┐
│  PLUGIN (TypeScript, in-process, on every bot)                  │
│  TurnObserver → TierClassifier → MetricsCollector               │
│  before_model_resolve hook → routes maintenance sessions        │
│    to tier3 (Haiku) automatically                               │
│  Writes annotations to /Users/Shared/evolve/annotations/        │
│  HTTP routes: /evolve/status, /evolve/metrics                   │
│  Dashboard: /evolve/ (primary bot only)                         │
└───────────────────────────┬─────────────────────────────────────┘
                            │ JSONL annotations + turn events
┌───────────────────────────▼─────────────────────────────────────┐
│  ANALYZER (Python, scheduled via launchd)                       │
│  measure.py           — daily metrics per bot (ALL bots, 01:00) │
│  better_engine_refresh.py — 15-min tick; runs generator_runner  │
│  generator_runner.py  — runs ~26 coaches whose cadence is due   │
│  heal.py              — gateway health check + restart (5min)   │
│  cost.py              — cost/model anomaly detection (daily)    │
│  review.py            — security gate for proposals             │
│  apply.py             — apply approved proposals (5min)         │
│  spend_alert.py       — hourly spend threshold check            │
│  cron_alert.py        — hourly cron silence detection           │
│  analyze.py           — legacy weekly detectors (still active)  │
│  community_intel.py   — weekly external Kaizen scan (optional)  │
└─────────────────────────────────────────────────────────────────┘
```

**Note on Forge/Sandbox:** The Forge validation step (isolated OpenClaw instance running `validate.py`) was removed in April 2026. The security gate (`review.py`) is sufficient for the current proposal risk level. Forge will be revisited if proposals begin including higher-risk changes.

---

## The Proposal Pipeline (Arbiter Model)

Every change to any bot travels through the arbiter. No shortcuts.

```
Generators (around two dozen coaches, run by generator_runner on a 15-min tick)
  → observe() emits a Proposal + optional motivating Signals
  → proposal written to proposals/pending/

Monitors (pod_report, audit, host_health, …) write Signals separately
  → signals/firing/{id}.json (signal-subscriber daemon dispatches
    subscribed generators within seconds)

Routing decision (arbiter.routing):
  → reversible + bounded blast radius → approved_auto (autonomous path)
  → otherwise → pending, awaiting human approval in admin UI

Human approval (admin UI, proposals/pending/ for approved_auto skips this):
  → reject → archived/
  → snooze → snoozed/ (wakes automatically)
  → approve → approved_human (stays in pending/)

apply.py (every 5 min, on TARGET BOT as that bot's user):
  → sees approved_auto or approved_human proposal
  → applies change within own user context (no sudo)
  → health check after config changes
  → FAIL → rollback + archive as failed_reverted or failed_flagged
  → PASS → proposal moves to applied/ (verify daemon owns it next)

Verify daemon (post-apply claim check):
  → succeeded → archived/
  → failed   → archived/ as failed_* + alert
```

Proposal statuses and their subdirs (authoritative in `arbiter.store`):
- **`pending/`** — draft, pending, approved_auto, approved_human, dispatched
- **`snoozed/`** — deferred until `snoozed_until`
- **`applied/`** — applier ran; verify daemon owns these next
- **`archived/`** — terminal: succeeded / failed_* / rejected / dismissed / superseded

---

## Role Assignments

Each bot in the pod has one or more roles. Roles are additive.

| Role | Key scripts | launchd jobs |
|------|-------------|--------------|
| **member** (all bots) | measure.py, apply.py, heal.py, evolve_config.py, models.py, spend_alert.py, cron_alert.py | measure daily, apply every 5min, spend+cron alert hourly |
| **primary** (one bot) | + better_engine_refresh.py, analyze.py, cost.py, outcome.py, slack_signals.py, expansion.py | + better-engine 15min tick, analyze Sunday, cost daily, admin-ui keepalive |
| **security** | + review.py | + review every 2min |

A single bot can hold multiple roles. Typical small network:
- `primary-bot` = primary + security + member
- `member-bot-1`, `member-bot-2` = member

With a dedicated security bot:
- `primary-bot` = primary + member
- `security-bot` = security only
- Others = member

---

## The Shared Directory Contract

`/Users/Shared/evolve/` is the integration surface between all bots.

```
/Users/Shared/evolve/
  network.json          ← canonical config (single source of truth)
  security_rules.json   ← security reviewer rules (never modified by proposals)
  annotations/
    {bot_id}/           ← JSONL turn annotations (written by plugin)
  metrics/
    {date}/
      {bot_id}.json     ← daily metrics (written by measure.py)
  cost/
    {date}.json         ← daily cost summary
    tier-usage/
      {bot_id}/
        {date}.jsonl    ← tier usage log (tier0-3 call counts)
  incidents/
    {date}/
      {bot_id}-{ts}-{type}.json  ← gateway incidents (heal.py)
  proposals/
    pending/            ← draft / pending / approved_* / dispatched (awaiting apply)
    snoozed/            ← deferred until snoozed_until
    applied/            ← applier ran; verify daemon owns these next
    archived/           ← terminal: succeeded / failed_* / rejected / dismissed
  signals/
    firing/             ← actively firing signals
    snoozed/            ← snoozed signals
    archived/           ← resolved / dismissed signals (90-day retention)
  generators/           ← per-generator GeneratorRecord (track record, config)
  profiles/             ← per-bot YAML+Markdown profiles (weights + body)
  test-results/
    {bot_id}/
      {application_id}/
        {ts}-{mode}.json  ← test run results
        latest.json       ← symlink to most recent
  applications/
    {bot_id}/
      {application_id}.json   ← approved application manifests
      _history/              ← archived previous manifest versions
  keystore/
    keys.json           ← key registry (names, scopes, sync timestamps)
    sync-log.json       ← {bot_id: last_synced_at}
    vault/              ← encrypted key values (machine-key XOR)
```

**Permissions:** `1777` (sticky bit) — each bot writes its own files, cannot delete or overwrite others.

**Atomic writes:** All files written via `.tmp` + `rename()` to prevent partial reads.

**Schema versioning:** Every JSON file includes `schema_version` for migration safety.

**network.json is canonical:** Scripts read config from `/Users/Shared/evolve/network.json` first. Local copies in bot workspaces are legacy fallbacks only and should not be relied on.

---

## Bot Autonomy Model

The key architectural insight: **each bot applies its own changes.**

```
primary-bot (primary + security)          member-bot (member)
  generator_runner runs coaches        apply.py polls proposals/pending/
  → proposal written to pending/       → sees approved proposal for member-bot
  → review.py screens it              → applies within /Users/<bot-id>/ context
  → routing: auto-eligible?                → no sudo needed
      yes → approved_auto             → health check + rollback if broken
      no  → human approves in UI      → proposal moves to applied/
  → apply.py on target bot applies it      → verify daemon checks claim
```

The autonomy gate (`arbiter.routing.is_autonomous_eligible`) requires all three:
- `reversibility == "auto"` (applier can undo via paired RevertPlan)
- `blast_radius ∈ {local, bot}` (no platform-wide effect)
- `touches` disjoint from irreversibility surfaces

There are no cross-user writes during normal operation. The shared directory is the message bus. pod-admin-user (root) is only involved in initial setup and exceptional interventions.

---

## Model Tier System

Code never references a model by name. It references a tier. The tier system lives in `network.json` and `models.py`.

| Tier | Role | Policy | Default |
|------|------|--------|---------|
| **tier0** | Cross-model judge | Must differ from tier2 provider (anti-Goodhart) | openai/gpt-4o |
| **tier1** | Power | Explicit user request only. Daily limit enforced. | claude-opus-4-6 |
| **tier2** | Workhorse | Default for user-facing conversations | claude-sonnet-4-6 |
| **tier3** | Grunt | Background tasks: analysis, testing, judging | claude-haiku-4-5 |

Each tier has primary models + fallbacks. Changing a model means one line in `network.json` via `evolve-admin models set` — no code changes anywhere.

---

## Application Manifests

An application manifest is the contract between "what a bot should do" and "how to verify it's doing that." Every application a bot performs should have a manifest before being built.

```
scanner.py detects applications from workspace files
reviewer.py runs interactive review:
  - Satisfaction score (1-5) → RSI measurement target
  - Known issues → negative test cases
  - Desired improvements → future proposals
  - Goals, tests, privacy constraints
manifest saved to shared_dir/applications/{bot_id}/{cap_id}.json
```

Compliance checks run via `better_engine_refresh.py`'s compliance scan step (driven by `application_scanner`) and feed Signals that the `manifest_quality` / `workspace_inventory` / `workspace_security` generators consume.

---

## Security Model

### The security reviewer (review.py)

All non-autonomous proposals pass through `review.py`. The reviewer's mandate is `security_rules.json` — a static file that proposals can never modify (protected by `no_self_modification` rule).

Eight auto-reject rules:
- No 0.0.0.0 gateway binding
- No auth disable
- No modification of evolve's own scripts
- No credential/auth file writes
- No `sudo` in proposed scripts
- No outbound network calls in proposed scripts
- No writes outside bot's own workspace
- No launchd plist modifications

Four auto-flag rules (pass to human with warning): model changes, cost threshold changes, channel config changes, low-confidence proposals.

**Dedicated security bot (optional):** Set `security.mode = "dedicated"` and `security.botId = "security-bot"` to run `review.py` on a separate bot. Provides isolation: if the primary is compromised, the security reviewer is unaffected. The same `review.py` code runs either way.

### The apply.py safety gates

1. Only applies proposals targeting this bot (bot_id match)
2. Requires `approved_auto` or `approved_human` status
3. Backs up config/files before applying
4. Health-checks gateway after config changes
5. Auto-rollback if health check fails
6. All results logged + Telegram alert; proposal moved to `applied/`

---

## Key Design Decisions

### Session classification (productive vs maintenance)

Every session is classified as:
- **productive**: Objective work — advances human goals
- **maintenance**: Fixes problems with the bot system itself
- **ambiguous**: Mixed or unclear

These are session labels, not model roles. A role (`fast`/`standard`/`power`/`max`/`judge`) is a separate concept describing compute/cost class. See `docs/model-roles.md`.

A `maintenance` session is not bad. It's a signal that something caused it to be necessary. The session_quality generator (and the legacy `analyze.py` detectors) target the root cause, not the session itself. `maintenance` sessions are also routed to tier3 (Haiku) by default, since debugging config errors doesn't require Sonnet-class reasoning.

### Goodhart's Law mitigation

Multiple diverse metrics. Cross-model judge (tier0 ≠ tier2 provider). No single optimization target. Rejection reasons (fixed vocabulary) feed back into the analysis engine via `feedback/rejections.jsonl`.

### Human approval for non-autonomous proposals

For proposals that don't qualify for the autonomous path, two gates apply before any production change:
1. **Security review** (automated, `review.py`) — auto-rejects dangerous proposals via `security_rules.json`
2. **Human approval** (Pod-admin, via admin UI) — explicit approve or reject required

Proposals on the autonomous path skip human approval but require a `RevertPlan` and pass a claim-based verify check post-apply. If the verify check fails, the change is automatically rolled back and the proposal is archived as `failed_reverted`. No irreversibility-surface touch can use the autonomous path.

---

## See also

- `docs/getting-started.md` — Installation and first run
- `docs/operator-runbook.md` — Diagnostics and recovery for production issues
- `docs/scripts.md` — Every analyzer script, what it does, when it runs
- `docs/configuration.md` — Full network.json reference
- `docs/model-roles.md` — Model rungs & roles, session routing, changing models
- `docs/applications.md` — Application manifests, priorities, writing good tests
- `docs/feedback-loop.md` — End-to-end: how sessions become improvements
- `docs/continuity-engine.md` — Task extraction, queue, inline/agent execution
- `docs/roadmap.md` — Product roadmap and release strategy
- `packages/admin/README.md` — evolve-admin CLI reference

---

## Architecture Principle: OC CLI for Live State; Direct ACL Reads for Persisted Config

*Added 2026-04-06. Amended 2026-05-31 — the original "never read `.openclaw/` directly" framing was written before `set_evolve_read_acl()` granted the `evolve` user cross-bot ACL read access, which removed the permission constraint that motivated it. The amended rule splits OC state into two layers and reaches each through the right channel.*

### The Rule

OpenClaw exposes two distinct kinds of state, and Evolve reaches each differently:

- **Live state** — gateway up, current sessions, heartbeat, security-audit results, cron run history. These don't sit in a single file; OC computes them on demand from runtime data. Call `openclaw <cmd> --json` via `oc_cli.py`. The file alone can't answer "is this true right now."
- **Persisted config** — `openclaw.json`, `auth-profiles.json`, `exec-approvals.json`, `workspace/` files, POD_CONDUCT.md. These are user-facing schema, atomically written, and ARE the source of truth (the CLI just reads the same file). Read directly from `/Users/<bot>/.openclaw/...` using the macOS ACL granted by `set_evolve_read_acl()` in `deploy.py`. Fall back to `sudo /bin/cat` for bots not yet ACL'd. See `CLAUDE.md` for the canonical read/write patterns.
- **Writes to bot-owned files** — only OC writes its own files through its own CLI when possible. When Evolve must author config (setup wizard installing initial state, applier patching a setting), use the `/tmp` staging + `sudo /bin/cp` pattern documented in CLAUDE.md.

```
Evolve UI / admin daemon
    │
    ├── reads /Users/Shared/evolve/             ← Evolve's own data (metrics, proposals, signals, applications)
    ├── reads /Users/<bot>/.openclaw/*.json     ← persisted config layer, via ACL
    ├── calls openclaw <cmd> --json             ← live-state layer, via oc_cli.py
    └── writes via /tmp + sudo /bin/cp          ← when authoring bot-owned config
```

### Why

The split exists because the two layers answer different questions:

1. **Live state requires the runtime.** Heartbeats, in-flight sessions, gateway health, security-audit results aren't sitting in a single file — OC computes them on demand from process state. Asking the CLI is the only correct answer for "what is true right now."
2. **Config IS the file.** `openclaw.json` and friends are stable user-facing schema, written atomically by OC and Evolve. Reading them directly is faster, doesn't shell out, doesn't require OC to be running, and works inside daemons that can't easily sudo. The file is authoritative; the CLI just reads it.
3. **Cross-user permission was the original blocker — it isn't anymore.** Before `set_evolve_read_acl()` existed, `evolve` couldn't read `/Users/<bot>/.openclaw/` and the only way across was `sudo -u <bot> openclaw <cmd>`. That constraint motivated the original "never read directly" framing. The ACL has since replaced sudo for config reads, and the code (across `config.py`, `wizard_verify.py`, `deploy.py`, `recovery.py`, `ocadmin.py`, `safety_summary.py`, `health.py`) followed.
4. **Separation of concerns.** OC owns per-bot state; Evolve owns pod aggregation and intelligence. That holds regardless of which channel is used to read.

### Pod-wide shared dir

`/Users/Shared/evolve/` is Evolve's own data directory — metrics, proposals, signals, annotations, status files, applications. Evolve reads and writes this freely. It is not OC state.

### OC CLI commands for live-state queries

| Purpose | OC Command |
|---|---|
| Bot liveness | `openclaw status --json` |
| Gateway health | `openclaw health --json` |
| Cron jobs (live status + run history) | `openclaw cron list --json` + `openclaw cron runs --json` |
| Security audit | `openclaw security audit --json` |
| Session list (current) | `openclaw sessions --json` |
| Heartbeat | `openclaw system heartbeat last --json` |

For `models list`, `channels list`, and `approvals get`, the CLI is correct but so is reading the corresponding config file directly under ACL — they return the same data from the same source. Prefer whichever is simpler at the call site; reach for the file when you need it without OC running.

### Cross-Bot Invocation Pattern

All cross-bot OC CLI calls go through `oc_cli.py`:

```python
from oc_cli import oc_command

# Runs: sudo -u team-bot-a openclaw status --json
result = oc_command("team-bot-a", ["status", "--json"])

# Runs: sudo -u admin-bot openclaw models list --json  
result = oc_command("admin-bot", ["models", "list", "--json"])
```

`oc_cli.py` handles: user mapping from network.json, sudo invocation, JSON parsing, timeout, error handling, and caching (configurable TTL to avoid hammering the CLI on every page load).

### What Evolve Builds That OC Does Not

The **Improve** layer is entirely Evolve-built — OC has no equivalent:

- Session quality metrics (productive/maintenance ratio, resolution rate)
- Pod-wide health scoring and trend tracking
- Proposal generation, security gating, and pipeline
- Outcome tracking (did this change help?)
- Classifier accuracy auditing
- Application manifests and test framework
- Continuity Engine (task queue, idle execution)
- Proactive expansion suggestions

In the **Operate** layer, Evolve adds pod aggregation on top of OC's per-bot tools:

- "Show me all bots' cron jobs at once" — OC can only show one bot at a time
- "Which bot's API key is expiring?" — OC has no cross-bot key health view
- "Is any bot's channel disconnected?" — OC checks per-bot; Evolve aggregates

### What This Means for the UI

UI sections (post-v2.2 IA) that are primarily OC wrappers (thin aggregation layer):
- Usage → Sessions (session classification)
- AI Optimization → Models/Config
- Maintenance → Cron Tracker
- Plugins → Channels (Messaging section)
- Security → Audit

UI sections that are entirely Evolve (no OC equivalent):
- Cost Optimization
- Continuity Engine
- Apps (manifests, gallery, forge)
- Recommendations (Better Engine proposals)
- Pod health scoring and analytics
