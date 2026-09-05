---
title: "Quick Start"
slug: quick-start
audience: public
last_reviewed: 2026-08-26
concepts:
  - quick-start
  - install
  - first-week
  - day-zero
  - adopt-existing-bots
ui_surface: null
related_specs: []
---

# Quick Start

Step-by-step actions for new admins after install. Estimated time: 30–45 min.

This guide is also available in the dashboard at **Getting Started → Quick Start**.

---

## Day 0: right after the setup wizard finishes

What the wizard already did depends on which kind of machine you ran it on.

**If the machine had no bots on it — a from-scratch pod:**

- Created a user account for each bot
- Installed OpenClaw and started each gateway
- Added a dedicated `evo` bot, which is how you talk to Evolve itself

**If you adopted bots that were already there:**

- Left each existing bot's account, OpenClaw config and provider keys alone
- Added the Evolve plugin to every bot you adopted
- Added a dedicated `evo` bot alongside them, on its own account
- **Ran a first app-discovery scan of the bots it adopted** — so there is
  already something waiting on Apps → Discovered (Step 2 below)

**Either way, it also:**

- Set up the evolve background service (admin server on port 5050)
- Wrote `/Users/Shared/evolve/network.json` (`/var/lib/evolve/network.json` on
  a Linux pod)

Now do these five things before anything else:

1. **Verify the admin server is running** — on the pod machine, run
   `curl -s http://localhost:5050/api/health`. Should return `{"status":"ok"}`.
2. **Set up your SSH tunnel** (see Step 1 below) so you can open the dashboard
   from your laptop.
3. **Note your bot IDs** — the names you gave each bot during setup. On an
   adopted pod they're the accounts your bots were already running under.
4. **Open Apps → Discovered** (Step 2 below). On an adopted pod this is the
   first thing worth looking at; on a from-scratch pod it will be empty until
   your bots have done some work.
5. **Set up your first messaging channel** (see Step 4) so bots can talk back.

---

## Step 1: SSH tunnel from your laptop

The admin dashboard runs at `http://localhost:5050` on your Mac. To open it
from your laptop without exposing it to the network, forward the port over SSH.

**Quickest — a plain ssh one-liner** (one session):

```bash
ssh -N -L 5050:localhost:5050 <pod-host> &
```

