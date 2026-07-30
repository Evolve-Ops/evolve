---
title: "Help: Maintenance Page"
slug: maintenance
audience: public
last_reviewed: 2026-06-06
concepts:
  - maintenance
  - gateway-status
  - cron-jobs
  - openclaw-version
  - admin-server
  - logs
ui_surface: admin.maintenance
related_specs: []
---

# Help: Maintenance Page

The Maintenance page is the operational control center for the pod's infrastructure — gateway status, cron jobs, OpenClaw version, logs, and the admin server itself. Use this page when something is broken or you need to check on the health of Evolve's own infrastructure.

**Ask evo to act, not narrate.** This is the page where chat shortcuts pay
off most:

- *"pause all bots"* / *"resume all bots"* — pod-wide pause with the
  destructive-tier confirm gate (you'll be asked to confirm).
- *"restart team-bot-a"* / *"redeploy evolve"* — single-bot operations without
  hunting for the right row.
- *"any infra daemons down?"* — evo summarizes `pod_state.host` for you.
- *"show team-bot-a's recent errors"* — `pod_state.errors` returns the last raw
  log lines for triage.

The destructive operations (pause-all, remove) always require explicit
`confirm: true` regardless of authority tier — defense in depth.

---

## Subtabs

### Status

Shows the live status of every bot's OC gateway. The table shows:
- **Bot** — bot name and user
- **Gateway** — responding / not responding (HTTP health probe)
- **Port** — the port the gateway is listening on
- **Last heartbeat** — when the bot last sent a heartbeat (indicating active use)
- **Uptime** — how long the gateway has been running

**Auto-refresh** checkbox — keeps the table updating every 30 seconds.

**Refresh** button — manual refresh.

If a gateway is down, you can restart it from this tab. Evolve's `heal.py` also runs every ~5 minutes and restarts failed gateways automatically.

*Note: This page probes each gateway via HTTP. A bot appearing "offline" here doesn't necessarily mean the bot is broken — the gateway process could be healthy but not yet responding (still starting up), or the port configuration could be wrong.*

### Cron Jobs

All scheduled jobs running on every bot, aggregated in one view. Data comes from `openclaw cron list --json` on each bot. Shows:
- Job name and description
- Schedule (cron expression)
- Last run time and result
- Next scheduled run
- Status (active, disabled, failing)

**Auto-refresh** (60s) keeps this view current.

Common Evolve cron jobs on each bot:
- `evolve-measure` — daily at 01:00, aggregates session metrics
- `evolve-analyze` — weekly Sunday 02:00, runs pattern detectors and generates proposals
- `evolve-heal` — every 5 minutes, checks gateway health and restarts if needed
- `evolve-audit` — every 15 minutes, security audit
- `evolve-task-runner` — every 15 minutes, runs the Continuity Engine task queue
- `pod_perms_drift_monitor` — hourly, watches for between-deploy ACL / ownership drift on shared directories. Emits a Signal when a managed dir's permissions change outside a deploy. Added PR 2217.

### Infra Jobs

LaunchDaemon plists installed in `/Library/LaunchDaemons/` — the OS-level services that keep Evolve's own jobs running. The table shows:

- **Label** — the launchd service identifier (e.g., `ai.evolve.evolve.admin-ui`)
- **Schedule** — when the job runs: `on-demand` for manually-triggered jobs, `every Ns` for interval jobs, or a calendar spec for time-based jobs
- **Loaded** — whether the service is currently registered with launchd (✓ loaded / not loaded)
- **Enabled** — whether the service will auto-start on reboot; a loaded-but-disabled job will vanish after the next restart
- **PID** — the process ID if the job is actively running; `—` if stopped
- **Last Exit** — exit code from the most recent run; `0` is success, non-zero (shown in red) indicates a failure

This is distinct from cron jobs (which run inside OC): Infra Jobs are macOS launchd services that run even when OC isn't running.

If an infra job shows as not loaded, it may have been unloaded by a macOS update or a system restart. Use `sudo launchctl bootstrap system <plist-path>` to reload it.

### OC Version

Shows the installed version of OpenClaw on each bot vs. the latest available on the npm registry. If a bot is behind, an upgrade notice appears here.

**Refresh** checks npm for the latest version.

To upgrade OpenClaw: go to System subtab → Upgrade Evolve Pod.

### Gateway Logs

Live log output from each bot's OC gateway process. Select a bot from the tabs.

**Auto-refresh** (10s) streams new log lines.

Logs are read from the bot's OC log file (shown at the bottom of the panel). Look here for:
- Plugin errors (TypeScript errors from the Evolve plugin)
- Gateway startup/restart events
- API errors (rate limits, auth failures)
- Model routing decisions (if debug logging is enabled)

### Admin Server

Status and controls for the Evolve admin Flask server itself (the server running this UI).

**Service Status Card:**
- Shows whether the admin server is running as a launchd service
- **↺ Restart Server** — restarts the Flask process (you'll briefly lose connectivity)
- **⬇ Install as Service** — installs the admin server as a persistent launchd service (if not already)
- **✕ Uninstall Service** — removes the launchd service (server won't restart after reboot)
- **View Logs** — shows the last 100 lines of the admin server log

**Diagnostic Report Card:**
- Captures system info, configuration summary, and recent error logs into a report
- **✉ Send Report** — emails the report (requires email configuration)
- **⬇ Save Report** — downloads the report as a file

### Setup

The tunnel setup wizard for accessing the admin UI from your laptop via SSH tunnel (for when the pod host isn't on the same network). Steps:
1. Install admin server as a persistent service
2. Set up SSH keys between laptop and the pod host
3. Configure tunnel parameters (hostname, port, SSH key path)
4. Download setup scripts
5. Add a browser URL shortcut
6. Verify the connection

This is only needed if you want to access the admin UI remotely. If you're on the same network, just browse to `http://<pod-host-ip>:5050`.

### Claude Access (MCP Bridge)

Configuration for connecting Claude Desktop to your pod via the MCP Bridge.

**MCP Bridge Card** — shows whether the MCP Bridge is running on port 5051.

**Bot Context Access Card** — shows which bots Claude Desktop can read/write context for.

**Claude Desktop Config Card** — generates the JSON snippet to paste into `~/Library/Application Support/Claude/claude_desktop_config.json`. Two modes:
- **Same machine (localhost)** — if Claude Desktop is on the same host as the pod
- **Remote (Tailscale)** — if Claude Desktop is on your laptop connecting over Tailscale VPN

**Recent Activity** — shows recent MCP Bridge calls (writes only toggle to filter).

### System

**Evolve Version & Sync Status** — shows current Evolve version vs. the latest in the repo. If your install is behind the repo, an upgrade option appears.

**Upgrade Evolve Pod:**
- **Upgrade All Bots** — updates the Evolve plugin on every bot and restarts their gateways
- **Upgrade Selected Bot** — single bot upgrade
- **Skip plugin rebuild** — skips TypeScript compilation (faster, use when only Python code changed)
- **Dry run** — shows what would happen without making changes

**Danger Zone:**
- **Uninstall Evolve** — removes Evolve from the pod. This uninstalls the plugin from all bots, removes launchd services, and optionally removes the `evolve` user. Bot users and their OC config are NOT removed.

### Pod Health

A comprehensive health scan of the entire pod. Shows a scored checklist of checks per bot:

- **Evolve plugin installed and active** — is the TypeScript plugin running?
- **Primary model reachable** — can the bot make an API call to its configured model?
- **Auth order** — is the most cost-effective auth method listed first?
- **Gateway bind address** — should always be `127.0.0.1`, not `0.0.0.0`
- **Evolve plugin installed** — plugin present and responding at `/evolve/status`
- **API key presence** — expected providers have keys configured

**Scan Now** — runs a fresh health scan (takes 15–30 seconds for a full pod).

**Fix Issues** — for issues with automatic fixes available, applies them in bulk. Not all issues have automatic fixes — some require manual action (shown with instructions).

---

## Common Questions

**A bot's gateway is down. How do I restart it?**
Go to Status subtab, find the bot, and click restart. Alternatively, heal.py runs every 5 minutes and will restart it automatically. If it keeps going down, check Gateway Logs for errors.

**The admin server shows as not running as a service. Should I worry?**
If you're running the admin server manually (e.g., from a terminal), it won't survive a reboot. Install it as a service from the Admin Server subtab so it auto-starts. This is essential for a production pod.

**How do I access the admin UI from my laptop?**
Use the Setup subtab to configure an SSH tunnel. You'll download a script that establishes the tunnel and optionally adds a browser shortcut. Requires Tailscale or a direct SSH connection to the pod host.

**Pod Health shows critical issues — where do I start?**
Start with the highest-severity issues (red ❌). Critical issues usually involve: plugin not installed, gateway binding to 0.0.0.0, or no working API keys. Most have specific fix instructions shown inline. Run Fix Issues for any that have automatic remediation.
