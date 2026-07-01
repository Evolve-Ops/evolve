# Spec: Session Management UI

*Created: 2026-04-19*
*Status: Draft — not yet built*

---

## Background

OpenClaw maintains a persistent session store per bot. Sessions accumulate over time — main Telegram sessions, cron sessions, subagent sessions — and can linger long after they're no longer needed. Old sessions using retired model configurations (MAX accounts, deprecated model IDs) can cause confusion and waste. There is currently no way to inspect or manage sessions from the Evolve admin UI.

This spec covers Phase 1: read-only session inventory with a kill action. Phase 2 (automatic sweeper rules) is scoped separately at the bottom.

---

## Goals

- Give operators visibility into which sessions are active on each bot
- Make it easy to close stale or unwanted sessions without needing shell access
- Surface model and age information so operators can spot sessions stuck on outdated model configs

---

## Non-Goals (Phase 1)

- Automatic sweeper / garbage collection (Phase 2)
- Bulk-close actions (Phase 2)
- Session content inspection (never — sessions are private to the bot user)
- Cross-bot session views (per-bot only in Phase 1)

---

## UI Placement

Add a **Sessions** panel to the existing bot detail view (the page shown when you click into a specific bot). This is a peer panel to the existing Config, Integrations, and Maintenance panels — not part of the Analytics sessions browser, which shows historical annotated sessions, not live openclaw sessions.

The panel should appear in the **Maintenance** section of the bot detail view, since session hygiene is an operational task.

---

## Session Data

OpenClaw sessions are retrieved via the CLI:

```
openclaw sessions list
```

Expected fields per session (based on observed output):

| Field | Description |
|---|---|
| `key` | Unique session identifier (e.g., `agent:main:cron:...ab50ea`) |
| `model` | Model the session was created with (e.g., `claude-sonnet-4-6`, `gpt-4.1`) |
| `type` | Session type: `main`, `cron`, `subagent`, `heartbeat` |
| `last_active` | Timestamp or relative age (e.g., `14d ago`) |
| `active` | Whether the session is currently processing work |

The output format should be confirmed against actual `openclaw sessions list --json` output before implementation. If a `--json` flag does not exist, parse the text output.

---

## Backend

### New Routes

**`GET /api/bots/<bot_id>/live-sessions`**

Lists all sessions for a bot by running `openclaw sessions list` as the bot user.

```python
# Command to run as bot user:
["sudo", "-u", bot_user, OC_BIN, "sessions", "list", "--json"]

# Returns:
{
  "sessions": [
    {
      "key": "agent:main:...",
      "model": "claude-sonnet-4-6",
      "type": "cron",
      "last_active": "2026-04-05T12:00:00Z",
      "active": false
    },
    ...
  ]
}
```

Error handling:
- If `openclaw` exits non-zero, return `{ "error": "<stderr>", "sessions": [] }`
- If bot user cannot be resolved, return 404

**`POST /api/bots/<bot_id>/live-sessions/close`**

Closes a single session by key.

Request body: `{ "key": "agent:main:cron:...ab50ea" }`

```python
# Command to run as bot user:
["sudo", "-u", bot_user, OC_BIN, "sessions", "close", key]
```

Validation:
- Reject keys containing shell metacharacters (`;`, `&`, `|`, `$`, `` ` ``, `\n`, etc.)
- Key must match pattern `[\w:.\-]+` — alphanumeric, colon, dot, hyphen only

Returns: `{ "ok": true }` or `{ "ok": false, "error": "<stderr>" }`

### Sudoers

No new sudoers grant required. The existing grant covers openclaw:

```
evolve ALL=(ALL) NOPASSWD: /opt/homebrew/lib/node_modules/openclaw/bin/openclaw
evolve ALL=(ALL) NOPASSWD: /usr/local/lib/node_modules/openclaw/bin/openclaw
```

Use `_resolve_oc_bin()` (or equivalent) to pick the correct path at runtime.

### Route Registration

Add to `_register_bot_config_routes()` or a new `_register_sessions_routes()` called from `create_app()`.

---

## Frontend

### Sessions Panel

In the bot detail view, add a collapsible **Sessions** panel with:

- A **Refresh** button (re-fetches live session list)
- A table with columns: Key (truncated), Model, Type, Last Active, Actions
- A **Close** button per row — clicking prompts "Close session `<key>`?" confirmation, then POSTs to the close endpoint and refreshes the list
- An empty state: "No active sessions found" if the list is empty
- An error state: shows the openclaw error message if the fetch fails

**Key display:** truncate session keys to the last 8 characters with a tooltip showing the full key. Example: `...ab50ea`

**Model display:** highlight sessions using non-current models in amber — e.g., if the bot's configured primary model is `claude-sonnet-4-6` but the session shows `claude-opus-4-5`, flag it visually.

**Age display:** show relative time (e.g., "14 days ago") derived from `last_active` timestamp.

**Active indicator:** sessions where `active: true` should show a green dot and "Active" badge. Do not show a Close button for actively-processing sessions, or show it with a warning: "This session appears active — close anyway?"

---

## Phase 2: Sweeper Rules (Out of Scope Now)

Sweeper rules would allow operators to configure automatic session cleanup policies per bot, such as:

- Close `cron` sessions older than 7 days
- Close `subagent` sessions older than 24 hours
- Close sessions using a retired model (configurable list)

Implementation would require:
- A sweeper config stored in `evolve_config.json` per bot
- A `sweep_sessions.py` job (or addition to `heal.py`) running on a schedule
- A UI to configure sweeper rules (similar to existing cron job config UI)
- Dry-run mode with preview before enabling

This is not in scope until Phase 1 is live and operators have had time to use it manually.

---

## Open Questions

1. Does `openclaw sessions list` support `--json` output, or does it require text parsing?
2. Are there session types beyond `main`, `cron`, `subagent`, `heartbeat`?
3. Should the panel auto-load when the bot detail view opens, or only on explicit Refresh click? (Prefer explicit to avoid extra subprocess calls on every page open.)
4. Is there a safe way to distinguish "session exists but idle" from "session exists and actively consuming tokens from a stuck loop"? If so, the UI should flag the latter more urgently.
