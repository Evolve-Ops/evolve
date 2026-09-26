---
title: "Help: Overview Page"
slug: overview-page
audience: public
last_reviewed: 2026-06-05
concepts:
  - overview-page
  - pod-status
  - bot-tiles
  - dashboard
ui_surface: admin.overview
related_specs: []
---

# Help: Overview Page

The Overview page shows the live state of your entire pod at a glance — which bots are online, what's pending, and whether anything needs attention.

**Ask evo to act on what you see.** The chat widget's suggested prompts on
this page are operator-shaped:

- *"what's wrong with team-bot-a?"* — evo pulls team-bot-a's bot tile, audit findings, and
  recent errors, then narrates the situation.
- *"restart team-bot-a's gateway"* — wraps the same restart the bot-tile's inline
  button drives; useful when you're already chatting.
- *"redeploy evolve"* — applies an evolve-version upgrade to the primary bot.
- *"pause all bots"* or *"resume all bots"* — pod-wide pause / resume (the
  destructive confirm gate still applies).

The Dashboard's evo context includes every bot's current tile chips, so
asking *"why is the version_drift chip firing on admin-bot?"* gets a direct answer.

---

## What You See

**Top stats bar:**
- **Active Pods** — number of bots currently online (responding to health probes)
- **Proposals Pending** — improvement proposals waiting for your approval (click to go to Recommendations)
- **Pod Health** — composite health score from the last scan (click to go to Maintenance → Pod Health)

**Banners** (shown when relevant):
- **Health banner** — pod health issues detected; click to see details
- **Sync banner** — Evolve repo is out of date with the latest version
- **OC banner** — OpenClaw version issues on one or more bots

**ETR strip** (Evolve Test Rig — shown only when its dashboard is reachable from this browser):
- Status dot, open issue counts, current catalog state
- Link to the ETR dashboard at `localhost:5052`. The strip hides silently on phones or remote browsers where ETR isn't reachable, so the absence of this strip is normal.

**Pods section:**
- One card per bot in your pod
- Shows gateway status, last heartbeat, active sessions
- **+ Add Bot** button starts the bot setup wizard

**Make Your Pod Better strip:**
- Recommendations from Better Engine — operational, security, cost, app-quality, onboarding, exploration, whimsy
- Filter by bot or recommendation type. These complement the full proposal queue on the Recommendations page.

**Getting Started strip** (only on fresh pods):
- A short checklist of next setup steps, hidden once your pod has activity.

---

## What Bot Cards Show

Each bot card shows:
- **Status dot** — green (online), yellow (degraded), red (offline)
- Bot name and user account
- Gateway health (responding/not responding)
- Active session count
- Last activity time

---

## Common Questions

**Why does a bot show as offline?**
The gateway isn't responding to health probes. This usually means:
1. The OC gateway crashed — go to Maintenance → Status to restart it
2. The launchd service isn't running — check Maintenance → Infra Jobs
3. heal.py hasn't had a chance to restart it yet (heals every ~5 minutes)

**What are Proposals Pending?**
The analysis engine found patterns in your bots' sessions and generated improvement suggestions. These could be config changes, script changes, or investigation recommendations. They need your approval before anything changes. Go to Recommendations to review them.

**What does Pod Health mean?**
A composite 0–100 score based on: gateway liveness, API key health, app test results, security audit results, and cost anomalies. A score of 100 means everything is working perfectly. Below 60 is a warning; below 40 is critical. Go to Maintenance → Pod Health for details on what's contributing to a low score.

**The + Add Bot button — what does it do?**
Opens the bot setup wizard. This creates a new macOS user, installs OpenClaw, deploys the Evolve plugin, configures channels and API keys, and registers the bot with the pod. You'll need at least one API key and one messaging channel token ready.

**How often does this page update?**
The stats bar and pod cards poll automatically. Gateway health is checked every time the page loads; the pod health score reflects the last manual or scheduled scan.
