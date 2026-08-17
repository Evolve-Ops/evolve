---
title: "Help: Settings Page"
slug: settings
audience: public
last_reviewed: 2026-06-05
concepts:
  - settings
  - pod-configuration
  - network-json
  - modules
  - primary-bot
ui_surface: admin.settings
related_specs: []
---

# Help: Settings Page

The Settings page (in the **Settings** bucket) is where you configure the pod and manage individual Evolve components. It has two main areas: pod configuration and the Modules tab.

**Looking for pod admins, passphrases, or per-bot owners?** Those moved to the [Users](users.md) page (also in the Settings bucket). Settings now covers pod / network / bot configuration only.

**Ask evo from chat.** Most settings changes can be staged as proposals
through chat: *"change the primary bot to admin-bot"*, *"edit the pod alert
chat target"*. Evo surfaces the relevant proposal (or stages a one-off
when no generator exists for that change) for you to confirm. The
`evolve-admin wipe-telemetry` CLI also has a chat path — *"wipe my evo
telemetry"*. Generic config edits use `ConfigPatch` via
`proposal_action(action="apply")`.

---

## Pod Configuration

The top section of Settings covers pod-level config — network identity, bot roster, shared directory path, feature flags, and module enable/disable toggles. Changes here write to `network.json` and take effect on the next daemon cycle.

Three tabs:

- **Network** — pod context (deploy machine, Unix admin account, admin URL), shared SSH key path for backups, default backup account.
- **Bot** — the bot roster (add / remove bots, primary-bot designation, ordering).
- *(Identity moved to [Users](users.md))*

See [docs/configuration.md](../configuration.md) for the full `network.json` reference.

---

## Modules tab

The Modules tab shows each Evolve component as a toggleable module — what it does, whether it's enabled, and key tuning settings. Use this tab to turn components on or off without editing config files directly.

### What Modules Are

Evolve is composed of loosely coupled components. Each module corresponds to one or more scripts and plugin hooks that can be enabled or disabled independently. You don't need all modules running — you can use Evolve purely for the Operate layer (running the pod) without the Improve layer (the Better Engine), or run the full stack.

### The Module Grid

Each card shows:
- **Module name** — e.g., "Continuity Engine", "Security Audit", "Weekly Analysis"
- **Status** — enabled / disabled / degraded
- **Description** — what this module does
- **Toggle** — enable or disable the module
- **Settings** (if any) — key configuration parameters surfaced without editing JSON

### Core Modules

#### Pod Management
Always on. Gateway health monitoring, heal.py auto-restart, and the admin server. Cannot be disabled without uninstalling Evolve.

#### Security Audit (`audit.py`)
Runs every 15 minutes. Checks identity integrity (SOUL.md/AGENTS.md hashes), gateway bind address, exec allowlist, machine-level security (firewall, SSH config, user accounts, listening ports). Generates CRITICAL alerts for serious findings.

#### Cost Monitoring (`cost.py`)
Runs daily. Tracks spend per bot and per model, checks auth health, detects model drift from expected configuration. Generates alerts when spend anomalies are detected.

#### Session Metrics (`measure.py`)
Runs daily at 01:00. Aggregates session annotations from the plugin into structured daily metrics — productive/maintenance ratio, resolution rate, app usage, cost per session. The Better Engine depends on this module.

#### Continuity Engine (`defer_runner.py`)
Runs every 2 minutes. Fires the follow-ups bots have scheduled for themselves via the `defer` tool (delivering a stored message or running a stored instruction as a short agent turn). See the Continuity help for details.

#### Weekly Analysis (`analyze.py`)
Runs Sunday at 02:00. Runs pattern detectors on accumulated metrics and session summaries; materializes findings into the v2 arbiter queue. Requires Session Metrics to have been running for at least 1 week. New patterns increasingly land as v2 generators in `packages/analyzer/generators/` rather than additions here.

#### Security Review (`review.py`)
Runs every 2 minutes. Screens pending proposals against 8 hard auto-reject rules. A proposal that passes becomes `reviewed` and appears in Recommendations for your approval.

#### Proposal Application (`apply.py`)
Runs every 5 minutes. Applies approved proposals to target bots with backup, health check, and auto-rollback.

#### Outcome Tracking (`outcome.py`)
Runs daily at 09:00. Sends 7-day check-in messages for applied proposals. Your responses calibrate the analysis engine's thresholds.

#### Expansion Engine (`expansion.py`)
Runs monthly (first Sunday at 04:00). Finds app coverage gaps — things bots do repeatedly that aren't covered by any manifest.

#### Community Intelligence (`community_intel.py`)
Aggregates signals from external sources. Community-derived findings surface as suggestions through the standard arbiter queue.

#### Better Engine refresh (`better_engine_refresh.py`)
Runs every 15 minutes. Drives the v2 generator portfolio: pulls observations, runs each generator on its cadence, ingests proposals into the arbiter, advances verify-daemon state, and refreshes calibration snapshots. This is the heartbeat of the Better Engine.

#### Check-in (`verify/`)
Runs alongside the Better Engine refresh. For every applied suggestion carrying a falsifiable claim, checks whether the metric moved in the claimed direction within the claim's window (1d / 7d / 30d). On refutation: revert / flag / escalate per the suggestion's revert plan.

### Common Questions

**Which modules are safe to disable?**
The Improve-layer modules (Weekly Analysis, Expansion Engine, Outcome Tracking, Community Intelligence) can be disabled if you want Evolve to operate as a pure ops tool. The core modules (Pod Management, Security Audit, Cost Monitoring, Session Metrics, Continuity Engine) are safe to disable individually but reduce Evolve's usefulness.

**A module shows "degraded" — what does that mean?**
The module is running but encountering errors on some (not all) operations. Examples: Session Metrics is running but failing for one specific bot; Security Audit is running but can't read one bot's config file. Check the module's associated log or the Gateway Logs for the affected bot.

**I disabled a module — when does it take effect?**
Immediately for the next scheduled run. The launchd job still fires, but the script checks the enabled flag and exits early. The current run (if in progress) completes normally.

**Can I change module schedule frequency?**
Not via this UI currently — schedules are set in the launchd plist files. To change a schedule, modify the plist via Admin Actions or manually (Maintenance → Infra Jobs).

**What's the difference between disabling a module here vs. unloading its launchd service?**
Disabling here sets a flag in Evolve's config — the launchd service still fires on schedule, but the script sees the disabled flag and exits immediately. Unloading the launchd service prevents it from firing at all. Either works; the Modules UI is easier to toggle back on.
