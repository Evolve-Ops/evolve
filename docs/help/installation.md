---
title: "Installing Evolve"
slug: installation
audience: public
last_reviewed: 2026-08-26
concepts:
  - installation
  - install
  - clone-repo
  - prerequisites
  - api-keys
  - hardware
  - openclaw-versions
  - adopt-existing-bots
ui_surface: null
related_specs: []
---

# Installing Evolve

This is the one place to start. Installing Evolve takes about 30 minutes of
hands-on time once you have your accounts and keys ready — most of the elapsed
time before that is gathering API keys and waiting for account approvals.

A working pod needs three things: a **dedicated, always-on machine**, an
**account with an LLM provider** — Anthropic (deepest integration; MAX
subscription or API key), OpenAI, Google, or xAI all work, as do
OpenAI-compatible endpoints like OpenRouter or a local Ollama — and a
**messaging channel** (Telegram is the easiest). Everything else is optional and can be added after
your first bot is running.

---

## Step 1 — Clone the repository (do this first)

**Evolve installs and updates itself from a git checkout.** Before you run
anything else, clone the repository onto the machine that will host your pod.

This is the single step new operators most often get wrong, so it's worth saying
plainly:

> ⚠️ **It must be a real `git clone` — not a downloaded ZIP or tarball.**
> Evolve keeps itself current by pulling new releases into this checkout on a
> schedule. A ZIP can't pull updates, so a pod installed from one freezes on the
> code you installed on day one and never gets fixes. Clone it, don't download
> it.

Where the checkout lives depends on your platform — the per-platform guides
below give the exact command, owner, and path:

- **macOS:** `/Users/Shared/evolve-repo`
- **Linux / VPS:** `/var/lib/evolve/repo`, owned by the `evolve` service account.

The auto-updater pulls from the clone's origin on its own. A plain **HTTPS clone
of the public repo needs no credential** — anonymous pulls just work. Only a
*private* origin (e.g. your own fork) needs a **read-only deploy key**; the
setup wizard generates one and prints the registration walkthrough when that
applies.

Don't worry about the venv or the wizard yet — just know that **the clone comes
first**, and that on a durable (production) pod it has to be a clone the pod can
pull from unattended.

---

## Step 2 — Get your prerequisites ready

### Hardware — a dedicated, always-on machine

Evolve assumes a machine that does nothing else and never sleeps. The chassis
doesn't matter:

- **Mac** — a Mac mini is the classic choice; a retired Apple-Silicon MacBook on
  a shelf works just as well (its battery doubles as a built-in UPS). Apple
  Silicon is recommended; Intel is supported best-effort.
- **Linux** — a dedicated, always-on Ubuntu/Debian VPS or box.

In both cases "dedicated" means dedicated: no other day-to-day users or
workloads. The thing to guard against is a "retired" laptop quietly drifting back
into personal use months later.

A few more hardware notes: prefer **wired ethernet** over WiFi for 24/7
reliability, and consider a small **UPS** (~$100) to prevent data corruption on
power blips.

### Accounts and API keys

You only strictly need the first two. The rest unlock more capability and can be
added later.

