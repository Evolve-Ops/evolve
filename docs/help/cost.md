---
title: "Help: What Evolve costs your bots"
slug: cost
audience: public
last_reviewed: 2026-10-03
last_refreshed_against: "2026-09-28..2026-10-05; 3 merged PRs touched concepts: attributed-spend, breaker-reactivation, evolve-overhead, overhead-breaker"
concepts:
  - evolve-overhead
  - overhead-breaker
  - attributed-spend
  - breaker-reactivation
ui_surface: admin.cost
related_specs:
  - internal/decision-evolve-overhead-2026-09-07.md
---

# Help: What Evolve costs your bots

Evolve sits next to your bots and, to do its job, sometimes makes model calls of its own: it summarises a conversation, classifies a message, decides which model should answer. It also puts a few thousand tokens of its own tool descriptions into every prompt. None of that is your bot talking to a person, and all of it is billed. The **Evolve overhead** panel on the Usage page puts one number on it so you can see whether Evolve is earning its keep.

Evolve is meant to be quiet. The number should be small and, over time, go down.

---

## What the number means

For each bot, over the last 7 days:

| Column | What it is |
|---|---|
| **Total spend** | Everything the bot spent, Evolve's calls included. |
| **Evolve calls** | How many model calls Evolve made for itself. Expand the row to see them by kind — *classifiers & routing*, *session summaries* — and, under each, by where the call came from. |
| **Calls $** | What those calls cost. Where OpenClaw priced a call, this is OpenClaw's own figure (marked *billed*); where it did not, it is Evolve's price table and is marked *estimate*. |
| **Context (est.)** | **An estimate, always.** Evolve's tool descriptions ride in the prompt of every turn the bot answers. This is that weight (the tool-description tokens Evolve logs at start-up) × the number of turns × the answering model's cached-input price. |
| **Overhead** | Calls $ + Context. |
| **Share** | Overhead ÷ total spend. This is the number to watch. |
| **Calls / message** | Evolve model calls per message sent to the bot. |
| **Person / no speaker** | The bot's spend split by who was waiting (see below). |

The last row is the pod, summed. The weekly spend receipt carries the same figure as one line.

## Why the target is 5%

The target is **at most 5% of a bot's spend and at most one Evolve model call per message**. It is a starting point, not a law: Evolve's whole case is that the bot costs less, or works better, with it than without it, and an overhead of a few percent is a price most operators will pay for that. Much above it and the machinery is the expensive part of the bot.

Evolve does not meet the target by asking for a bigger budget. It meets it by doing less: deciding with rules and files where it can, and calling a model only when there is something a rule cannot decide.

## How to move it

The two settings live in `network.json`, under `evolve.overhead`:

| Key | Default | Meaning |
|---|---|---|
| `share_max` | `0.05` | Evolve's share of a bot's spend. |
| `calls_per_user_turn_max` | `1` | Evolve model calls per message. |
| `window_minutes` | `60` | How long a stretch the breaker looks at. |
| `min_calls` | `5` | Fewer Evolve calls than this in the window never trip the breaker, however the ratio reads. |
| `breaker_ttl_hours` | `4` | How long a trip stands before Evolve may try again. |
| `enabled` | `true` | `false` turns the breaker off; the number stays. |

Edit the file to change them. To lower the number itself, the levers are elsewhere: fewer classifier and summariser calls, and a smaller set of tools in the prompt.

---

## The Evolve overhead breaker

If, over a rolling hour, a bot's Evolve calls pass either target, **Evolve's own breaker trips for that bot**. This is a different thing from the bot's cost breaker:

- **What stops:** Evolve's routing, session judges, classifiers and session summaries for that bot. Routing falls back to the rule and the bot's primary model.
- **What does not:** the bot. It keeps answering every message on the model it would have used. Nothing here pauses a conversation, a heartbeat or a scheduled job, and no message is held.

The card on the bot's tile and on this panel says so in plain words. It names the caller — for example *"Evolve's own routing calls: 41 calls in the last hour for 3 messages to the bot"* — with the busiest session and the count, so you know what to fix before you turn it back on.

The breaker measures Evolve's **model calls**, not the context estimate: tripping can take calls away but cannot take tool descriptions off a prompt.

Separately, if a bot's model-routing hook fires more than ten times its usual rate for that hour of the day, a **Needs you** line appears with the busiest session and what it was asking. That is the early warning for any runaway loop, before the spend shows it.

## What "Resume" accepts

**Resume Evolve's machinery** turns Evolve's routing, judges and summaries back on for that bot. It *accepts the window*: the calls that tripped the breaker are not judged again, and the count starts from the moment you click. A fresh runaway after that still trips it — the target does not move, only the starting line.

This is Evolve's button, not the bot's. It does not touch the bot's cost breaker, its daily cap, or its conversations. A trip also lifts by itself after `breaker_ttl_hours`.

## A person working, and everything else

The daily cost cap exists to catch a bot running away on its own. A person working a bot hard is the product doing its job, so the two are measured differently:

- **A person** — a turn with a resolved human speaker (a known user on a known channel). Counted against a wide rolling window, **7 days** by default (`thresholds.attributedWindowDays`): up to the daily cap on average, every day of the window.
- **No speaker** — cron jobs, heartbeats, Evolve's own calls, loops. These keep the tight per-day cap.

Both numbers show on the bot's cost-breaker card. If a person has spent the whole window's budget, the allowance lapses and every dollar counts against the day again. Anything Evolve cannot prove was a person's counts as no-speaker, so a mistake can only make the cap tighter, never looser. Reactivating a bot starts the window fresh, the same way the daily cap does.

None of this changes which model answers a message.

---

## Common questions

**Why is Context only an estimate?** Evolve cannot see inside a prompt; it knows how big its own tool descriptions are and which model answered. The figure is that arithmetic, labelled as such.

**The bot's share is over 5% but the breaker has not tripped.** The breaker watches Evolve's model calls over the last hour, with a floor of five calls. A bot whose overhead is mostly context, or a quiet bot, can sit above the target without anything to turn off. The panel flags it; nothing pauses.

**Can the breaker pause my bot?** No. It only removes Evolve's own machinery from the path.
