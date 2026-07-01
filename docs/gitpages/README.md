# Evolve

**The operating model layer for OpenClaw pods.**

OpenClaw gives you powerful AI bots. Evolve answers the questions that come next: are they running? what are they costing? are they getting better? is anyone tampering with them? how do you manage five bots without living in terminal windows?

Evolve runs on the same machine as your bots — no cloud, no new infrastructure — and adds the pod-level layer that makes running multiple AI assistants practical, observable, continuously improving, and secure.

---

> ⚠️ **Alpha software. Read the [warnings](#before-you-install) before installing.**
> Built for macOS 14+ (tested on Mac mini M4); Linux (Ubuntu) supported. Will likely break an existing OpenClaw setup.
> Back up your bot configs first.

---

## What It Does

Four layers of capability. Operate the pod. Extend what bots can do. Improve the system on a cadence. Access it all from anywhere.

### Operate — run your pod

| Capability | What it does |
|---|---|
| **Bot lifecycle** | One-command deploy from bare Mac to running pod (`evolve-admin setup --fresh`). Safe-upgrade gates every OpenClaw update. Pod Conduct — a shared behavioral contract — is injected into every bot on deploy. |
| **Health & integrations** | Gateway liveness, channel health, API key & OAuth freshness — all bots, all integrations. Auto-restarts failed gateways. Custom alerts and reports. |
| **Resources** | Daily/monthly spend per bot and per model. Smart tier routing — Haiku for maintenance sessions, Sonnet for productive ones — saves ~60% on background work. Graduated cost-cap remediation ladder (warn → tier downgrade + cron pause → hard L1 breaker). Caps roll at midnight in the pod's local timezone. |
| **Security & access** | HMAC-signed proposals. 15-minute audits with drift detection. Approval gate for every change. Independent security alert channel. Identity model separates admin, primary user, and secondary-user roles. An Intentional Deviations page lets you mark a setting as deliberate so generators stop pitching reverts. |
| **Issues & feedback loop** | `evo improve "this could be better"` from any thread captures and drafts an issue; promote when ready and it lands on GitHub. Inbound issues filed by others on repos you maintain get LLM-triaged and queued for review, with an opt-in auto-response policy and a 24-hour undo. |
| **OpenClaw admin coverage** | Inventoried, baselined, proposal-gated coverage of every OpenClaw config surface: MCP servers (curated catalog + GHSA advisory feed), plugins (install-provenance scoring + signed-bypass for first-party plugins), webhook ingress + per-plugin typed-hook policies (catches the silent-disable class), and a content scanner over the markdown files bots read at session start — HTML-comment injection, zero-width Unicode, authority-impersonation, encoded payloads, structural emptiness. Mark-Reviewed suppression flow. |

### Extend — build out what bots can do

| Capability | What it does |
|---|---|
| **Identification & cataloging** | Structured manifests describe what each bot actually does. Viewer and editor in the dashboard. Usage correlation links every session back to the apps it touched. |
| **Forge** | Generate new capability apps from a spec. LLM-enriched manifests, test cases generated alongside, an interactive feedback loop while you build, and signed files-pack bundles for community packages. |
| **Gallery** | Installable blueprints. One-click setup for common patterns — calendar sync, GitHub integration, journal, contacts, EA pack, email, Morning Briefing, Task Manager. Apps inherit each bot's LLM stack rather than credentialing themselves. |
| **App Coherence & Reconciliation** | Manifest fields are typed and tagged with provenance (authored vs observed). Three checks run before deploy: reconciliation against the filesystem, coherence on the manifest's own claims, and the existing Tier 3 audit. `evo app-changes` and `evo app-scan` drive it from chat. |
| **Skills via vetted MCP** | Google Workspace suite (Gmail + Calendar + Docs + Sheets + Drive — read + write), Notion, Linear, Zoom, GitHub-MCP land via a bundled-plugin pattern. Each catalog entry is a single registry file. |
| **Capability tests** | Verify claimed capabilities still work. Test runner alongside each app. Failures surface through the Better Engine. |

### Improve — the Better Engine

| Capability | What it does |
|---|---|
| **Session analysis** | Every turn classified — productive, maintenance, or ambiguous. Daily metrics aggregated. Quality drift surfaces through the Better Engine before it becomes chronic. |
| **Continuity engine** | Tasks extracted from session transcripts and surfaced at the next session start. Recurring tasks. Approval UI. Makes stateless bots feel continuous. |
| **Generator portfolio** | Specialized generators — guardians (Sysadmin Watchdog, Budget Hawk, Security Warden), optimizers competing on track record, cross-bot coaches (engagement_amplifier, pod_capability_lift) that pattern-match across the fleet, and a meta-guardian (Evolve Watchdog) that watches the system itself. An anti-domain layer lets you mark a topic out of scope so generators stop fighting your upstream choices. |
| **Config Intent** | When you set a config value, evo records why so generators stop proposing reverts on deliberate deviations. Intent-aware generators (`cron_caps_filler` and the rest) consult the intent ledger before drafting a proposal. |
| **Verify & profile** | Every proposal carries a falsifiable claim and is graded against it; generators gain or lose authority by outcome. Per-bot dimension weights drive ranking, editable with audit. Changes always route to a human before they touch a production bot. |

### Access — use Evolve from anywhere

| Capability | What it does |
|---|---|
| **Admin UI** | Local-only web dashboard at `localhost:5050`. Every surface — pod health, costs, proposals, generators, profiles, applications. No cloud. Heavy turns stream as evo works on them. |
| **evo conversational** | Type `evo` in any bot conversation, DM evo on Telegram, or use the chat panel that follows you across the dashboard. Same brain everywhere. Per-conversation tier control (Auto / Fast / Standard / Power) trades cost for depth on the fly. |
| **Claude Desktop bridge** | MCP server connects Claude Desktop to the live pod over Tailscale. Deep-work sessions start with full pod context — workspace memory, pending tasks, active proposals. |
| **Help corpus & feedback loop** | A first-party evolve-knowledge corpus drives in-context help on every page and a public help site. `evo improve` captures feedback into the Issues queue. |

---

## Architecture

```
Layer 1: Bot users (e.g. personal-assistant-bot, project-manager-bot)
         → Do the actual work. Each runs an OC gateway. Evolve monitors them.

Layer 2: evolve user (dedicated OS user)
         → Manages and improves the pod.
         → Runs admin server, cron jobs, analysis, proposals, security audits.
         → Cannot be influenced by the bots it manages.

Layer 3: Admin user (you)
         → Has sudo access. Approves proposals, manages keys, deploys updates.
         → The only human in the loop.
```

Bots cannot influence their own management layer. Every change to a production bot requires human approval.

---

## Before You Install

**This is beta software.** Specific things to understand:

- **It will likely break your existing OpenClaw setup.** Evolve installs deeply — new system accounts, launchd jobs, shared directories, OpenClaw config files. Back up any bot config you care about before running the wizard.
- **It may introduce security issues.** The security features are implemented but not fully audited. Do not rely on them as your only safeguard.
- **It costs real money.** If you configure cloud LLM providers, Evolve makes API calls on your bots' behalf. Set spending limits on your API accounts before installing.
- **Built for macOS 14+ (tested on Mac mini M4); Linux (Ubuntu) is supported.** Don't install on a machine you can't afford to wipe.

### Hardware

| | Minimum | Recommended |
|---|---|---|
| Machine | Any Mac (macOS 14+) | Mac mini M4 / M4 Pro |
| RAM | 16GB | 24GB |
| Storage | 256GB SSD | 512GB SSD |
| Network | WiFi | Wired ethernet |

macOS 14 (Sonoma) or later. One admin account with sudo access.

### Required Accounts

**LLM provider (pick at least one):**

| Provider | How to get a key |
|---|---|
| Anthropic | console.anthropic.com → API Keys |
| OpenAI | platform.openai.com → API Keys |
| Google Gemini | aistudio.google.com → API Keys |
| Ollama (local) | ollama.com — runs on the pod host itself |

**Messaging channel (pick at least one):**

| Channel | Setup time |
|---|---|
| Telegram | 5 min via @BotFather |
| Slack | 15 min via Slack app config |
| Discord | 10 min via Discord Developer Portal |

**Strongly recommended:**
- Brave Search API key (web search for your bots — free tier: 2,000 queries/month)
- Google OAuth credentials JSON (Gmail, Calendar, Drive, Sheets)
- Tailscale (admin UI from laptop + MCP Bridge — free for personal use)
- Private GitHub repo (security backup and drift detection)

### Software Prerequisites

```bash
python3 --version   # needs 3.9+
node --version      # needs 20+ (brew install node)
```

---

## Install

```bash
# Clone to shared directory
sudo git clone https://github.com/evolve-ops/evolve /Users/Shared/evolve-repo

# Set up Python environment
sudo python3 -m venv /Users/Shared/evolve-venv
sudo /Users/Shared/evolve-venv/bin/pip install -e /Users/Shared/evolve-repo/packages/admin/

# Run the setup wizard
sudo evolve-admin setup --fresh
```

Estimated time with all accounts and keys ready: **~30 minutes.**
Estimated time gathering accounts from scratch: **~90 minutes.**

Full pre-install checklist: [docs/pre-install-checklist.md](docs/pre-install-checklist.md)

---

## After Install

The admin dashboard runs at `http://localhost:5050`.

```bash
evolve-admin status          # pod health summary
evolve-admin deploy <bot>    # install/update plugin on a bot
evolve-admin upgrade         # update Evolve across the pod
```

---

## Repository Layout

```
packages/
  plugin/     OpenClaw plugin (TypeScript) — runs in-process on every bot
  analyzer/   Analysis engine (Python) — metrics, security, RSI generators, verify daemon
  admin/      evolve-admin CLI + web UI + setup wizard + MCP Bridge

docs/         Architecture specs and deployment guides
applications/ Installable app blueprints
tools/        Evolve Test Rig (ETR) — regression-detection harness
```

---

## A Note on Billing

As of April 2026, Anthropic ended MAX subscription access for third-party tools including OpenClaw. All OpenClaw instances now require API keys (pay-per-use).

This makes Evolve's cost management features more important, not less. Set spend limits on your Anthropic account before installing.

---

## Contributing & Feedback

This is a beta. Feedback is the whole point.

→ **Bug reports:** [open an issue](https://github.com/evolve-ops/evolve/issues/new?template=bug_report.md)
→ **Feature requests:** [open an issue](https://github.com/evolve-ops/evolve/issues/new?template=feature_request.md)
→ **Questions:** [start a discussion](https://github.com/evolve-ops/evolve/discussions)

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to be useful as a beta tester.

---

## License

Business Source License 1.1 — see [LICENSE](LICENSE). Free for non-commercial use; each version auto-converts to Apache License 2.0 four years after publication.

---

*Not affiliated with Anthropic or the OpenClaw project.*
