# Spec: launchd_python_signal — Python-by-default scheduled actions

**Date:** 2026-06-03
**Status:** Draft
**Supersedes (for most cases):** [docs/spec-heartbeat-instruction-2026-06-03.md](spec-heartbeat-instruction-2026-06-03.md)
**Related:** Signal store ([docs/spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md)), Signal-subscriber ([docs/spec-signal-subscriber-2026-05-31.md](spec-signal-subscriber-2026-05-31.md))

---

## 1 — Motivation

The v17 `oc_heartbeat_instruction` mechanism (shipped 2026-06-03 in PRs #1999–#2004) writes natural-language instructions to `HEARTBEAT.md`. The bot LLM reads those instructions on every heartbeat and decides what to do.

This is the wrong default for most scheduled actions:

- **Every heartbeat wakes the LLM**, even when the action's check would find nothing actionable.
- At the pod's current cadence (every 30 min × 9 bots × ~$0.015 Haiku heartbeat) that's ~$5–9/day just for "check if there's a task to surface" — and most of those checks come up empty.
- The **gallery v1.3 task-manager pre-v17** had this right: `task-check.sh` ran via cron as plain Python, emitted `TASK_DUE:` / `FOLLOWUP_NEEDED:` signal lines only when something needed attention. The v17 refactor regressed that pattern because OpenClaw had no `hooks.heartbeat[]` array we could wire it into.

The fix: a **Python-by-default** scheduled-action mechanism that runs cheap launchd-scheduled scripts and only escalates to the bot's LLM when there's something real to surface. The Signal store + signal-subscriber daemon are already in place to handle the escalation.

`oc_heartbeat_instruction` stays — narrowed to its real use case: instructions the LLM genuinely needs to evaluate on every heartbeat (e.g. user-state monitoring that requires natural-language judgment). Most apps shouldn't use it.

---

## 2 — Mechanism: `launchd_python_signal`

New enum value alongside `oc_heartbeat_instruction` / `oc_session_instruction` in `MANIFEST_SCHEMA_VERSION = 18`.

```python
MECHANISM_LAUNCHD_PYTHON_SIGNAL = "launchd_python_signal"
```

Forge's Phase 4.5 dispatches this mechanism via a new `install_python_signal_action` helper which:

1. Writes a wrapper Python script to `{workspace}/evolve/scheduled/<id>.py`.
2. Writes a LaunchAgent plist to `~/Library/LaunchAgents/<label>.plist` that runs the wrapper on schedule.
3. Calls `launchctl bootstrap` to activate the plist.

The wrapper runs the bot's command, parses stdout for declared signal patterns, and writes a Signal to the Signal store *only* when a pattern matches.

---

## 3 — Install recipe shape

```json
{
  "id": "task-check",
  "mechanism": "launchd_python_signal",
  "trigger": {
    "kind": "scheduled",
    "schedule": "every_30_minutes"
  },
  "install": {
    "label": "com.{bot}.task-check",
    "command": "python3 scripts/unified_task_system.py check",
    "cwd": "${workspace}",
    "schedule": {
      "every_minutes": 30
    },
    "signal_patterns": ["TASK_DUE:", "FOLLOWUP_NEEDED:"],
    "signal_type": "task_pending",
    "signal_severity": "info",
    "exec_policy": "inherit"
  }
}
```

Fields:

| Field | Required | Notes |
|---|---|---|
| `label` | yes | LaunchAgent label; must match `com.{bot_id}.{anything}` pattern (same convention as existing `install_launch_agent`). |
| `command` | yes | Shell command to run. Relative paths resolve against `cwd`. |
| `cwd` | yes | Working directory. `${workspace}` placeholder substituted at install time. |
| `schedule.every_minutes` OR `schedule.cron` | yes | Either a frequency (will compose `StartInterval`) or a `StartCalendarInterval` block (cron). |
| `signal_patterns` | yes | Substring matches in stdout. Any non-empty match triggers a Signal write. |
| `signal_type` | yes | Routed to `signals.store.observe(type=…)`. Existing values: `task_pending`, `proposal_pending`, etc. |
| `signal_severity` | no | Default `info`. Can be `warning` / `error`. |
| `exec_policy` | no | `inherit` (default) / `full`. Same as existing fields. |

---

## 4 — Wrapper script behavior

The wrapper is generated from a template at install time. Pseudocode:

```python
#!/usr/bin/env python3
# evolve-managed: launchd_python_signal wrapper for {label}
import json, os, subprocess, sys
from pathlib import Path

# Config — frozen at install time
COMMAND = """{command}"""
CWD = "{cwd}"
PATTERNS = {patterns_repr}
SIGNAL_TYPE = "{signal_type}"
SIGNAL_SEVERITY = "{signal_severity}"
BOT_ID = "{bot_id}"
APP_ID = "{app_id}"
SHARED_DIR = Path("{shared_dir}")
LABEL = "{label}"

try:
    proc = subprocess.run(
        COMMAND, shell=True, cwd=CWD, capture_output=True,
        text=True, timeout=300,
    )
except subprocess.TimeoutExpired:
    sys.exit(0)   # silent fail — wrapper never escalates on timeout

# Find matched lines
matched = []
for line in proc.stdout.splitlines():
    if any(pat in line for pat in PATTERNS):
        matched.append(line.strip())

if not matched:
    sys.exit(0)   # the happy path — no LLM ever involved

# Escalate via Signal store
try:
    sys.path.insert(0, str(SHARED_DIR.parent.parent / "evolve-repo" / "packages" / "analyzer"))
    from signals import store as signal_store
    signal_store.observe(
        bot_id=BOT_ID,
        type=SIGNAL_TYPE,
        severity=SIGNAL_SEVERITY,
        summary=f"{{len(matched)}} item(s) from {{LABEL}}",
        details={{
            "matched_lines": matched[:50],   # cap to keep Signals lean
            "command": COMMAND,
            "app_id": APP_ID,
        }},
        source=f"launchd:{{LABEL}}",
        shared_dir=SHARED_DIR,
    )
except Exception as exc:
    print(f"signal store unavailable: {{exc}}", file=sys.stderr)
    sys.exit(1)
```

**Cost properties:**
- Happy path (no signal pattern matched): zero LLM cost. Just a Python subprocess.
- Escalation path: one Signal write. The signal-subscriber wakes the bot's LLM only when there's a real signal.

---

## 5 — Signal-to-bot-wake integration

The existing signal-subscriber daemon (per spec-signal-subscriber-2026-05-31) currently routes signals to **generators** via charter `subscribes_to:`. For this mechanism to work end-to-end, signals also need to wake the **owning bot's LLM session** so the bot can act on the surfaced item.

Two options, with the tradeoffs:

**Option A — Add bot-wake to signal-subscriber.** When a signal lands with `source=launchd:<label>`, signal-subscriber dispatches a templated nudge via `openclaw agent --deliver` (the gateway's plain-send `/api/message` endpoint was removed in OC 2026.6 — see docs/spec-gallery-delivery-convention-2026-06-11.md): *"You have N pending items from {label}. Summary: {matched_lines[:3]}."*

**Option B — Write a thin generator** (`signal_to_bot_nudge`) that subscribes to signals from `launchd:*` sources and POSTs to the bot's gateway. Re-uses the existing subscriber→generator path.

Option B is structurally cleaner (the generator pattern is already proven). Option A is fewer moving parts but couples the subscriber to bot transports.

Recommendation: **Option B.** Tracked as a separate PR in this sprint.

---

## 6 — Model-tier handling

The `oc_heartbeat_instruction` mechanism has a known concern: the bot's heartbeat config (`agents.defaults.heartbeat.model`) is sometimes hardcoded to a cheap model (e.g. `claude-haiku-4-5`). If that model fails over to tier-2 / tier-1 (because of a provider outage, model deprecation, or model-registration drift like the bug #2019 fixed), cost can balloon silently.

The `launchd_python_signal` mechanism **sidesteps this entirely**:

- The Python wrapper makes **zero LLM calls**.
- The escalation path (signal-subscriber → bot LLM) uses the bot's normal session-model config, not heartbeat-specific overrides.
- The bot's normal session model goes through the documented tier-resolution path (`models.resolve_tier(...)`), so operator overrides + fallbacks apply correctly.

Net: this mechanism removes the "heartbeat hardcoded to a specific model" failure mode by removing the heartbeat from the LLM picture entirely.

(The `oc_heartbeat_instruction` mechanism stays and inherits the existing tier-resolution issue. That's a separate concern, tracked as a follow-on chip.)

---

## 7 — Operator concerns

| Concern | Handling |
|---|---|
| Uninstall must remove the LaunchAgent + wrapper | `evolve-admin app uninstall` already walks `scheduled_actions[].installed_artifact`. Phase 4.5's uninstall pair must understand the new mechanism's artifacts (plist path + wrapper script path). |
| Debugging: which heartbeat fired when? | Each wrapper invocation appends a one-line entry to `{workspace}/evolve/scheduled/<id>.log` (rotated weekly). |
| Cost monitoring | Per-bot Anthropic cost stays driven by actual LLM sessions, not by scheduled actions. The wrapper's signal-write is observable via the Signal store; the bot-wake (Option B above) is observable via the generator log. |
| What if the Signal store is full / unreachable? | Wrapper exits 1 with stderr message. launchd records the exit code; we'll add an OK-skew monitor in a later PR. |

---

## 8 — Migration path from `oc_heartbeat_instruction`

When a gallery manifest's `scheduled_actions[*].mechanism` is updated from `oc_heartbeat_instruction` to `launchd_python_signal`:

1. The next install on a fresh bot uses the new mechanism end-to-end.
2. For bots that already have the old mechanism installed: the deprecated section in `HEARTBEAT.md` should be left in place but the bot's heartbeat is now wasteful. The improvement-job verifier (chip from earlier today) needs to handle the mechanism swap — same shape as the LaunchAgent→HEARTBEAT.md swap that surfaced job j-effe972d's bug.

A `evolve-admin app migrate-mechanism <pkg_id> --to launchd_python_signal` CLI helper that walks the bot's existing manifest and rewires it without a full re-forge would be cleaner than a re-install. Tracked as a follow-on.

---

## 9 — Acceptance criteria

For the foundation PR (this spec's companion implementation):

1. `MANIFEST_SCHEMA_VERSION` bumps to 18 with `MECHANISM_LAUNCHD_PYTHON_SIGNAL` added.
2. New helper `install_python_signal_action(bot_id, label, command, cwd, schedule, signal_patterns, signal_type, ...)` exists in `install_helpers.py`.
3. The helper writes:
   - A wrapper script to `{workspace}/evolve/scheduled/<id>.py`
   - A LaunchAgent plist to `~/Library/LaunchAgents/<label>.plist`
4. The wrapper:
   - Runs the command and parses stdout for `signal_patterns`
   - Writes a Signal via `signals.store.observe()` only when at least one pattern matches
   - Exits 0 on the no-signal happy path (silent)
5. Tests cover both helper output (plist shape, wrapper content) and wrapper behavior (signal match → write, no match → silent).

For follow-on PRs:

6. Phase 4.5 dispatcher routes the new mechanism to the helper.
7. Signal-to-bot-wake generator (`signal_to_bot_nudge`) posts to the bot gateway on signals from `launchd:*` sources.
8. Admin-daemon HTTP endpoint for evo / socket clients.
9. Gallery republish: task-manager + unified-task-system swap from `oc_heartbeat_instruction` to `launchd_python_signal`.
10. Deriver-prompt update so future scanned exports prefer `launchd_python_signal` for any periodic check that emits machine-readable signal lines.

---

## 10 — Implementation plan (multi-PR)

| PR | Scope | LOC est. |
|---|---|---|
| **T-A.1** | This spec + schema v18 + `install_python_signal_action` helper + wrapper template + tests | ~600 |
| **T-A.2** | Phase 4.5 dispatcher wiring + uninstall partner + tests | ~300 |
| **T-A.3** | `signal_to_bot_nudge` generator (Option B) + tests | ~400 |
| **T-A.4** | Admin-daemon `/api/forge/install/python-signal` endpoint + tests | ~300 |
| **T-A.5** | Gallery republishes (task-manager, unified-task-system) | ~50 |
| **T-A.6** | Deriver-prompt update + tests | ~100 |
| **T-A.7** | Live migration of the personal-bot reference account, the test-bot, and the other v17 installs | runbook only |

T-A.1 is this PR. T-A.2 through T-A.7 follow.

---

## 11 — Open questions

- **Schedule shape**: should `every_minutes` be the operator-facing field, or `every` (allow `"30m"` / `"1h"` strings)? The latter is friendlier; the former is unambiguous. Inclined toward `every` with a parser. **Decision needed before T-A.1 lands.**
- **Bot-wake rate limiting**: if a bot has 10 apps each emitting signals frequently, the signal-subscriber could overload the bot's gateway. Worth a per-bot throttle in T-A.3. **Decision needed before T-A.3 lands.**
- **Operator notification when a wrapper repeatedly fails to escalate** (Signal store down, command crashes): probably an `app_heartbeat_health` monitor in a later PR. **Defer.**
