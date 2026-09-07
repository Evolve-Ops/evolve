# Evolve — Continuity Engine (v2)

The Continuity Engine (CE) is the part of Evolve that makes bots follow through
on commitments across sessions. A bot has no persistence between turns — it
cannot wait, sleep, or run background work. When it commits to acting later
("remind me in 20 minutes", "I'll check the build and let you know"), CE is
what makes that commitment actually fire.

> **v1 → v2.** Continuity v1 tried to detect deferred commitments *after the
> fact*: a `task_extractor.py` scanned session transcripts at session end
> (keywords + an LLM pass), wrote structured tasks into a pod-wide queue
> (`task_queue.py`), and a 15-minute `task_runner.py` executed them — inline
> Python actions directly, agent tasks via idle-gated dispatch, with an
> operator approval queue in between. That stack (task_extractor / task_queue /
> task_runner / inline_executor / recurrence) was removed in PR #835. v2
> inverts the model: instead of guessing at commitments post-hoc, the bot
> **explicitly schedules its own follow-ups** at the moment it makes them, via
> a plugin tool. There is no extraction, no inline-Python executor, no
> operator approval queue, and no morning digest. For the v1 design, see this
> file's git history and the dated `internal/spec-*` records.

---

## Architecture

```
Bot commits to a future action (during a normal turn)
    │
    ▼
`defer` plugin tool (DeferTool.ts, registered by the evolve plugin)
    │  Bot passes due_at (absolute ISO 8601 UTC) plus either:
    │    message — literal text to deliver when due
    │    action  — instruction for a follow-up agent turn
    │  Tool appends one JSONL row to the bot's own queue
    ▼
/Users/<bot_id>/.openclaw/workspace/evolve/defer-queue.jsonl
    │
    ▼
defer_runner.py (launchd: ai.openclaw.evolve.defer-runner,
    │            every 2 min, runs as the `evolve` user, pod-wide)
    │  Walks every bot's queue, fires due rows via `openclaw agent`
    │  dispatch against the row's session, archives fired/failed rows
    ▼
defer-archive.jsonl (terminal rows; append-only)
```

Both modes fire through the same `openclaw agent` dispatch — intentionally
uniform, one code path, one failure surface:

- **`message`** — the agent is told to deliver the stored text verbatim to the
  user. Use when the content is already known at scheduling time.
- **`action`** — the agent is told to do the stored instruction and then reply
  to the user. Costs one agent turn of real work when it fires.

Either way, cost is one cheap agent turn per fire.

---

## The `defer` tool

Registered by the evolve plugin (`packages/plugin/src/tools/DeferTool.ts`)
for bots whose Evolve integration tier is `full` — lower tiers (`off`,
`monitor`, `manage`) do not get the tool (see the `TIERS` table in
`packages/plugin/src/config.ts`).

Parameters:

| Param | Meaning |
|-------|---------|
| `due_at` | Absolute ISO 8601 UTC timestamp to fire at. The bot computes this itself from the current time in its system context. |
| `message` | Literal text to deliver when fired. Mutually exclusive with `action`. |
| `action` | Instruction for a follow-up turn. Mutually exclusive with `message`. |

Returns a `defer_id` and the absolute `fires_at` timestamp.

The tool description explicitly tells the bot it has no persistence between
turns and MUST call `defer` for any commitment to act later — that is the
whole v2 bet: prevent the silent-failure mode at the source instead of
detecting it after the fact.

---

## Storage: per-bot queue, no pod-wide store

Everything lives in the bot's own home (one queue per bot):

```
/Users/<bot_id>/.openclaw/workspace/evolve/
├── defer-queue.jsonl        # active rows (status = pending)
├── defer-queue.jsonl.lock   # flock target for the runner's rewrite
└── defer-archive.jsonl      # terminal rows (fired / failed); append-only
```

Row schema (v1 of the schema):

```json
{
  "defer_id": "uuid4",
  "bot_id": "team-bot-a",
  "channel_id": "…",
  "session_id": "…",
  "session_key": "agent:main:telegram:direct:…",
  "fires_at": "2026-05-05T13:04:00Z",
  "created_at": "2026-05-05T12:44:02Z",
  "mode": "message",
  "message": "Reminder: check the oven.",
  "action": null,
  "status": "pending",
  "fired_at": null,
  "result": null,
  "schema_version": 1
}
```

Notes:
- `session_id` is the ephemeral session UUID — the thing
  `openclaw agent --session-id` accepts; the runner uses it for dispatch.
- `session_key` is the routing-style key (`agent:main:telegram:…`) —
  diagnostic only, the CLI does not accept it.
