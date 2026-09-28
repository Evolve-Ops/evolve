# Evolve

> Run a fleet of AI agents on your own hardware — with spend caps that actually stop the spending, and changes that verify themselves or roll back. Local, private, installable as an app on every device.

[![License: BSL 1.1](https://img.shields.io/badge/License-BSL%201.1-blue.svg)](LICENSE)
[![Status: Alpha](https://img.shields.io/badge/Status-Alpha-orange.svg)]()

[OpenClaw](https://openclaw.ai) gives you powerful AI agents that run locally. The hard part is no longer running agents — dashboards, cron management, and approval UIs are everywhere now. The hard part is leaving them running unattended. Evolve is the **enforcement layer**: it turns a handful of OpenClaw bots into a **pod** — a real app, managed and improving on its own, that you install on your phone, tablet, and laptop — and wraps every bot in limits that are enforced by the system, not charted on a dashboard for you to notice.

Your data stays local. Nothing irreversible happens without your approval — reversible, bounded changes can apply themselves and roll back automatically if they fail their own check. Vigilant by default. Friendly by design.

> ⚠️ **Alpha software.** Runs on a machine dedicated to the job — a Mac mini or retired Apple-Silicon laptop (the wizard configures power/sleep; Intel Macs are best-effort), or a dedicated Ubuntu/Debian box or VPS. Tested on Mac mini M4 / macOS 14 and Ubuntu 24.04. Will likely break an existing OpenClaw setup. Back up your bot configs first (`~/.openclaw/` per bot). See the [install hub](docs/help/installation.md).

## Your agents can't bankrupt you

The signature failure of autonomous agents is a loop at 3 a.m. billing tokens nobody asked for. Most stacks *show* you spend; Evolve *enforces* it, with a graduated per-bot ladder that fires automatically:

- **See it coming.** Warn thresholds plus an intraday velocity forecast — if today's spend rate projects to cross a bot's cap by midnight, you get the alert while there's still a knob to turn.
- **Hard daily cap (L1 breaker).** Cross the cap and a cost breaker trips automatically: heartbeat and background sessions stop, remaining chat turns are downgraded to the cheapest tier, and you're alerted with the day's spend and the path to raise or lift the cap. Caps ship on by default, in code — a bot you never got around to configuring inherits the pod default and is still capped.
- **Hard stop (L2 breaker).** A second, higher rung halts the bot's gateway entirely. Manual reset only.

A tripped bot halts cleanly behind a big Reactivate button while the rest of the pod keeps running. The pod's self-heal loop treats a tripped bot as intentionally down — it won't revive it — and timed trips auto-clear when they expire. Breakers can also be tripped by hand, per bot or pod-wide, from the UI or CLI. The separate runaway-activity detector (loop-shaped usage against each bot's own baseline) ships observe-only during calibration — it logs what *would* have tripped, and you decide when to arm auto-trip.

## Changes are verified or rolled back

Around two dozen coaches watch the pod and propose improvements — config tuning, model routing, app repairs. What makes that safe to run unattended is that proposals are falsifiable, not vibes:

- **Every proposal carries a falsifiable claim** — a metric, an expected direction, a window. Not "this should help": a check the system can run.
- **A verify daemon evaluates the claim after the change applies.** A change that fails its own claim is automatically reverted and flagged. Pure measurement, no LLM in the loop.
- **Track record gates authority.** Each coach's verified wins and losses raise or lower how much it's allowed to do on its own. Reversible, bounded changes can apply themselves; anything irreversible waits for your approval.
- **Canary releases (opt-in).** Flip the pod to canary mode and code updates soak on a designated canary bot behind static checks before the fleet moves; a new firing alert on the canary during the soak fails the gate. One command rolls the pod back.
- **Apps are conformance-scanned.** Skills (Gmail, Calendar, Telegram) get organized into **applications** — each a structured contract: a goal, the parts it depends on, and a continuous check that what's installed still matches what it promised.

## Is this for you?

Evolve is built for people who:

- Can install Plex, run Home Assistant, or set up Tailscale without a guide
- Want AI agents working for them but don't trust their data to a cloud service
- Have one or many bots and need them to feel like a system, not a pile of terminal sessions
- Are comfortable with "alpha" meaning *real bugs you'll report*, not *polished beta*

If you'd rather pay $20/mo and not think about it, Evolve isn't there yet. If you'd rather own the box, run it on your terms, and tune it as you go — read on.

## Also in the box

- **A bot for every project.** Spin one up for the renovation, the foundation, the case, the college search. Archive when done. Adding a bot is cheap enough to do for a single project — **a bot for that, in five minutes.**
- **Installable PWA admin UI** on phone, tablet, and laptop — push notifications, offline state, same code everywhere, no app stores.
- **Cost dashboards** — per-bot spend against its cap, so you can see how close any bot is to tripping.
- **Conversational access in any bot** ("evo, what's pending?") and an MCP bridge for Claude Desktop deep-work sessions over Tailscale.
- **Operations coverage** — bot lifecycle, gateway health, model routing, security audits, identity model, contextual in-app help.
- **Skills catalog** — Gmail, Calendar, Slack, Telegram, Discord, Obsidian, GitHub, and more, organized into applications with manifests and a forge.

→ See [the product site](https://evolveops.dev/) for screenshots and the full story.
→ [docs/use-cases.md](docs/use-cases.md) for example applications.

## Quick start

macOS (Linux/VPS: see [the Linux install guide](docs/help/install-linux-vsp.md)):

```bash
# 0. Prerequisites: Python 3.10+ and Node 20+ (macOS system Python is 3.9 — too old)
brew install python@3.12 node

# 1. Clone to the shared deploy location
sudo git clone https://github.com/evolve-ops/evolve /Users/Shared/evolve-repo

# 2. Create venv and install the analyzer + admin CLI (analyzer first — admin
# depends on it; compat mode keeps "git pull is the deploy" working)
sudo $(brew --prefix)/bin/python3.12 -m venv /Users/Shared/evolve-venv
sudo /Users/Shared/evolve-venv/bin/pip install -e /Users/Shared/evolve-repo/packages/analyzer/ --config-settings editable_mode=compat
sudo /Users/Shared/evolve-venv/bin/pip install -e /Users/Shared/evolve-repo/packages/admin/
sudo mkdir -p /usr/local/bin && sudo ln -sf /Users/Shared/evolve-venv/bin/evolve-admin /usr/local/bin/evolve-admin

# 3. Run the setup wizard — bare Mac to running pod in one pass
sudo evolve-admin setup --fresh

# 4. Pair your browser with the admin UI (already running as a service)
sudo evolve-admin pair
open http://127.0.0.1:5050
```

**Minimum viable pod:** a machine dedicated to this job (Mac mini, retired Apple-Silicon laptop, or Ubuntu box/VPS) + an account with a supported LLM provider (Anthropic, OpenAI, Google, or xAI — or any OpenAI-compatible endpoint) + Telegram bot token. ~30 minutes when you have the keys ready.

→ Full walkthrough: [docs/help/installation.md](docs/help/installation.md) (install hub) → [macOS](docs/help/install-macos.md) | [Linux/VPS](docs/help/install-linux-vsp.md)
→ Installing with an AI coding agent (Claude Code etc.): [docs/help/install-with-an-agent.md](docs/help/install-with-an-agent.md)

## Development setup

Dev machines use [uv](https://docs.astral.sh/uv/) — one command installs both
Python packages (editable) plus the locked test/lint toolchain from `uv.lock`:

```bash
uv sync          # creates .venv with evolve-admin, evolve-analyzer, pytest, ruff, pyright
uv run python -m pytest packages/admin     # run a suite
```

Deploy boxes do NOT use uv — they keep the pip editable installs above
(compat mode is load-bearing; see `packages/analyzer/pyproject.toml`).

## Architecture

Evolve runs on the same machine as your bots — no cloud, no new infrastructure.

```
packages/
  plugin/     OpenClaw plugin (TypeScript) — runs in-process, annotates turns, routes models
  analyzer/   Analysis engine (Python) — measures, detects patterns, generates proposals
  admin/      evolve-admin CLI + web UI + setup wizard
docs/         Architecture, deployment, configuration, help
```

Three user accounts on the deployment box separate concerns:

- **Bot users** run the actual OpenClaw gateways. Each bot has its own macOS account.
- **`evolve` user** runs the management layer (admin server, cron jobs, analysis). It can read every bot's state but bots cannot influence it.
- **Admin user (you)** has sudo. Approves proposals, manages keys, deploys updates.

→ [docs/local-deployment-architecture.md](docs/local-deployment-architecture.md) for the deep dive.

## Status

Pre-1.0. Active development. Things will break. File issues using the in-app "Send feedback" button — it files (or pre-fills) a GitHub issue with your environment details and saves a diagnostic snapshot locally that you can attach. See [docs/help/feedback.md](docs/help/feedback.md).

- [Roadmap](https://evolveops.dev/roadmap.html)
- [Discussions](https://github.com/evolve-ops/evolve/discussions)
- [Bug reports](https://github.com/evolve-ops/evolve/issues/new?template=bug_report.yml)

## Contributing

Source-available under the Business Source License 1.1. See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution flow, DCO sign-off (`git commit -s`, encouraged but not currently enforced), and what kinds of PRs we're accepting at this stage. If you're pointing a coding agent at this repo: [CLAUDE.md](CLAUDE.md) is for *developing* Evolve, [docs/help/install-with-an-agent.md](docs/help/install-with-an-agent.md) is for *installing* it, and [AGENTS.md](AGENTS.md) is the in-pod repo-ops bot's charter.

For security issues, do not file public issues — see [SECURITY.md](SECURITY.md).

## License

[Business Source License 1.1](LICENSE). Free for non-commercial use. Each version auto-converts to Apache License 2.0 four years after publication.

Existing clones obtained under MIT (prior to 2026-05-30) retain their MIT rights to that snapshot — license changes are not retroactive.
