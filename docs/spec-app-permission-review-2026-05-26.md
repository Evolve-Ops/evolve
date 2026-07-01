# `app_permission_review` + `app_permission_bootstrapper` — design spec (B.2)

**Status:** Draft. Implementation spec for Phase B.2 of [spec-app-derived-permissions-2026-05-24.md](spec-app-derived-permissions-2026-05-24.md) §5.

**Date:** 2026-05-26.

> **2026-05-25 update — `app_permission_bootstrapper` retired.** A 124-proposal
> triage surfaced that one Investigation per manifest-completeness gap was just
> noise the operator had to clear by hand. The bootstrapper has been removed;
> its inference is now run at scan time by the App Scan's Pass C backfill in
> `evolve_admin.applications.scanner` (look for `_infer_permissions_block`).
> Apps with no `permissions:` block get one inferred at the next scan; apps
> that already have one are left untouched (the spec keeps `permissions` OUT
> of `AUTO_MANAGED_FIELDS` precisely because once-written values are
> operator-owned). The `app_permission_review` generator described below is
> unaffected and still ships as documented.

**Parent spec:** [spec-app-derived-permissions-2026-05-24.md §5](spec-app-derived-permissions-2026-05-24.md) — the high-level design.

**Sibling spec (already shipped):** [spec-app-permission-drift-2026-05-25.md](spec-app-permission-drift-2026-05-25.md) — B.1's drift detector. B.1 catches drift between *manifests* and *exec-approvals.json*. B.2 catches drift *within* manifests themselves.

---

## Goal

Keep each app's manifest-declared permissions honest:

- **Necessary** — declared entries are still referenced in the app's actual scripts.
- **Sufficient** — everything the app's scripts actually do is declared.
- **Right-scoped** — explicit wildcards aren't wider than the scripts justify.

Plus seed the system: since the pod has **0 manifests with `permissions:` blocks today** (Q6 audit, parent spec §"Open question 6"), B.2 needs a bootstrapper that proposes initial blocks from the reconciler's inferred entries. Without that, B.2's static checks have nothing to operate on.

Two new generators, paired:

1. **`app_permission_bootstrapper`** — for each manifest without a `permissions:` block, propose an initial block. Investigation-shaped: the operator copies the suggested block into the manifest manually (no typed `UpdateManifest` applier in v1 per the agreed scope).
2. **`app_permission_review`** — for each manifest *with* a `permissions:` block, run the spec's necessary/sufficient/overkill static checks plus the pod-aware consolidation pass.

Both pure Python, no LLM cost.

---

## Scope decisions (locked in for v1)

These were settled in the conversation that preceded this spec:

1. **Both generators ship in one PR** as paired work.
2. **All proposals are `Investigation`-shaped** in v1. No new typed `UpdateManifest` applier. The operator reads the proposal context and edits the manifest by hand. A typed applier can land later if friction is meaningful — likely in Phase C alongside the opt-in UI.
3. **`permissions._affirmed` is read-only in v1.** The generator skips entries that match. The mechanism for *writing* affirmations from proposal rejections is deferred — for now the existing rejection-cooldown in `generator_runner._refresh_existing` covers "operator rejected this; don't show it again for 14 days," which is most of the practical need.
4. **Spec §5 stays as-written.** No scope changes to the pod-aware second pass, the three outcomes for cross-referenced findings, or the finding categories.

---

## Architecture

### Direct-observation generators, no monitor split

Unlike B.1 (monitor + generator split via Signals), B.2 uses direct-observation generators that read manifests inside `observe()` and emit Proposals directly. Rationale:

- B.2's findings are slow-moving manifest-quality suggestions, not real-time alerts. The Alerts-page visibility B.1 gains from a Signal intermediary isn't load-bearing here.
- The pod-aware second pass needs all-apps-on-this-bot context in one place anyway. That's natural inside a single `observe()` call per bot.
- Halves the surface area of the change without losing capability.

### Triggers

Per parent spec §5:

