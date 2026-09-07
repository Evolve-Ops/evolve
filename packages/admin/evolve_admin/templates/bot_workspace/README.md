# {bot_id} workspace

This is the OpenClaw workspace for **{bot_id}**, a {role} bot in this pod.

## Files in this directory

- `SOUL.md` — the bot's identity, tone, and boundaries.
- `AGENTS.md` — agents, tools, and capabilities available to this bot.
- `MEMORY.md` — durable facts the bot should remember across sessions.
- `README.md` — this file.

These files are loaded into every session via OpenClaw's contextFiles
mechanism. Edit `SOUL.md` and `AGENTS.md` to customize this bot's behavior;
`MEMORY.md` grows over time as the bot learns about its users.

## Provisioned by Evolve

This bot was created by `evolve-admin setup`. The Evolve plugin manages its
gateway, runs health checks, and proposes configuration improvements. See the
Evolve admin UI for the operator surface.
