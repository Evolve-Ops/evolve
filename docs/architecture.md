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
│  measure.py           — daily metrics, full fleet (01:00)       │
│  better_engine_refresh.py — 15-min tick; runs generator_runner  │
│  generator_runner.py  — runs ~26 coaches whose cadence is due   │
│  signal_subscriber_runner.py — long-running daemon; dispatches  │
│    subscribed generators seconds after a Signal fires           │
│  heal.py              — gateway health check + restart (5min)   │
│  verify/daemon.py     — post-apply claim check (5min)           │
│  spend_alert.py       — spend + burst threshold check (5min)    │
│  cron_alert.py        — hourly cron silence detection           │
│  analyze.py           — legacy weekly detectors (Sun 02:00)     │
└─────────────────────────────────────────────────────────────────┘
```

**Note on Forge/Sandbox and the legacy security gate:** The Forge validation step — a separate, isolated OpenClaw instance that ran `validate.py` — was removed in April 2026. The validation harness itself (`validate.py`) no longer ships to bots — it was dropped from `ANALYZER_SCRIPTS` in the 2026-08-14 retirement (#3641) and runs in-process on the admin host only, whenever a proposal is approved (`_validate_proposal_immediately()` in the admin server). The legacy standing security gate (`review.py`, driven by `security_rules.json`) was retired and **executed** 2026-08-14 (#3641; decided 2026-07-28): both files are deleted, and the deny mandate now lives in `arbiter/security_screen.py` as a fail-closed leg of `arbiter.routing.is_autonomous_eligible` — a proposal that trips a deny rule never applies autonomously, and a missing AST layer fails closed to human review. Proposal safety is that routing gate plus the human-approval, validation, and post-apply verify gates described below. `cost.py` and `community_intel.py` also still ship but have no scheduled job — they are manual/CLI tools today.

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

arbiter.apply (on the ADMIN HOST, as the evolve service user):
  → sees approved_auto or approved_human proposal
  → captures a snapshot, stores it as the proposal's revert plan
  → dispatches to the Applier registered for the action kind
  → FAIL → proposal stays approved_*; verify daemon owns the outcome
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

### Event-driven generator dispatch (signal-subscriber)

The `ai.evolve.evolve.signal-subscriber` LaunchDaemon (`signal_subscriber_runner.py`, long-running KeepAlive under the `evolve` user, installed by `install-infra-jobs`) watches `{shared_dir}/signals/firing/` at 1 Hz. When a Signal lands whose `type` matches an active generator's charter `subscribes_to: [<type>, …]` declaration, the daemon invokes that generator's observe() path within seconds (5 s spec ceiling) instead of waiting for the next periodic sweep. A per-(generator, signal) ledger at `{shared_dir}/signal_subscribers/ledger.jsonl` prevents re-dispatch across daemon restarts. The periodic generator sweep (via `better_engine_refresh.py`) remains the safety net for daemon downtime and unsubscribed generators; arbiter dedup merges a duplicate Proposal emitted by both paths. See [docs/spec-signal-subscriber-2026-05-31.md](spec-signal-subscriber-2026-05-31.md).

---

## Roles & Where Jobs Run

Each bot in the pod has a role: `primary` (one bot) or `member` (the arbiter's `BotRole` literal — there is no other role). Role determines docs, dashboard (`dashboardEnabled` is primary-only), and approval-audience resolution — it no longer determines the launchd job set. Scheduled jobs split into two groups (every row below corresponds to a JobSpec in `packages/admin/evolve_admin/deploy.py`):

| Group | Jobs and cadences |
|-------|-------------------|
| **Per bot, as the bot's own user** (installed by `deploy_bot`) | cost_event_converter.py every 15 min · app_audit_runner.py Tier-2 every 6 h + Tier-3 hourly · backup.py daily 02:00 · doctor_pass_runner.py daily 03:17 |
| **Pod-wide, as the `evolve` service user** (installed by `install_evolve_infra_jobs` / `sudo evolve-admin install-infra-jobs`) | measure.py daily 01:00 (full fleet) · better_engine_refresh.py every 15 min + urgent WatchPaths · analyze.py Sun 02:00 · outcome.py daily 09:00 · heal.py every 5 min · verify daemon every 5 min · spend_alert.py every 5 min · cron_alert.py hourly · defer_runner.py every 2 min · audit.py every 15 min · pod_health_runner.py every 60 s · signal_notifier_runner.py every 60 s · signal_subscriber_runner.py (long-running KeepAlive) · pod_report.py hourly (self-gated to report_hour) · weekly_review.py Sun 03:00 · usage_logger.py daily 03:30 · slack_signals.py daily 03:00 · expansion.py Sundays 04:00 (self-gates to the first week of the month) · admin-ui (KeepAlive) · plus the remaining infra fleet — tuples extraction daily 01:30, manifest-reflex every 60 s, weekly-bot-trends Sun 03:30, retention, log-rotation, backup-signal, and the monitor/audit jobs; `install_evolve_infra_jobs` in deploy.py is the authoritative list |

There is no scheduled "security" role: the legacy `review.py` reviewer job no longer exists (see Security Model below). `network.security.botId` remains in network.json as the target pointer for the security-repair CLI path (`evolve-admin repair-security_bot`), not as a job-bearing role.

---

## The Shared Directory Contract

`/Users/Shared/evolve/` is the integration surface between all bots.

```
/Users/Shared/evolve/
  network.json          ← canonical config (single source of truth)
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

