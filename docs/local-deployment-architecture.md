# Evolve — Local Deployment Architecture

*Last updated: 2026-04-13*

---

## Two Separate Concerns

Evolve has two completely distinct identities that must never be conflated:

### 1. Evolve as a Software Project

The code. The product. What gets open-sourced and shared with other operators.

```
Repository:  https://github.com/evolve-ops/evolve
Local path:  /Users/Shared/evolve-repo/
Owner:       pod-admin-user (the developer account)
```

This directory contains only source code. No bot state, no secrets, no runtime config. Anyone can clone it. The repo is portable and public-safe.

**Rule:** Nothing in `evolve-repo/` should contain machine-specific state, credentials, or runtime data. If it does, it's in the wrong place.

### 2. Evolve as a Local Installation

The running system. Machine-specific. Not in the repo.

```
Runtime data:    /Users/Shared/evolve/
Python venv:     /Users/Shared/evolve-venv/
Scheduled jobs:  /Library/LaunchDaemons/ai.openclaw.evolve.*.plist
Admin UI:        http://localhost:5050 (process owned by evolve)
```

**Rule:** Runtime data (metrics, proposals, annotations, alerts) lives in `/Users/Shared/evolve/`. The repo is never the source of runtime truth.

---

## Multi-User macOS Design

### Account Structure

| Account | Role | Has OC Instance? |
|---|---|---|
| `pod-admin-user` | Admin / Developer | No (by design) |
| `evolve` | Infrastructure Manager | Yes (minimal — no plugins, no channels) |
| `admin-bot` | Primary OC bot | Yes |
| `team-bot-a` | Member OC bot | Yes |
| `forge` | Sandbox OC bot | Yes |
| `team-bot-c` | Member OC bot | Yes |

*Note: `security-bot` watchdog user retired — security monitoring delivered by Security Protocol v2 running as `evolve` user (git backup + audit.py + HMAC signing). See [threat-model.md](threat-model.md) for the current security posture.*

**Security principle:** `pod-admin-user` has no OC instance. It has sudo access to all accounts. This is intentional — the admin account should not be running AI workloads. It exists to manage the system.

**The `evolve` user** is a dedicated macOS system account (headless, no GUI login) that owns all infrastructure management processes: the admin server, cron jobs, sandbox orchestration, and analysis scripts. It was introduced to replace the earlier pattern where these ran as `admin-bot`. The setup wizard creates and configures it.

### How the Tension is Resolved

Evolve needs to:
1. Read state from multiple bot accounts (metrics, session data)
2. Write to a shared directory all bots can access
3. Run scheduled jobs
4. Serve a web UI that can control all bots
5. Send alerts

The `evolve` user handles all of this via a combination of direct ownership of `/Users/Shared/evolve/` and narrow sudo grants defined in `/etc/sudoers.d/evolve` (rendered by the setup wizard; audit the live file for the exact grant list).

---

## Ownership Model

```
pod-admin-user owns:
  /Users/Shared/evolve-repo/          ← source code (755 dirs, 644 files)
  /Users/Shared/evolve-venv/          ← Python dependencies (755)
  /opt/homebrew/                      ← Homebrew-managed tools (npm, node, openclaw)

evolve owns:
  /Users/Shared/evolve/               ← runtime data dir
  /Users/Shared/evolve/metrics/       ← daily metrics per bot
  /Users/Shared/evolve/annotations/   ← turn annotations from OC plugin
  /Users/Shared/evolve/proposals/     ← generated proposals
  /Users/Shared/evolve/scoreboard/    ← health scores
  /Users/Shared/evolve/feedback/      ← rejection feedback
  /Users/Shared/evolve/alerts/        ← alert files for Admin-bot to surface
  Admin server process (port 5050)
  All scheduled infrastructure jobs

root owns:
  /Users/Shared/evolve-repo/packages/plugin/dist/   ← built plugin (OC security scanner requires root)

Each bot owns their own ~/.openclaw/
```

**Why evolve owns the data dir:** The evolve user is the infrastructure manager. It writes pod-wide metrics, generates proposals, and manages alerts. Bots write only their own data to per-bot subdirectories.

**Why pod-admin-user owns Homebrew tools:** OpenClaw is system infrastructure (used by every bot) installed via Homebrew npm. It lives in the Homebrew prefix (`/opt/homebrew/`) which is owned by the admin user who installed Homebrew. This is correct and conventional — system tools are owned by the system admin. See §"OpenClaw Updates" below.

**Why dist/ needs root:** OpenClaw's plugin security scanner rejects plugins not owned by the running user or root. Since multiple users (admin-bot, team-bot-a, forge) install the same plugin, root ownership is the only way to satisfy all of them.

---

## How Cross-Bot Access Works

The admin server (running as `evolve`) accesses bot data via two mechanisms:

### 1. Narrow sudo grants
```bash
# Read a bot's openclaw.json
sudo /usr/bin/cat /Users/team-bot-a/.openclaw/openclaw.json

# Write an approved config change via /tmp staging
sudo /bin/cp /tmp/evolve-team-bot-a-1234.json /Users/team-bot-a/.openclaw/openclaw.json

# Restart a bot gateway
sudo /bin/launchctl kickstart -k system/ai.openclaw.team-bot-a-gateway
```
All grants are defined in `/etc/sudoers.d/evolve`. The file itself is the authoritative list — it is rendered by the setup wizard and safe to audit directly; [threat-model.md](threat-model.md) covers the rationale.

