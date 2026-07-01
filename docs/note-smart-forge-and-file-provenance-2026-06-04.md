# Note: smart forge + per-file provenance

**Date:** 2026-06-04
**Status:** Architectural note (extends [docs/note-hybrid-gallery-2026-06-03.md](note-hybrid-gallery-2026-06-03.md) and [docs/spec-files-pack-hybrid-2026-06-03.md](spec-files-pack-hybrid-2026-06-03.md))

---

## What changed

The hybrid gallery note (2026-06-03) introduced two classes of gallery app — manifest-only and manifest+files — and treated them as a binary choice. After conversation with the operator, that framing is too coarse. The reality is **two independent axes** with one of them a **spectrum**:

| Axis | Values | Notes |
|---|---|---|
| **Class** (file coverage) | manifest-only · partial files-pack · complete files-pack | Spectrum, not binary |
| **Provenance** (where the app came from) | evolve-bundled · community-shared · operator-local | Independent of class |

Any combination is valid: an evolve-bundled app might ship as partial files-pack; a community-shared app might be manifest-only; an operator-local app might be a complete files-pack. Class and provenance are orthogonal.

## The class spectrum

| Class | What it ships with | Install path |
|---|---|---|
| **Manifest-only** | Manifest, build_spec, no `files_pack` block | Pure LLM-forge (every file generated; ~$30/install) |
| **Partial files-pack** | Manifest + `files_pack` block + `gallery/<slug>/files/` containing SOME of the manifest's declared files | Hybrid: copy the bundled files cheaply, LLM-forge fills the gap |
| **Complete files-pack** | Manifest + `files_pack` block + `gallery/<slug>/files/` containing ALL of the manifest's declared files | Pure copy + substitute (~$0/install) |

The partial case is the natural fit for "stable scripts bundled, per-bot prompts forged" patterns:

```
manifest.files[]:
  scripts/tasks.py            → bundled  (stable across installs)
  scripts/notifications.sh    → bundled  (stable across installs)
  HEARTBEAT.template.md       → forge    (LLM tailors for this bot's voice)
  config.local.json           → forge    (LLM picks per-bot defaults)
```

## Manifest as source of truth

The manifest's `files[]` array is **authoritative** about what an installed app needs. Each entry declares its **provenance**:

```json
{
  "files": [
    {
      "path": "scripts/tasks.py",
      "role": "script",
      "provenance": "bundled",
      "file_id": "f-c9d0e1f2",
      "sha256": "...",
      "..."
    },
    {
      "path": "HEARTBEAT.template.md",
      "role": "doc",
      "provenance": "forge",
      "file_id": "f-aa11bb22",
      "..."
    }
  ]
}
```

Provenance values:

| Value | Install behavior |
|---|---|
| `"bundled"` | Forge copies from the files-pack (with placeholder substitution). Refuses to install if the file isn't actually in the files-pack — that's a manifest/files-pack drift error. |
| `"forge"` | Forge generates via LLM. Even when a files-pack exists for this package, forge skips this file in the files-pack copy phase and asks the LLM to generate it. |
| `""` (omitted) | Inferred. If a `files_pack` block exists on the manifest AND the file's path is listed in `files/manifest.json` → infer `"bundled"`. Else → infer `"forge"`. Keeps all existing manifests working without edits. |

The omitted-defaults rule is the backward-compatibility hinge: pre-existing manifests behave exactly as they do today.

## Smart forge as a per-file dispatcher

Today's install dispatcher (in `forge_engine._maybe_install_via_files_pack`) is all-or-nothing: either every file comes from the files-pack OR every file gets LLM-generated. The spectrum model needs a **per-file dispatcher**:

```
for each file in manifest.files[]:
  resolve provenance (explicit or inferred)
  if bundled:
    copy from gallery/<slug>/files/, substitute, write
  if forge:
    add to "LLM gap" list

if LLM gap list is empty:
  install complete — skip LLM-forge phase entirely
else:
  run LLM-forge phase, but only ask it to produce the gap files
  Phase 4.5 runs as today (scheduled actions, heartbeat, hooks)
```

Phase 4.5 stays unchanged — `scheduled_actions[]`, heartbeat instructions, and openclaw hook patches are always forge's job regardless of file provenance.

## What was already explicit (kept)

The user flagged: "the manifest should also be explicit about what cron/heartbeat or daemons need to be set." That's already true in the existing schema:

| Field | Purpose | Since |
|---|---|---|
| `scheduled_actions[]` | Declarative contract for cron/heartbeat/daemon work; Phase 4.5 reads + installs | Schema v13 |
| `heartbeat_evidence` | Tier-2 verifies sections still resolve | v13 |
| `cron_evidence` | Tier-2 verifies launchd/crontab entries are loaded | v13 |
| `crons[]` | Legacy raw crontab strings (pre-scheduled_actions) | v4 |

