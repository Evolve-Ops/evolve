# clawchief — OpenClaw Chief of Staff Starter Team-bot-a

**Source:** https://github.com/snarktank/clawchief  
**Archived:** 2026-04-13

> Turn your OpenClaw into a Chief of Staff

`clawchief` is a public OpenClaw starter team-bot-a for turning OpenClaw into a founder / chief-of-staff operating system. It is opinionated about the *architecture* of the workflow, but meant to be customized for your own people, programs, calendars, inboxes, trackers, and recurring routines.

---

## What this repo gives you

A portable operating model with:

- a source-of-truth `clawchief/` layer for priorities, task state, meeting-note policy, and action policy
- an orchestrator heartbeat
- a canonical markdown task system with a separate completed-task archive
- executive-assistant and business-development skills
- cron templates with short DRY prompts
- install docs and workspace templates

## The design pattern

The system works best when you separate:

1. *prioritization* → `clawchief/priority-map.md`
2. *resolution policy* → `clawchief/auto-resolver.md`
3. *meeting-note ingestion policy* → `clawchief/meeting-notes.md`
4. *live task state* → `clawchief/tasks.md`
5. *archive state* → `clawchief/tasks-completed.md`
6. *local environment details* → `workspace/TOOLS.md`
7. *recurring orchestration* → `workspace/HEARTBEAT.md` + cron jobs

**That separation is the main thing this repo is trying to teach.**

## Core operating lessons baked into this repo

- Use Gmail *message-level* search, not thread-only search
- Check *all relevant calendars* before booking
- Keep one canonical live task file
- Archive prior-day completions into a separate file
- Update the real source of truth in the same turn when you act
- Keep cron prompts short and let skills hold workflow logic
- Use a policy layer for prioritization and a separate policy layer for auto-resolution
- Treat meeting notes as a live signal source, not passive documents

## Credit / inspiration

A lot of the evolution of this setup was influenced by Pedro Franceschi's OpenClaw setup, which he explained during his conversation with Ashlee Vance on the Core Memory podcast:

- Pedro Franceschi: https://github.com/pedrofranceschi
- Podcast segment: https://www.youtube.com/watch?v=9ZbbxSgrjhw&t=3847s

In particular, his setup helped inspire the direction of:

- the priority map
- the auto-resolver layer
- the ingestion pipeline mindset for turning passive context into active operational state