**Persistent — `evolve-admin connect`.** Installs a persistent launchd agent
that auto-reconnects on reboot or network drop, then opens the dashboard in
your browser. `evolve-admin` is **not on PyPI** — install it on the laptop
from a clone of the repo first (see
[Accessing the dashboard](install-macos.md#accessing-the-dashboard)):

```bash
evolve-admin connect --host <pod-host>
```

Manage it later with `evolve-admin connect --status` and stop it with
`evolve-admin connect --uninstall`. `<pod-host>` is the SSH host of the
pod machine — an alias from your `~/.ssh/config`, or the full `user@host` form.

The same command works from any client OS — macOS or Linux terminal, and
Windows 10+ PowerShell (Windows ships the OpenSSH client). Your laptop's OS
doesn't matter; the dashboard is a browser app.

Then open `http://localhost:5050` in your browser. Add a shell alias if you
run it often:

```bash
# in ~/.zshrc or ~/.bashrc
alias pod='ssh -N -L 5050:localhost:5050 <pod-host>'
```

**No-CLI alternative.** The Maintenance page can generate a persistent tunnel
installer for you: go to Maintenance → Claude Access → download the `.command`
file and run it once on your laptop. Same launchd agent, no terminal required.

Verified: port 5050 is the default from `packages/admin/evolve_admin/service.py`
(`generate_plist(host="127.0.0.1", port=5050)`).

---

## Step 2: See what your bots are already doing

Go to **Apps → Discovered**.

Evolve reads each bot's workspace, its schedules and its standing instructions
and works out which of them look like *applications* — a recurring thing the bot
does for someone, rather than a loose script. Each one it finds lands here as a
**draft**: something the bot appears to be doing that nobody has vouched for yet.

**On a pod you adopted, this page has content on day zero.** Evolve scanned each
bot as it deployed, so the drafts are your bots' real habits, not examples.

Discovery runs three ways, and you don't have to remember any of them:

- **On arrival** — once per bot, the first time Evolve deploys to it
- **Weekly** — a sweep every Sunday, so a habit that forms in week three still
  shows up
- **On demand** — the **Sync all bots** button on this page

Press **Sync all bots** now if the page is empty, or if you've just added a bot.
A full pass takes roughly a minute per bot. The page carries a *Last scanned*
line so you can always tell whether you're looking at a fresh answer or a stale
one, and it tells you plainly when a bot has never been scanned rather than
implying there was nothing to find.

Then work the list:

- **Promote** — yes, this is a real app. It becomes a defined app on the Apps
  page, where it can be audited, installed on your other bots, and managed
  alongside everything else on the pod.
- **Never** — no, and stop showing me this one.
- Leave it alone if you're not sure. Nothing expires.

Each row carries a **readiness** band — Evolve's read on whether the app is
solid enough to offer to the person it serves. A draft can read *ready* and
still not be offered; when that happens the row says which measurement is
missing rather than leaving you to guess.

Don't try to clear the queue on day one. Promote the two or three you recognise
immediately; that's enough to give the rest of the dashboard something real to
work with.

---

## Step 3: Add another bot (if not done in wizard)

Skip this if the wizard already added every bot you need. Otherwise:

**From the dashboard:** click the + button in the Overview header bar.

**From the CLI:**
```bash
sudo evolve-admin add-bot <bot-id> --port <port>
```

You'll need: the bot's macOS username, its role (`primary` or `member`), and
the gateway port to assign. If the bot already has an OpenClaw gateway
running, `add-bot` will reuse it; otherwise it provisions one.

`add-bot` registers the bot in `network.json` and deploys to it in a single
step. Pass `--no-deploy` to skip the deploy if the bot's host isn't reachable
yet — you can run `sudo evolve-admin deploy <bot-id>` later.

Other useful options: `--user <macos_account>` (if the bot lives on a shared
account), `--role primary` (for the pod's primary bot).

---

## Step 4: Set up a primary messaging channel (if not done in wizard)

Most pods install a channel during the wizard — skip if yours already works.
Otherwise, bots need a way to reach you (and vice versa). Supported channels:

| Channel | Who it's for | Where to set up |
|---|---|---|
| **Slack** | Teams, work bots | Skills page → Slack → Install |
| **Telegram** | Personal bots, alerts | Skills page → Telegram → Install |
| **Discord** | Community bots | Skills page → Discord → Install |
| **iMessage** | Personal bots, no cloud (macOS pod hosts only) | Skills page → iMessage → Install |

For each channel, you'll need API credentials (a bot token for Slack/Telegram/
Discord, or no credentials for iMessage). The Skills install flow walks you
through it.

iMessage only works when the pod itself runs on a Mac (it reads Messages.app's
local database — an upstream constraint), so pods on other host platforms
won't show it in the catalog. The other channels work on any pod host.

After install, test by sending `evo` to the bot. You should get a response.

---

## Step 5: Install your first app from the Gallery

The App Gallery has proven application blueprints ready to install on any bot.

Start with **Morning Briefing**:
- Go to Apps → Gallery
- Click Morning Briefing → Install
- Pick which bot should deliver it, set the delivery time
- The Forge will build the app on that bot — takes about a minute

Morning Briefing is a good starter because:
- It's low-stakes (read-only: email + calendar + weather)
- It demonstrates the Applications vs. Skills distinction concretely
- It gives the Better Engine its first real data to work with

---

## Step 6: Daily routine

Once everything is running, the dashboard gives you three things to check:

**Overview** — glanceable pod state. Any alerts, current spend, bot health.
Check this if something feels off.

**Recommendations** (Improve section) — the Better Engine's weekly queue.
Check this on Fridays or whenever the badge shows new items. Each suggestion
takes 10–30 seconds to act on.

**Maintenance** — system-driven alerts that need your attention. Check this
when the badge lights up. Most issues are one-click fixes.

You don't need to check anything else on a quiet day. The system surfaces
what needs attention; you don't need to hunt for it.

---

## Step 7: When you need more

**Deeper bot config** — `evolve-admin menu exec-approvals <bot>` manages what
commands a bot can run. `evolve-admin menu logs <bot>` tails the gateway log.
If you can't remember the flag syntax, run `evolve-admin menu` with no
arguments for the interactive single-letter menu.

**From your laptop** — `evolve-admin connect --host <pod-host>` installs a
persistent SSH tunnel to the admin UI and opens it in your browser.
Re-run with `--status` to check it, `--uninstall` to stop reconnecting.

**From chat** — `evo help` shows everything available from your messaging app.
`evo summary` gives a pod-wide status update without opening the dashboard.

**From the CLI** — `sudo evolve-admin --help` lists all subcommands.
`sudo evolve-admin health` runs a full pod health check and tells you what
to fix.

**For scripted ops** — the CLI accepts all the same operations the dashboard
does. Useful for cron jobs, automation, or doing things in bulk across bots.
