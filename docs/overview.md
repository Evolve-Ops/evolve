# Evolve — Overview

*Last updated: 2026-05-06*

---

## Alpha Software — Read This First

**Evolve is alpha software. It is not ready for general use.**

It is being shared with a small group of people who run OpenClaw pods, know what
they're doing at the system level, and are willing to hit problems and report back.
If that isn't you, wait for a more stable release.

Specific things to understand before installing:

**It will probably break your existing OpenClaw setup.**
Evolve installs deeply — it creates new system accounts, modifies launchd jobs,
writes to shared directories, and touches OpenClaw configuration files. Existing
bots may need to be re-provisioned. Back up any bot configuration you care about
before running the setup wizard.

**It may introduce security issues.**
Evolve is designed to make OpenClaw pods more secure, but it is alpha code with
incomplete testing. It has not been audited. The security features described in
this document are implemented to varying degrees of completeness. Do not rely on
them as your only safeguard.

**It requires real API keys and real money.**
If you configure cloud LLM providers, Evolve will make API calls on your bots'
behalf. The Better Engine, cost monitoring, and analysis scripts all hit
live endpoints. Set spending limits on your API accounts before you install.

**It is only tested on Mac mini (M4) running macOS 14.**
Other hardware and OS versions may work but are untested. Do not install on a
machine you can't afford to wipe and restore.

**Feedback is the whole point.**
If you're in this group, you're here because you can tell the difference between
something broken and something wrong, and you'll say so. File issues, send messages,
or push fixes. This document and the software will improve faster with your input.

