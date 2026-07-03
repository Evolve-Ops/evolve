# Evolve — Continuity Engine

The Continuity Engine (CE) is the part of Evolve that makes bots work asynchronously — extracting tasks from sessions, queuing them, and executing them when the human is idle or away.

---

## What problem it solves

During a conversation, a bot might commit to deferred work:
- "I'll write that to memory tonight"
- "Remind me to check in with Elizabeth next week"
- "Keep working on the CAD script overnight"

Without CE, these commitments evaporate when the session ends. CE intercepts them, turns them into structured tasks, queues them, and executes them at the right time — zero human re-initiation required.

---

## Architecture overview

```
Session ends
    │
    ▼
task_extractor.py (runs at session end via TurnObserver)
    │  Scans turns for deferred intents
    │  Classifies: inline_python vs agent_session
    │  Deduplicates against existing queue
    ▼
task_queue.py (persistent JSONL queue)
    │  Stores tasks with state machine
    │  Tracks dependencies, recurrence, auth level
    ▼
task_runner.py (polls every 15 minutes via launchd)
    │
    ├── inline tasks (always)
    │     inline_executor.py runs Python-only actions
    │     Zero AI cost, instant
    │
    └── agent tasks (idle only)
          openclaw agent --session-id <key> --message "TASK:<id>..."
          Agent runs with full tools in normal session
          Writes TASK_COMPLETE: or TASK_BLOCKED: to results file
          Runner picks up result on next cycle
```

---

## Task extraction

### How extraction works