### 2. Shared Directory
```
/Users/Shared/evolve/
```
All bots write their own metrics/annotations here. The admin UI reads from here. This is the primary shared state channel between bots.

---

## Component Ownership and Process Model

```
Process                   Owner    How it starts
──────────────────────────────────────────────────────────────
OC Gateway (admin-bot)        admin-bot    system LaunchDaemon
OC Gateway (team-bot-a)          team-bot-a      system LaunchDaemon
OC Gateway (evolve)       evolve   system LaunchDaemon
OC Plugin (in-proc)       bot user loaded by OC gateway
Admin UI (Flask, :5050)   evolve   system LaunchDaemon
evolve-admin CLI          pod-admin-user  run manually

Scheduled jobs (all run as evolve):
  measure                 evolve   LaunchDaemon, daily 1am
  heal                    evolve   LaunchDaemon, every 5min
  analyze                 evolve   LaunchDaemon, weekly Sun 2am
  spend_alert             evolve   LaunchDaemon, hourly :10
  cron_alert              evolve   LaunchDaemon, hourly :15
```

---

## The Admin UI's Role

The Flask admin server (`evolve-admin serve`) runs as **evolve** because:
- `evolve` owns the runtime data (`/Users/Shared/evolve/`)
- `evolve` has the sudo grants needed to read/write bot configs
- Keeping infrastructure processes under a dedicated account provides isolation from both the admin user and the bot accounts

**Access:** http://localhost:5050 (localhost only by default; SSH tunnel for remote access)

---

## OpenClaw Updates

OpenClaw is system infrastructure installed via Homebrew npm (`npm install -g openclaw`). It is owned by `pod-admin-user` (the Homebrew user) and lives at `/opt/homebrew/bin/openclaw`.

**Updates must be run manually as the admin user:**
```bash
sudo npm install -g openclaw
```

The admin UI shows a banner when a newer version is available but does not attempt to run the install — the `evolve` process does not own the Homebrew prefix and should not. Autonomous updates of system-level software by a background process are intentionally out of scope. Granting the `evolve` user npm/Homebrew write access is deliberately excluded from the sudoers surface.

---

## Developer Workflow

When making changes to Evolve:

```bash
# 1. Edit code (as pod-admin-user or via agent)
# All source files in /Users/Shared/evolve-repo/ owned by pod-admin-user

# 2. Build plugin (TypeScript → JavaScript)
cd /Users/Shared/evolve-repo/packages/plugin && npm run build
sudo chown -R root:wheel /Users/Shared/evolve-repo/packages/plugin/dist
sudo chmod -R 755 /Users/Shared/evolve-repo/packages/plugin/dist

# 3. Deploy to bots
evolve-admin deploy admin-bot
evolve-admin deploy team-bot-a
# OR
evolve-admin deploy --all

# 4. Commit and push
cd /Users/Shared/evolve-repo && git add -A && git commit -m "..." && git push
```

**evolve-admin deploy** handles: reinstalling the CLI, building the plugin, fixing permissions, installing the OC plugin on each bot, reinstalling cron jobs, restarting gateways, and verifying.

---

## What Agents Can and Cannot Do

**Agents (Claude Code) run as pod-admin-user** — the developer account that owns the repo. Agents can:
- Read and write any file in `/Users/Shared/evolve-repo/`
- Read `/Users/Shared/evolve/` (shared data dir, world-readable)
- Commit and push to git
- Run Python scripts in the venv

Agents cannot (without sudo, which requires a terminal):
- Write to `/Library/LaunchDaemons/`
- Read bot `~/.openclaw/` files directly
- Change file ownership
- Restart system LaunchDaemons

**Pattern for sudo-required operations:** Agents write a bash script (e.g., `apply_changes.sh`) to `/Users/Shared/evolve/` and notify Pod-admin to run `sudo bash /Users/Shared/evolve/apply_changes.sh`. They never attempt sudo inline.

---

## File Path Reference

| What | Path |
|---|---|
| Source code | `/Users/Shared/evolve-repo/` |
| Analyzer scripts | `/Users/Shared/evolve-repo/packages/analyzer/` |
| Admin CLI + UI | `/Users/Shared/evolve-repo/packages/admin/` |
| OC Plugin source | `/Users/Shared/evolve-repo/packages/plugin/src/` |
| OC Plugin built | `/Users/Shared/evolve-repo/packages/plugin/dist/` |
| Python venv | `/Users/Shared/evolve-venv/` |
| evolve-admin CLI | `/Users/Shared/evolve-venv/bin/evolve-admin` |
| Runtime data | `/Users/Shared/evolve/` |
| Metrics | `/Users/Shared/evolve/metrics/` |
| Annotations | `/Users/Shared/evolve/annotations/` |
| Proposals | `/Users/Shared/evolve/proposals/` |
| Network config | `/Users/Shared/evolve/network.json` |
| Launchd plists | `/Library/LaunchDaemons/ai.openclaw.evolve.*.plist` |
| Admin UI | `http://localhost:5050` |
| OpenClaw binary | `/opt/homebrew/bin/openclaw` |