- `status` transitions: `pending → fired | failed`. Terminal rows move to the
  archive file.

**Concurrency contract.** The bot side appends with POSIX `O_APPEND`
(atomic for writes ≤ PIPE_BUF, far above any row — no lock needed). The
runner takes the flock only when it rewrites the queue to drop fired rows,
and rewrites via tempfile + rename. Shared code for both sides of the Python
surface is `packages/analyzer/defer_queue.py`.

**Permissions.** The bot writes its own queue natively. The `evolve` user
(which the runner runs as) reads and rewrites it via the read+write ACL that
`deploy.py::set_evolve_read_acl()` sets on `~/.openclaw/workspace/evolve/`
during every deploy. No sudo, no /tmp staging.

---

## The runner

`packages/analyzer/defer_runner.py`, installed as the pod-wide LaunchDaemon
`ai.openclaw.evolve.defer-runner` (StartInterval=120, runs as `evolve`).
There is no per-bot variant — each cycle walks every bot declared in
`network.json` (the single source of truth; no `/Users/` scans).

Per cycle, for each bot:
1. Skip the bot if its `continuity_engine` module is disabled (see below).
2. Read the queue; find rows whose `fires_at` is due.
3. Dispatch each due row via `openclaw agent` against the row's session,
   wrapping the payload in system-initiated framing (XML-tagged delimiters so
   the model treats the inner content as data, not instructions).
4. Mark the row `fired` (or `failed`), append it to the archive, rewrite the
   queue without it.

**Dispatch timeout is 120s, deliberately generous.** The dispatched agent run
takes 25–40s end-to-end, and the CLI process must stay alive until the
gateway acknowledges `--deliver` — killing it early aborts the channel send
even though the agent turn completed server-side (discovered 2026-05-06 when
fired messages never reached Telegram). 120s also fits inside the 2-minute
cycle, so a stuck dispatch can't block the next cycle.

The 2-minute cadence (vs. v1's 15 minutes) is so a "remind me in 20 minutes"
defer feels ±2 min, not ±15.

---

## Enabling / disabling

Two independent switches:

- **Plugin tier** — the `defer` tool is only registered at tier `full`
  (per-bot `tier` field in the bot's `openclaw.json` plugin config). A bot
  below `full` can't schedule defers in the first place.
- **`continuity_engine` module** — per-bot toggle in `network.json`
  (`bots.<id>.continuity_engine.enabled`, default **on**). The runner skips
  disabled bots. The Bot Config page in the admin UI exposes this switch.

---

## Operator surface

There is deliberately **no admin queue, approval flow, or `evolve-admin
tasks` CLI** under v2 — the bot owns its queue, and neither mode executes
operator-approved code (v1's `inline_python` executor is gone; an `action`
row is just an agent turn, subject to the same tool gates as any other turn).

To inspect or intervene:

```bash
# What's pending for a bot (run on the pod host)
cat /Users/<bot_id>/.openclaw/workspace/evolve/defer-queue.jsonl

# What fired / failed recently
cat /Users/<bot_id>/.openclaw/workspace/evolve/defer-archive.jsonl

# Runner logs
cat /Users/evolve/.openclaw/logs/evolve-defer-runner.log
```

The admin UI's Maintenance page shows a one-line defer-runner health summary
(backed by `/api/defer/health`) — this replaced v1's Continuity tab.

To cancel a pending defer, delete its row from the queue file (take care to
preserve the JSONL shape); or just tell the bot, which can explain what it
scheduled.

---

## What v2 deliberately dropped, and why

| v1 feature | v2 disposition |
|------------|----------------|
| Keyword + LLM task extraction at session end | Gone. The bot schedules explicitly at commit time — no guessing, no spurious tasks, no missed single-turn commitments. |
| `inline_python` executor (file writes, git commits, shell allowlist) | Gone. Both modes fire as a normal agent turn; there is no CE-owned code-execution path to secure. |
| Operator approval queue (`needs_approval` / `evolve-admin tasks approve`) | Gone with the executor — there's no extracted-code path that needs pre-approval. (The principle it enforced still stands for any future LLM-extraction path: see `docs/principle-inline-task-needs-approval.md`.) |
| Idle-gating + budget gate for agent dispatch | Gone. Fires are user-visible follow-ups the bot promised; they fire when due. Cost is one agent turn per fire. |
| Recurrence engine | Gone. Recurring work belongs to OC cron / scheduled actions, not CE. |
| Cross-bot dispatch | Gone. A bot defers only its own follow-ups. |
| Morning digest | Gone with the queue — there's no overnight approval backlog to summarize. |
