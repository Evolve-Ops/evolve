---
title: "Help: Continuity Page"
slug: continuity
audience: public
last_reviewed: 2026-06-05
concepts:
  - continuity-engine
  - deferred-intent
  - inline-python
  - agent-session
  - idle-detection
ui_surface: null   # background-system explainer; the continuity engine has no dedicated sidebar page
related_specs: []
---

# Help: Continuity Page

The Continuity page is the dashboard for the Continuity Engine (CE) — the system that extracts deferred tasks from session transcripts, queues them, and runs them asynchronously. It makes stateless OC bots feel like they remember and follow through on commitments.

---

## What the Continuity Engine Does

When a bot says "I'll write that to memory tonight" or "remind me to follow up on that," those commitments normally evaporate when the session ends. The CE intercepts them:

1. **Extracts** deferred intents from session turns (keyword matching + LLM enrichment)
2. **Queues** them as structured tasks with type, authorization level, and conditions
3. **Executes** them at the right time:
   - `inline_python` tasks (simple file writes, git commits, HTTP checks) run every 15 minutes, always
   - `agent_session` tasks (tasks needing the full bot) run only when the human is idle (no heartbeat for 15+ minutes)
4. **Reports** results via Telegram morning digest (08:00 daily)

---

## Stats Grid

Four counters at the top:
- **Pending** — tasks waiting to run (includes those awaiting approval)
- **Needs Approval** — tasks extracted by the LLM that require your explicit approval before running
- **Running** — tasks currently being executed
- **Completed (7d)** — tasks finished in the past week

---

## Tabs

### Task Queue

Shows pending tasks with status filter (All, Pending, Needs Approval, Running).

**Columns:**
- **Title** — action-oriented description of what the task will do
- **Bot** — which bot will execute it
- **Type** — `inline_python` (no AI cost) or `agent_session` (AI cost, runs when idle)
- **Auth level** — `autonomous` (runs automatically) or `needs_approval` (waits for you)
- **Status** — current state in the task lifecycle
- **Created** — when the task was extracted

**Task lifecycle:**
```
pending → (if needs_approval) → needs_approval → approved → running → done
                                                                     ↓
                                                                  blocked → cancelled
```

**Approving a task:** Click the approve button on a `needs_approval` task. Once approved, it will run on the next runner cycle (within 15 minutes).

**Cancelling a task:** Click cancel. The task won't run and is moved to cancelled state.

### Completed

History of finished tasks — both successful (`done`) and failed (`blocked`). Shows what the task did, when it ran, and the result message from the bot.

`blocked` tasks couldn't complete — the bot reported an error (e.g., permission denied, file not found). They stay in the history for review. If the underlying issue is fixed, you can re-create the task manually.

### Schedule Config

Runner settings:
- **Runner enabled** — master on/off switch for the Continuity Engine
- **Idle threshold** — how many minutes of no heartbeat before agent tasks are allowed to run (default: 15 min)
- **Max agent tasks per run** — how many agent tasks can dispatch per 15-minute cycle (default: 3)
- **Daily budget cap** — agent tasks stop dispatching when this fraction of daily budget is spent
- **LLM extraction enabled** — whether to use LLM enrichment for task extraction (costs one tier3 call per session with deferred intent signals)

---

## Common Questions

**What kinds of tasks get extracted?**
The extractor looks for deferred intent signals in session turns:
- Time-based deferrals: "I'll do that later / tonight / tomorrow / next week"
- Reminders: "remind me to...", "remember to..."
- Follow-ups: "let's revisit / follow up on..."
- Monitoring: "keep an eye on...", "check if..."
- Multi-step work: "we should tackle X systematically"
- External actions: "send an email / message / slack"

**Why do LLM-extracted tasks always require approval?**
Security rule: the LLM could theoretically extract a malicious task from a session (prompt injection). Requiring human approval for all LLM-extracted tasks means the runner never executes unreviewed AI-generated code autonomously. Only keyword-classified inline tasks (simple file writes, git commits) can run autonomously.

**What's the difference between inline_python and agent_session tasks?**
- `inline_python` — runs Python-only actions directly: write a file, append to a log, run a git commit, send a notification, check if a URL is up. No AI cost. Runs every 15 minutes regardless of idle state.
- `agent_session` — starts a full OC bot session to accomplish the task. Has AI cost. Only dispatches when the human is idle (no heartbeat for 15+ minutes) to avoid interfering with real sessions.

**A task shows as "blocked" — what happened?**
The bot reported that it couldn't complete the task. The `result` field shows the error message. Common causes: permission errors (file path the bot can't write), missing tool (API key not configured), or an ambiguous task that the bot couldn't interpret.

**How do I add a task manually?**
Use the CLI: `evolve-admin tasks add --title "..." --description "..." --execution-type inline_python --action append_file --action-params '{"path": "...", "content": "..."}'`. The UI doesn't currently have a manual task creation form — the task queue is populated by the extractor.

**The morning digest says tasks ran overnight — where do I see results?**
In the Completed tab. Each completed task shows the result message from the bot and when it ran. The Telegram morning digest is a summary; the UI has the full history.

**Tasks aren't being extracted from sessions — why?**
Possible causes:
1. The Evolve plugin isn't installed on the bot (`evolve-admin deploy <bot>`)
2. LLM extraction is disabled in Schedule Config
3. Sessions have fewer than 3 turns (LLM extraction only fires for longer sessions)
4. The session didn't contain deferred intent language that matches the extractor's patterns
