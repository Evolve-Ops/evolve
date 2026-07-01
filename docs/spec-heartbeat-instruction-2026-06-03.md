# Spec: oc_heartbeat_instruction — replace oc_heartbeat_hook

**Date:** 2026-06-03
**Status:** Draft
**Amends:** [spec-forge-side-effects-2026-06-02.md](spec-forge-side-effects-2026-06-02.md) §4–§5 (mechanism enum, install recipe, Phase 4.5 dispatch) and §7 (verifier A1)
**Related:** [project_oc_per_bot_hook_optin](memory) (PR 7 of OC was about `hooks.allowConversationAccess`, NOT a generic hook surface)

---

## 1 — Why

On 2026-06-02, after the 8-PR sequence (PRs 0–8 of spec-forge-side-effects) merged, I attempted to validate Phase 4.5 by re-forging task-manager on personal-bot. The bot LLM forge dispatch eventually timed out (SIGTERM at 3 min), so I bypassed it and directly invoked the `install_oc_hook` helper with the post-PR-8 declarative recipe:

```python
install_oc_hook(bot_id="personal-bot", hook_event="heartbeat",
                command="python3 scripts/tasks.py check")
```

The helper executed all four documented steps correctly: read openclaw.json, append a hook entry under `hooks.heartbeat[]`, call `safe_write_bot_config` for OC schema validation, abort on rejection. **And it correctly aborted** with:

```
Config validation FAILED (forge: install hooks.heartbeat entry):
  hooks: Invalid input
```

The defense-in-depth (validate against OC schema before write) caught a fundamental design bug: **OpenClaw has no top-level `hooks` field in its config schema.**