The recurring-behavior contract layer is already explicit and load-bearing. This note doesn't change it; just confirms the model.

## Files-pack metadata extension (additive)

The files-pack format itself doesn't need to change for the spectrum to work — the manifest already tells the dispatcher what's bundled vs what's forge. But two optional metadata hints help operator-review surfaces (F-P.4 lint, F-P.7.b UI):

```json
{
  "format_version": "1.0",
  "files_count": 3,
  "snapshot_source_pkg_version": "...",
  "snapshot_at": "...",
  "sha256": "...",

  "partial": true,                      ← NEW, optional, default false
  "coverage_intent": "stable_scripts"   ← NEW, optional, free-form
}
```

- `partial: true` documents the operator's intent that this files-pack is deliberately partial (not all files declared in the manifest are bundled). The F-P.4 integrity sweep treats orphan bundled-files as warnings when `partial: true` is set, errors when absent.
- `coverage_intent` is a free-form hint for the review UI ("stable_scripts", "doc_skeletons", "prompts_only", etc.).

Both are additive and backward-compatible.

## Provenance axis (axis #2 — not yet built)

The provenance axis (evolve-bundled / community-shared / operator-local) is a **separate, future** concern. This note flags it for the F-P.12+ work; the smart-forge piece below ships independently.

When provenance lands, it'll likely surface as:
- `gallery/index.json` entries gain a `source: "evolve" | "community" | "operator-local"` field
- UI gallery cards render trust badges
- Install path may add a confirmation gate for community-sourced apps
- Files-pack signing (sha256-verifiable, optional GPG-signed) becomes relevant for `community` sources

Out of scope for this note; tracked separately.

## Implementation plan (per-file provenance, this PR)

| Change | Where | Notes |
|---|---|---|
| Add `FILE_PROVENANCE_BUNDLED` / `FILE_PROVENANCE_FORGE` constants | `applications/manifest.py` | Public constants for callers |
| Add `ApplicationManifest.files_partition()` helper | `applications/manifest.py` | Walks files[], returns `{bundled: [paths], forge: [paths]}` with inferred defaults |
| Extend `install_files_pack_to_workspace` with optional `allowed_paths` filter | `applications/files_pack.py` | When set, only install files whose path is in the set. Existing callers pass None → install everything (backward-compat). |
| Update `_maybe_install_via_files_pack` to partition + install bundled subset only | `applications/forge_engine.py` | Returns a result that includes which files were covered + which still need LLM generation. When the gap is empty, skip the LLM phase entirely. |
| Test the partition helper, the filter, and the dispatcher behaviour | `tests/` | Unit tests for partition; integration tests for the dispatcher's mixed-mode path |

LLM-forge integration with the "gap" set is a follow-up — for now, when the gap is non-empty, the dispatcher returns None and the existing LLM-forge path runs (generating ALL files, since it doesn't yet know to skip bundled ones). The downside is one redundant generation step for partial files-packs; the upside is no risk of regression in the all-or-nothing case while we ship the schema + helper. The LLM-side awareness is small + bounded (a parameter to the build prompt) and can land separately.

## Compatibility

- Pre-existing manifests: no edits needed. The inference rule (`files_pack` present → infer "bundled"; absent → infer "forge") preserves today's behavior exactly.
- The two new optional files-pack fields (`partial`, `coverage_intent`) are additive.
- F-P.4's integrity sweep already validates per-file SHAs; that doesn't change.
- F-P.7.a's snapshot engine doesn't need to know about provenance — it stamps files in the files-pack as before; the manifest deriver (or operator hand-edit) decides which to mark "bundled" vs leave as "forge".

## What this enables

1. **Cheap installs for stable parts of an app.** Stable scripts in a partial files-pack don't pay LLM cost every install.
2. **Per-bot LLM tailoring stays cheap.** Prompts and configs marked "forge" still flow through the existing per-install LLM path.
3. **Operator clarity.** The manifest reads as a single source-of-truth document explaining what an install will produce and where each piece comes from.
4. **Forge remains the install layer.** No bypass of forge's side-effect (Phase 4.5) work. Scheduled actions, heartbeat instructions, hook patches keep their existing path.
5. **Path to provenance axis.** Once class-spectrum is done, provenance (evolve/community/operator) layers on top cleanly.

## Out of scope (named so they're not surprises)

- **Provenance axis** (F-P.12) — gallery cards rendering "ships with Evolve" vs "community" badges.
- **LLM-side gap awareness** (F-P.11.b) — making the LLM-forge phase skip files already covered by the files-pack instead of regenerating them.
- **Files-pack signing** (F-P.13) — relevant once community-shared apps are real.
- **Cross-bot promotion of partial files-packs** — multi-bot install paths that compose a complete install from multiple partial packs. Unlikely to be needed; flagged for posterity.