| Need | Required? | Where to get it |
|---|---|---|
| **LLM provider** — an API key from Anthropic (MAX subscription or API key), OpenAI, Google, or xAI; OpenAI-compatible endpoints (OpenRouter, Ollama, …) also work | **Required (any one)** | e.g. [console.anthropic.com](https://console.anthropic.com), [platform.openai.com](https://platform.openai.com), [aistudio.google.com](https://aistudio.google.com), [console.x.ai](https://console.x.ai). Set a spending cap where your provider supports one. |
| **Messaging channel** — Telegram bot token **+** your Telegram user ID | **Required** | Telegram → `@BotFather` (`/newbot` → token) and `@userinfobot` (your numeric ID). |
| **Brave Search** API key | Strongly recommended | [brave.com/search/api](https://brave.com/search/api/) — free tier covers most personal use. |
| **Google Workspace** OAuth credentials | Recommended | [console.cloud.google.com](https://console.cloud.google.com) — unlocks Gmail / Calendar / Drive. The longest setup (~45 min). |
| Slack / Discord bot tokens | Optional | For team or community bots — add from the Skills page later. |
| GitHub personal access token | Optional | For coding bots. (Separate from the deploy key in Step 1.) |
| Tailscale | Strongly recommended | [tailscale.com](https://tailscale.com) — private access to the admin UI from your laptop and phone, no port-forwarding. |

You're ready to install the moment you have **an LLM provider key, a
Telegram bot token, and your Telegram user ID**.

### Software the machine needs

- **Python 3.10+** — on macOS the system Python is 3.9, which is too old:
  `brew install python@3.12`
- **Node.js** — 20+ on macOS (`brew install node`), **24+** on Linux (NodeSource)
- **git** and `sudo` / root access — on macOS, installing
  [Homebrew](https://brew.sh) brings the Xcode Command Line Tools (and `git`)
  with it
- **OpenClaw 2026.6.11 or newer** — or let the setup wizard install it for
  you. The floor matters: see *Which OpenClaw versions Evolve supports*
  just below.
- **Linux only:** the `acl` package (`sudo apt-get install -y acl`) — without it,
  permission grants silently do nothing

### Which OpenClaw versions Evolve supports

Evolve sits on top of OpenClaw rather than replacing it, so the OpenClaw release
on the machine decides how much of Evolve actually works. Two of the thresholds
below fail *quietly* — nothing errors, the capability simply isn't there — so
it's worth checking before you install:

```bash
openclaw --version
```

| OpenClaw version | What it means for Evolve |
|---|---|
| **Below 2026.4.12** | Works, but the pod's security posture check raises a warning about the OpenClaw version on the Security page until you upgrade. |
| **Below 2026.6.11** | **Not recommended.** OpenClaw doesn't yet fire the `before_tool_call` hook, so Evolve's tool-approval gate — the layer that can veto a bot's tool call *before* it runs — never fires. Everything installs and runs; that one protection is absent, and nothing says so. |
| **2026.6.11 or newer** | The supported minimum. The tool-approval gate works. |
| **2026.7 or newer** | Recommended — **and check your Node version.** From 2026.7 OpenClaw keeps scheduled jobs in a SQLite store instead of a JSON file, and Evolve reads that store to tie a scheduled run back to the app that owns it. Reading it needs **Node 22.13+**. On older Node the link is never made, so per-app cost stays empty for scheduled work. (The Linux guide already requires Node 24. On macOS the floor is Node 20 — upgrade if you're on 2026.7+.) |

**Tested through OpenClaw 2026.7.1.** That is the newest release Evolve has been
run against end to end. Newer OpenClaw releases are expected to work and simply
haven't been verified yet; if you're on one and something looks off, that's
worth [telling us](feedback.md).

Worth reading before you commit a machine: the
**[threat model](../threat-model.md)** — what runs as root, where API keys live
on disk, and what "dedicated machine" is protecting you from. The wizard asks
you to acknowledge exactly this model during setup.

---

## Step 3 — Choose your platform

The hands-on install differs by operating system. Pick your platform and follow
that guide end-to-end:

- **[Install Evolve on macOS](install-macos.md)** — Mac mini or a retired
  MacBook. The simplest path.
- **[Install Evolve on Linux (Ubuntu / VPS)](install-linux-vsp.md)** — a
  dedicated Linux box or cloud VPS. A few platform-specific landmines to verify;
  the guide calls each one out.
- **Using Claude Code or another AI coding agent to help?** Point it at
  **[Installing with an AI agent](install-with-an-agent.md)** — what the agent
  can do unattended, what needs a human at the terminal, and the traps.

**Already running OpenClaw bots on that machine?** You are not starting over.
Both platform guides have an **"I already run OpenClaw bots"** section: the
setup wizard finds the bots that are already there, offers to adopt them, and
leaves each one's OpenClaw config and provider keys alone. Read that section
instead of the from-scratch walkthrough.

After either guide you'll have a running pod and an admin dashboard. From there,
the **[Quick Start](quick-start.md)** walks you through your first week, and
**[Install the app on your phone or desktop](pwa-install.md)** puts the dashboard
one tap away.

Day-2 references, for when you need them:
**[Keeping Evolve up to date](updating.md)** ·
**[Troubleshooting](troubleshooting.md)** ·
**[Backups](backup.md)** ·
**[Sending feedback](feedback.md)** ·
**[Uninstalling](uninstall.md)**