- **Primary trigger:** runs as a phase of `scan_workspace_pipeline` after manifest generation. Operators expect findings when they explicitly scan.
- **Secondary trigger:** standalone scheduled job at cadence `weekly`.

Implementation: both triggers funnel through the same `observe(ctx)` entry point. The scheduled-job invocation is the standard `generator_runner` path. The scan-pipeline invocation calls a new helper `generator_runner.run_one_generator(shared_dir, network, generator_id, bot_id)` from the tail of `scan_workspace_pipeline`. The helper reuses the existing ingest pipeline (dedup, fingerprint, charter invariants, rejection cooldown) so the scan path and the scheduled path produce identical proposals.

### File layout

```
packages/analyzer/generators/
├── app_permission_bootstrapper/
│   ├── __init__.py
│   ├── charter.yaml
│   ├── observe.py
│   └── proposals.py
└── app_permission_review/
    ├── __init__.py
    ├── charter.yaml
    ├── observe.py
    ├── review.py          # First pass — per-app static checks
    ├── consolidation.py   # Second pass — pod-aware cross-reference
    └── proposals.py       # Finding → Investigation Proposal factory
```

Plus:
- `packages/analyzer/generator_runner.py` — register both generators + add `run_one_generator(...)` helper
- `packages/admin/evolve_admin/applications/scanner.py` — call `run_one_generator` from `scan_workspace_pipeline` tail for `app_permission_review` only (bootstrapper runs on cadence, not on every scan)

---

## `app_permission_bootstrapper`

### When it fires

For each non-hidden, non-deprecated manifest on the bot whose `permissions:` field is absent or empty AND whose reconciler-inferred entries (from `_entries_for_app`) are non-empty:

→ Emit one Investigation proposal suggesting an initial `permissions:` block.

### What the proposal says

