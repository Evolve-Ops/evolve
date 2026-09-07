---
title: "Getting Started with Evolve"
slug: getting-started
audience: public
last_reviewed: 2026-06-05
concepts:
  - what-is-evolve
  - install
  - orientation
  - openclaw
ui_surface: null
related_specs: []
---

# Getting Started with Evolve

Welcome to Evolve — the management layer for your OpenClaw bot pod.

---

## What Evolve does

OpenClaw gives you powerful AI bots. Evolve answers the questions that come next:
are they running, what are they costing, are they getting better, and how do you
manage several of them without living in a terminal window?

OpenClaw is to AI assistants what Linux is to servers — powerful, open, and
technically unfriendly. Evolve is what Ubuntu is to Linux: it assembles the stack
into something a household or small business can actually run. Your data stays on
your hardware. You approve every meaningful change.

---

## evo is the front door

The fastest way to get something done in Evolve is to **ask evo** in chat. The
floating chat widget at the bottom-right of every page (or the dedicated Chat
tab) connects you to evo, the pod's primary bot, which can read every page's
state AND take action on your behalf — restart bots, apply proposals, snooze
alerts, install apps, rotate credentials, audit security findings, and so on.

Each page surfaces example prompts in the chat widget's empty state — those
examples are the operations evo can do for you from that page. They're not the
only things evo can do; they're starting points for the most common operator
intent on each page. Try them, then describe what you actually want — evo
matches your description to the right tool or proposal and does the work.

