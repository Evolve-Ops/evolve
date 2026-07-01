# `app_permission_drift` — design spec (B.1)

**Status:** Draft. Implementation spec for Phase B.1 of [spec-app-derived-permissions-2026-05-24.md](spec-app-derived-permissions-2026-05-24.md) §4.

**Date:** 2026-05-25.

**Parent spec:** [spec-app-derived-permissions-2026-05-24.md §4](spec-app-derived-permissions-2026-05-24.md) — establishes the generator at a high level; this spec pins the implementation details (Signal schema, finding kinds, monitor logic, proposal mapping, mode-awareness rules).

**Sibling:** B.2 (`app_permission_review`) — separate spec, separate PR. B.1 targets exec-approvals drift specifically; B.2 targets manifest-declaration hygiene.

---

## Goal

Detect three classes of state inconsistency between each bot's app manifests and its live `exec-approvals.json` / workspace, emit per-finding Signals for visibility on the Alerts page, and emit per-finding Proposals so the operator can selectively accept fixes.

This is the *first* concrete consumer of the reconciler's manifest-derived permission model. Phase A computes the would-be allowlist; B.1 detects when reality and that model disagree.

---

## Architecture

Mirrors the existing `permission_monitor` (producer) + `auth_drift_filler` (generator) split. Two new modules:

1. **Monitor** at `packages/analyzer/permissions/app_manifest_monitor.py` — observes per-bot manifest + exec-approvals + workspace state, emits `app_permission_drift` Signals via `signals.store.observe` + `signals.store.sweep_resolve`.

2. **Generator** at `packages/analyzer/generators/app_permission_drift/` — consumes those Signals, fans out per-finding Proposals.

The split gives the operator two surfaces: findings visible on the Alerts page (Signals), and actionable proposals in the Improvements queue (Proposals).

### Why this matches `permission_monitor` / `auth_drift_filler` shape

