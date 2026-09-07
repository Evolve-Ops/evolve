# Applications vs. Skills

Evolve organizes OpenClaw's capabilities into two distinct layers: **skills** and **applications**. Understanding the difference helps you make the most of your bots.

## Skills — the capabilities

A **skill** is a capability primitive — one thing your bot can do well.

- "Send a Slack message"
- "Read my Gmail inbox"
- "Look up a calendar event"
- "Control a Hue light"
- "Save a file to Obsidian"
- "Remember what the user told me"

OpenClaw ships with thousands of skills, built and maintained by the community. Each skill is self-contained, tested, and documented. You install a skill once, and any bot on your system can use it.

## Applications — what your bots actually do

An **application** is a goal-shaped contract — something that delivers real value to you.

- "Morning Briefing" — email + calendar + weather + news, delivered in a single message at 7 AM
- "Project Manager" — tracks tasks, deadlines, email threads, and workspace files all in one place
- "Client Intake" — interviews new clients, stores responses, flags red flags
- "Deadline Watch" — monitors your calendar and email for upcoming commitments and surfaces them early
- "Journal" — daily prompts, memory, reflection

Each application is built from multiple skills working in concert. As Pod-Admin describes it: *"A project manager has email and task management and Obsidian integration and memory, etc., skills all working in concert to move a project forward."* The application is the thing you actually care about; the skills are what makes it possible.

## Why both matter

**Skills alone aren't enough.** If you had to manually wire together five different skills every time you wanted your bot to do something, the friction would be overwhelming. "I need to read email, then check the calendar, then fetch the weather, then call the news API, then format it nicely" is a lot of setup for what should be a single feature: "give me my morning briefing."

**Applications make the friction disappear.** You install "Morning Briefing" once. The application handles the skill orchestration, the scheduling, the formatting, and the delivery. It knows what it's supposed to do (deliver useful information by 7 AM) and Evolve can measure whether it actually does it.

## What Evolve adds

Evolve sits on top of the OpenClaw skill ecosystem and adds the application layer.

**Skills come from OpenClaw and beyond.** The community has built thousands of them. ClawHub is the distribution point. Evolve installs and manages them on your bots.

**Applications are how Evolve organizes them.** Each application is:
- A **contract** — what it claims to do, what it needs to succeed, what success looks like
- **Tested** — applications include smoke tests and behavioral tests that verify they still work
- **Measured** — Evolve tracks whether each application is hitting its contract
- **Improvable** — when an application isn't delivering, Evolve proposes improvements

And critically: **the same application looks different on different bots.** When you install "Project Manager" on your personal bot and on your team bot, Evolve generates two different implementations — one optimized for individual workflows, one for team coordination. The contract is the same (manage tasks, deadlines, and context); the implementation adapts to what each bot can do.

## The ecosystem picture

```
OpenClaw Skills
(5,700+ community capabilities)
         ↓
    Evolve Applications
(Goal-shaped contracts built on skills)
         ↓
    Your Bots
    (Running applications, delivering value)
```

- **OpenClaw** = skill libraries and the core agent runtime
- **ClawHub** = where community skills are shared
- **Evolve** = the application framework that turns skills into reliable, measurable tools your household or business actually uses every day

Skills are the building blocks. Applications are the house.

## Getting started

Start by exploring what applications are available in the **Apps → Gallery** tab of your admin dashboard. Each one shows which skills it depends on. As you install applications and skills on your bots, you'll develop an intuition for which skills power which applications.

If you want to understand your own bot's setup, the **Skills** page shows every skill your bot has installed, which ones are configured, and which applications are using them.
