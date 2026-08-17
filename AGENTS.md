# AGENTS.md — Evolve Repo Agent Instructions

> **Scope check — is this file for you?** This file is the charter for the
> **in-pod repo-ops bot**: an agent that runs *inside a deployed pod* as a bot
> user, operating on the deploy checkout. If you are an agent **helping a
> human install Evolve**, read [docs/help/install-with-an-agent.md](docs/help/install-with-an-agent.md)
> instead. If you are an agent **developing Evolve** from a laptop clone, read
> [CLAUDE.md](CLAUDE.md) and [ONBOARDING.md](ONBOARDING.md).

## Who you are
You are a coding agent working on the Evolve project.
Repo: /Users/Shared/evolve-repo/
You run as the pod's repo-ops bot user (the actual username is configured
in `/Users/Shared/evolve/network.json` and varies per pod deployment).

## Ownership Model
READ THIS: docs/local-deployment-architecture.md

Short version:
- You can read/write anything in /Users/Shared/evolve-repo/ (your user owns it)
- You can read/write /Users/Shared/evolve/ (runtime data, your user owns it)
- You CANNOT write to /Library/LaunchDaemons/ (needs sudo)
- You CANNOT read /Users/{bot}/.openclaw/ directly (permissions)
- You CANNOT change file ownership (needs sudo)

## When you need sudo
Write a bash script to /Users/Shared/evolve/apply_{task_name}.sh and end your
output with:
"Run to complete: sudo bash /Users/Shared/evolve/apply_{task_name}.sh"

Never attempt inline sudo. Never claim you ran sudo when you can't.

## Where code lives
- Analyzer scripts: packages/analyzer/
- Admin CLI + UI: packages/admin/evolve_admin/
- OC Plugin: packages/plugin/
- Docs: docs/
- Runtime data (NOT in repo): /Users/Shared/evolve/

## Python
Always use: /Users/Shared/evolve-venv/bin/python3
Never use: /usr/bin/python3 or python3 (might be wrong version/missing deps)

## Heartbeat Design Rule
If implementing heartbeat behavior, use the /api/heartbeat/check endpoint.
Only alert if should_alert is true. Otherwise return HEARTBEAT_OK silently.
"Silence is signal. Noise trains people to ignore alerts."

## Wiring Silence-First Heartbeats to a Bot

If a bot using Evolve wants silence-first heartbeats, add this to HEARTBEAT.md:

```
## Silence-First Design
Before sending any alert, check: GET http://localhost:{evolve_port}/api/heartbeat/check
Only send a message if should_alert is true.
When nothing needs attention: reply HEARTBEAT_OK only.
```

The evolve_port is typically 5050 (admin UI port). The check returns:
- should_alert: true/false
- reasons: list of specific issues (gateway down, spend threshold, stale crons, old proposals)
- checked_at: ISO timestamp

A copy of this integration note is at /Users/Shared/evolve/heartbeat-integration-note.md

## Committing
Always commit and push when done:
  git add -A
  git commit -m "..."
  git push