The key architectural insight is the **autonomy gate**: a suggestion either
qualifies to apply without a human, or it waits for one.

It used to be "each bot applies its own changes" — a per-bot daemon running as
the target bot's own user, so an applied change needed no sudo. That daemon was
retired in August 2026 once it turned out to have been watching a directory
suggestions never reach (it had applied nothing in its entire recorded history),
and the run-as-the-bot boundary went with it: appliers now run on the admin host
as the `evolve` service user and write bot config through the same narrow sudo
grants as the rest of the admin surface. See
[principle-each-bot-applies-its-own-changes.md](principle-each-bot-applies-its-own-changes.md)
for the superseded principle and
[design-proposal-signing-key-2026-08-18.md](design-proposal-signing-key-2026-08-18.md)
for why.

```
primary-bot (primary)                  admin host (evolve service user)
  generator_runner runs coaches        arbiter.apply picks up approved_*
  → proposal written to pending/       → captures a snapshot / revert plan
  → routing: auto-eligible?            → dispatches to the action's Applier
      yes → approved_auto              → writes the target bot's config
      no  → human approves in UI       → proposal moves to applied/
                                       → verify daemon checks claim
```

The autonomy gate (`arbiter.routing.is_autonomous_eligible`) requires all of:
- `reversibility == "auto"` (applier can undo via paired RevertPlan)
- `blast_radius ∈ {local, bot}` (no platform-wide effect)
- `touches` disjoint from the irreversibility surfaces (`auth`, `tools`, `channel_config`, `gateway_core`, `app_install`, `app_removal`, `bot_specialization`)
- a verifiable `claim` and an attached `revert_on_failure` plan
- not an autonomy-ladder promotion (upward autonomy changes are permanently human-gated; demotions follow the normal rules)
- a clean pass through the folded security screen (`arbiter/security_screen.py`) — deny-rule hits or a missing AST layer fail closed to human review

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

### Proposal safety gates (arbiter)

The legacy standing security reviewer (`review.py`, driven by `security_rules.json`) was retired and **executed** 2026-08-14 (#3641): both files are deleted from the codebase and from bot deploys (`ANALYZER_DATA_FILES` is empty). Proposal safety is enforced by the arbiter pipeline instead:

1. **Routing gate** (`arbiter.routing.is_autonomous_eligible`) — only reversible, bot-local proposals that carry a claim and a RevertPlan and touch no irreversibility surface can take the autonomous path, **and** the folded security screen (`arbiter/security_screen.py`) must report no deny-rule hits with its AST layer available — a denial or a missing AST layer fails closed to human review; everything else waits in `pending` for human approval. Autonomy-ladder promotions can never be autonomous.
2. **Human approval** (Pod-admin, admin UI) for every non-autonomous proposal.
3. **In-process validation on approval** (`validate.py`, invoked by the admin server's `_validate_proposal_immediately()`) — shadow-apply, schema, and safety checks before apply.
4. **The applier's own gates** (`arbiter.apply` → the per-kind Applier) and the **post-apply verify daemon** (below).

`security_rules.json` is gone — the mandate is code (`arbiter/security_screen.py`), not shipped data. `network.security.botId` remains as the target pointer for the security-repair CLI (`evolve-admin repair-security_bot`), not as a scheduled-reviewer role.

### The applier safety gates

1. Requires `approved_auto` or `approved_human` status (`arbiter.apply` refuses anything else)
2. Refuses to apply against a tripped circuit breaker; the proposal stays `approved_*` for the next sweep
3. Captures a snapshot first and attaches it as the proposal's revert plan — no snapshot, no apply (claim-carrying proposals only; a claim-less proposal has nothing meaningful to revert, so it applies without one — `arbiter/apply.py`)
4. Dispatches through the Applier registered for the action kind; config-writing appliers health-check the gateway and roll back on failure
5. Proposal moves to `applied/`; the verify daemon owns the post-horizon verdict (succeeded / failed_reverted / failed_flagged)

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
1. **Arbiter routing** (automated, `arbiter.routing`) — the proposal is held in `pending` unless `is_autonomous_eligible` passes
2. **Human approval** (Pod-admin, via admin UI) — explicit approve or reject required; on approval the admin server validates the proposal in-process (`validate.py`) before apply

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
- `docs/continuity-engine.md` — Bot-scheduled defers (`defer` tool), per-bot queue, defer runner
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
- Continuity Engine (bot-scheduled defers, timed follow-up delivery)
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
- Apps (manifests, gallery, forge)
- Recommendations (Better Engine proposals)
- Pod health scoring and analytics