What evo can do for you, by surface (see each page's help for details):

- **Dashboard** — investigate per-bot issues, restart / redeploy / remove a bot,
  pause or resume the whole pod.
- **Plugins → Credentials** — surface unhealthy integrations, ask about a
  specific key, file a rotation as a proposal.
- **Plugins → Plugins / MCP / Hooks / Activity** — explain drift from the
  baseline, audit recent permission changes, walk through what's installed.
- **Security** — investigate firing findings, run a per-app audit, summarize
  posture across the pod.
- **Recommendations** — list pending proposals, apply / reject / snooze any of
  them, mark deferred-completion proposals complete.
- **Usage** — explain spend, surface the biggest contributors, project month-end.
- **Maintenance** — pause / resume the pod, redeploy, check daemon health.
- **Backup** — run a backup now, surface the latest size estimate, restore
  from latest commit, explain classification audit findings.
- **Apps** — audit a specific app or all apps on a bot, check a forge job's
  status, install from the gallery.
- **AI Optimization** — change a bot's default tier (`evo tier-default …`),
  explain why a session anchored on a particular tier.
- **Cost Optimization** — set a daily cap, explain a runaway session, surface
  premium-models-on-autonomous spotter findings.
- **Reports / Alerts** — explain a firing alert, snooze / dismiss / resolve it.
- **Users** — approve / reject pending pairing requests, disconnect a user,
  flip a bot between single-user and multi-user mode.
- **Settings** — wipe local evo telemetry, summarize the current config.

When evo can't do something directly (no tool yet, no proposal yet), it tells
you so AND records a tool-gap entry so future versions cover the request. The
operator doesn't have to file feature requests; the system collects them.

---

## The three ways in

You can do nearly anything through any of the three interfaces:

| Interface | Best for |
|---|---|
| **This dashboard** (localhost:5050) | Visual inspection, approval queues, one-click installs |
| **`evo` keyword** (any bot chat) | Quick status, surfacing suggestions, approvals without opening a browser |
| **`evolve-admin` CLI** | Scripted ops, deeper config, troubleshooting |

All three share the same state — nothing is hidden in one interface that you can't
reach from another.

---

## The `evo` keyword system

Type `evo` (or `evolve`) as a standalone message to **any bot on your pod**. The
system intercepts it before the LLM runs — zero AI cost, instant response.

Verified command list (source:
`packages/admin/evolve_admin/evo/subcommands.py`, `_REGISTRY` — 35 commands as
of 2026-05-30; selected highlights below — `evo help` lists every command
available to you, role-gated):

| Command | What it does | Who can run it |
|---|---|---|
| `evo` (bare) | Shows the top recommendation for this bot | Primary + Admin |
| `evo better` | Same as bare — surfaces the top suggestion | Primary + Admin |
| `evo summary` | Pod-wide summary: alerts, spend, pending suggestions | Primary + Admin |
| `evo status` / `evo week` | Aliases for `evo summary` | Primary + Admin |
| `evo cost` / `evo usage` | Spend report | Primary + Admin |
| `evo alerts` | List firing alerts | Primary + Admin |
| `evo health` | Pod health one-liner | Primary + Admin |
| `evo security` | Latest audit findings | Primary + Admin |
| `evo integrations` | Channel + key health | Primary + Admin |
| `evo apps` | List apps installed on this bot | Primary + Admin |
| `evo app <name>` | Detail on one app | Primary + Admin |
| `evo gallery` | Browse the App Gallery | Primary + Admin |
| `evo install <app>` | Install a gallery app on this bot | Primary + Admin |
| `evo audit [<app>]` | Run a per-app audit | Primary + Admin |
| `evo app-audit <app>` | Force one app's audit | Primary + Admin |
| `evo skills` | What skills are installed | Anyone |
| `evo tier <fast\|standard\|power\|auto>` | Set your own default tier on this bot | Anyone |
| `evo tier-default <fast\|standard\|power\|auto>` | Set the bot's default tier | Primary + Admin |
| `evo mute` / `evo unmute` | Mute / unmute the bot | Primary + Admin |
| `evo profile` | View what this bot has learned about you | Anyone |
| `evo profile dnt on` | Stop learning, wipe existing notes | Anyone |
| `evo guide` | Author the team guide for this bot | Primary + Admin |
| `evo claim <passphrase>` | Claim admin or primary status | Anyone |
| `evo wizard` | Starts the onboarding wizard for this bot | Anyone |
| `evo continuity` | Learn about the Continuity Engine | Anyone |
| `evo improve` / `evo revise` | Tweak how this bot behaves | Primary + Admin |
| `evo bug` / `evo feature` / `evo intake` | File a bug / feature / general intake into the developer Inbox | Anyone |
| `evo fail` | Mark the latest turn as failed (calibration) | Primary + Admin |
| `evo connect <integration>` | Walk through connecting an integration | Primary + Admin |
| `evo setup-google` | Google OAuth wizard | Primary + Admin |
| `evo default` | Show / set your defaults | Anyone |
| `evo fun` | Surprise me | Anyone |
| `evo help` | List every command available to you | Anyone |
| `evo help <name>` | Detail on one command | Anyone |

Natural-language variants that work (verified in
`packages/admin/evolve_admin/evo/subcommands.py`, `_PHRASE_ALIASES`):
- "evo, what's on this week?" → `evo summary`
- "evo, what's on for this week?" → `evo summary`

The keyword is matched whenever `evo` or `evolve` is the first word of the
message (case-insensitive, trailing punctuation like `evo,` or `evo.` is
fine). Bare `evo` returns the top recommendation directly; `evo <subcommand>`
routes through the registry above; phrase aliases like "evo, what's on this
week?" match the full phrase before the LLM is involved. Messages where the
first word isn't `evo`/`evolve` go to the LLM as usual.

---

## Applications vs. Skills

**A skill** is a capability primitive — one thing a bot can do.

- "Read my Gmail inbox"
- "Send a Slack message"
- "Save a note to Obsidian"

OpenClaw ships with thousands of skills. You install them on your bots from the
Skills page.

**An application** is a goal-shaped contract built from several skills working together.

- "Morning Briefing" — email + calendar + weather, delivered at 7 AM
- "Email Triage" — classifies incoming mail, surfaces the ones that need you
- "Client Intake" — interviews new clients, stores responses, flags red flags

Evolve maintains each application as a contract: what it claims to do, what success
looks like, and how to verify it. The system tracks whether each application is
hitting its contract and proposes improvements when it's not.

Skills are the building blocks. Applications are the house.

See also: [docs/applications-vs-skills.md](../applications-vs-skills.md)

---

## RSI and the Recommendations queue

The Better Engine watches your pod constantly. When it sees something worth
improving — a cost spike, a capability gap, a security finding, a voice drift —
it writes a **Recommendation** (internally: a Proposal).

Recommendations appear on the **Recommendations** page in the Improve section.
Each one shows:
- What it suggests and why
- The dimension (cost, security, utility, substrate health, etc.)
- A falsifiable claim ("this reduces gateway restarts ≥30% over 7 days")
- Act / Snooze / Dismiss buttons

You approve → the system applies the change and later verifies the claim held.
You dismiss → the generator records that feedback and learns.

You can also surface the top recommendation from chat: `evo` or `evo better`.

This is RSI applied to applications, not to skills. The engine improves what your
bots do, not what they're capable of doing.

---

## The Continuity Engine

The Continuity Engine makes bots work between conversations.

During a session, a bot might commit to deferred work:
- "I'll write that to memory tonight"
- "Remind me to check back in 20 minutes"
- "Keep an eye on that repository"

A bot has no persistence between turns — without the Continuity Engine, these
evaporate when the session ends. Instead, the bot schedules each follow-up
itself the moment it commits, using its `defer` tool: either a literal message
to deliver at a set time, or an instruction to act on later.

A background runner fires due follow-ups every 2 minutes as a short turn in
the original conversation. There is no approval queue — a fired defer is just
the bot keeping a promise it made to you out loud, under the same permissions
as any other turn.

Most of the time it's invisible — the follow-up simply arrives when promised.
Runner health shows as a one-line summary on the Maintenance page.

Source: [docs/continuity-engine.md](../continuity-engine.md)

---

## The `evolve-admin` CLI

The full command-line interface for deeper operations. Privileged actions
(`setup`, `deploy`, `add-bot`, `retire-bot`, `refresh-sudoers`,
`restart-gateways`, anything that edits a bot's `.openclaw/` files) require
`sudo` on the deploy box. Read-only operations (`status`, `health`, `connect`,
`keys`, most `menu` subcommands run as the bot's owner) work without it.

Main command groups (verified from
`packages/admin/evolve_admin/cli.py` and
`packages/admin/evolve_admin/ocadmin.py`):

| Group / Command | What it does |
|---|---|
| `evolve-admin setup --fresh` | First-time pod setup wizard (also the adopt path for existing OpenClaw bots) |
| `evolve-admin setup` | Config wizard for a pod that already has an Evolve service layer |
| `evolve-admin deploy <bot>` | Install/update Evolve on a specific bot |
| `evolve-admin add-bot` | Register a new bot |
| `evolve-admin retire-bot` | Remove a bot from the pod |
| `evolve-admin health` | Run a pod health check |
| `evolve-admin status` | Pod health summary (one screen) |
| `evolve-admin application scan` | Scan bot workspaces for apps |
| `evolve-admin keys` | Manage shared and per-bot API keys |
| `evolve-admin models` | Manage model tiers (tier1/tier2/tier3/tier0) |
| `evolve-admin menu` | Interactive bot-config menu (no flag syntax to memorize) |
| `evolve-admin menu version` | Show OpenClaw version |
| `evolve-admin menu upgrade` | Upgrade OpenClaw |
| `evolve-admin menu exec-approvals <bot>` | Manage per-bot exec allowlists |
| `evolve-admin menu logs <bot>` | Tail a bot's gateway logs |
| `evolve-admin menu usage` | OpenClaw usage statistics |
| `evolve-admin connect --host mini` | Open the admin UI from your laptop via SSH tunnel |
| `evolve-admin service install` | Install the admin server as a LaunchDaemon |
| `evolve-admin mcp-bridge install` | Install the Claude Desktop MCP bridge |
| `evolve-admin warden suppressions` | List security warden suppressions |
| `evolve-admin evo claim-primary` | Set a bot's primary user |
| `evolve-admin evo show-identity` | Check who is claimed on a bot |

Run `evolve-admin --help` or `evolve-admin <command> --help` for full option lists.

---

## Four features that work in the background

**Continuity Engine** — fires the follow-ups bots schedule for themselves via
the `defer` tool ("remind me in 20 minutes" actually fires in 20 minutes).
Mostly invisible unless a fire fails.

**RSI watchdogs** — a portfolio of specialized generators (14 today, in
`packages/analyzer/generators/`) monitors spend, security, session quality,
gateway health, classifier accuracy, app health, and more. They run on
schedule and write to the Recommendations queue only when they see something
worth surfacing.

**User profile inferrer** — each bot quietly builds a user model from conversations.
This feeds Recommendations ("your bot surfaces improvements before you think to ask
for them"). No approval queue for individual facts — the bot's primary user
implicitly authorizes inference. Turn it off with `evo profile dnt on`.

**Security warden** — continuous audit of every bot's configuration, file hashes,
and exec allowlists. Writes to the Maintenance page (Alerts tab) when something
changes unexpectedly. Runs independently of the main pod so a compromised bot
can't silence it.
