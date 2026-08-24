# Evolve — Deployment Guide

> **Looking for the install procedure?** The canonical, user-facing install guide
> is **[Installing Evolve](help/installation.md)** (start with the repo clone,
> then [macOS](help/install-macos.md) or [Linux/VPS](help/install-linux-vsp.md)).
> This document is the **deep operator reference** — every deploy step, day-2
> operations, and troubleshooting — and stays here for that purpose.

Real-world steps to deploy Evolve. The single command is `evolve-admin deploy <bot>`.
This doc explains what it does and how to verify it worked.

---

## Prerequisites

- macOS 14+ (Linux paths differ — see [help/install-linux-vsp.md](help/install-linux-vsp.md))
- Python 3.10+ (macOS system Python is 3.9 — `brew install python@3.12`)
- Node.js 20+ (for TypeScript plugin build)
- `sudo` access
- OpenClaw installed on all target bots

---

## Step 1: Install Evolve admin CLI

```bash
sudo git clone https://github.com/evolve-ops/evolve /Users/Shared/evolve-repo
sudo $(brew --prefix)/bin/python3.12 -m venv /Users/Shared/evolve-venv
# Analyzer first (admin depends on it). compat-mode editable writes a plain
# .pth entry, so modules added later by `git pull` import without reinstall.
sudo /Users/Shared/evolve-venv/bin/pip install -e /Users/Shared/evolve-repo/packages/analyzer/ --config-settings editable_mode=compat
sudo /Users/Shared/evolve-venv/bin/pip install -e /Users/Shared/evolve-repo/packages/admin/
# The venv install does not put evolve-admin on PATH — symlink it
sudo mkdir -p /usr/local/bin
sudo ln -sf /Users/Shared/evolve-venv/bin/evolve-admin /usr/local/bin/evolve-admin
evolve-admin --help
```

---

## Step 2: Set up shared directory

```bash
sudo evolve-admin setup-shared
```

Creates `/Users/Shared/evolve/` with all required subdirectories and correct ownership.

---

## Step 3: Configure network.json

If you're starting fresh, run the full wizard (creates accounts, installs OC, deploys everything):
```bash
sudo evolve-admin setup --fresh
```

If you already have OC installed and want to add Evolve to existing bots:
```bash
sudo evolve-admin setup
```

Or create `network.json` manually at `/Users/Shared/evolve/network.json`. Minimum required fields:
```json
{
  "networkId": "my-pod",
  "primary": "admin-bot",
  "members": ["admin-bot", "team-bot-a"],
  "sharedDir": "/Users/Shared/evolve",
  "bots": {
    "admin-bot": { "role": "primary", "port": 19000 },
    "team-bot-a":   { "role": "member",  "port": 19001 }
  }
}
```

---

## Step 4: Deploy to a bot

```bash
sudo evolve-admin deploy admin-bot
```

This runs 8 steps automatically:

| Step | What happens |
|------|-------------|
| 1. Reinstall evolve-admin | Updates CLI from repo HEAD |
| 2. Build plugin | Compiles TypeScript plugin (`npm install` if needed, then `npx tsc`) |
| 3. Fix plugin permissions | `root:wheel` + `755` so all bot users can load it |
| 4. Install OC plugin | Writes plugin config to bot's `openclaw.json`; runs `openclaw plugins install -l <plugin-src>` as bot user |
| 5. Fix shared dir perms | `chown` shared subdirs to bot user |
| 6. Reinstall cron jobs | Installs launchd jobs in `/Library/LaunchDaemons/` |
| 7. Restart gateway | `launchctl kickstart` or `openclaw gateway restart` |
| 8. Verify | HTTP check to `http://localhost:<port>/evolve/status` |

To deploy to all bots at once:
```bash
sudo evolve-admin deploy --all
```

---

## Step 5: Primary bot additional jobs

When `role=primary`, deploy additionally installs on the primary bot:

| Job | Schedule | Purpose |
|-----|----------|---------|
| analyze | Sunday 02:00 | Pattern detection → proposals |
| report | 06:15 + 18:15 | Health report via Telegram |
| outcome | Daily 09:00 | 7-day post-apply outcome check |
| defer-runner | Every 2 min | Continuity Engine v2 — fires bot-scheduled defers (pod-wide job) |
| slack-signals | Daily 03:00 | Ingest Slack channel signals |
| expansion | Sunday 04:00 | Proactive application gap detection |
| spend-alert | Hourly at :10 | Spend threshold check |
| cron-alert | Hourly at :15 | Cron silence detection |
| admin-ui | KeepAlive | `evolve-admin serve` on port 5050 |

Community Intelligence is staged (disabled by default). To install after enabling:
```bash
sudo launchctl bootstrap system /Users/Shared/evolve/plists/ai.evolve.<bot>.community-intel.plist
```

---

## Post-Install Steps

### Backfill historical data