→ To give feedback or report issues: [github.com/evolve-ops/evolve/issues](https://github.com/evolve-ops/evolve/issues)

---

## What Evolve Is

Evolve is the **operating model layer for OpenClaw pods**.

OpenClaw gives you powerful AI bots. Evolve answers the questions that come next:
are they running? what are they costing? are they getting better? is anyone tampering
with them? how do you manage five bots without living in terminal windows?

It runs on the same machine as your bots — no cloud, no new infrastructure — and adds
the pod-level layer that makes running multiple AI assistants practical, observable,
continuously improving, and secure. You can use it purely for operations, or let
the Better Engine run and watch your bots improve on a cadence. Both work independently.

Evolve organizes around four verbs:

- **Operate** — run the pod. Deploy, monitor, heal, route models, cap spend, audit security, manage identities.
- **Extend** — build out what bots can do. Identify capabilities via structured manifests, generate new apps with Forge, install blueprints from the Gallery.
- **Improve** — the Better Engine surfaces the one thing to do next. Generator portfolio competing on track record; every proposal carries a falsifiable claim.
- **Access** — reach Evolve from anywhere. Admin UI, `evolve` keyword in any bot conversation, or Claude Desktop over Tailscale.

The numbered sections below work through each component in detail; sections 1–7 cover
Operate, sections 8–10 cover Improve and Continuity, and section 11 covers Session Quality
(part of Improve). Capability development (Extend) and Access surfaces are described inline
where they intersect each section.

---

## Components

```
packages/
  plugin/     OpenClaw plugin (TypeScript) — runs in-process on every bot
               Annotates turns, routes models, exposes metrics
  analyzer/   Analysis engine (Python) — measures, detects patterns, generates proposals
               ~40 scripts covering metrics, security, healing, testing, Better Engine adaptation
  admin/      evolve-admin CLI + web UI + setup wizard
               One command to set up a pod; dashboard at localhost:5050

docs/         Architecture, specs, deployment guides
applications/ Installable application app blueprints (EA Pack, etc.)
```

**Shared directory** (`/Users/Shared/evolve/`) is the single source of truth for pod
state: annotations, metrics, proposals, keystore, applications, apply-results. All bots
write to it; none can delete each other's files (sticky-bit permissions).

**Account structure:** Bot users (admin-bot, team-bot-a, etc.) do the work. A dedicated `evolve`
macOS user manages and monitors everything — it cannot be influenced by the bots it
manages. The human operator (admin account with sudo) is the only approval authority
for changes to production bots.

---

## The Eight Benefits

### Operations Layer

---

#### 1. Pod Management

Deploy, configure, and monitor a fleet of OpenClaw bots as a unified system.

`evolve-admin setup --fresh` goes from a bare Mac to a running pod in one pass —
it creates user accounts, installs OpenClaw, deploys the plugin, sets up gateways,
configures cron jobs, and provisions the shared directory. From there, every bot is
managed from one place.

- `evolve-admin deploy <bot>` installs or updates the plugin on any bot
- `evolve-admin upgrade` keeps Evolve current across the whole pod
- GUI drag-to-reorder model catalogs per bot

**The problem it solves:** Without this, each bot is a snowflake. Config drifts.
Keys expire on different schedules. You SSH into each account separately to check
if anything is broken.

---

#### 2. Health & Integrations

Continuous visibility into gateway liveness, API key health, channel status, and
OAuth token freshness — across every integration every bot depends on.

- Gateway liveness monitored per bot; `heal.py` auto-restarts failed gateways
- API key health checks: Brave, Google, GitHub, Runway, and any other registered key
- Channel status: Telegram, Slack, Discord
- OAuth token freshness with expiry warnings
- Pod health score (0–100 composite) with historical trend charts
- Alerts via Telegram when something breaks

**The problem it solves:** "Why isn't the bot responding?" is almost always a dead
API key or a crashed gateway. Without monitoring, you find out from a user complaint.
With it, you find out from an alert.

---

#### 3. Usage & Cost

Full spend visibility per bot and per model, smart model routing, and configurable
spend controls — all in one place.

**Visibility:**
- Daily and monthly spend per bot and per model
- Efficiency scoring: cost-per-useful-turn, model tier utilization
- Historical cost trends and anomaly detection
- API billing monitoring (note: Anthropic ended MAX access for third-party tools
  April 4, 2026 — every OC call now costs real API money)

**Smart routing (model tiers):**
Evolve's plugin classifies every session and routes it to the appropriate model tier:

| Tier | Role | Default |
|------|------|---------|
| tier1 | Power (explicit user request) | claude-opus-4-6 |
| tier2 | Workhorse (default user-facing) | claude-sonnet-4-6 |
| tier3 | Grunt (background, analysis) | claude-haiku-4-5 |
| tier0 | Cross-model judge | gpt-4o |

Maintenance sessions auto-route to tier3. Productive sessions stay on tier2.
No code references model names — only tiers — so swapping a model is one line
in `network.json`.

**Spend controls:**
- Per-bot daily caps
- Auto-routing to cheaper models when a bot approaches its budget
- Configurable spend alerts at operator-defined thresholds

**The problem it solves:** API costs accumulate fast and surprises are common.
Running the wrong model for the task both costs more and delivers worse results.

---

#### 4. Security

Evolve monitors your bots, your machine, and the Better Engine pipeline for
security issues — and alerts you when anything deviates from expected state. Most
OpenClaw operators have no visibility into whether their bots' configurations have
drifted, whether their machine's security posture has regressed, or whether an
automated change did something it shouldn't have. Evolve closes those gaps.

**Bot security monitoring**
Each bot's security-relevant state is continuously checked:
- **Exec allowlist** — unexpected new entries flag immediately; this is the most
  common vector for a bot to gain unintended applications
- **Gateway bind address** — should always be `127.0.0.1`; binding to `0.0.0.0`
  exposes the gateway to the local network
- **Identity integrity** — SOUL.md and AGENTS.md are hash-monitored every 15 minutes;
  if a bot's behavioral constraints change outside the proposal pipeline, you find out
- **Config drift** — any change to `openclaw.json` not traceable to an approved
  proposal triggers an immediate alert

**Machine security monitoring**
The host machine is audited on the same 15-minute cycle:
- Firewall status
- SSH configuration (PasswordAuthentication, PermitRootLogin)
- User accounts — new accounts trigger a CRITICAL alert
- Listening ports — any port not in the expected baseline is flagged

**Cost anomaly detection**
Runaway API spend often signals something has gone wrong — a looping prompt, an
unintended scheduled job, a compromised key. Cost anomalies are tracked alongside
security events, not buried in billing dashboards.

**Controlled change management**
When Evolve or the Better Engine proposes a change to a bot, eight hard
rules are enforced automatically — no proposal can override them:
- No 0.0.0.0 gateway binding
- No auth disable
- No modification of Evolve's own scripts
- No credential file writes
- No `sudo` in proposed scripts
- No outbound network calls in proposed scripts
- No writes outside the target bot's workspace
- No launchd plist modifications

Beyond the auto-reject rules, every proposal is HMAC-signed at creation and verified
at every gate. Tampered or externally injected proposals are caught before they can
be applied. Each bot's config is committed to a private GitHub repo nightly; the live
state is diffed against the last commit after every backup, so any unexplained change
is caught within 24 hours and recovery is a single `git checkout`.

**Independent alert channel**
Security alerts use a dedicated Telegram bot token, stored separately from the general
notification channel. If Evolve's general alerts are misconfigured or broken, security
alerts still reach you.

**The problem it solves:** Most OpenClaw operators discover security issues by accident —
a bot starts behaving differently, costs spike, or something just feels off. Evolve
replaces that with continuous, systematic monitoring across every bot and the machine
they run on, with alerts that fire before the damage is done.

---

### Intelligence Layer

---

#### 5. App Framework

A structured system for defining, building, testing, and continuously improving
what your bots actually do.

**Application manifests**
A manifest is a structured contract for an application: what the bot is supposed to do,
how to verify it's working, what its constraints are, and the history of improvements
applied to it. Manifests are auto-detected from workspace evidence (file patterns,
SOUL.md content) and LLM-enriched at scan time. Operators can add test cases, rate
satisfaction, and note issues. The manifest IS the contract the Better Engine
adapts against — it defines what "working" means.

**Testing & QA**
Test types include `file_exists`, `http`, `script`, and `behavioral` (LLM-judged).
Tests have priority levels: `core` (always run, immediate alert on failure), `feature`
(always run, alert after 2 consecutive failures), and `optional` (skipped under budget
pressure). Results are tracked in the dashboard and feed the Better Engine.

**App Gallery & Forge**
The App Gallery is a library of packaged app blueprints: Morning Brief, Email Manager,
Home Controller, Travel Assistant, EA Pack, and more. Installing a gallery app is
forge run #0 — the bot builds everything in its own environment from a spec, not
pre-packaged code. Two bots installing the same app produce different implementations
suited to their workspaces.

Every improvement cycle is another forge run with richer usage data. The engine
doesn't change; the inputs get richer over time. All artifacts carry embedded
provenance markers linking them to their app and the forge run that created them.

**App RSI (Recursive Self-Improvement)**
As a bot uses an app, the forge engine collects usage data, identifies what's working
and what isn't, proposes specific changes, validates them in the Sandbox, and applies
approved changes back to production. The app improves on a cadence without operator
intervention beyond approval.

**The problem it solves:** Bots accumulate applications organically. Without manifests,
no one knows what they're supposed to do or how to verify it works. Without a gallery,
every operator builds the same EA pack from scratch. Without forge, improvements require
manual configuration changes.

---

#### 6. Pod-wide Adaptation (Better Engine)

A recurring cycle that makes the pod measurably better over time — independent of
any individual app.

```
Session happens
  ↓ (plugin, in-process)
TurnObserver annotates every turn (session class, model tier, cost, corrections)
  ↓ (daily 01:00)
measure.py collects daily metrics per bot
  ↓ (weekly Sunday 02:00)
analyze.py runs 12 pattern detectors → generates specific proposals
  ↓ (at ingest)
the arbiter's security screen sends unsafe proposals to human review (review.py retired 2026-08-14)
  ↓ (human action)
Operator approves or rejects in the dashboard
  ↓ (on approval)
arbiter.apply writes the change, health-checks, auto-rolls back if broken
  ↓ (7 days later)
outcome.py measures whether the change helped → feeds back into analyze.py
```

Every proposal includes context: what pattern triggered it, what the expected
improvement is, and what confidence the detector has. The Sandbox validates changes
before they reach production bots. Improvement history tracks what was tried and
what happened.

Proposals also cover `POD_CONDUCT.md` — the universal behavioral contract injected
into every bot's session. Amendments go through the same proposal/approval flow.

**The problem it solves:** Without a feedback loop, bots don't improve — and in most
setups there's no feedback loop. Operators manually tweak prompts when something
feels wrong, with no systematic way to measure whether the tweak helped.

---

#### 7. Continuity Engine

Bot-scheduled follow-ups that make stateless bots keep their promises.

Every session a bot has is stateless — it starts fresh with no memory of prior
conversations beyond what's in its workspace files, and it cannot wait or run
background work. The Continuity Engine bridges that gap:

- The bot schedules its own follow-ups via the `defer` plugin tool the moment
  it commits to acting later ("remind me in 20 minutes", "I'll check the build")
- Each defer stores either a literal message to deliver or an instruction for
  a follow-up turn, with an absolute fire time
- A pod-wide runner fires due defers every 2 minutes as a short agent turn in
  the original conversation

**The problem it solves:** Bots make commitments they don't keep because there's
no mechanism to carry them forward — the promise evaporates when the session
ends. The Continuity Engine makes the bot feel like it remembers, even though
it doesn't.

---

#### 8. Claude Desktop & Dispatch Integration

An MCP Bridge that connects Claude Desktop and Claude Desktop Dispatch to the live
pod over Tailscale VPN — no SSH tunnels required.

Most operators use two tools: an always-on OC pod for ambient, ongoing work via
Telegram, and Claude Desktop for deep keyboard sessions. Without a bridge, these
are completely separate contexts. You repeat everything at the start of every
Claude Desktop session.

With the MCP Bridge:
- Claude Desktop starts with full pod context: workspace memory, pending tasks,
  active proposals, recent metrics
- Notes and context written in Claude Desktop appear in the bot's workspace
- Evolve proposals are reviewable directly from Claude Desktop
- The deep-work tool and the ambient tool share the same context layer

```
Laptop (Claude Desktop / Dispatch)
  → Tailscale VPN
    → Mac mini: Evolve MCP Bridge (port 5051)
      → All bots' workspaces and metrics (read)
      → Designated primary context bot (write — Admin-bot by default)
```

Requires Tailscale on both the Mac mini and the operator's laptop. Configured via
`evolve-admin`.

---

## Before You Install

Full checklist with setup instructions: [pre-install-checklist.md](pre-install-checklist.md)

### Hardware

| | Minimum | Recommended |
|---|---|---|
| Machine | Mac mini M4 | Mac mini M4 Pro |
| RAM | 16GB | 24GB |
| Storage | 256GB SSD | 512GB SSD |
| Network | WiFi | Wired ethernet |

macOS 14 (Sonoma) or later. One admin account with sudo access. Remote login enabled
for SSH.

### Accounts

**Required: one LLM provider (pick at least one)**

*Cloud providers:*

| Provider | How to get a key | Notes |
|---|---|---|
| Anthropic | console.anthropic.com → API Keys | MAX subscription also works; set a spend limit |
| OpenAI | platform.openai.com → API Keys | GPT-4o, o1, etc. |
| Google Gemini | aistudio.google.com → API Keys | Gemini 2.0 Flash/Pro |
| xAI (Grok) | console.x.ai → API Keys | Grok-2 |
| Mistral | console.mistral.ai → API Keys | Mistral Large/Small |

*Local / open-source:*

| Runner | Models | Notes |
|---|---|---|
| Ollama | Llama 3, Qwen, Mistral, Gemma, Phi, and more | Easiest local setup; runs on the Mac mini itself |
| LM Studio | Same model library as Ollama | GUI-based; good for experimenting with models |
| Any OpenAI-compatible endpoint | Anything | Point OpenClaw at the local URL; no API key needed |

Running a local model on the same Mac mini as your bots works well for lighter tasks
(tier3 analysis, background jobs). For user-facing work, a cloud provider typically
gives better results. A common setup is local models for tier3, cloud for tier2/tier1.

You can register multiple providers for failover routing or cost optimization. The setup
wizard walks you through which models map to which tiers.

**Required: one messaging channel (pick at least one)**

| Channel | Best for | Setup time |
|---|---|---|
| Telegram | Personal use, single operator | 5 min via @BotFather |
| WhatsApp | Personal use, existing WhatsApp users | 10 min via Meta Business API |
| Slack | Team bots, workplace assistants | 15 min via Slack app config |
| Discord | Community bots, developer setups | 10 min via Discord Developer Portal |

Each bot gets its own token. You can mix channels across bots in the same pod.

**Strongly recommended:**
- Brave Search API key — web search tool for your bots (free tier: 2,000 queries/month)
- Google OAuth credentials JSON — unlocks Gmail, Calendar, Drive, Sheets, Docs
- Tailscale — admin UI access from your laptop + MCP Bridge; free for personal use
- Private GitHub repo — security backup and drift detection

**Optional (add after your first bot is running):**
- Dedicated security alert channel token — separate from general notifications
- GitHub personal access token — for coding bots (`repo`, `read:org` scopes)
- ElevenLabs API key — text-to-speech
- Runway API key — AI video
- Perplexity API key — alternative web search with citations
- Home Assistant long-lived token — smart home control

### Software Prerequisites

Before running the setup wizard:
- Python 3.10+ (macOS system Python is 3.9 — `brew install python@3.12`)
- Node.js 20+ (`brew install node`)
- Evolve repo cloned to `/Users/Shared/evolve-repo/`

```bash
sudo git clone https://github.com/evolve-ops/evolve /Users/Shared/evolve-repo
sudo $(brew --prefix)/bin/python3.12 -m venv /Users/Shared/evolve-venv
# Analyzer first (admin depends on it), compat-editable
sudo /Users/Shared/evolve-venv/bin/pip install -e /Users/Shared/evolve-repo/packages/analyzer/ --config-settings editable_mode=compat
sudo /Users/Shared/evolve-venv/bin/pip install -e /Users/Shared/evolve-repo/packages/admin/
sudo mkdir -p /usr/local/bin && sudo ln -sf /Users/Shared/evolve-venv/bin/evolve-admin /usr/local/bin/evolve-admin
sudo evolve-admin setup --fresh
```

### Time Estimates

| Task | Time |
|---|---|
| LLM provider API key (any) | 5 min |
| Messaging channel token (Telegram/Discord/Slack/WhatsApp) | 5–15 min |
| Brave Search API key | 5 min |
| Google OAuth setup | 45 min |
| GitHub token | 5 min |
| Tailscale | 15 min |
| **Total (minimal — one LLM + one channel)** | **~15 min** |
| **Total (full setup)** | **~90 min** |
| **Actual wizard run (keys in hand)** | **~30 min** |
