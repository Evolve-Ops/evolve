# Evolve

> Run a fleet of AI agents on your own hardware. Local, private, installable as an app on every device.

[![License: BSL 1.1](https://img.shields.io/badge/License-BSL%201.1-blue.svg)](LICENSE)
[![Status: Alpha](https://img.shields.io/badge/Status-Alpha-orange.svg)]()
[![DCO](https://img.shields.io/badge/DCO-required-green.svg)](CONTRIBUTING.md)

<!-- TODO: screenshot of the admin UI here once evo-screen-overview.png is generated for the new deployment -->

[OpenClaw](https://openclaw.ai) gives you powerful AI agents that run locally. Evolve is the layer on top: a packaging system that turns a handful of OpenClaw bots into a **pod** — a real app, managed and improving on its own, that you install on your phone, tablet, and laptop. Same code everywhere, no app stores.

Your data stays local. Nothing irreversible happens without your approval — reversible, bounded changes can apply themselves and roll back automatically if they fail their own check. Vigilant by default. Friendly by design.

> ⚠️ **Alpha software.** Runs on any Mac dedicated to the job — a Mac mini or a retired Apple-Silicon laptop (the wizard configures power/sleep); Intel Macs are best-effort. Tested on Mac mini M4 / macOS 14. Will likely break an existing OpenClaw setup. Back up your bot configs first. See [pre-install checklist](docs/pre-install-checklist.md).

## Is this for you?

Evolve is built for people who:

- Can install Plex, run Home Assistant, or set up Tailscale without a guide
- Want AI agents working for them but don't trust their data to a cloud service
- Have one or many bots and need them to feel like a system, not a pile of terminal sessions
- Are comfortable with "alpha" meaning *real bugs you'll report*, not *polished beta*

If you'd rather pay $20/mo and not think about it, Evolve isn't there yet. If you'd rather own the box, run it on your terms, and tune it as you go — read on.

## What it does

- **A bot for every project.** Spin one up for the renovation, the foundation, the case, the college search. Archive when done. Adding a bot is cheap enough to do for a single project — **a bot for that, in five minutes.**
- **Skills become apps.** OpenClaw gives you skills (Gmail, Calendar, Telegram). Evolve organizes them into **applications** — each one a structured contract: a goal, the parts it depends on, and a continuous check that what's installed still matches what it promised.
- **Continuous improvement.** Around two dozen coaches watch the pod and propose changes. Every proposal carries a falsifiable claim that the system checks after it applies, and each coach's track record raises or lowers how much authority it gets.
- **Circuit breakers.** Per-bot spend caps and loop detection. A tripped bot halts cleanly behind a big Reactivate button while the rest of the pod keeps running. Auto-trip ships in observe-only mode during calibration — you decide when to arm it.
- **Three surfaces.** The PWA admin UI on any device. Conversational access in any bot ("evo, what's pending?"). Claude Desktop bridge for deep-work sessions over Tailscale.

→ See [the product site](https://evolveops.dev/) for screenshots and the full story.

## Quick start

```bash
# Clone to shared location
sudo git clone https://github.com/evolve-ops/evolve /Users/Shared/evolve-repo

# Create venv and install the analyzer + admin CLI (analyzer first — admin
# depends on it; compat mode keeps "git pull is the deploy" working)
sudo python3 -m venv /Users/Shared/evolve-venv
sudo /Users/Shared/evolve-venv/bin/pip install -e /Users/Shared/evolve-repo/packages/analyzer/ --config-settings editable_mode=compat
sudo /Users/Shared/evolve-venv/bin/pip install -e /Users/Shared/evolve-repo/packages/admin/

# Run the setup wizard — bare Mac to running pod in one pass
sudo evolve-admin setup --fresh

# Open admin UI
evolve-admin serve --open
```

**Minimum viable pod:** a Mac dedicated to this job (a mini, or a retired Apple-Silicon laptop) + Anthropic API account + Telegram bot token. ~30 minutes when you have the keys ready.

→ Full walkthrough: [docs/getting-started.md](docs/getting-started.md)
→ Prerequisites checklist: [docs/pre-install-checklist.md](docs/pre-install-checklist.md)

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

## Capabilities at a glance

**Operate** — bot lifecycle, gateway health, model routing, spend caps, circuit breakers, security audits, identity model.

**Improve** — skills catalog (Gmail, Calendar, Slack, Telegram, Discord, Obsidian, GitHub, …), application manifests and forge, a ~two-dozen-coach generator portfolio, falsifiable claims with verify daemon.

**Access** — installable PWA on phone/tablet/laptop with push notifications and offline state, `evolve` keyword in any bot conversation, MCP bridge for Claude Desktop, contextual in-app help.

→ [docs/use-cases.md](docs/use-cases.md) for example applications.
→ [Product site](https://evolveops.dev/) for the full feature tour.

## Status

Pre-1.0. Active development. Things will break. File issues using the in-app "Send feedback" button — it pre-fills a GitHub issue with your environment and a diagnostic snapshot.

- [Roadmap](https://evolveops.dev/roadmap.html)
- [Discussions](https://github.com/evolve-ops/evolve/discussions)
- [Bug reports](https://github.com/evolve-ops/evolve/issues/new?template=bug_report.yml)

## Contributing

Source-available under the Business Source License 1.1. See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution flow, DCO sign-off requirement (`git commit -s`), and what kinds of PRs we're accepting at this stage. [AGENTS.md](AGENTS.md) has everything a coding agent needs to get oriented: architecture, file map, conventions, and safe zones.

For security issues, do not file public issues — see [SECURITY.md](SECURITY.md).

## License

[Business Source License 1.1](LICENSE). Free for non-commercial use. Each version auto-converts to Apache License 2.0 four years after publication.

Existing clones obtained under MIT (prior to 2026-05-30) retain their MIT rights to that snapshot — license changes are not retroactive.
