# Spec: Files-pack hybrid — canonical files in the gallery, forge as the exception

**Date:** 2026-06-03
**Status:** Draft
**Related:** [docs/spec-forge-side-effects-2026-06-02.md](spec-forge-side-effects-2026-06-02.md), [docs/spec-scanned-export-2026-06-02.md](spec-scanned-export-2026-06-02.md), [docs/audit-uts-export-2026-06-03.md](audit-uts-export-2026-06-03.md), [docs/spec-launchd-python-signal-2026-06-03.md](spec-launchd-python-signal-2026-06-03.md)

---

## 1 — Motivation

Two cost realities from 2026-06-03's ledger:

- **Per-install cost:** $33.64 for one Unified Task System install on the personal-bot reference account. Spread across 13 gallery apps × 9 bots, that potentially costs **$3,500+** just to populate the pod. Every re-forge or improvement run pays that again.
- **Steady-state cost:** ~$5–9/day pod-wide from heartbeat-driven LLM sessions. Track A (PRs #2028, #2031) is closing this side.

The install cost is the bigger immediate threat to the framework's scalability. And the [Unified Task System export-fidelity audit](audit-uts-export-2026-06-03.md) surfaced a finding that strengthens the case for changing course:

> When forge re-built the source bot's `unified_task_system.py` on the target, the output was character-identical to the source for all 16 categories, all 16 ID prefixes, all 4 priority enum values, all 4 light enum values, `BLOAT_THRESHOLD = 150`, `DEFAULT_ARCHIVE_DAYS = 30`, and the full v6.0 `tasks.json` schema.

Forge spent $33 to produce a file that was structurally identical to a file we already had. **The customization argument for forge — the reason we LLM-generate instead of copying files — is weaker than the design assumed.** Most apps don't need per-bot customization at install time; they need the same code with a few placeholders (`{bot_id}`, `{workspace}`) substituted in a small number of files.

The pitch — "applications, not skills" — is preserved by keeping the `build_spec` as the durable contract. What changes is the **default install path**: copy + substitute instead of LLM-generate.

---

## 2 — Goals and non-goals

### Goals
1. **First-install cost ≈ $0** for any gallery app whose files-pack has been populated.
2. **Customization story preserved.** A bot that genuinely needs a custom variant can opt back into LLM-forge via a flag.
3. **`build_spec` stays first-class.** It's the durable contract, the regeneration source, and the input to the deriver / scanned-export pipeline.
4. **No behaviour regression for existing installs.** Manifests without a files-pack continue to use the current LLM-forge path.
5. **Operator can populate a files-pack from any working install** via a CLI snapshot tool.

### Non-goals
- Eliminating LLM-forge entirely. The "I want a custom variant" path stays.
- Solving the steady-state heartbeat cost (Track A's job; orthogonal).
- Restructuring the gallery directory layout beyond adding a `files/` subdirectory per package.

---

## 3 — Architecture overview

Three install paths, in priority order:

```
manifest.files_pack.version present + gallery/<slug>/files/ exists
  ↓ "fast install" — copy + substitute, ~$0 cost, no LLM
  ↓
manifest.files_pack absent OR gallery/<slug>/files/ missing
  ↓ "LLM install" — current forge path (build / critique / refine / test)
  ↓
manifest.files_pack present BUT operator passes --regenerate
  ↓ "LLM install with snapshot capture" — LLM install runs, then
    automatically updates the gallery's files/ from the new output
```

Phase 4.5 (`_materialize_scheduled_actions`) and Phase 5 (manifest finalisation) **run in all three paths**. They don't depend on which produced the files.

---

## 4 — Gallery directory layout

For a package `p-XXXXXXXX` with slug `task-manager`:

```
gallery/
└── task-manager/
    ├── p-XXXXXXXX.json           # main manifest (existing)
    └── files/                    # NEW — files-pack
        ├── manifest.json         # per-file metadata
        ├── scripts/
        │   ├── tasks.py
        │   ├── task_updater.py
        │   └── task-check.sh
        └── TASKS.md
```

`gallery/<slug>/files/manifest.json` shape:

```json
{
  "version": "1.0",
  "snapshot_source": {
    "bot_id": "team-bot-a",
    "pkg_version": "2026.06.03-1.3",
    "snapshot_at": "2026-06-03T12:00:00Z",
    "snapshot_by": "evolve-admin snapshot-files-pack"
  },
  "files": [
    {
      "path": "scripts/tasks.py",
      "mode": "0644",
      "sha256": "a3f9…",
      "placeholders": [],
      "size_bytes": 27511
    },
    {
      "path": "scripts/task-check.sh",
      "mode": "0755",
      "sha256": "b7c1…",
      "placeholders": ["bot_id", "workspace"],
      "size_bytes": 415
    },
    {
      "path": "TASKS.md",
      "mode": "0644",
      "sha256": "c5e2…",
      "placeholders": ["bot_id"],
      "size_bytes": 7881
    }
  ]
}
```

`placeholders` is **explicit, not auto-detected at install time**. Only files that declare placeholders go through substitution; the rest are copied verbatim. This is the safety property — a Python source file that contains a literal `{bot_id}` as a string (e.g. in a docstring example) does not get accidentally substituted.

---

## 5 — Manifest schema addition (v19)

Bump `MANIFEST_SCHEMA_VERSION` from 18 → 19. New top-level field:

```python
@dataclass
class ApplicationManifest:
    # ...
    files_pack: dict | None = None
```

`files_pack` is None when no files-pack exists. When present, it carries small metadata about the files-pack the operator should look at:

```json
{
  "version": "1.0",
  "files_count": 6,
  "snapshot_source_pkg_version": "2026.06.03-1.3",
  "sha256": "<sha256 of files/manifest.json>"
}
```

The per-file metadata stays in `gallery/<slug>/files/manifest.json` (large, lives in the repo). The package manifest just carries enough to detect "is this stale" / "does it exist".

---

## 6 — Variable substitution

### Supported placeholders (v1)

| Placeholder | Resolved value |
|---|---|
| `{bot_id}` | the target bot's id from `network.json` |
| `{bot_user}` | the macOS account for that bot |
| `{workspace}` | absolute path to the bot's workspace |
| `{shared_dir}` | `/Users/Shared/evolve` |
| `{pkg_id}` | the manifest's `pkg_id` |
| `{app_id}` | the manifest's `id` |
| `{installed_at}` | ISO timestamp at install time |

### Rules

1. **Substitution is scoped to declared placeholders.** A file's `placeholders` list in the per-file metadata names the substitution set. Unlisted placeholders pass through unchanged.

2. **Substitution is content-only by default.** Filenames don't get substituted unless the file metadata also carries `placeholders_in_path: [...]`. (Rare — but supports cases like `scripts/{bot_id}-cron.sh`.)

3. **Literal braces escape with double braces.** `{{` → `{`, `}}` → `}`. Same convention as Python's str.format.

4. **Mode is preserved verbatim** from the metadata's `mode` field (`"0644"` / `"0755"`). The snapshot tool reads the source file's mode at capture time.

5. **SHA-256 integrity check.** Install computes the sha256 of the (pre-substitution) content and compares to the metadata. Mismatch is a hard error — the gallery has drifted from what was snapshotted.

---

## 7 — Install flow changes

Pseudo-code for the install dispatcher:

```python
def install_app(bot_id, pkg_id, manifest, regenerate=False):
    files_pack = manifest.files_pack or {}
    files_pack_dir = gallery_dir / manifest.slug / "files"

    if not regenerate and files_pack and files_pack_dir.exists():
        # FAST PATH — Phases 1-3 skipped entirely
        log("Phase 1-3: skipped (using gallery files-pack)")
        _copy_and_substitute(files_pack_dir, bot_workspace, bot_context)
        # Phase 4.5 + Phase 5 still run normally
    else:
        # LLM PATH — existing forge dispatch
        _run_llm_forge(bot_id, manifest)
        if regenerate and was_successful:
            # Bonus: capture the just-forged output back into the gallery
            _capture_snapshot_to_gallery(bot_id, manifest)
```

The LLM path is unchanged — same `_forge_app` flow, same critique rounds, same test gate. The fast path is a new, much shorter code path that bypasses all of Phases 1-3.

---

## 8 — Snapshot tool

```bash
evolve-admin snapshot-files-pack \
    --bot team-bot-a \
    --pkg p-9bfa1c84 \
    --out gallery/task-manager/files/
```

Behaviour:

1. Resolves the source bot's installed manifest for `pkg_id`.
2. For each entry in `manifest.files[]`, reads the file from the source bot's workspace (`evolve` user has ACL read).
3. For each file, **auto-detects probable placeholders** by scanning for known patterns:
   - `/Users/{source-bot-user}/.openclaw/workspace` → suggests `{workspace}`
   - `com.{source-bot-id}.` → suggests `{bot_id}`
   - The source bot's literal id appearing in non-docstring contexts → suggests `{bot_id}`
4. Writes the files to `--out`, generates `files/manifest.json`, and writes a per-file `.suggested-placeholders.md` for the operator to review.
5. Operator reviews + manually adjusts `files/manifest.json` if auto-detection got something wrong, then commits.

Auto-detection is a hint, not authoritative. The operator's review is where the placeholder list becomes durable.

---

## 9 — Validation + scrub

The existing public-launch scrub guard (`test_public_launch_scrub.py`) walks tracked files looking for reserved tokens. With files-packs landing in the repo, three new failure modes to guard against:

1. **Reserved tokens inside files-pack files** (e.g. the snapshot captured a source bot's id without `{bot_id}` substitution). → existing scrub catches this if the file is tracked.

2. **`placeholders` metadata declares a token but the file doesn't contain it** (drift between metadata and content). → new validation pass at PR-time: walk every files-pack manifest, confirm each declared placeholder appears in the file's content.

3. **File content contains a recognisable bot-specific pattern (`/Users/<sourcebot>/`, `com.<sourcebot>.`, etc.) that's NOT in the placeholders list.** → new linter: warn if a file looks like it should have placeholders declared but doesn't.

Add a new test `test_files_pack_integrity.py` that runs (2) and (3) across all tracked `gallery/*/files/manifest.json` files.

---

## 10 — Migration path

| State | Behaviour |
|---|---|
| Gallery package without `files_pack` field or without `gallery/<slug>/files/` dir | LLM-forge install (no change from today) |
| Gallery package with both | Files-pack install by default; LLM-forge available via `--regenerate` flag |
| Existing installed apps on bots (personal-bot reference account, atlas-like, etc.) | Unchanged. The next improvement-job run uses whichever path the gallery currently supports. |

Migration of existing gallery packages (T-A.5 from the Track A spec, but now broader):
- task-manager (`p-9bfa1c84`): snapshot from atlas (clean v17 install), publish as v1.5
- unified-task-system (`p-c20a5564`): snapshot from the personal-bot reference (already installed, hand-finished), publish as v1.2
- ea-pack (`p-aab5e569`): snapshot from the personal-bot reference, publish as v1.1
- The other ~10 gallery packages: snapshot from the bot they were forged on; commit one at a time

Each migration is a small PR that runs the snapshot tool, commits the files, and bumps the package version.

---

## 11 — Cost characteristics after this lands

| Operation | Cost (current) | Cost (after files-pack) |
|---|---|---|
| Install gallery app on new bot | ~$30 LLM | ~$0 file copy |
| Re-install / version upgrade | ~$30 LLM | ~$0 file copy |
| First population of a new gallery package | one $30 LLM forge | unchanged — population IS the snapshot source |
| Re-snapshot after gallery package improvement | $0 (file copy from contributor bot) | $0 (same) |
| Custom variant install (operator opts into LLM) | ~$30 LLM | ~$30 LLM (path preserved) |
| Improvement run | ~$30 LLM | ~$0 if no source change; ~$30 LLM if regenerating |
| Forge contributor authoring a new app from scratch | ~$30 LLM | ~$30 LLM (unchanged) |

**Net pod-wide install cost** at current scale (13 apps × 9 bots = 117 installs):
- Today: ~$3,500
- After files-pack: ~$30 (one snapshot run per app) + ~$0 per install = ~**$390 total** (one-time)

After the first round of snapshots are committed to the repo, the marginal install cost drops to $0 essentially permanently.

---

## 12 — What this means for `build_spec`

`build_spec` stays as the **durable contract** of the app, used in:

1. **Bootstrapping a new app from scratch** — operator writes a build_spec, runs forge against a reference bot to produce the first files-pack.
2. **Regenerating** — operator passes `--regenerate` on install; LLM-forge runs against the build_spec, produces new files, optionally updates the gallery's files-pack.
3. **Scanned-export pipeline (PRs #2005–#2010)** — the deriver still produces a build_spec from scanner output. Same path; the build_spec is now the *input* to a snapshot-and-publish step instead of the *output* shipped to bots.
4. **Improvement runs** — operator wants to evolve the app via natural language, not by editing files. Forge against the build_spec, capture the new files-pack.

The build_spec is not vestigial — it's the durable source-of-truth that the files-pack is a snapshot of. The files-pack is the *deployable artifact*; the build_spec is the *generative source*.

---

## 13 — Acceptance criteria

For the foundation PR (this spec's first implementation):

1. Schema v19 with `files_pack` field on `ApplicationManifest`.
2. Variable substitution library — `substitute_placeholders(content, declared, context) -> str` — with full coverage of all 7 v1 placeholders, double-brace escaping, missing-placeholder handling.
3. Read helper — given `gallery/<slug>/files/`, return the list of files + per-file metadata.
4. Tests for both, covering edge cases (empty content, all placeholders, none-declared, sha256 mismatch, mode preservation).
5. No behaviour change yet — the install dispatcher continues to use LLM-forge for all packages. Files-pack reading is exercised only by tests.

For follow-on PRs:

6. Install dispatcher prefers files-pack when present.
7. Snapshot CLI (`evolve-admin snapshot-files-pack`).
8. Validation test (`test_files_pack_integrity.py`) — declared placeholders appear in content, no orphan bot-specific patterns.
9. First gallery republish using files-pack (task-manager v1.5).
10. Operator runbook for the new install path + snapshot workflow.

---

## 14 — Implementation plan (multi-PR)

| PR | Scope | LOC est. |
|---|---|---|
| **F-P.1** | This spec + schema field + substitution library + read helper + tests | ~700 |
| **F-P.2** | Install dispatcher prefers files-pack + integration tests | ~400 |
| **F-P.3** | `evolve-admin snapshot-files-pack` CLI + tests + small runbook update | ~500 |
| **F-P.4** | `test_files_pack_integrity.py` + linter for orphan bot-specific patterns | ~300 |
| **F-P.5** | First gallery republish (task-manager v1.5 with files-pack) | manifest + files |
| **F-P.6** | Operator runbook for the new install path | docs only |
| **F-P.7** | Migrate remaining gallery packages (one per PR for reviewability) | varies |

F-P.1 is this PR. F-P.2 through F-P.7 follow.

---

## 15 — Open questions (resolved 2026-06-03)

1. **`files_pack.version` semantics.** ✅ **Separate.** `files_pack.format_version: "1.0"` lives in the per-file `gallery/<slug>/files/manifest.json` (this spec's format); manifest's `pkg_version` continues to track the package as a whole. The install dispatcher can do compat checks orthogonally. F-P.1 implements this.

2. **Per-file SHA enforcement vs files-pack-level SHA.** ✅ **Both.** Per-file SHA in `files/manifest.json` for content integrity at install time; top-level `files_pack.sha256` in the package manifest = SHA-256 of `files/manifest.json` itself for stale-detection. F-P.1 implements `compute_files_pack_sha256()` for the top-level digest.

3. **Empty placeholder resolution.** ✅ **Hard error.** Raises `FilesPackPlaceholderError` with the file path + placeholder name (and a separate `resolve_install_context` helper that fails-fast at context construction). Forces missing-context bugs to surface as clear failures, not silently-broken installs. F-P.1 implements this.

4. **`--regenerate` capturing back to the gallery.** ✅ **Local write.** Operator's checkout gets updated `gallery/<slug>/files/`; they review with `git diff` + run scrub guard before committing. Auto-PR would couple install logic to GitHub plumbing — fragile, and removes the operator's review-before-commit moment. F-P.2 implements this when the dispatcher gains `--regenerate`.

5. **Inheritance / variant overrides.** ✅ **Defer to v2.** Use case is real but design space is large; v1 ships without it.

---

## 16 — Relationship to Track A

Track A (#2028, #2031) addresses **steady-state heartbeat cost**. Track F-P (this spec) addresses **install cost**. They're independent and complementary.

After both land:
- Install: $0 per app per bot (files-pack)
- Steady-state: $0–0.50/day per bot per app (Python-by-default + signal-on-actionable)

The combined picture: the framework becomes operationally cheap enough to ship 13 apps × 9 bots without flinching at the cost line. Which is the precondition for a public launch story.