The Investigation's `context` contains:
- The app's id + name
- The proposed block as JSON (so the operator can copy-paste into the manifest)
- A one-sentence rationale per kind:
  - `exec` entries from the reconciler's `inferred` set
  - `crons` entries (each cron's schedule + script)
- A reminder that fs_read / fs_write / network_egress can be added by the operator after running the app's scripts past static review

Example context:

```
The "Task Manager" (i-task) app on team-bot-a has no `permissions:` block yet. The
reconciler infers these entries from its files/realized_files/crons:

  permissions:
    exec:
      - ops/tools/unified_task_system.py
      - ops/tools/scratch_session_compactor.py

To activate this block, edit /Users/team-bot-a/.openclaw/workspace/manifests/i-task.json
and add the above under a top-level "permissions" key. Once the block is in
place, app_permission_review will start checking it for necessary/sufficient/
overkill issues, and Phase C's opt-in toggle will be able to switch the bot
to allowlist mode using this as the seed.

fs_read, fs_write, and network_egress are advisory today (no OC runtime
enforcement) — add them based on what the app's scripts actually do; they'll
flow into audit visibility now and runtime enforcement once OC ships the
corresponding policy primitives.
```

### Charter

```yaml
id: app_permission_bootstrapper
cadence: weekly
type: guardian
dimension: safety
bucket: improve
purpose: >
  For each installed app without a permissions: block, propose an
  initial block seeded from the reconciler's inferred entries. Closes
  the back-loaded-value gap in B.2: until apps have permissions
  blocks, the review generator has nothing to check.
invariants:
  - id: action_kind_allowed
    params:
      allowlist: [Investigation]
  - id: touches_forbidden
    params:
      forbidden: [auth_config, plugins, channel_config, gateway_core, cron_config]
```

### Why weekly (not on every scan)

The bootstrapper's findings are stable — an app either has a `permissions:` block or it doesn't, and that changes only when an operator approves a proposal. Running on every scan would re-emit the same proposals every time the operator clicks "scan workspace," which is annoying. Weekly cadence + the 14-day rejection cooldown means the operator sees a fresh bootstrapper proposal per app once after install and (maybe) once after dismissal expires. After that, blocks stay populated and the bootstrapper is silent for that app.

---

## `app_permission_review` — first pass (per-app static checks)

For each non-hidden manifest *with* a `permissions:` block, run the spec's three check categories. Each check produces a candidate finding; the candidate is filtered through the `_affirmed` set, then passed to the consolidation pass.

### Finding kinds (first pass)

| Kind | Category | Severity | Trigger |
|---|---|---|---|
| `permission_exec_unused` | Necessary | warn | `permissions.exec[]` entry references a file that doesn't exist OR is never referenced by any script in the app's `files[]` / `realized_files[]` |
| `permission_fs_read_unused` | Necessary | info (advisory) | `permissions.fs_read[]` path is never grep-matched in any of the app's scripts |
| `permission_fs_write_unused` | Necessary | info (advisory) | Same as above for `fs_write` |
| `permission_network_egress_unused` | Necessary | info (advisory) | `permissions.network_egress[]` host is never grep-matched |
| `permission_env_unused` | Necessary | info (advisory) | `permissions.env[]` var name is never grep-matched |
| `permission_exec_missing_declaration` | Sufficient | warn | A script in `files[]`/`realized_files[]` is not covered by any inferred OR explicit `permissions.exec` entry. Overlaps with B.1's `declared_not_allowed` — but B.2 specifically flags the *manifest* needing the entry, whereas B.1 flags the *exec-approvals.json* needing the entry. Different remediation target. |
| `permission_egress_missing_declaration` | Sufficient | info (advisory) | A network host hardcoded in a script doesn't appear in `permissions.network_egress[]` |
| `permission_exec_overkill_wildcard` | Overkill | info | Explicit `permissions.exec` entry contains a wildcard significantly broader than the matching script paths. Lower confidence; doesn't fire unless the wildcard match set is much larger than the actual files. |
| `permission_egress_overkill_wildcard` | Overkill | info | `permissions.network_egress` wildcard significantly broader than grep-matched hosts. |

### Static-check heuristics

Necessary checks:
- **`exec` entry → file exists?** Resolve the entry's path against the workspace root. `Path.exists()` check.
- **`exec` entry → referenced by any script?** Grep all `.py` / `.sh` / `.bash` scripts in this app's `files[]` + `realized_files[]` for the entry's basename. Suppresses false positives when an exec entry references a script that's only invoked from outside the app (cron, launchd, etc.).
- **`fs_read` / `fs_write` / `network_egress` / `env` → grep-matched?** Substring search across the app's scripts. Conservative: any single match across any script keeps the entry.

Sufficient checks:
- **Script → covered by exec entry?** Use the reconciler's `_entries_for_app(manifest)` to compute the inferred set; union with the manifest's explicit `permissions.exec`. Any script in `files[]` / `realized_files[]` whose path isn't in the union → emit `permission_exec_missing_declaration`.
- **Hardcoded host → in network_egress?** Regex-grep each script for `https?://([a-z0-9.-]+)` and `api\.[a-z0-9.-]+`. For each match, check whether the host appears in any `permissions.network_egress` entry (allowing wildcards).

Overkill checks (lower confidence — info severity only):
- **Wildcard exec entry → matches more files than the app declares?** If `permissions.exec` contains `something/*` and the app declares N matching files but the workspace has M (M > N), flag.
- **Wildcard network_egress → matches more hosts than grep finds?** If `permissions.network_egress` contains `*.example.com` and only one specific subdomain is grep-matched, flag (low confidence — operator may know about others).

### Operator-affirmed entries

For each candidate finding, before passing to consolidation:

1. Read `manifest.permissions._affirmed` (defaults to `[]`).
2. Build the candidate's affirmation key: `f"{kind}:{entry_kind}:{entry_value}"` (e.g. `"unused:exec:scripts/foo.py"`).
3. If the key is in `_affirmed`, skip the finding silently.

The key shape is documented so a v2 mechanism (auto-affirm on rejection) can match against it.

---

## `app_permission_review` — second pass (pod-aware consolidation)

After all per-app candidates are collected, run the spec's three-outcome cross-reference against sibling manifests on the same bot.

### For each `*_unused` candidate (a "narrow this declaration" finding):

Ask: does any *other* installed app on this bot:
- (a) Declare the same resource in its `permissions:` block?
- (b) Use the resource in one of its scripts (grep-matched), without declaring it?

| Cross-reference result | Outcome |
|---|---|
| (a) sibling declares it | Emit the narrowing proposal AS-IS, annotated *"the permission remains in effect via app B's declaration; this proposal narrows app A's declared surface but does not change effective permissions."* |
| (b) sibling uses it without declaring it | Emit a **move proposal** instead: "remove from app A, add to app B." Single Investigation with both edits in its context. |
| No sibling references it at all | Emit the narrowing proposal AS-IS. |

### For each `*_missing_declaration` candidate (a "this app should declare this" finding):

Ask: does any other installed app on this bot already declare the missing resource?

| Cross-reference result | Outcome |
|---|---|
| Sibling declares it | Emit the addition proposal annotated *"already declared by app C; this proposal makes the dependency explicit on app A."* |
| No sibling declares it | Emit AS-IS. |

### For `*_overkill_wildcard` candidates:

No consolidation needed — wildcard tightening is per-app. Emit as-is.

---

## Implementation outlines

### Per-bot run

```python
# app_permission_review/observe.py

@dataclass
class AppPermissionReviewContext:
    bot_id: str
    shared_dir: Path

def observe(ctx) -> list[Proposal]:
    from .review import find_per_app_candidates
    from .consolidation import consolidate
    from .proposals import build_proposal
    from evolve_admin.app_permissions.reconciler import (
        _iter_manifest_files, _read_manifest_raw, _entries_for_app,
    )
    from evolve_admin.config import bot_home, load_network

    network = load_network()
    home = bot_home(ctx.bot_id, network)
    workspace = home / ".openclaw" / "workspace"
    manifests_dir = workspace / "manifests"

    # Load every active manifest into memory (we need the whole set for
    # consolidation).
    manifests: list[dict] = []
    for mpath in _iter_manifest_files(manifests_dir):
        raw, _ = _read_manifest_raw(mpath)
        if raw is None:
            continue
        if (raw.get("status") or "").lower() in ("hidden", "deprecated"):
            continue
        manifests.append(raw)

    # First pass — per-app candidates.
    all_candidates: list[Finding] = []
    for m in manifests:
        all_candidates.extend(find_per_app_candidates(m, workspace))

    # Second pass — pod-aware consolidation.
    consolidated = consolidate(all_candidates, manifests, workspace)

    # Convert each consolidated finding into one Investigation proposal.
    return [build_proposal(f, ctx.bot_id) for f in consolidated]
```

### Static-check primitives

`review.py` exports `find_per_app_candidates(manifest, workspace) -> list[Finding]`.

```python
def find_per_app_candidates(manifest: dict, workspace: Path) -> list[Finding]:
    perms = manifest.get("permissions") or {}
    if not isinstance(perms, dict) or not perms:
        return []  # bootstrapper's territory, not review's

    affirmed = set(perms.get("_affirmed") or [])
    app_scripts = _resolve_app_scripts(manifest, workspace)
    app_script_bodies = _read_script_bodies(app_scripts)

    findings = []
    findings.extend(_check_exec_necessary(perms, app_scripts, app_script_bodies, manifest, affirmed))
    findings.extend(_check_fs_necessary(perms, app_script_bodies, manifest, affirmed))
    findings.extend(_check_network_necessary(perms, app_script_bodies, manifest, affirmed))
    findings.extend(_check_env_necessary(perms, app_script_bodies, manifest, affirmed))
    findings.extend(_check_exec_sufficient(perms, manifest, affirmed))
    findings.extend(_check_egress_sufficient(perms, app_script_bodies, manifest, affirmed))
    findings.extend(_check_exec_overkill(perms, workspace, manifest, affirmed))
    findings.extend(_check_network_overkill(perms, app_script_bodies, manifest, affirmed))
    return findings
```

Script-body grep is bounded (max 100KB per script body, max 50 scripts per app) to prevent runaway on weird app shapes. Anything past either cap → script is skipped silently for grep purposes (but still counted as "declared" for missing-declaration checks).

### Consolidation

`consolidation.py` builds an index of (resource_kind, resource_value) → list[sibling_app_id] across all manifests, then runs the three-outcome decision per candidate.

```python
def consolidate(
    candidates: list[Finding],
    all_manifests: list[dict],
    workspace: Path,
) -> list[Finding]:
    sibling_index = _build_sibling_index(all_manifests, workspace)
    out = []
    for c in candidates:
        out.append(_annotate_or_convert(c, sibling_index, all_manifests))
    return out
```

The sibling index has two layers:
- *Declared*: every (kind, value) declared in any manifest's `permissions:` block, with the list of declaring app_ids.
- *Used*: every (kind, value) grep-matched in any app's scripts (whether declared or not), with the list of using app_ids.

Cross-reference is a dict lookup.

### Proposal building

`proposals.py` is the Finding → Investigation factory. Same pattern as B.1's `signal_proposals.py`, but every action is an `Investigation` so the factory is shorter.

---

## Bootstrapper outline

```python
# app_permission_bootstrapper/observe.py

@dataclass
class AppPermissionBootstrapperContext:
    bot_id: str
    shared_dir: Path

def observe(ctx) -> list[Proposal]:
    from evolve_admin.app_permissions.reconciler import (
        _iter_manifest_files, _read_manifest_raw, _entries_for_app,
    )

    home = bot_home(ctx.bot_id, ...)
    manifests_dir = home / ".openclaw" / "workspace" / "manifests"
    proposals = []
    for mpath in _iter_manifest_files(manifests_dir):
        raw, _ = _read_manifest_raw(mpath)
        if raw is None or (raw.get("status") or "").lower() in ("hidden", "deprecated"):
            continue
        if _has_meaningful_permissions_block(raw):
            continue  # already has a block; review's territory
        inferred = list(_entries_for_app(raw))
        if not inferred:
            continue  # nothing to seed
        proposals.append(_build_bootstrap_investigation(raw, inferred, ctx.bot_id))
    return proposals
```

`_has_meaningful_permissions_block` returns True iff `manifest.permissions` is a dict with at least one non-underscore key (so stubs like `{"_note": "..."}` still count as "no block").

---

## scan_workspace_pipeline integration

At the tail of `scan_workspace_pipeline` (right before its final return, after INSTALLED_APPS.md regen and status="done"):

```python
# Trigger app_permission_review immediately so the operator sees findings
# in the proposals queue alongside their scan results. Reuses the normal
# ingest pipeline (dedup, fingerprint, charter invariants, cooldown).
# Best-effort — never let a review failure break the scan return.
try:
    from generator_runner import run_one_generator
    run_one_generator(shared_dir, config, "app_permission_review", bot_id=bot_id)
    _slog(f"[scanner] app_permission_review triggered for {bot_id}")
except Exception as exc:
    _slog(f"[scanner] app_permission_review trigger skipped (non-fatal): {exc}")
```

The bootstrapper is NOT triggered from the scan pipeline (per the §"Why weekly" rationale above — operators don't want re-bootstrap proposals on every scan).

`run_one_generator(shared_dir, network, generator_id, bot_id=None)` is a new helper in `generator_runner.py`. It loads the registry, finds the named generator, builds its context (via the existing `_CONTEXT_FACTORIES`), calls `observe`, and runs proposals through the same ingest path as `run_generators`. Skips the cadence check (since the caller is opting in deliberately) but honors all invariants and dedup.

---

## Test plan

### `tests/test_app_permission_bootstrapper.py`

- `test_no_permissions_block_proposes_initial_block`
- `test_existing_permissions_block_no_proposal`
- `test_stub_block_with_only_underscore_keys_treated_as_missing`
- `test_v7_arc_realized_files_seed_exec_entries`
- `test_v4_string_files_seed_exec_entries`
- `test_crons_seed_cron_entries`
- `test_hidden_manifest_skipped`
- `test_proposal_context_includes_copyable_block_json`

### `tests/test_app_permission_review_static.py`

- Per finding kind, 1-3 tests covering positive case + edge cases
- `test_exec_unused_when_file_missing_and_no_script_references`
- `test_exec_unused_skipped_when_grep_matches_basename`
- `test_fs_read_unused_via_substring_grep`
- `test_network_egress_unused`
- `test_env_unused`
- `test_exec_missing_declaration_from_files_array`
- `test_exec_missing_declaration_from_realized_files`
- `test_egress_missing_declaration_via_https_regex`
- `test_overkill_wildcard_when_match_set_much_wider`
- `test_affirmed_key_in_permissions_skips_finding`

### `tests/test_app_permission_review_consolidation.py`

- `test_no_sibling_reference_emits_as_is`
- `test_sibling_declares_resource_annotates_proposal`
- `test_sibling_uses_resource_without_declaring_emits_move_proposal`
- `test_missing_declaration_with_sibling_coverage_annotates`
- `test_overkill_findings_pass_through_unchanged`
- `test_consolidation_doesnt_crash_on_empty_manifest_set`

### `tests/test_app_permission_review_observe.py`

- End-to-end: synthetic bot home with multiple manifests + permissions blocks; assert proposals match expected mix of kinds + consolidation outcomes
- `test_observe_skips_apps_without_permissions_blocks`
- `test_observe_emits_zero_proposals_for_clean_bot`
- `test_observe_handles_malformed_manifest_per_app_skip`

### `tests/test_run_one_generator.py`

- `test_run_one_generator_triggers_observe_and_ingests`
- `test_run_one_generator_unknown_id_returns_zero`
- `test_run_one_generator_per_bot_with_explicit_bot_id`
- `test_run_one_generator_honors_invariants` (rejection on disallowed action kind)

---

## Out of scope for B.2

- **Typed `UpdateManifest` action + applier.** Adds complexity for one-click apply but Investigation suffices for v1 (operator hand-edits).
- **Auto-write to `permissions._affirmed` on proposal rejection.** The rejection-cooldown already covers most of the practical need.
- **Cross-bot consolidation.** B.2 consolidates within a bot only. Cross-bot patterns (e.g. "admin-bot and team-bot-a both run `cost-watch.py` — should this be a shared library?") are a different concern.
- **LLM-driven review.** Pure-Python static analysis only. The cost-monitoring infra must itself be cheap (parent spec, [[feedback_rsi_low_cost_preference]]).
- **Phase C integration.** The opt-in UI lands in Phase C and will likely produce the first wave of operator-edited `permissions:` blocks. When that ships, B.2 should benefit immediately without further code changes.

---

## Success criteria

This pair has worked when:

1. **A fresh manifest with no `permissions:` block produces a bootstrapper proposal** on the next generator run. The operator copies the suggested JSON into the manifest. Next run, the bootstrapper is silent for that app.
2. **A `permissions:` block with a stale entry produces a `permission_*_unused` review proposal.** Operator removes the entry; next run, the proposal is gone.
3. **A script with an undeclared exec or egress produces a `permission_*_missing_declaration` proposal.** Operator adds the entry; next run, gone.
4. **A wildcard wider than scripts justify produces an overkill proposal at info severity.** Doesn't fire on legitimate wildcards (e.g. `*.anthropic.com` when both `api.anthropic.com` AND `console.anthropic.com` are referenced).
5. **The pod-aware second pass converts cross-bot redundancies** to "move" proposals when a sibling app actually uses an entry another app declares.
6. **Running an explicit "scan workspace"** surfaces review findings in the proposals queue within the same admin-UI session, not on a delay.
7. **Re-rejection of a bootstrapper proposal** doesn't keep nagging — the 14-day rejection cooldown holds it back.