- The parent spec §4 explicitly says: *"This is the inverse of the existing `auth_drift_filler` family."* Inverse in intent; same shape in implementation.
- Same signal-store dedup semantics get us free idempotency (same drift doesn't refire).
- Same generator charter invariants surface — action-kind allowlists, touches denylists.
- Architectural consistency: every other monitor+generator pair on the pod uses this split.

---

## Signal schema

Single Signal type, four finding sub-kinds.

### `app_permission_drift`

| Field | Value |
|---|---|
| `producer` | `"app_manifest_monitor"` |
| `type` | `"app_permission_drift"` |
| `flavor` | `"maintenance"` |
| `scope` | `"bot"` |
| `bot_id` | the bot |
| `signature` | `f"app_permission_drift:{bot_id}:{kind}:{pattern_or_path}"` |
| `severity` | derived (see §"Severity mode-awareness" below) |
| `title` | short headline, e.g. `"team-bot-a: scripts/journal.py declared but not allowlisted"` |
| `body` | longer prose for the Alerts page |
| `details` | structured payload (schema below) |

#### `details` schema

```jsonc
{
  "kind": "declared_not_allowed"
        | "allowed_not_declared"
        | "workspace_orphan_script"
        | "declared_missing_file",

  // Which app the finding implicates. Null only for workspace_orphan_script.
  "app_id": "i-XXXXXXXX" | null,
  "app_name": "Task Manager" | null,

  // The pattern (for exec entries) or path (for workspace/file findings).
  "pattern": "scripts/journal.py",

  // The bot's current tools.exec.security mode at observation time.
  "current_mode": "full" | "allowlist" | "deny",

  // The bot's role — used by the generator to skip primary bots when
  // emitting allowlist-mutation proposals.
  "role": "member" | "primary",

  // For allowed_not_declared: which manifest used to declare it.
  "former_declaring_app_id": "i-XXXXXXXX" | null,

  // Free-form rationale string carried forward to the proposal body.
  "rationale": "manifest scripts/journal.py is in files[]/realized_files[] but no agents.main.allowlist entry matches",
}
```

The signature includes `kind` and the pattern/path so the four finding sub-kinds dedupe independently. Same `pattern` flagged as both orphan and missing-file (theoretically possible if the workspace listing races against the manifest) produces two distinct signals, not one.

---

## Finding kinds

### 1. `declared_not_allowed`

**Definition.** An exec entry the reconciler would compute (script path from `files[]` / `realized_files[]` / `crons[].script`, or pattern from explicit `permissions.exec`) is missing from the bot's live `exec-approvals.json`.

**Triggers when.**
- The reconciler's `_entries_for_app(manifest)` produces an entry with `kind="exec"`.
- That entry's pattern is not present in any `agents.<id>.allowlist` / `agents.<id>.approvals` / `defaults.allowlist` array in the live `exec-approvals.json`.

**Severity.**
- In `allowlist` mode: **critical**. The bot literally can't run a declared script. This is the original team-bot-a Slack failure shape.
- In `full` mode: **info**. The bot can run it (full mode bypasses the allowlist), but the allowlist seed is incomplete — Phase C's opt-in flow would land the operator in a broken `allowlist` state if applied as-is.
- In `deny` mode: **suppressed**. The bot doesn't run anything; the allowlist is moot.

**Proposal action.** `UpdateExecApproval(operation="add", scope="agent", agent_id="main", pattern=<the script path>)`.

### 2. `allowed_not_declared`

**Definition.** An entry exists in the live `exec-approvals.json` whose source is `app-derived` (per provenance) but no installed app declares it.

**Triggers when.**
- An entry's pattern doesn't match any pattern the reconciler computes from current manifests.
- AND (in Phase A, where exec-approvals.json doesn't carry explicit provenance metadata for legacy entries): the entry doesn't look like an operator-set entry. Heuristic for B.1: skip entries that look like sysadmin commands (regex matches against `/usr/sbin/`, `/usr/bin/sudo`, etc.) — those are most likely operator-set, not stale-app-derived. Tracked as a follow-on: once Phase C lands explicit provenance tagging in the exec-approvals.json schema, this heuristic gets replaced with an explicit `source` field check.

**Severity.**
- In `allowlist` mode: **warn**. The entry is granting permission for a script that no longer has a declaring app. Worth removing for hygiene, but not breaking anything.
- In `full` mode: **suppressed**. Same as above — exec-approvals.json isn't gating anything in full mode, so a stale entry is harmless.
- In `deny` mode: **suppressed**.

**Proposal action.** `UpdateExecApproval(operation="revoke", scope="agent", agent_id="main", pattern=<the pattern>)`.

### 3. `workspace_orphan_script`

**Definition.** A `.py` / `.sh` / `.bash` / `.zsh` file exists in the bot's workspace, but no manifest's `files[]` / `realized_files[]` / `permissions.exec` declares it.

**Triggers when.**
- Walking `/Users/<bot>/.openclaw/workspace/` (excluding `manifests/`, `.git/`, `evolve/`, `__pycache__/`).
- Encountering a file with a script extension whose workspace-relative path doesn't appear in any manifest the reconciler reads for this bot.

**Severity.** Always **info**, regardless of mode. The script can run (in full mode) or can't (in allowlist mode), but either way it's a *visibility* finding — the bot has scripts off-manifest. Operator can decide to declare it, retire it, or note it as operator-set.

**Proposal action.** `Investigation` — surfaces the orphan, asks the operator to bind to an app or retire. Mutating the bot's state isn't B.1's job; the operator runs a scan / forge / spec-session to decide.

**Note on cost.** Walking the workspace dir per-bot per-run could be slow for bots with large workspaces. B.1 uses a max-depth cap (4) and a max-files cap (500) on the walk — over either, emit a single `workspace_orphan_script` Signal with `details.kind="workspace_walk_truncated"` flagging the problem rather than missing real orphans silently. Production cadence is daily so the walk runs at most once per day per bot.

### 4. `declared_missing_file`

**Definition.** A manifest declares a file (in `files[]`, `realized_files[]`, or `permissions.exec`) that doesn't exist on disk.

**Triggers when.**
- For each entry the reconciler reads, check whether the file at `workspace_root/path` exists.
- If not, emit a finding tagged to the declaring app.

**Severity.** Always **info**. The manifest is stale; the script's gone. Not breaking anything (no script means no exec call).

**Proposal action.** `Investigation` — surfaces the stale declaration, asks the operator to remove it from the manifest. Direct manifest mutation is B.2's job (`app_permission_review` proposes manifest narrowing); B.1 emits an Investigation so the finding doesn't get lost.

---

## Severity mode-awareness

The monitor reads the bot's current `tools.exec.security` from `openclaw.json` once per run. It uses that mode in two places:

1. **Severity assignment** (per-kind, per the table above).
2. **Signal emission gating.** Several kinds are suppressed in modes where they're meaningless (e.g. `allowed_not_declared` is suppressed in `deny` mode because `exec-approvals.json` isn't being read at all). The monitor emits no Signal for suppressed cases — `sweep_resolve` then auto-archives any previously-firing instance.

`details.current_mode` is always written so the generator (and the Alerts UI) can reason about why a finding has a given severity.

---

## Monitor implementation outline

```python
# packages/analyzer/permissions/app_manifest_monitor.py

PRODUCER = "app_manifest_monitor"

def scan_bot(shared_dir: Path, bot_id: str, network: dict) -> int:
    """Observe one bot's state, emit/resolve Signals. Returns # signals observed."""
    from evolve_admin.app_permissions.reconciler import _entries_for_app, _iter_manifest_files
    from evolve_admin.config import bot_home, get_bot_user

    bot_user = get_bot_user(bot_id, network)
    role = (network.get("bots") or {}).get(bot_id, {}).get("role") or "member"
    home = bot_home(bot_id, network)
    workspace = home / ".openclaw" / "workspace"
    manifests_dir = workspace / "manifests"

    # Read live state.
    oc_cfg = _read_openclaw_json(bot_user)  # via sudo /bin/cat fallback
    exec_approvals = _read_exec_approvals(bot_user)
    current_mode = (oc_cfg or {}).get("tools", {}).get("exec", {}).get("security", "")

    # Collect declared-by-manifests entries (reuse reconciler logic).
    declared: dict[str, dict] = {}  # path → {app_id, app_name}
    for mpath in _iter_manifest_files(manifests_dir):
        try:
            m = json.loads(mpath.read_text())
        except Exception:
            continue
        for entry in _entries_for_app(m):
            if entry.kind != "exec":
                continue
            declared.setdefault(entry.pattern, {
                "app_id": entry.app_id,
                "app_name": entry.app_name,
            })

    # Collect live allowlist entries.
    allowed: set[str] = _collect_allowlist_patterns(exec_approvals)

    # Collect workspace scripts.
    workspace_scripts = _walk_workspace_scripts(workspace)  # bounded walk

    kept_signatures: set[str] = set()

    # 1. declared_not_allowed
    if current_mode != "deny":
        for path, info in declared.items():
            if path in allowed:
                continue
            kept_signatures.add(_emit(
                shared_dir, bot_id, kind="declared_not_allowed",
                pattern=path, app_id=info["app_id"], app_name=info["app_name"],
                current_mode=current_mode, role=role,
                severity=("critical" if current_mode == "allowlist" else "info"),
            ))

    # 2. allowed_not_declared
    if current_mode == "allowlist":
        for pat in allowed:
            if pat in declared:
                continue
            if _looks_operator_set(pat):
                continue
            kept_signatures.add(_emit(
                shared_dir, bot_id, kind="allowed_not_declared",
                pattern=pat, app_id=None, app_name=None,
                current_mode=current_mode, role=role,
                severity="warn",
            ))

    # 3. workspace_orphan_script
    for script_path in workspace_scripts:
        if script_path in declared:
            continue
        kept_signatures.add(_emit(
            shared_dir, bot_id, kind="workspace_orphan_script",
            pattern=script_path, app_id=None, app_name=None,
            current_mode=current_mode, role=role,
            severity="info",
        ))

    # 4. declared_missing_file
    for path, info in declared.items():
        full = (workspace / path) if not Path(path).is_absolute() else Path(path)
        if full.exists():
            continue
        kept_signatures.add(_emit(
            shared_dir, bot_id, kind="declared_missing_file",
            pattern=path, app_id=info["app_id"], app_name=info["app_name"],
            current_mode=current_mode, role=role,
            severity="info",
        ))

    # Auto-resolve everything we didn't re-emit this run.
    signals_store.sweep_resolve(
        shared_dir,
        producer=PRODUCER,
        kept_signatures=kept_signatures,
        scope_filter={"bot_id": bot_id},
    )
    return len(kept_signatures)
```

`_emit(...)` calls `signals_store.observe(...)` with the right signature, returns the signature for the kept-set.

The shared dependence on `_entries_for_app` from the reconciler is intentional — keeps the monitor and the preview file in agreement about what manifests declare.

---

## Generator implementation outline

```python
# packages/analyzer/generators/app_permission_drift/observe.py

GENERATOR_ID = "app_permission_drift"
PRODUCER = "app_manifest_monitor"
SIGNAL_TYPE = "app_permission_drift"

def observe(ctx: AppPermissionDriftContext) -> list[Proposal]:
    proposals = []
    for sig in signals_store.iter_active(
        ctx.shared_dir,
        producer=PRODUCER,
        bot_id=ctx.bot_id,
        state="firing",
    ):
        if getattr(sig, "type", None) != SIGNAL_TYPE:
            continue
        try:
            proposals.extend(make_proposals(sig))
        except Exception:
            continue
    return proposals
```

```python
# packages/analyzer/generators/app_permission_drift/signal_proposals.py

def make_proposals(signal) -> list[Proposal]:
    """One signal → one proposal. Fan-out lives at the monitor (one Signal
    per finding), so the generator is a thin Signal → Proposal mapper."""
    details = _details(signal)
    kind = details.get("kind")
    bot_id = signal.bot_id
    pattern = details.get("pattern")
    sig_id = signal.id
    current_mode = details.get("current_mode")
    role = details.get("role")

    # Primary bots: never emit allowlist-mutation proposals. The
    # primary-bot-deny carve-out + the upcoming spec-evo-account-separation
    # mean exec-approvals on a primary bot are out of scope.
    if role == "primary" and kind in ("declared_not_allowed", "allowed_not_declared"):
        return []

    if kind == "declared_not_allowed":
        return [_add_approval_proposal(bot_id, pattern, details, sig_id, current_mode)]
    if kind == "allowed_not_declared":
        return [_revoke_approval_proposal(bot_id, pattern, details, sig_id)]
    if kind == "workspace_orphan_script":
        return [_orphan_investigation(bot_id, pattern, sig_id, current_mode)]
    if kind == "declared_missing_file":
        return [_missing_file_investigation(bot_id, pattern, details, sig_id)]
    return []
```

Per-finding factories build a single `Proposal` each with provenance + risk_tag + `motivating_signals=[sig_id]`. Proposal body explains the finding in operator-readable prose and (for `full`-mode `declared_not_allowed`) notes that the proposal is preparatory rather than immediately corrective.

### Mode-aware proposal phrasing

For `declared_not_allowed` in `full` mode, the proposal body reads roughly:

> Team-Bot-A's `task-manager` app declares `ops/tools/unified_task_system.py` in `realized_files` but no `exec-approvals.json` entry exists. The bot can currently run this script (`tools.exec.security="full"`), but if you later switch to `allowlist` mode this exec will fail. Apply this proposal to add the entry preemptively, or wait — Phase C will seed the allowlist from manifests automatically.

In `allowlist` mode:

> Team-Bot-A's `task-manager` app declares `ops/tools/unified_task_system.py` in `realized_files` and `tools.exec.security="allowlist"`, but no entry exists in `exec-approvals.json`. The bot cannot run this script. Apply this proposal to add the entry.

Same proposal action; different operator framing.

---

## Charter (`charter.yaml`)

```yaml
id: app_permission_drift
schema_version: 1
type: guardian
dimension: safety
bucket: improve
purpose: >
  Consume app_permission_drift Signals from app_manifest_monitor and
  emit per-finding proposals: UpdateExecApproval (add/revoke) for
  exec-approvals reconciliation, Investigation for workspace orphans
  and stale manifest declarations. Mirrors auth_drift_filler shape
  but pushes toward correctness-as-declared-by-intent, not toward a
  security baseline.
cadence: daily
resolves_when_silent: true
invariants:
  - id: action_kind_allowed
    description: >
      Emits typed UpdateExecApproval (allowlist mutation) and
      Investigation (operator-driven) only. No raw ConfigPatch.
      Never mutates manifests directly — that's B.2's job.
    check_kind: action_kind_allowed
    params:
      allowlist: [UpdateExecApproval, Investigation]
  - id: touches_forbidden
    description: >
      Filler only touches exec-approvals.json. Never reaches plugins,
      channel config, cron, gateway core, or manifest files.
    check_kind: touches_forbidden
    params:
      forbidden: [plugins, channel_config, gateway_core, cron_config, manifest]
```

Daily cadence matches `auth_drift_filler` and `permission_monitor`. Findings are visibility/hygiene, not real-time incidents.

---

## Registration

In `packages/analyzer/generator_runner.py`:

```python
def _make_app_permission_drift_ctx(
    shared_dir, network_config, bot_id, gen_config, now,
):
    from generators.app_permission_drift.observe import AppPermissionDriftContext
    if bot_id is None:
        return None  # per-bot only
    return AppPermissionDriftContext(bot_id=bot_id, shared_dir=shared_dir)

_CONTEXT_FACTORIES["app_permission_drift"] = (_make_app_permission_drift_ctx, True)
```

The monitor (`app_manifest_monitor.scan_bot`) gets called from wherever `permission_monitor` is currently scheduled — likely a sibling invocation in `better_engine_refresh.py` before `run_generators(...)` runs, so the Signals are fresh when the generator iterates.

---

## Provenance preservation

The exec-approvals.json on the mini today doesn't carry per-entry `source` metadata. B.1's `allowed_not_declared` finding therefore has a tail risk: an operator-set entry (security-bot's 29 today) could be mistaken for a stale-app-derived entry. Mitigation: the `_looks_operator_set` heuristic (matches sudo-form, sysadmin paths, etc.) plus operator review at the proposal stage.

Phase C will introduce explicit `source` metadata in exec-approvals entries (per the parent spec §3). Once that lands, B.1 should:

1. Read the `source` field directly instead of heuristic-matching.
2. Skip operator-set entries explicitly (only emit `allowed_not_declared` for `source: app-derived`).
3. Optionally emit a new finding kind `legacy_unclassified_entry` for entries with no source tag.

These are follow-on changes when Phase C ships the schema extension.

---

## Test plan

### Monitor tests (`tests/test_app_manifest_monitor.py`)

Synthetic bot home (`home_override`), each test case constructs a specific manifest + exec-approvals + workspace state and asserts the expected Signal emission:

- `test_declared_not_allowed_critical_in_allowlist_mode`
- `test_declared_not_allowed_info_in_full_mode`
- `test_declared_not_allowed_suppressed_in_deny_mode`
- `test_allowed_not_declared_warn_in_allowlist_mode`
- `test_allowed_not_declared_suppressed_in_full_mode`
- `test_allowed_not_declared_skips_operator_set_heuristic` (sudo paths)
- `test_workspace_orphan_script_info_regardless_of_mode`
- `test_declared_missing_file_info_regardless_of_mode`
- `test_signal_signature_dedups_across_runs`
- `test_sweep_resolve_archives_fixed_findings`
- `test_walk_depth_cap_prevents_runaway`

### Generator tests (`tests/test_app_permission_drift_generator.py`)

Synthetic Signal dicts → expected `Proposal` outputs:

- `test_declared_not_allowed_emits_UpdateExecApproval_add`
- `test_allowed_not_declared_emits_UpdateExecApproval_revoke`
- `test_workspace_orphan_script_emits_Investigation`
- `test_declared_missing_file_emits_Investigation`
- `test_primary_bot_skips_allowlist_mutation_proposals`
- `test_proposal_carries_motivating_signal_id`
- `test_proposal_carries_provenance_and_risk_tag`
- `test_proposal_body_mode_aware_phrasing` (full vs. allowlist for `declared_not_allowed`)

### Integration tests

- `test_monitor_then_generator_roundtrip` — observe state via monitor, run generator, assert proposals match findings 1:1.
- `test_charter_invariants_pass_against_emitted_proposals` — every emitted Proposal must satisfy the charter's `action_kind_allowed` + `touches_forbidden`. (Existing arbiter test pattern.)

---

## Out of scope for B.1

- **Auto-applying proposals.** B.1 emits proposals; the operator approves them through the existing approval flow. No bypass.
- **Mutating manifests.** That's B.2's job. B.1's `Investigation` proposals for stale declarations surface the finding but don't propose the manifest edit.
- **Cross-bot consolidation.** B.2's pod-aware second pass (parent spec §5) is out of B.1's scope — B.1 looks at each bot in isolation.
- **Network egress, fs_read, fs_write, env declarations.** Advisory in Phase A; B.1 doesn't surface them. Future generator territory.
- **Explicit provenance reading in exec-approvals.json.** Heuristic-based for B.1; explicit `source` field reading lands when Phase C extends the schema.

---

## Success criteria

This generator has worked when:

1. **The team-bot-a Slack failure shape produces a Signal.** Reproduce: bot in `allowlist` mode, manifest declares a script, exec-approvals missing the entry → `app_permission_drift` Signal fires with `severity=critical, kind=declared_not_allowed`.
2. **The "manifests still incomplete" visibility gap is surfaced.** Bot in `full` mode with workspace scripts not declared → `workspace_orphan_script` Signals fire at info severity. Operator sees them on the Alerts page.
3. **Proposals route through the existing approval flow.** Each finding produces a per-finding Proposal in the pending queue. Operator approves selectively. UpdateExecApproval applier writes the entry; the next monitor pass sweep-resolves the Signal because the entry now matches.
4. **The monitor doesn't refire fixed findings.** Once an `UpdateExecApproval(add)` proposal applies, the entry is in `exec-approvals.json`, the next monitor run doesn't see the gap, the Signal sweep-resolves. No infinite "still drifted" loop.
5. **Primary bots are skipped for allowlist-mutation proposals.** Even if evo's manifests declare scripts (unlikely but possible during Phase E transition), B.1 doesn't emit mutation proposals against evo's `exec-approvals.json`.