Checking `openclaw config schema` confirms this. OC has a `heartbeat` config object (under the agent's section) with fields `every`, `activeHours`, `model`, `session`, `target`, `directPolicy`, etc. — it's a **scheduling primitive** for firing turns at intervals, not a **command-registration surface**.

How team-bot-a, team-bot-c, team-bot-d, and security-bot already do heartbeat-driven actions: they put instructions in `HEARTBEAT.md` or `AGENTS.md` ("every heartbeat, run X and report any TASK_DUE: lines"), and the bot's session-driven LLM executes them when the heartbeat fires a turn.

So `mechanism: oc_heartbeat_hook` (spec-forge-side-effects §4.1) is built on an assumption that doesn't hold in OC. This spec replaces it with `mechanism: oc_heartbeat_instruction`, matching the proven pattern.

---

## 2 — The replacement

### 2.1 — Mechanism enum change

In `applications/manifest.py`, update `SCHEDULED_ACTION_MECHANISMS`:

```python
# REMOVE:
MECHANISM_OC_HEARTBEAT_HOOK = "oc_heartbeat_hook"      # ← bogus
MECHANISM_OC_SESSION_HOOK   = "oc_session_hook"        # ← also bogus

# ADD:
MECHANISM_OC_HEARTBEAT_INSTRUCTION = "oc_heartbeat_instruction"
MECHANISM_OC_SESSION_INSTRUCTION   = "oc_session_instruction"
```

`session_start` hooks have the same architectural issue — OC's `session_start` is a session-lifecycle event the plugin can hook in code (`session_surface.py::handleSessionStart`), not a config-level command registration. Bots achieve "do this at session start" the same way: text in `AGENTS.md` § Session Start that the LLM reads on its first turn.

So `oc_session_hook` → `oc_session_instruction` follows the same logic.

### 2.2 — The new `install` recipe shape

For `mechanism: oc_heartbeat_instruction`:

```json
"install": {
  "file": "HEARTBEAT.md",
  "section_anchor": "## Task Manager — Heartbeat Check",
  "body": "Every heartbeat, run `python3 scripts/tasks.py check` and surface any TASK_DUE: or FOLLOWUP_NEEDED: lines as messages.",
  "command": "python3 scripts/tasks.py check"
}
```

Fields:

| Field | Required | Meaning |
|---|---|---|
| `file` | yes | Markdown file in the bot's workspace to write to. Typically `HEARTBEAT.md`; `AGENTS.md` for session_instruction. |
| `section_anchor` | yes | Full markdown heading (with `##` or `###` prefix) used as the section identifier. Must be unique within the file. |
| `body` | yes | The natural-language instruction the bot LLM will read and execute. Should reference `command` so the auditor can cross-check. |
| `command` | yes | The actual command (matches what `body` references). Used by A1 verifier and the constraint critic. |

For `mechanism: oc_session_instruction`, same fields, just `file: "AGENTS.md"` typically.

### 2.3 — `installed_artifact` format

```
HEARTBEAT.md#Task Manager — Heartbeat Check
```

Hash-fragment is the literal section anchor minus the `#` prefix. The PR 3 verifier uses the same anchor-resolution code already in place for `evidence_locator` (`app_audit_structural._extract_section`).

---

## 3 — The new `install_heartbeat_instruction` helper

Replaces `install_oc_hook` in `applications/install_helpers.py`. Same return-envelope shape as the existing helpers (`{ok, artifact, error, …}`).

```python
def install_heartbeat_instruction(
    bot_id: str,
    file: str,
    section_anchor: str,
    body: str,
    *,
    network: dict | None = None,
) -> dict:
    """Idempotently install a section in the bot's HEARTBEAT.md (or similar).

    - Reads {workspace}/{file} (direct read; evolve has ACL on workspace).
    - If section_anchor exists: replaces the section body atomically.
    - If section_anchor is missing: appends as a new section at end of file.
    - Atomic write via temp file + os.replace.

    No sudoers needed, no gateway kickstart needed. The bot LLM reads the
    file on its next heartbeat turn and executes the instruction.

    Returns:
      ok: bool
      artifact: "{file}#{section_anchor}"
      already_present: bool
      error: str
    """
```

Key behaviours:

- **No `sudo` required.** Evolve already has write ACL on `/Users/{bot}/.openclaw/workspace/` per `set_evolve_write_acl()` in `deploy.py`.
- **No schema validation against OC.** The file is plain markdown; there's no OC config involved.
- **No gateway kickstart.** The bot's session-driven LLM reads the file fresh on the next heartbeat turn.
- **Idempotent.** Re-installing with the same section_anchor + body is a no-op; with a different body it replaces the section.
- **Refuse to clobber operator content.** If the section exists with an `<!-- evolve-managed -->` marker, replace freely. Without the marker, return `error: "section is operator-authored; refusing to overwrite"`. The marker is auto-inserted on first install:

  ```markdown
  ## Task Manager — Heartbeat Check
  <!-- evolve-managed: pkg=p-9bfa1c84 forge-job=j-XXXX -->

  Every heartbeat, run `python3 scripts/tasks.py check` and surface any
  TASK_DUE: or FOLLOWUP_NEEDED: lines as messages.
  ```

This is the same "evolve-managed" pattern documented in `project_pod_conduct_mechanism` for `POD_CONDUCT.md` — proven on this pod, no new abstractions.

### 3.1 — File creation

If the target file doesn't exist, the helper creates it with a default header:

```markdown
# Heartbeat instructions

The following sections are evolve-managed. Each describes one app's
heartbeat behavior. Do not edit `<!-- evolve-managed -->` sections by
hand — use `evolve-admin app pause/uninstall` instead.
```

This keeps `HEARTBEAT.md` self-documenting and gives the operator a single place to see all installed heartbeat actions.

---

## 4 — Phase 4.5 dispatch update

In `forge_engine._materialize_scheduled_actions`:

```python
# REMOVE the oc_heartbeat_hook / oc_session_hook branches.
# REPLACE with:

if mechanism in ("oc_heartbeat_instruction", "oc_session_instruction"):
    file = install_cfg.get("file") or ""
    section_anchor = install_cfg.get("section_anchor") or ""
    body = install_cfg.get("body") or ""
    if not (file and section_anchor and body):
        return failed("install requires file + section_anchor + body")
    result = install_heartbeat_instruction(
        manifest.bot_id, file, section_anchor, body,
    )
```

Everything else stays the same — same provenance stamping (`installed_at`, `installed_by`, `installed_artifact`), same per-action best-effort dispatch.

---

## 5 — Verifier A1 update

Spec-forge-side-effects §7 A1 checks each `scheduled_actions[]` entry's install is live. For `oc_*_instruction`:

```python
def check_install_present_instruction(action, ctx):
    artifact = action.get("installed_artifact")
    # Parse "HEARTBEAT.md#Section Anchor"
    file, _, section = artifact.partition("#")
    text = (ctx["workspace"] / file).read_text()
    section_text = _extract_section(text, section)  # existing helper
    if section_text is None:
        return Finding(
            assertion_id="scheduled_action_install_missing",
            severity=SEVERITY_MAJOR,
            summary=f"heartbeat instruction missing: {artifact}",
        )
    # Cross-check: the body still references install.command
    command = (action.get("install") or {}).get("command") or ""
    if command and command not in section_text:
        return Finding(
            assertion_id="scheduled_action_install_missing",
            severity=SEVERITY_MAJOR,
            summary=f"heartbeat instruction section exists but missing command {command!r}",
        )
    return None
```

This reuses `_extract_section` from `app_audit_structural.py` — the same code that already verifies `evidence_locator` anchors. No new infrastructure.

---

## 6 — Updates to PR 2 scanner attribution

PR 2's `_read_openclaw_hooks` and `_attribute_hook_to_app` walk `openclaw.json#hooks.{event}[]`, which we now know doesn't exist as a valid OC field. The code is harmless (returns empty list) but should be retired:

- **Delete `_read_openclaw_hooks`** — replace with `_read_heartbeat_md_sections(bot_id)` that walks `HEARTBEAT.md` and `AGENTS.md`, returning evolve-managed sections.
- **Delete `_attribute_hook_to_app`** — replace with `_attribute_instruction_to_app` that matches by section anchor's package fingerprint (the `<!-- evolve-managed: pkg=… -->` marker).
- **Keep LaunchAgents scanning** unchanged — that path was correct.

The attribution path becomes: read each `<!-- evolve-managed: pkg=p-XXXX -->` marker, look up the app whose `pkg_id` matches, attach the section as a `scheduled_actions[]` entry on that app's manifest with `installed_by: "scanner:backfill"`.

---

## 7 — Updates to PR 1's admin-daemon API

The `POST /api/forge/install/openclaw-patch` endpoint becomes vestigial. Replace with:

```
POST /api/forge/install/heartbeat-instruction
  body: {bot_id, file, section_anchor, body}
  → 200 {ok, artifact, already_present, error?}
  → 502 {ok: false, error}
  → 400 {ok: false, error: "missing_fields", missing: [...]}
```

Keep the openclaw-patch endpoint for **other** openclaw.json patches that DO have legitimate use cases (e.g., updating `agents.defaults.heartbeat.every` to change the cadence). Restrict it to non-hooks json_pointer paths.

---

## 8 — Gallery republish: `p-9bfa1c84` → 2026.06.03-1.3

Replace task-manager's `scheduled_actions[]`:

```json
"scheduled_actions": [
  {
    "id": "task-check",
    "mechanism": "oc_heartbeat_instruction",
    "trigger": {
      "kind": "heartbeat",
      "schedule": "every_heartbeat",
      "evidence_path": "HEARTBEAT.md",
      "evidence_locator": "Task Manager — Heartbeat Check"
    },
    "install": {
      "file": "HEARTBEAT.md",
      "section_anchor": "## Task Manager — Heartbeat Check",
      "body": "Every heartbeat, run `python3 scripts/tasks.py check`. If the output contains lines starting with `TASK_DUE:` or `FOLLOWUP_NEEDED:`, surface each one as a separate operator-visible message.",
      "command": "python3 scripts/tasks.py check"
    },
    "inputs": [{"path": "tasks.json", "kind": "data_file"}],
    "outputs": [{"kind": "session_message", "channel": "primary"}],
    "summary": "Surface overdue tasks and follow-up needs every heartbeat"
  }
]
```

`p-aab5e569` (ea-pack) does NOT change — its three actions are time-of-day cron jobs that stay on `mechanism: launchd`. The spec's §3 P3 framing is correct: heartbeat instructions for in-session work, LaunchAgent for actions that must fire when no session is active.

---

## 9 — Migration

**On personal-bot:**
- task-manager is already uninstalled (manifest removed during the 2026-06-02 admin-UI uninstall attempt). No migration needed.
- ea-pack continues working unchanged (LaunchAgents) — no migration needed.

**On other bots running pre-PR-4 task-manager (none on this pod yet):**
- Their manifests will have `scheduled_actions: []` (empty — no install attempt was ever made).
- Re-forge or `evolve-admin app update task-manager` will install the new instruction.

**On bots whose manifests carry `mechanism: oc_heartbeat_hook`** (none yet, but if any slip through):
- Migration helper `_migrate_mechanism_v17`: any `oc_heartbeat_hook` → `oc_heartbeat_instruction`; clear `installed_artifact` (forces re-install); preserve everything else.
- Schema v16 → v17 bump tracking the migration.

---

## 10 — Acceptance criteria

1. **Re-forging task-manager on a fresh bot** with the new gallery `p-9bfa1c84@2026.06.03-1.3`:
   - Phase 4.5 calls `install_heartbeat_instruction`
   - `HEARTBEAT.md` gains the `## Task Manager — Heartbeat Check` section with the `<!-- evolve-managed -->` marker
   - Manifest's `scheduled_actions[0]` has `installed_at`, `installed_by: forge:{job_id}`, `installed_artifact: HEARTBEAT.md#Task Manager — Heartbeat Check`
2. **A1 verifier on the same bot** finds the section + command, returns no findings against the task-check action.
3. **Bot session at next heartbeat** runs `python3 scripts/tasks.py check` and surfaces any TASK_DUE: lines (this is the bot LLM's responsibility, not forge's — but the operator should be able to verify it on the first heartbeat after install).
4. **`evolve-admin app pause task-manager`** removes the section from `HEARTBEAT.md`; the bot stops running the check.
5. **`evolve-admin app unpause task-manager`** re-installs the section.
6. **`evolve-admin app uninstall task-manager`** removes the section, the files, AND the manifest (in that order — see the spawned uninstall-order task).

---

## 11 — Implementation plan

Two PRs:

| # | Work | LOC est |
|---|---|---|
| 1 | New helper + admin-daemon endpoint + Phase 4.5 dispatch update + verifier A1 update + scanner attribution swap + schema v17 migration | ~600 |
| 2 | Gallery republish `p-9bfa1c84` v1.3 + ad-hoc fix to any pre-shipped `oc_heartbeat_hook` references in other gallery specs (none expected — task-manager is the only one) | ~50 |

Both small. Could be one PR if no concerns about reverting independently.

---

## 12 — Why not just delete `oc_heartbeat_hook`?

A reader might ask: why not just remove the enum value entirely and pretend it never existed?

Because the 2026-06-02 audit findings and the 8-PR sequence cite the mechanism by name in commit messages, PR bodies, and the spec itself. Renaming it in-place creates a clean audit trail: the schema-validation failure that surfaced the bug IS the validation working as designed. The lesson — "test your design assumptions against the real schema before shipping eight PRs that depend on them" — is worth preserving as a marker in the codebase.

`SCHEDULED_ACTION_MECHANISMS` keeps `oc_heartbeat_hook` for one schema version (v17) with a `_DEPRECATED_MECHANISMS` set, then removes in v18. The `install_oc_hook` helper stays for one version too, returning a clear error pointing at `install_heartbeat_instruction`.

---

## 13 — Open questions

1. **Should the bot LLM be prompted to confirm execution?** The current pattern (per team-bot-a/team-bot-c/team-bot-d) is fire-and-forget: the LLM reads HEARTBEAT.md, runs the command, surfaces output. No confirmation loop. Keep it that way for now; add a verification step (e.g. `pod_state.last_heartbeat_executions`) if reliability becomes an issue.

2. **What about `oc_session_hook` use cases?** No app on this pod currently has one. The `session_surface.py::handleSessionStart` pattern (used by POD_CONDUCT and bot-guide) is a plugin-side hook, not something a bot-installed app can extend. Defer the `oc_session_instruction` implementation until there's a real use case.

3. **HEARTBEAT.md ordering.** If multiple apps install heartbeat instructions, the order matters (e.g., task-check might want to run BEFORE the daily archive). Spec says "append to end of file" for new sections. A future enhancement could add an `install.order_hint: "before:archive"` field; for now, document that operators can manually reorder sections in HEARTBEAT.md (the `<!-- evolve-managed -->` marker is preserved, so re-install won't clobber the reorder).

4. **What if HEARTBEAT.md doesn't exist on a bot that's never had one?** Helper creates it with the documented header. The bot LLM may not have been told to *read* HEARTBEAT.md at session start. Solution: the bot's `AGENTS.md` should reference HEARTBEAT.md ("Read HEARTBEAT.md on every heartbeat turn"). For new bots, this should be part of the bot-template. For existing bots without the reference, install needs to also patch AGENTS.md to reference HEARTBEAT.md. Detect "no AGENTS.md HEARTBEAT.md reference" → fail install with a clear remediation message.

---

## 14 — Relationship to the broader framework

This spec doesn't undo any of the 8 PRs from spec-forge-side-effects-2026-06-02. The architecture survives intact:

- **Schema v16** scheduled_actions sub-fields → still valid (mechanism + install + provenance)
- **Scanner attribution** (PR 2) → small swap (HEARTBEAT.md sections instead of openclaw.json hooks)
- **Verifier A1–A6** (PR 3) → A1 changes its resolution target; A2/A5/A6 unchanged
- **Forge Phase 4.5** (PR 4) → dispatcher changes one branch
- **Test gate hardening** (PR 6) → unchanged
- **Env portability lint** (PR 7) → unchanged
- **Gallery republish** (PR 8) → republished again

The 8 PRs were *structurally right*. They surfaced this design bug exactly as a verification framework should — and the defense-in-depth (`safe_write_bot_config`) prevented the wrong assumption from corrupting any bot's openclaw.json. That's the framework working as designed, on its first real-world test, against itself.
