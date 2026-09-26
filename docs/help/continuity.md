---
title: "Help: Continuity Engine"
slug: continuity
audience: public
last_reviewed: 2026-07-31
concepts:
  - continuity-engine
  - defer-tool
  - defer-queue
  - defer-runner
ui_surface: null   # background-system explainer; the continuity engine has no dedicated sidebar page
related_specs: []
---

# Help: Continuity Engine

The Continuity Engine (CE) is the background system that makes bots follow
through on commitments across sessions. When a bot says "remind me in 20
minutes" or "I'll check the build and let you know," CE is what makes that
follow-up actually happen — a bot session has no persistence of its own.

There is no dedicated Continuity page anymore. The engine is designed to be
invisible; its only dashboard surface is a one-line runner health summary on
the **Maintenance** page.

---

## How it works

1. **The bot schedules its own follow-ups.** During a normal conversation,
   when a bot commits to acting later, it calls its `defer` tool with an
   absolute fire time and either a literal **message** to deliver, or an
   **action** — an instruction for a follow-up turn.
2. **The commitment is stored in the bot's own queue** — a small JSONL file in
   the bot's workspace (`~/.openclaw/workspace/evolve/defer-queue.jsonl`).
3. **A pod-wide runner fires due rows.** Every 2 minutes, the defer runner
   checks each bot's queue and fires anything due by starting a short agent
   turn in the original conversation: a `message` defer delivers the stored
   text; an `action` defer does the stored instruction and replies.

Earlier versions of the engine tried to *extract* commitments from session
transcripts after the fact and ran them through an approval queue. That
design is gone — the bot now schedules explicitly at the moment it commits,
which is both more reliable (single-turn commitments aren't missed) and safer
(nothing is inferred from transcripts, and there is no CE-owned code
execution path at all).

---

## Common questions

**Why is there no approval queue?**
A fired defer is just a normal agent turn in the bot's own session — the same
tool gates and permissions as any other turn, delivering a follow-up the bot
promised you out loud. There's no extracted code to review, so there's
nothing to approve.

**What does a fired defer cost?**
One short agent turn per fire (for a `message` defer, a very cheap one).

**How precise is the timing?**
The runner cycles every 2 minutes, so a fire lands within about ±2 minutes of
the scheduled time.

**Can I turn it off for a bot?**
Yes, two ways:
- Toggle the **Continuity Engine** switch on the bot's card on the Bot Config
  page (stored as `bots.<id>.continuity_engine.enabled` in `network.json`,
  default on) — the runner then skips that bot's queue.
- Lower the bot's Evolve integration `tier` below `full` — the `defer` tool
  is then not offered to the bot at all.

**How do I see what a bot has scheduled?**
Ask the bot — it can tell you what it deferred. On the pod host, the queue is
plain JSONL:
```bash
cat /Users/<bot-id>/.openclaw/workspace/evolve/defer-queue.jsonl    # pending
cat /Users/<bot-id>/.openclaw/workspace/evolve/defer-archive.jsonl  # fired/failed
```

**A follow-up never arrived — where do I look?**
1. The Maintenance page's defer-runner health line (is the runner alive and
   cycling?).
2. The bot's archive file (did the row fire and fail? the `result` field has
   the error).
3. The runner log at `/Users/evolve/.openclaw/logs/evolve-defer-runner.log`.
4. Whether the bot actually called `defer` at all — if it just *said* "I'll
   remind you" without scheduling, nothing was queued. Bots at tier `full`
   are instructed to always schedule, but it's worth checking the queue.

For the full architecture (row schema, locking, permissions, runner
dispatch), see [docs/continuity-engine.md](../continuity-engine.md).
