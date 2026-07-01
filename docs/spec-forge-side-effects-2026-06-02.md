# Spec: Forge Side-Effects + App-Attributed Schedules

**Date:** 2026-06-02
**Status:** Draft
**Supersedes/extends:** [docs/spec-audit-extensions-2026-05-17.md](spec-audit-extensions-2026-05-17.md) (Workstream A — scheduled_actions extraction)
**Related:** [docs/spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md), [docs/spec-export-import-forge-2026-05-26.md](spec-export-import-forge-2026-05-26.md), [docs/spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md) (admin-daemon socket pattern)

---

## 1 — Background

On 2026-06-01 we forged `task-manager` (gallery `p-9bfa1c84`) onto **personal-bot**. Forge wrote all six files faithfully, tests passed, `last_structural_verify: ok`, manifest committed `approved`.

But one of two stated `success_criteria.observable_outcomes` — *"Overdue and blocked tasks are surfaced automatically by the daily cron"* — is **silently broken**. The build_spec described a LaunchAgent plist and a `launchctl load` step, but:

- Forge's `_apply_forge_output` ([forge_engine.py:2027](../packages/admin/evolve_admin/applications/forge_engine.py)) never installs side-effects — zero `launchctl`, `subprocess`, or admin-daemon calls beyond test execution.
- The manifest's `crons: []` reflects this honestly, but `last_verification` only walks `files[]`, so a half-installed app still passes verify.
- The tier-3 audit caught the drift 24h later as a `manifest_drift / minor` finding — the backstop works, but the install-time UX still claims success.

This spec closes that gap and tightens two adjacent gaps the same audit exposed: the scanner doesn't attribute existing crons/hooks to apps (so team-bot-a's and team-bot-c's hand-installed `task-check` jobs would also read as missing), and the audit produces a ~40% false-positive rate that will desensitize operators if unaddressed.

---

## 2 — Problems

### P1 — Forge writes files but doesn't materialize the contract

Anything outside `workspace/` (LaunchAgents, openclaw.json hook entries, ACLs, `launchctl bootstrap`) does not happen. The build_spec can describe a plist in prose, the bot LLM can transcribe a plausible plist into a code block, and forge will treat that as "documentation" rather than an install step. There's no structured side-effects contract for forge to enforce.

### P2 — Scanner is blind to per-app crons/hooks

[scanner.py](../packages/admin/evolve_admin/applications/scanner.py) snapshots `launchctl_labels` and reads the system crontab, but does **not**:
- Walk `/Users/{bot}/Library/LaunchAgents/*.plist`
- Parse `openclaw.json` for `hooks[]` entries
- Attribute either to apps via Label prefix or invoked-script path

Result: an existing working install (team-bot-a's `unified_task_system`, team-bot-c's `tasks.py`) will have `scheduled_actions: []` on its manifest even though a real cron is running, and a missing-but-promised cron is indistinguishable from a never-promised cron.

### P3 — LaunchAgent is the wrong default for OC-resident apps

The personal-bot `task-check.sh` reads tasks.json, emits text signals on stdout, exits 0. That's exactly the shape OC's hook plumbing already handles inline (`session_surface.py::handleSessionStart` returns `{systemAppend: ...}`). LaunchAgents add:
- A privileged install step (write to `/Library/LaunchAgents/`, `launchctl bootstrap`)
- An out-of-session channel (stdout to `/tmp/{bot}-task-check.log`) the bot only sees if something else routes it
- Drift surface (plist on disk, launchd registry, manifest claim — three places to keep in sync)

OC hooks avoid all three. They run as the bot user, are declared in `openclaw.json` (which already survives deploy), and their output reaches the bot's next turn natively. **Prefer OC hooks; fall back to LaunchAgent only when the action needs to fire while no session is active** (heavy nightly compute, network-isolated jobs).

### P4 — Audit false-positive rate (~40% on personal-bot's task-manager)