After installing Evolve, populate historical charts with existing OC session data:

```bash
evolve-admin backfill admin-bot --days 30
evolve-admin backfill team-bot-a --days 30
```

This reads OC's existing session logs and populates Evolve's metrics directory
with historical data. Backfilled data is marked as estimated and won't affect
the intelligence layer's analysis.

---

## Post-deploy verification

```bash
# 1. Plugin status
curl http://localhost:<bot-port>/evolve/status

# 2. Pod status
evolve-admin status

# 3. Admin UI
evolve-admin serve --open

# 4. Confirm launchd jobs loaded
sudo launchctl list | grep evolve
```

Expected from `curl .../evolve/status`:
```json
{"bot_id": "admin-bot", "status": "ok", "session_count": 0}
```

---

## Launchd job schedule (every bot)

| Job | Schedule |
|-----|----------|
| measure | Daily 01:00 |
| apply | Every 5 min (StartInterval 300) |
| test | Saturday 03:00 |

Logs: `/Users/<bot>/.openclaw/logs/evolve-<job>.log`

---

## Updating Evolve

**Updates are automatic.** The `repo-puller` daemon fast-forwards the deploy
checkout from origin every 15 minutes (`git pull --ff-only`) and rebuilds what
changed — that's the normal way fixes reach your pod. You don't need to do
anything. See [help/updating.md](help/updating.md) for the full story.

To pick up a fix *right now* instead of waiting for the next tick:

```bash
sudo evolve-admin upgrade
```

That pulls, rebuilds the plugin, and redeploys in one pass.

**Avoid running `git pull` in the deploy checkout by hand** — the puller
already owns that checkout, and on pods using the opt-in `canary` release
channel (`pod.release.mode` in `network.json`), out-of-band git operations get
repaired back to the release pointer. On canary pods, use
`sudo evolve-admin release status|pin|rollback|promote` instead.

---

## Module enablement by stage

Don't enable everything at once.

**Week 1 (default):** Observer, metrics, healing, apply active. Analysis and CE disabled.

**Week 3 (after first data):**
```bash
evolve-admin modules enable analysis
```

**Week 6 (after calibration):**
```bash
evolve-admin modules enable outcomes
```

**Month 2 (optional):**
```bash
evolve-admin modules enable slack_signals
```
(The Continuity Engine is default-on; disable it per-bot via
`bots.<id>.continuity_engine.enabled` in `network.json` if unwanted.)

---

## Deployment-specific configuration

After running the wizard, customize `/Users/Shared/evolve/network.json`:

### Classifier hints (deployment-specific vocabulary)

```json
"classifierHints": {
  "productive_extra": ["your-project-name", "team-member-name", "domain-term"],
  "maintenance_extra": ["your-custom-error-term"]
}
```

### Slack channel → application mapping

```json
"slackChannelApplications": {
  "#your-channel": "your-application-id"
}
```

---

## Day-2 operations

### Add a bot
```bash
# Add to network.json (members[] and bots{}), then:
sudo evolve-admin deploy <new-bot-id>
```

### Remove a bot

Three lifecycle paths depending on intent:

```bash
# Disconnect from Evolve, keep the bot running as an independent OpenClaw bot:
sudo evolve-admin detach-bot <bot-id>

# Graceful full retirement: stops daemons, archives workspace + closure summary,
# removes from network.json. Reversible via archive restore.
sudo evolve-admin retire-bot <bot-id>

# Irreversible full removal: retire + dscl-delete macOS user + rm -rf /Users/<bot>/.
# Refuses to dscl-delete if the bot piggybacks on an existing operator account.
sudo evolve-admin delete-bot <bot-id>
```

### Back up
```bash
tar czf /tmp/evolve-backup-$(date +%Y%m%d).tar.gz /Users/Shared/evolve/
```

---

## Common issues

### measure.py writes nothing

The measure script runs as the bot's macOS user, not root. If permission denied:
```bash
sudo evolve-admin setup-shared
# Or manually:
sudo chown -R admin-bot:wheel /Users/Shared/evolve/metrics
sudo chmod -R 755 /Users/Shared/evolve/metrics
```

### Plugin not loading

```bash
tail -50 /Users/<bot>/.openclaw/logs/gateway.err.log | grep evolve
# Reinstall plugin:
sudo evolve-admin deploy <bot-id>
```

### analyze.py generates no proposals after 7 days

```bash
ls /Users/Shared/evolve/annotations/<bot-id>/
ls /Users/Shared/evolve/metrics/
# If annotations empty: plugin not running — re-deploy
sudo evolve-admin deploy <bot-id>
```

### Proposals in pending/ but not in admin UI

A proposal that was rejected moves to `proposals/archived/` with
`status: "rejected"` — there is no `proposals/rejected/` directory. Check the
reason there:
```bash
python3 -m json.tool /Users/Shared/evolve/proposals/archived/<latest>.json | grep rejection_reason
```