`task_extractor.py` runs at session end (triggered by TurnObserver's `session_end` hook). It scans all turns in the session for deferred intent signals.

**Two extraction paths:**

**1. Keyword extraction (free, always runs)**

18 deferred intent patterns:
- "I'll do that later / tonight / tomorrow"
- "next time we talk"
- "remember to / remind me to"
- "let's revisit / follow up on"
- "keep an eye on / monitor this"
- "when you get a chance / overnight"
- etc.

8 multi-step patterns (tasks that need sustained work):
- "let's work on X together over the next few days"
- "we should tackle X systematically"
- etc.

6 external action patterns:
- "send an email / message / slack"
- "submit the form / inquiry"
- etc.

If any pattern matches, keyword extraction produces a draft task and optionally hands off to LLM extraction for enrichment.

**2. LLM extraction (cost: one tier3 call per session)**

Only fires if keyword signals are found AND the session has > 3 turns. Uses a structured prompt to extract:
- `title` — clean, action-oriented task name
- `description` — what needs to happen
- `execution_type` — `inline_python` or `agent_session`
- `auth_level` — `autonomous` or `needs_approval`
- `action` + `action_params` — for inline tasks only
- `not_before` — if a time was mentioned

**Security rule:** LLM-extracted tasks with `execution_type: inline_python` are always forced to `needs_approval` regardless of what the LLM says. Prompt injection cannot manufacture autonomous file-write tasks.

### Inline classification (5 rules)

Before LLM extraction, the keyword classifier attempts inline classification:

| Rule | Trigger pattern | Action |
|------|-----------------|--------|
| append_file | "write/add/log/note to daily notes/memory.md/tasks.md" | `append_file` to resolved path |
| git_commit | "commit/backup changes" | `git_commit` in workspace |
| send_notification | "ping me/notify pod-admin" | `send_notification` via openclaw |
| http_check | "check if [site] is up/running" | `http_check` GET/HEAD |
| append_file (named) | "add/write to memory.md" by name | `append_file` to named path |

If a rule matches but the target path is ambiguous, it falls back to `agent_session` (safer).

### Deduplication

Before writing a task, the extractor checks existing pending tasks for word-overlap similarity (Jaccard, threshold 0.5, stop-words filtered). Tasks that are semantically similar to an existing pending task are dropped.

---

## Task queue

### Task state machine

```
pending ──────────────────────────────────────────► done
   │                                                  │
   │ (human approves)        (runner completes)       │
   ▼                              │                   │
needs_approval ──────────────────►│                   │
   │ (approved)                   │                   │
   ▼                              │                   │
approved ─────────────────────────►                   │
   │                                                  │
   ▼                                                  │
running ──────────────────────────────────────────────►
   │ (timeout / failure)
   ▼
blocked ──────────────────────────────────────────────► (cancelled by human)
                                                        │
                                                        ▼
                                                    cancelled
```

`expired` — tasks with `expires_at` that passed before execution.

### Task schema

```json
{
  "task_id": "uuid",
  "title": "Write session notes to daily log",
  "description": "Append today's session summary to memory/2026-04-05.md",
  "application": "health-tracking",
  "execution_type": "inline_python",
  "auth_level": "autonomous",
  "action": "append_file",
  "action_params": {
    "path": "memory/2026-04-05.md",
    "content": "## 14:30 Session notes\n..."
  },
  "status": "pending",
  "priority": 5,
  "created_at": "2026-04-05T21:00:00Z",
  "not_before": null,
  "expires_at": "2026-04-06T09:00:00Z",
  "attempts": 0,
  "max_attempts": 3,
  "conditions": [],
  "recurrence": null,
  "parent_task_id": null,
  "bot_id": null,
  "context": {}
}
```

### Auth levels

| Level | Who can run | Human approval required |
|-------|-------------|------------------------|
| `autonomous` | Runner runs without asking | No — but only keyword-classified tasks can be autonomous |
| `needs_approval` | Sits in queue until human approves | Yes — via `evolve-admin tasks approve <id>` or admin UI |

**All LLM-extracted inline tasks → forced to `needs_approval`**
**All agent_session tasks → `needs_approval` by default**
**Keyword-classified inline tasks → may be `autonomous` if the action is low-risk**

### Conditions

Tasks can have preconditions that must be met before they run:

**Time-based:**
```json
{"type": "time", "not_before": "2026-04-06T08:00:00Z"}
```

**Dependency (task must be done):**
```json
{"type": "dependency", "task_id": "abc123"}
```

**Result-conditioned (task must succeed with specific output):**
```json
{"type": "dependency", "task_id": "abc123", "result_status": "done", "result_contains": "tests passed"}
```

### Recurrence

Tasks can recur on a schedule. Supported specs:

| Spec | Meaning |
|------|---------|
| `"daily"` | Every day at same time |
| `"weekdays"` | Monday–Friday |
| `"weekly"` | Same day of week |
| `"every monday"` | Every Monday |
| `"every 3 days"` | Every 3 days |
| `"monthly"` | Same day of month |
| `"first sunday"` | First Sunday of each month |
| `{"type":"cron","hour":8,"weekday":1}` | Custom cron-like |

When a recurring task completes, `handle_recurrence_after_done()` in task_runner.py creates the next instance and notifies Pod-admin via Telegram.

**Chain limit:** 52 instances maximum (prevents runaway queues from misconfigured recurrence).

---

## Execution paths

### Path 1: inline_python

Runs immediately on every runner cycle, regardless of idle state. Zero AI cost.

**Supported actions** (all path-validated against workspace and shared_dir roots):

| Action | Params | Description |
|--------|--------|-------------|
| `write_file` | `path`, `content` | Write file (creates dirs) |
| `append_file` | `path`, `content` | Append to file |
| `git_commit` | `message`, `cwd` | `git add -A && git commit` |
| `send_notification` | `message` | Telegram via `openclaw message send` |
| `http_check` | `url`, `expect_status` | HEAD request, verify status |
| `shell_safe` | `cmd` (list), `cwd` | Whitelist-only shell: `ls cat cp mv mkdir rm touch git` |

**Security rules for inline_python:**
- All file paths resolved and validated against allowed roots via `Path.is_relative_to()` (not `startswith`)
- `shell_safe`: cmd must be a list (no string eval), no shell metacharacters (`|;&><` etc.), binary must be in whitelist, `find` is excluded (supports `-exec`/`-delete`), `rm` blocks `-r/-R/-rf` flags
- All LLM-extracted inline tasks require human approval regardless of `auth_level`

### Path 2: agent_session

Fires only when Pod-admin is idle (heartbeat > 15 minutes ago). AI cost applies.

**Idle detection:**
```bash
openclaw system heartbeat last --json  # → ts field (epoch ms)
```

**Budget gate:** Checked before each agent dispatch cycle. Three limits:
- Hard floor: $0.10 minimum daily budget remaining
- Overnight reserve: tasks don't consume >30% of daily budget (leaves headroom for morning)
- Per-run cap: max 15% of daily budget per runner cycle
- Max 3 agent tasks dispatched per runner cycle

**Dispatch:**
```bash
# Same-bot (default)
openclaw agent --session-id agent:main:telegram:direct:123456789 \
  --message "TASK:<task_id>|<title>|<description>|<params_json>"

# Cross-bot dispatch is not currently enforced — see "Cross-bot delegation"
# below. The allowlist key was removed in 2026-05 (no consumer remained).
openclaw agent --agent team-bot-a --message "TASK:..."
```

**Result protocol:**
The agent writes to `{shared_dir}/tasks/agent-results.jsonl`:
```json
{"task_id": "uuid", "status": "done", "result": "Completed successfully. Notes added to memory.", "ts": "..."}
```

Or on failure:
```json
{"task_id": "uuid", "status": "blocked", "result": "Could not access file — permission denied", "ts": "..."}
```

The runner picks this up on the next 15-minute cycle and updates task state.

---

## Morning digest

At 08:00 daily, `task_runner.py --morning-digest` sends a Telegram summary:

```
📋 Overnight task summary

✅ Completed (2):
  • Write session notes to daily log
  • Git commit workspace changes

⏳ Awaiting your approval (1):
  • Send weekly report to Elizabeth — needs_approval

❌ Blocked (1):
  • Update CAD script — could not parse existing file

📅 Scheduled today:
  • Weekly PROJECT-X review (every monday, 09:00)
```

---

## Cross-bot delegation

Admin-bot can delegate tasks to Team-bot-a and vice versa.

> **Note:** the historical `crossBotDispatch` allowlist key was removed in
> 2026-05 because no enforcement code consulted it. There is currently no
> per-pair allowlist — any bot may target any other bot via `--agent`. If
> you need to re-introduce gating, add a check in the dispatcher and a
> reader for the network.json key.

**Create a cross-bot task:**
```bash
evolve-admin tasks add \
  --title "Update PROJECT-X task board" \
  --description "Mark the electrical work package as complete" \
  --bot <bot-id> \
  --auth needs_approval
```

---

## CLI reference

```bash
# List tasks
evolve-admin tasks list
evolve-admin tasks list --status needs_approval
evolve-admin tasks list --bot <bot-id>

# Task detail
evolve-admin tasks show TASK_ID

# Approve a pending task
evolve-admin tasks approve TASK_ID

# Cancel a task
evolve-admin tasks cancel TASK_ID

# Add a task manually
evolve-admin tasks add \
  --title "Check example.com uptime" \
  --description "Verify the Example Corp website is responding" \
  --execution-type inline_python \
  --action http_check \
  --action-params '{"url": "https://example.com", "expect_status": 200}'

# Recurring task
evolve-admin tasks add \
  --title "Write weekly protein log summary" \
  --recurrence "every monday" \
  --bot <bot-id>

# Task with dependency
evolve-admin tasks add \
  --title "Deploy if tests pass" \
  --depends-on UPSTREAM_TASK_ID=done:tests passed

# Run the runner manually (dry run)
evolve-admin tasks run --dry-run

# Stats
evolve-admin tasks stats
```

---

## Tuning

### Reducing spurious task extraction

If too many tasks are being extracted from casual conversation:

1. Raise the signal threshold — require 2+ pattern matches before LLM extraction
2. Disable LLM extraction: `enableLLMExtraction: false` in plugin config
3. Set `execution_type` default to `needs_approval` for all extracted tasks (already the case for inline)

### Reducing agent task cost

1. Raise idle threshold (default 15 min) to require longer idle periods
2. Lower per-run budget cap (default 15%)
3. Set all agent tasks to `needs_approval` — they won't run until Pod-admin approves

### Increasing task throughput

1. Lower the idle threshold
2. Raise MAX_AGENT_TASKS_PER_RUN (default 3) in task_runner.py
3. Schedule the runner more frequently (default 15 min)

---

## Monitoring

```bash
# Queue health
evolve-admin tasks stats

# View result file directly
cat /Users/Shared/evolve/tasks/agent-results.jsonl

# Check budget remaining
cat /Users/Shared/evolve/tasks/budget/daily.json
```
