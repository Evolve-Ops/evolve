# Gallery Merge: Task Manager + Unified Task System (2026-06-05)

Status: **shipped** in `p-9bfa1c84@2026.06.05-1.0`. This doc explains why the merge happened, what the merged spec inherits from each predecessor, and how existing installs of either app auto-upgrade.

## The duplication

Two task-management apps shipped in the Evolve gallery, both with similar shape and overlapping scope:

| Gallery slug | `pkg_id` | Origin | Posture |
|---|---|---|---|
| `task-manager` | `p-9bfa1c84` | Hand-authored synthesis of two reference implementations | Thin spec; dict-keyed `tasks.json`; single `check` command; cron-style heartbeat |
| `unified-task-system` | `p-c20a5564` | Exported from `team-bot-a` (real production install), stripped of source-specific bits | Richer spec; array-shaped `tasks.json`; `next`/`summary`/`prune-expired`/`tag-registry` commands; tag-registry-driven normalisation; year-bucketed archive; self-throttled daily prune |

These are the same idea iterated twice. Operators browsing the gallery would have no clean way to choose between them — the names invite comparison shopping, and the substantive answer is that Unified Task System is what the operator usually wants.

## What's in the merged spec

The merger lives at `gallery/task-manager/p-9bfa1c84.json` (version `2026.06.05-1.0`) and inherits **Unified Task System's substance** under **Task Manager's pkg_id**:

- **CLI vocabulary**: `unified_task_system.py` with `add | list | show | update | complete | next | summary | tags | tag | lights | tag-registry | archive | prune-expired | migrate`.
- **Data shape**: array-of-tasks `{version, last_updated, tasks: [...]}` with `title` (not `name`), `priority` enum (not integer urgency), `status_history` (not `status_log`), and the `expires` field for event-triggered pending todos.
- **Year-bucketed archive**: `archive/completed_{YYYY}.json` / `archive/cancelled_{YYYY}.json`.
- **Two heartbeat-driven scheduled actions**: `task-manager-next-check` (every heartbeat) + `task-manager-daily-prune` (self-throttled via `memory/last_prune.txt`). The daily prune is also declared `safety_net_for: ["task-manager-next-check"]`.
- **Tag registry**: `tag_registry.json` is the canonical source of normalisation; the in-code alias dict is a fallback.

The merged spec is also re-leveled to v20 quality per `docs/spec-app-coherence-and-reconciliation-2026-06-05.md`:

- `schema_version: 20` / `manifest_shape: "v20"`.
- `provenance` block with per-field origins; the spec itself is `forge_built`.
- `scheduled_actions[*]` carry `state: "active"`, `quality: "verified"`, and `safety_net_for[]` where applicable.
- `files[*]` are layer-typed (`code`, `config`, `data`, `behavior_doc`, `reference`).
- `volatile_paths[]` declares the archive and the daily-prune throttle file so reconciliation does not flag them on every scan.
- Coherence Pass A passes: every recurring claim has a declared mechanism; every declared input file is in `files[]` or under a `volatile_paths[*].glob`.

## What's deprecated

- `gallery/unified-task-system/` (the directory + the `p-c20a5564.json` manifest + the files-pack at `files/`) is removed in the same commit. The `gallery/index.json` entry for `p-c20a5564` is removed.
- The earlier files-pack at `gallery/task-manager/files/` (a snapshot of the older `scripts/tasks.py` shape) is also removed — its on-disk shape does not match the merged spec, and leaving it in place would cause forge's fast-path installer to materialise the wrong CLI. `files_pack.files_count` on the merged manifest is `0`; forge LLM-builds from `build_spec` until a reference bot has been migrated and a fresh snapshot taken.

## How existing installs auto-upgrade

The migration is handled at the gallery resolver layer via `LEGACY_KEY_MIGRATION` in `packages/admin/evolve_admin/applications/gallery.py`, mirroring the pattern in `packages/admin/evolve_admin/alerts/catalog.py`:

```python
LEGACY_KEY_MIGRATION: dict[str, str] = {
    "p-c20a5564": "p-9bfa1c84",
}
```

`load_gallery_package(pkg_id, shared_dir)` resolves the input through the map before looking up the manifest. Concrete consequences:

| Caller | Behaviour before the merge | Behaviour after |
|---|---|---|
| **`get_update_status(manifest)`** for a bot with installed `p-c20a5564` | Looked up `p-c20a5564` in the gallery, found `2026.06.03-1.2`, reported "up to date" | Looks up `p-c20a5564` → resolves to `p-9bfa1c84@2026.06.05-1.0`, reports an update available |
| **`load_gallery_package("p-c20a5564", ...)`** from `apply_actions` / `reconcile_actions` / `forge_engine` | Returned the Unified Task System manifest | Returns the merged Task Manager manifest |
| **`load_gallery_package("p-9bfa1c84", ...)`** | Returned the older Task Manager manifest | Returns the merged Task Manager manifest |
| **Gallery list rendering** (`list_gallery_packages`) | Showed both packages in the gallery UI | Shows only the merged Task Manager |

The first row is the load-bearing one: every bot that has Unified Task System installed sees the merged Task Manager as an upgrade candidate the next time the update detector runs. When the operator approves the upgrade, forge runs against the merged `build_spec`. The build spec includes a `migrate` CLI command that converts the legacy dict-keyed `tasks.json` shape into the new array shape, preserving every task object verbatim (mapping `name → title`, `status_log → status_history`, integer `urgency → priority` enum, defaulting any missing v20 fields).

Bots that were on the older Task Manager (`p-9bfa1c84` pre-2026.06.05) also see the upgrade — they're already on the surviving pkg_id, so the standard update path fires. Their `tasks.json` is the dict-keyed legacy shape that the `migrate` command converts.

The migration entry is permanent. We keep `p-c20a5564 → p-9bfa1c84` in the map until we have positive confirmation that no bot still references the retired id. Removing the entry prematurely would strand any install that hadn't been upgraded.

## Coverage

- `packages/admin/tests/test_gallery_legacy_key_migration.py` exercises both directions of the resolution (`p-c20a5564` → `p-9bfa1c84` and `p-9bfa1c84` → itself), confirms the retired-id entry is present, asserts every map target resolves to a real package, and includes a `no-double-hop` invariant so future entries don't accidentally introduce chains.
- The merged spec passes the public-launch scrub guard (`test_public_launch_scrub.py`) — placeholder bot names only.

## What this merge is not

- It is **not** a deprecation of the older Task Manager's contract surfaces. The merged spec's `interface_contract.enums` keeps `status` and `light` identical, and the `migrate` command preserves every existing task object.
- It is **not** a code change to the installed bot scripts. The gallery is a blueprint layer; the operator still has to approve the upgrade per bot before forge materialises the new `scripts/unified_task_system.py`.
- It does **not** retire the `p-c20a5564` identifier from test fixtures. A couple of admin tests use `p-c20a5564` as a fixture pkg_id for forge / install-signal scenarios; those continue to work because they don't depend on the gallery resolver returning a manifest for that id.

## Related

- v20 schema framework: `docs/spec-app-coherence-and-reconciliation-2026-06-05.md`
- Pattern reference: `packages/admin/evolve_admin/alerts/catalog.py` (`LEGACY_KEY_MIGRATION` for alert event keys)
- Atlas Daily Digest (a v20-shaped community import used as the quality bar): `gallery/imported/p-7b26ba5e.json` on the mini deploy