Of 5 task-manager findings, 2 were noise (TAG_ALIASES customization the spec explicitly requested; `task_updater.py --dry-run` not updating a field it doesn't touch). Tier-3 audit lacks the build_spec's "Customization Guidance" section in its prompt context, so it flags intentional bot-specific adaptation as drift.

---

## 3 — Design principles

1. **The manifest is the contract.** Anything forge promises must be declared in a structured field, not narrated in `build_spec` prose. If it's not in a structured field, the verifier can't check it and the scanner can't attribute it.
2. **Prefer OC-native mechanisms.** Heartbeat hooks > LaunchAgents > crontab. New build_specs default to hooks; LaunchAgent requires explicit justification.
3. **Privileged installs go through admin-daemon.** Same socket pattern that closed the evo cutover ([spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md) §12). Forge never `sudo`s directly.
4. **Scanner and forge share one schema.** Both populate `scheduled_actions[]`. A round-trip (forge → scanner) should land in the same shape.
5. **Verify what the contract promises, not just what's listed.** If `scheduled_actions[]` declares an action, verify the underlying install. If the build_spec promises one, the scanner-discovery loop should surface it on first sweep.

---

## 4 — Schema additions (manifest v16)

Extend the existing `scheduled_actions[]` field ([manifest.py:116](../packages/admin/evolve_admin/applications/manifest.py)) with a `mechanism` discriminator. This is additive — pre-v16 entries default to `mechanism: "unknown"` and the scanner backfills.

```json
"scheduled_actions": [
  {
    "id": "task-check",
    "mechanism": "oc_heartbeat_hook",   // NEW — see §4.1
    "trigger": {
      "kind": "heartbeat",              // existing — kind ∈ heartbeat | cron | launchd | session_start
      "schedule": "every_heartbeat",    // existing — interpreted per kind
      "evidence_path": "TASKS.md",
      "evidence_locator": "section 'Cron / Automation'"
    },
    "install": {                         // NEW — declarative install recipe
      "command": "python3 scripts/tasks.py check",
      "cwd": "${workspace}",
      "output_signals": ["TASK_DUE:", "FOLLOWUP_NEEDED:"],
      "silent_when_no_output": true,
      "plist_label": null,               // populated only for kind=launchd
      "hook_event": "heartbeat",         // populated only for kind ∈ {heartbeat, session_start, ...}
      "exec_policy": "inherit"           // inherit | preflight_allowed_commands
    },
    "inputs": [{"path": "tasks.json", "kind": "data_file"}],
    "outputs": [{"kind": "session_message", "channel": "primary"}],
    "summary": "Surface overdue tasks and follow-up needs",

    // Populated by forge / scanner / verifier — not authored
    "installed_at": "2026-06-02T01:23:45Z",
    "installed_by": "forge:j-25b6b13f",
    "installed_artifact": "openclaw.json#hooks.heartbeat[2]",  // path-fragment back to the install site
    "last_verified": "2026-06-02T01:30:00Z"
  }
]
```

### 4.1 — `mechanism` enum

| Value | Where it lives | Install path | When to choose |
|---|---|---|---|
| `oc_heartbeat_hook` | `openclaw.json` `hooks.heartbeat[]` | L2 ConfigPatch via admin-daemon (same pattern as `UpdatePermissionConfig`, [project_l1_l2_applier_architecture]) | Periodic checks that produce session-visible output. **Default for new apps.** |
| `oc_session_hook` | `openclaw.json` `hooks.session_start[]` (or other event) | L2 ConfigPatch | Per-session injection (greetings, status, pending follow-ups) |
| `launchd` | `/Users/{bot}/Library/LaunchAgents/com.{bot}.{app}.{action}.plist` | admin-daemon `install_launch_agent` API | Out-of-session work; must run when bot is idle |
| `crontab` | bot user crontab via `sudo crontab -u {bot}` | admin-daemon `install_crontab_entry` API | Legacy; discouraged for new apps |
| `external` | Outside this pod (e.g. cloud cron) | None — declared for documentation only | Webhook-driven, GitHub Actions, etc. |
| `unknown` | — | — | Scanner-backfill default when attribution fails |

### 4.2 — Gallery build_specs become partially declarative

Today's build_specs embed plist XML and `launchctl load` shell commands in prose. New build_specs lift those into a structured `scheduled_actions[]` block at the top level of the gallery manifest. The narrative `build_spec` stops describing *how* to install — it only describes the script the action invokes.

Old gallery entry (excerpt):
> "**Register as LaunchAgent cron** (runs every 4 hours): ```<plist XML>``` Write to `/Users/{bot_id}/Library/LaunchAgents/...` and load: `launchctl load ~/Library/LaunchAgents/...`"

New gallery entry:
```json
"scheduled_actions": [{
  "id": "task-check",
  "mechanism": "oc_heartbeat_hook",
  "install": {"command": "python3 scripts/tasks.py check", "hook_event": "heartbeat", "silent_when_no_output": true},
  "summary": "Surface overdue / blocked / follow-up tasks every heartbeat"
}]
```

The build_spec prose then says *nothing* about plists or `launchctl`. The bot LLM writes the `check` command (it already does); forge installs the hook (new).

---

## 5 — Forge install phase (new "Phase 4.5: Materialize")

Insert between current Phase 4 (Test) and Phase 5 (Apply). Runs only if tests pass and operator approves. Per-action: best-effort, idempotent, fully reversible by `evolve-admin uninstall <app>`.

```
For each entry in manifest.scheduled_actions[]:
  mechanism = entry.mechanism
  match mechanism:
    case "oc_heartbeat_hook" | "oc_session_hook":
      → admin_daemon.patch_openclaw_json(bot_id, hook_event, command)
      → entry.installed_artifact = "openclaw.json#hooks.{event}[N]"
      → admin_daemon.kickstart_gateway(bot_id)   # ensure hook takes effect
    case "launchd":
      → admin_daemon.install_launch_agent(bot_id, plist_label, plist_xml)
      → entry.installed_artifact = "/Users/{bot}/Library/LaunchAgents/{label}.plist"
    case "crontab":
      → admin_daemon.install_crontab_entry(bot_id, schedule, command)
    case "external" | "unknown":
      → skip; record entry.installed_at = null, log warning
  stamp entry.installed_at, installed_by="forge:{job_id}"
```

Failures are non-fatal but visible:
- Each failed install becomes a `scheduled_action_install_failed` Signal (signal-store, see CLAUDE.md "Signal store" section), motivating an auto-proposal to retry or downgrade.
- The forge job result includes a per-action status table the operator sees on approval.

**The admin-daemon socket API** ([spec-evo-account-separation-2026-05-25.md §12](spec-evo-account-separation-2026-05-25.md) is the reference) gains three new methods:

- `install_launch_agent(bot_id, label, plist_xml) → {path, loaded: bool}`
- `install_crontab_entry(bot_id, schedule, command, label) → {entry_id}`
- `patch_openclaw_json(bot_id, json_pointer, value) → {schema_version, restart_required}`

Each writes via the existing `/tmp/`-staging + `sudo /bin/cp` + chmod pattern documented in CLAUDE.md (the "Writes — /tmp staging + sudo /bin/cp" section). For `openclaw.json` the L2 `UpdatePermissionConfig` applier already implements this; the new method generalizes the path.

---

## 6 — Scanner attribution (close the round-trip)

[scanner.py](../packages/admin/evolve_admin/applications/scanner.py) Phase 1 (Inventory) gains three new passes:

### 6.1 — LaunchAgents enumeration

For each plist in `/Users/{bot}/Library/LaunchAgents/`:
- Parse `Label`, `ProgramArguments`, `StartInterval` / `StartCalendarInterval`
- Attribute to an app via, in order:
  1. **Label namespace match**: `com.{bot}.{app-slug}.*` → app whose `id` slug-matches `{app-slug}`. (Forge installs use this convention.)
  2. **ProgramArguments path match**: if any arg references a file under `workspace/{path}` and that path matches an `app.files[*].path`, attribute to that app.
  3. **Heuristic match** (LLM Phase 2, low confidence): pass unattributed plists to the LLM clustering pass.
- Record into `WorkspaceInventory.launchd_entries[]`; downstream Phase 2 builds `scheduled_actions[]` from it.

### 6.2 — openclaw.json hooks enumeration

Read `/Users/{bot}/.openclaw/openclaw.json` `hooks` block. For each entry:
- Match the invoked command to an app's `files[*].path` (same path-match logic as above).
- If `hooks.{event}[N].command` references `scripts/tasks.py check`, attribute to the task-manager app.
- Record into `WorkspaceInventory.openclaw_hooks[]`.

### 6.3 — crontab enumeration (already partial)

Existing `sudo crontab -l` snapshot becomes app-attributed by the same path-match logic.

### 6.4 — Missing-scheduled-action finding

A new structural assertion (see §7) flags any app whose **build_spec** or **manifest.success_criteria.observable_outcomes** mentions automated/periodic surfacing but whose **scheduled_actions[]** is empty after scan + attribution. This is the "scan miss" the user wants to surface.

---

## 7 — Verifier expansion

`last_structural_verify` ([forge_engine.py:2147](../packages/admin/evolve_admin/applications/forge_engine.py) Phase C) gets six new assertions, layered on top of the existing file-presence walk. These overlap with the six Tier-2 assertions in [spec-audit-extensions-2026-05-17.md §3.3](spec-audit-extensions-2026-05-17.md) — this spec **implements** that earlier promise.

For each `scheduled_actions[]` entry:

1. **A1 — Install present.** Resolve `entry.installed_artifact`. If `oc_*_hook` → openclaw.json json-pointer resolves to a hook with matching `command`. If `launchd` → plist exists and `launchctl print` lists the label as loaded. If `crontab` → entry appears in `crontab -l`.
2. **A2 — Command runnable.** Dry-run the install's `command` (with `--help` or a `noop` flag where supported) and confirm exit 0.
3. **A3 — Inputs exist.** Every `inputs[*].path` (kind ≠ external) resolves under workspace.
4. **A4 — Evidence anchor.** `trigger.evidence_path` exists and contains the section named in `evidence_locator`.
5. **A5 — Output channel valid.** For `outputs[].kind = session_message` confirm the hook event will deliver to a session (i.e. the hook is registered in openclaw.json, not just declared on the manifest).
6. **A6 — No orphan installs.** Reverse check: every LaunchAgent matching `com.{bot}.*` and every `openclaw.json#hooks.*` entry whose command points into an app's workspace must appear on that app's `scheduled_actions[]`. Orphans surface as `unattributed_install` findings.

The result becomes `last_structural_verify.scheduled_actions: {ok: [...], missing_install: [...], orphan_install: [...], invalid: [...]}`.

---

## 8 — Audit calibration (P4 fix)

The tier-3 audit prompt (`packages/admin/evolve_admin/applications/audit/...`) gets two structural changes:

### 8.1 — Inject the build_spec's "Customization Guidance" section

When evaluating an installed app, pass the **build_spec's `## Customization Guidance` section** alongside the file diffs. The audit LLM currently sees the bot's code and the canonical spec, but doesn't see which divergences the spec *invited*. Loading the guidance section closes that loop — the LLM stops flagging spec-blessed customizations as drift.

Reference personal-bot's false positive: the spec said *"Customize TAG_ALIASES for this bot's domain"* and the bot did; the audit flagged it as drift because that sentence wasn't in its context window.

### 8.2 — Tighten "broken_path" semantics

`broken_path` requires the auditor to:
- Trace the code path it claims is broken,
- Identify the input that triggers it,
- Confirm the path runs in normal operation (not a dead branch).

The personal-bot `task_updater.py --dry-run` false positive failed all three — it flagged absence of a side-effect on a code path that never writes by design.

A 50-word reasoning section in the audit response (rubric-enforced) makes the auditor show its work and gives the dismiss path a clear pivot.

---

## 9 — Migration

### 9.1 — Schema bump (v15 → v16)

Additive only. Existing manifests get `mechanism: "unknown"` and `install: {}` on `scheduled_actions[]` entries. Manifests with `scheduled_actions: []` are unchanged.

### 9.2 — Backfill from scanner

Next scan pass (after §6 ships) populates `scheduled_actions[]` retroactively on team-bot-a's and team-bot-c's manifests from their existing LaunchAgents / openclaw.json hooks. No forge re-run required.

### 9.3 — Personal-bot remediation

- **Auto-remediation path**: the existing `manifest_drift` proposal (`467e19a4-...`) becomes auto-applicable: install the heartbeat hook via the new admin-daemon API.
- **Verification**: after install, re-run `last_structural_verify` → expect `scheduled_actions.ok: ["task-check"]`.

### 9.4 — Gallery `p-9bfa1c84` republish

Bump task-manager to `2026.06.02-1.2`:
- Move plist XML + `launchctl load` out of `build_spec` prose
- Add `scheduled_actions[]` block with `mechanism: "oc_heartbeat_hook"`
- Re-export to gallery so future forges install correctly on first try

### 9.5 — EA pack republish

Same treatment for `ea-pack`'s `commitment_tracker.py --list-due` (the missing-functionality finding `1ad27e69-...`). Either:
- Add a `scheduled_actions[]` entry with `oc_heartbeat_hook` that calls `--list-due` at heartbeat with a 24h debounce, **or**
- Wire `evening_sweep.py` to call `--list-due` and surface its output (build-time choice; the spec authoring guide should pick one).

---

## 10 — Acceptance criteria

This spec is "done" when **all** of the following hold:

1. A fresh forge of task-manager onto a clean test bot installs the heartbeat hook automatically and `scheduled_actions[0].installed_at` is populated.
2. The verifier's `scheduled_actions: ok` includes `task-check` after install; deleting the hook from openclaw.json flips it to `missing_install`.
3. Scanner re-pass on team-bot-a and team-bot-c attributes their existing `unified_task_system` and `tasks.py` schedules onto their manifests (team-bot-a's manifest gains a `scheduled_actions[]` entry with kind matching whatever they actually use today — likely crontab).
4. Re-auditing personal-bot's task-manager after the calibration changes (§8) produces ≤25% false-positive rate on the same finding set (today's 5 findings → ≤1 false positive).
5. An orphan LaunchAgent (one with no matching app) surfaces as an `unattributed_install` finding and routes to a Proposal asking "should this attach to app X or be removed?".

---

## 11 — Open questions

- **Heartbeat frequency.** OC's heartbeat interval is not formally documented (see exploration report §3). Before §5 ships, the heartbeat hook plumbing needs a documented minimum interval and a way to express "fire at most every N hours". Defer to OC upstream issue if needed.
- **Calendar-style triggers via hooks.** "Fire at 18:00 daily" doesn't map cleanly to "every heartbeat". Options: (a) LaunchAgent fallback for time-of-day schedules, (b) a thin OC hook that self-debounces by checking a state file, (c) a new OC event type. (a) is the safe near-term default.
- **Hook output routing.** For `outputs.kind: session_message`, does the bot see the heartbeat-hook output on its *next* turn, or asynchronously mid-session? `session_surface.py::handleSessionStart`'s `systemAppend` is the established channel; the heartbeat hook needs an equivalent (`systemAppend` for next turn? a "user-shaped pod note" channel like the one [feedback_evolve_bot_llm_visibility] proposed?). Resolve before §5 ships.
- **launchctl bootstrap vs. load.** `launchctl bootstrap` is the modern API but requires the user's launchd domain be active. For sleeping bots, fall back to `launchctl load` at next session-start. Implementation detail for the admin-daemon API.
- **Reversal.** `evolve-admin uninstall <app>` should undo the §5 installs. Spec the inverse operations (unload + delete plist; remove hook entry; remove crontab line) in the same PR as §5.

---

## 12 — Implementation order

1. **First PR — Schema + admin-daemon API.** Schema v16, three new admin-daemon socket methods, no behavior change yet.
2. **Second PR — Scanner attribution (§6).** Populates `scheduled_actions[]` from existing installs. Retroactive backfill for team-bot-a, team-bot-c, team-bot-d, etc. No forge changes.
3. **Third PR — Verifier assertions (§7).** Adds A1–A6 to `last_structural_verify`. Surfaces existing drift as findings. Still no forge install behavior.
4. **Fourth PR — Forge install phase (§5).** Wires forge to call the admin-daemon API. Audit personal-bot + ea-pack proposals become auto-applicable.
5. **Fifth PR — Audit calibration (§8).** Build_spec guidance injection + tightened broken_path rubric.
6. **Sixth PR — Test gate hardening (§13).** Constraint-verification critic, negative-path tests, orphan function detection.
7. **Seventh PR — Environment portability lint (§14).** Hardcoded-path + sudo-required-command detection in generated code AND in gallery build_specs.
8. **Eighth PR — Gallery republish.** New `p-9bfa1c84@2026.06.02-1.2` with declarative `scheduled_actions[]`, `systemsetup` bug fixed, `#!/usr/bin/env python3` shebangs.

Each PR independently shippable and verifiable. The personal-bot task-manager fix lands at PR 4; the team-bot-a/team-bot-c backfill at PR 2; the false-positive rate drop at PR 5. **PRs 6 and 7 should land before the team-bot-a-export round-trip test** (the validation plan's step 4) so the sandbox forge run exercises the hardened gate.

---

## 13 — Test gate hardening (constraint-as-test contract)

### 13.1 — The pattern this fixes

Three of the 2026-06-02 ea-pack findings share one root cause:

| Finding | Manifest declaration | Code reality |
|---|---|---|
| `behavior_mismatch` | `constraints.boundaries[0]`: "Fail silently with log entry" when the gateway is unreachable | Code raises / surfaces errors |
| `missing_functionality` | `identity.scope_includes`: "Configurable timing for all scheduled behaviors via bot config" | Timing hardcoded |
| `dead_code` | `ea_config()` defined as the bridge between bot config and behavior | Defined but never called |

The bot LLM **read the manifest's declarations and quietly omitted compliance**. Worst of all, `ea_config()` is the smoking gun: the LLM understood the intent enough to scaffold a config-loader function, then forgot to wire it up. The current test gate (`test_command` from the build_spec) only exercises happy paths — "add a task, list it, complete it" — which can't detect a missing config-driven behavior or an unhandled error case.

**Lesson:** *constraints declared in the manifest are not currently translated into tests of those constraints.* Forge runs the smoke test the build_spec provides, but doesn't independently verify that each `constraints.boundaries[]` item and each `identity.scope_includes[]` clause has an enforcement path in the code.

### 13.2 — Constraint-verification critic pass

Insert a new focused pass between the existing critic rounds and the test gate. The critic LLM is given:

- The full `constraints.boundaries[]` and `constraints.safety[]` arrays from the manifest
- The `identity.scope_includes[]` array
- The full generated code

It must produce, for each constraint/scope item, one of three verdicts:

- **`enforced`** — point to the file and line range that implements the constraint, with a one-sentence justification.
- **`absent`** — the constraint has no implementation in the code.
- **`unclear`** — the LLM can't tell from the code alone (legitimate when the constraint is "system-wide" or runtime-environmental).

Items marked `absent` block approval and route back to the bot LLM with the constraint quoted: *"the manifest promises X but the code doesn't implement it — add the enforcement or remove the promise."* Items marked `unclear` go to the operator review with the critic's reasoning.

Reference for the false-positive guard: the same critic should NOT flag a constraint that was explicitly relaxed in the build_spec's "Customization Guidance" section (same context-injection trick PR 0 used for the audit). Spec-blessed deviations don't count as "absent."

### 13.3 — Constraint-derived negative-path tests

When the build_spec declares a constraint of the form "X when Y" (e.g. "fail silently when gateway unreachable"), forge generates a test that:

1. Simulates Y (mocks the gateway as unreachable; sets a config value to override timing; etc.)
2. Asserts the constraint's behavioral signature (no exception leaks; log file gains an entry; exit code is 0; timing reflects the config override)

This is auto-generation from manifest text — Sonnet does it once during the forge build phase, the resulting test cases get appended to the build_spec's `test_command`. The test gate now runs both the happy path AND the constraint enforcement paths.

Where the constraint is too vague to auto-test ("be efficient"), the critic flags it for operator clarification rather than fabricating a test. Soft constraints become better-worded manifest text, not silently-passing tests.

### 13.4 — Orphan-function detection

Simple AST pass over the generated Python files: build a call graph from every `def` to every call site. Any top-level function with zero call sites is flagged. Excluded from the check:

- Functions whose name starts with `_test_` or that live in `tests/`
- Functions decorated with `@app.route`, `@click.command`, etc. (entry points the framework calls)
- The `main()` function

The bot LLM is asked to either wire up the orphan or delete it. `ea_config()` would have been caught immediately. The orphan check runs alongside the constraint critic, before the test gate.

### 13.5 — Why this isn't already in the spec author's job

The build_spec author *could* write better tests covering constraints. But:

- Auto-generation from declared constraints removes the spec author from the hot loop — every manifest constraint becomes machine-checkable, even ones the author forgot to test for.
- The orphan check catches LLM hallucination patterns the spec author would not have anticipated.
- Constraint-verification by a critic LLM is shaped differently from "did the smoke test pass" — it's "does each promise have an enforcement site?"

The three checks compose: the critic spots the missing wiring, the orphan check confirms the dangling implementation, the negative-path test proves the constraint actually holds in code execution. All three would have flagged `ea_config()` simultaneously.

---

## 14 — Environment portability lint

### 14.1 — The pattern this fixes

Two of the 2026-06-02 findings expose **hardcoded environment assumptions in generated code**:

| Finding | What was hardcoded | Why it fails |
|---|---|---|
| `broken_path` on ea-pack | `/Users/Shared/evolve-venv/bin/python3` in both `ea-morning.sh` and `ea-evening.sh` | Path lives on this pod's dev environment; not in `requirements.system[]`; would 404 on a fresh install. |
| `behavior_mismatch` on task-manager | `systemsetup -gettimezone` in `_local_tz()` | macOS requires admin for `systemsetup`. Silently falls back to UTC → tasks get wrong timestamps. **Inherited verbatim from the gallery build_spec.** |

The first is the bot LLM importing a fact from the dev environment without declaring it. The second is more uncomfortable: the gallery `p-9bfa1c84` build_spec ships with the buggy snippet, the bot LLM copies it faithfully, and the bug propagates to every install. **Spec-level bugs survive the forge because no pass examines the spec itself.**

The smoke test masks the timezone bug because UTC fallback exits 0 — the only way to know the timezone code is broken is to compare its output to the real system timezone, which the test doesn't do.

### 14.2 — Static lint over generated code

Forge-time AST + regex pass over every generated file. Flags:

1. **Absolute paths outside the bot's workspace** (`/Users/Shared/`, `/opt/`, `/etc/`, `/var/`) that aren't declared in `requirements.system[]`. The LLM either adds the dependency declaration or rewrites the path (e.g. discover Python via `shutil.which("python3")` or shebang).
2. **Shell commands known to require sudo on macOS:** `systemsetup`, `launchctl bootstrap`, `pmset`, `nvram`, `scutil --set`. The presence of these without sudo handling is a soft warning unless the manifest declares `requirements.privileged: true`.
3. **Python invocation via hardcoded venv path** instead of `#!/usr/bin/env python3` shebang. Use the shebang; let the deploy environment resolve Python.
4. **Implicit reliance on `LOCAL_TZ = ZoneInfo("UTC")` fallback** — any function with a `try: ... except: return ZoneInfo("UTC")` shape where the success branch involves an external command. Specifically catches the `systemsetup` pattern. Suggest `from datetime import datetime; datetime.now().astimezone().tzinfo` or `time.tzname` instead — no privilege required.

Output is a list of findings the bot LLM addresses during the critic loop. Persistent findings (the LLM can't or won't fix) become operator-visible decisions: accept (with `requirements.system[]` declaration) or reject (block forge approval).

### 14.3 — Symmetric lint over gallery build_specs

The same pass runs on gallery build_specs at publish time. The `systemsetup` snippet in `p-9bfa1c84` would have been flagged when the gallery entry was first published, and the spec author given a chance to use a portable alternative. New gallery entries that embed hardcoded paths or sudo-required commands without declaring them require explicit author override (a `requirements.system: ["…"]` block AND a one-line rationale in the build_spec).

This is the spec-quality dimension of the Cluster C finding. Fixing the bot's output without fixing the spec means every future install reproduces the same bug. The lint closes that loop.

### 14.4 — What this doesn't try to do

- Not a full sandbox / chroot validation — that's a much bigger investment.
- Not portability across operating systems (we're macOS-only for now).
- Not network-side environment ("does this domain resolve?"). Forge can't predict the user's network.

The 80/20 here is high-signal local-environment assumptions: paths, sudo, Python invocation. Three patterns cover the bulk of what the 2026-06-02 audit caught.

### 14.5 — Interaction with the §8 audit calibration

Once §14 is in place, future installs shouldn't produce Cluster C findings — they're caught at forge time. The audit's `broken_path` rubric tightening from §8.2 stays in place as a backstop for cases the lint missed (new patterns, novel commands). The two layers complement each other.
