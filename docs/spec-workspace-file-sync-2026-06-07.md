# Spec: Workspace file sync — re-run apply-actions and the bot's scripts catch up to the repo

**Date:** 2026-06-07
**Status:** Draft
**Related:** [docs/spec-files-pack-hybrid-2026-06-03.md](spec-files-pack-hybrid-2026-06-03.md), [docs/note-smart-forge-and-file-provenance-2026-06-04.md](note-smart-forge-and-file-provenance-2026-06-04.md), [docs/principle-apps-inherit-bot-llm.md](principle-apps-inherit-bot-llm.md), [docs/atlas-app-manifests/DEPLOY.md](atlas-app-manifests/DEPLOY.md)

---

## 1 — Motivation

On 2026-06-07 the compliance scanner kept flagging Atlas's `classifier.py` for hard-coded `https://api.anthropic.com/v1/messages` + `api_key` use. PR [#2294](https://github.com/anthropics/evolve/pull/2294) (merged 2026-06-06) plus follow-ups #2306 + #2330 had already replaced the offending code with the `oc_dispatch` transport — the canonical implementation of [principle-apps-inherit-bot-llm](principle-apps-inherit-bot-llm.md). The repo at `docs/atlas-app-manifests/scripts/atlas_lib/` carried the new code. The bot's workspace at `/Users/atlas/.openclaw/workspace/scripts/atlas_lib/` carried the *old* code. The gap stood for ~36 hours, during which:

- Atlas's running classifier kept calling the Anthropic API directly with a leaked key.
- Every classifier invocation kept charging that key.
- The compliance scanner kept firing the same Signal.
- The "fix" (the merged PR) was sitting in the repo, doing nothing.

The remediation was a hand `cp -r` from `/Users/Shared/evolve-repo/docs/atlas-app-manifests/scripts/` into `/Users/atlas/.openclaw/workspace/scripts/` plus a `__pycache__` purge. The operator gesture that was *supposed* to redeploy the app — `sudo evolve-admin apply-actions atlas atlas-daily-digest` — did nothing useful, because:

- **`apply_actions` only iterates `manifest.scheduled_actions[]`.** It materializes launchd plists. Workspace files are not in its purview.
- **Forge's `_maybe_install_via_files_pack` does the file copy, but only on the install path.** It runs at first install. There is no re-entry point that re-checks source-vs-installed drift.
- **Atlas has no files-pack at all.** Per [DEPLOY.md](atlas-app-manifests/DEPLOY.md), the four Atlas manifests are explicitly side-loaded from `docs/atlas-app-manifests/scripts/` via hand scp + cp. The forge engine's `find_files_pack_dir` only searches `gallery/<slug>/files/`, so Atlas's source is invisible to it.

The failure mode generalises: any manifest-driven app whose bundled source updates after first install silently keeps running stale code on every bot until someone notices. For security-sensitive changes (the apps-inherit-bot-llm class) the lag window is dangerous — old credentialed code keeps charging the leaked key. The Atlas case is the worst-case example; gallery apps that go through forge once and then get a files-pack update via PR have the same gap with a smaller blast radius.

## 2 — Goals and non-goals

### Goals
1. **`sudo evolve-admin apply-actions <bot> <app>` after a PR merge results in the bot's workspace files reflecting the new code on next invocation.** No hand scp, no `__pycache__` archaeology.
2. **Drift-aware default.** A clean run (nothing changed) is ~free and prints no noise. Cost matters for [feedback_rsi_low_cost_preference](memory_link).
3. **Same substrate for gallery files-packs and side-loaded sources.** Atlas's `docs/atlas-app-manifests/scripts/` and ea-pack's `gallery/ea-pack/files/` resolve through one code path.
4. **Audit trail.** The manifest records what was synced and when, so a future operator can answer "is this bot up to date?" without `diff -r`.
5. **No behaviour regression.** Manifests that don't opt in (no `files_pack` block, no `workspace_files_source`, no `files[]`) keep working exactly as today.

### Non-goals
- **Auto-detecting drift hourly.** Not in v1. The trigger is an operator gesture (`apply-actions`). v2 may add a pod-wide drift sweep — see §10.
- **Pruning orphans.** If a source file used to exist and was removed, v1 logs a warning and leaves the orphan in place. Operator gesture; see §6.
- **Replacing the LLM-forge install path.** For apps that genuinely need per-bot LLM-generated code, the existing path stays. Sync only touches files that are in the manifest's `files[]` and whose source resolves.
- **Cross-bot sync.** apply-actions is per-(bot, app). A pod-wide sweep that loops apply-actions across every bot already exists as `reconcile-actions`; the new sync runs inside apply-actions so reconcile-actions inherits it for free.

## 3 — Architecture overview

```
sudo evolve-admin apply-actions <bot> <app>
        │
        ├─ [optional --from-gallery] re-seed scheduled_actions[]
        │
        ├─ NEW: _sync_workspace_files(manifest, shared_dir, mode)            ← this spec
        │       │
        │       ├─ resolve_workspace_files_source(manifest)
        │       │     1. gallery files-pack       (find_files_pack_dir(pkg_id))
        │       │     2. workspace_files_source   (manifest-declared, this spec)
        │       │     3. None  → log + skip
        │       │
        │       ├─ load_or_synthesize_files_pack_metadata(source_dir, manifest)
        │       │     • real manifest.json present → load it (existing path)
        │       │     • absent → walk source_dir, compute sha256 per file,
        │       │       infer mode (0755 for .sh, 0644 default), use
        │       │       manifest.files[] as the path list filter
        │       │
        │       ├─ for each file:
        │       │     drift?  compare sha256(source) vs sha256(workspace)
        │       │     yes  →  install_files_pack_to_workspace(allowed_paths={p})
        │       │             record drift
        │       │     no   →  noop
        │       │
        │       ├─ invalidate __pycache__ in affected directories
        │       │
        │       └─ stamp manifest.workspace_sync = {at, source, drifted, orphans}
        │
        └─ _materialize_scheduled_actions(manifest)
```

Sync runs **before** `_materialize_scheduled_actions` so a launchd plist that points at `scripts/atlas_digest.py` materialises against the freshly-synced file rather than the stale one.

The skip logic at [forge_engine.py:3168–3182](packages/admin/evolve_admin/applications/forge_engine.py) (which short-circuits action materialisation when `installed_artifact` is set) is unaffected — that's the **action**-level skip and is correct for idempotence on plists. The new sync is separate; it operates on the **workspace files**, not on `scheduled_actions[]`.

## 4 — Manifest schema additions

### 4.1 `workspace_files_source` — optional source pointer

New optional top-level field on `ApplicationManifest`:

```json
{
  "id": "app_atlas_daily_digest",
  "pkg_id": "p-7b26ba5e",
  "files": [ { "path": "scripts/atlas_digest.py", ... } ],
  "workspace_files_source": "docs/atlas-app-manifests/scripts/"
}
```

Semantics:

- **Path is relative to repo root.** Resolved against `_REPO_ROOT` (the same constant `gallery.py` uses). On the mini, this is `/Users/Shared/evolve-repo/`. On a dev laptop, the worktree's checkout root.
- **Only the SOURCE side is declared.** The destination is always `{bot_workspace}/`. Each entry in `manifest.files[]` has a `path` that's already relative to `{bot_workspace}` (e.g. `scripts/atlas_digest.py`); concatenation with `workspace_files_source` yields the source path.
- **Optional.** Omitted → sync resolver falls through to the gallery files-pack path. If neither resolves, sync is a no-op (logged).
- **Validated, not enforced at write time.** A manifest with `workspace_files_source` pointing to a missing dir is loadable; sync logs "source missing" and continues.

The field belongs to the per-app manifest, not network.json or a central registry, because:

- It's an authoring concern (the manifest author knows where they keep the code).
- Gallery apps don't need it (resolution falls through to `find_files_pack_dir`).
- It survives manifest migration into gallery later — the field can be dropped when the app moves to `gallery/<slug>/files/`.

### 4.2 `workspace_sync` — audit stamp

New optional top-level field, written by sync, read by the admin UI / debug:

```json
{
  "workspace_sync": {
    "last_synced_at":   "2026-06-07T18:42:11Z",
    "source":           "docs/atlas-app-manifests/scripts/",
    "synced_count":     11,
    "drifted_paths":    ["scripts/atlas_lib/classifier.py"],
    "orphan_paths":     [],
    "missing_in_source":["scripts/atlas_lib/legacy_thing.py"],
    "errors":           []
  }
}
```

`drifted_paths` lists files whose sha mismatched before sync; `orphan_paths` lists files present in the workspace but absent in the source (logged-only in v1); `missing_in_source` lists `files[]` entries the resolver couldn't find on the source side (a manifest error to surface, distinct from orphans). Stamp is overwritten each run.

### 4.3 Schema migration

Field defaults are empty string / empty dict respectively. Existing manifests round-trip through `from_dict` → `to_dict` unchanged. No migration needed; `manifest.py:from_dict` already filters unknown keys.

## 5 — Resolver

```
def resolve_workspace_files_source(
    manifest: ApplicationManifest,
    *,
    repo_root: Path,
) -> WorkspaceFilesSource | None:
    """Return a source whose .files_pack_dir + .synthesise flag tell the
    sync caller how to load metadata."""

    # (1) gallery files-pack — preferred when present
    if manifest.pkg_id:
        from .gallery import find_files_pack_dir
        gallery_dir = find_files_pack_dir(manifest.pkg_id)
        if gallery_dir is not None:
            return WorkspaceFilesSource(
                files_pack_dir = gallery_dir,
                synthesise     = False,
                origin         = "gallery",
            )

    # (2) declared side-loaded source
    src = (manifest.workspace_files_source or "").strip()
    if src:
        candidate = (repo_root / src).resolve()
        if not str(candidate).startswith(str(repo_root.resolve())):
            # path traversal guard — operator-set field reads ok
            # but escaping the repo is rejected loudly
            raise WorkspaceFilesSourceError(
                f"workspace_files_source {src!r} escapes the repo root"
            )
        if candidate.is_dir():
            return WorkspaceFilesSource(
                files_pack_dir = candidate,
                synthesise     = True,
                origin         = "manifest",
            )
        # source declared but missing — return None and let sync log

    return None
```

The path-traversal guard matters: `workspace_files_source` is operator-authored, and the install runs as `evolve` with workspace write ACLs. A traversal escape that pointed at `/etc` would not actually let evolve write there, but the guard makes the contract explicit.

## 6 — Synthesising a files-pack from a side-loaded dir

When `synthesise=True`, sync builds an in-memory `FilesPackMetadata` by:

1. **Path set comes from `manifest.files[]`.** Each entry's `path` is the relative target; the source is `{source_dir}/{path}` (e.g. `docs/atlas-app-manifests/scripts/scripts/atlas_digest.py` — see §6.1 for the prefix concern).
2. **`mode` is inferred:** `.sh` → `0755`, everything else → `0644`. Matches what `chmod +x` would have set in the hand-deploy.
3. **`sha256` is computed at sync time** from the source file.
4. **`placeholders` is empty.** Side-loaded sources don't carry placeholder metadata; if a file needs substitution it has to declare it through a real `files-pack/manifest.json`. v1 is "copy bytes as-is" for side-loaded sources. (Atlas's scripts don't use placeholders — they read `{bot_id}` from the cron script, which is substituted by `sed` at deploy time, not by the install pipeline.)
5. **Files present in source but absent from `manifest.files[]` are ignored.** Sync only acts on files the manifest claims. This is the safety net: a stray `.DS_Store` or a developer's test file in the source dir does not land in the workspace.

### 6.1 The path-prefix question

Atlas's manifest entries look like `scripts/atlas_digest.py`. The deploy walkthrough scp's `docs/atlas-app-manifests/scripts/atlas_digest.py` (no leading `scripts/scripts/`). Two interpretations of `workspace_files_source`:

**(a)** It points at the directory whose children mirror the workspace tree. Then `workspace_files_source = "docs/atlas-app-manifests/"` and source = `docs/atlas-app-manifests/scripts/atlas_digest.py`. Clean mapping.

**(b)** It points at a "files staging" subdir, and the manifest's `files[]` paths are relative to that. Then `workspace_files_source = "docs/atlas-app-manifests/scripts/"` and Atlas's `files[]` entries need stripping the `scripts/` prefix.

**Decision: (a).** The manifest's `files[]` `path` is the workspace-relative target — same as the destination semantics that `install_files_pack_to_workspace` already implements. `workspace_files_source` mirrors that root. For Atlas, the field becomes:

```json
"workspace_files_source": "docs/atlas-app-manifests/"
```

This is consistent with how gallery files-packs work: `gallery/ea-pack/files/manifest.json` lists `scripts/morning_brief.py`, and the source file lives at `gallery/ea-pack/files/scripts/morning_brief.py`. The `files/` dir IS the staging root.

## 7 — Sync algorithm

```python
def _sync_workspace_files(
    manifest: ApplicationManifest,
    shared_dir: Path,
    *,
    bot_user: str,
    workspace_dir: Path,
    mode: SyncMode = SyncMode.DRIFT_AWARE,
) -> WorkspaceSyncResult:
    source = resolve_workspace_files_source(manifest, repo_root=_REPO_ROOT)
    if source is None:
        return WorkspaceSyncResult(skipped=True, reason="no source resolved")

    pack_metadata = (
        load_files_pack_metadata(source.files_pack_dir)
        if not source.synthesise
        else synthesise_files_pack_metadata(source.files_pack_dir, manifest)
    )

    context = resolve_install_context(
        bot_id       = manifest.bot_id,
        bot_user     = bot_user,
        workspace    = str(workspace_dir),
        pkg_id       = manifest.pkg_id or "",
        app_id       = manifest.id or "",
        installed_at = now_iso(),
        shared_dir   = str(shared_dir),
    )

    drifted: list[str] = []
    missing_in_source: list[str] = []
    for entry in pack_metadata.files:
        src = source.files_pack_dir / entry.path
        if not src.is_file():
            missing_in_source.append(entry.path)
            continue
        target = workspace_dir / entry.path
        if mode == SyncMode.DRIFT_AWARE and target.is_file():
            if _sha256_file(target) == entry.sha256:
                continue
        drifted.append(entry.path)

    paths_to_install = (
        set(drifted) if mode == SyncMode.DRIFT_AWARE
        else {f.path for f in pack_metadata.files}
    )

    install_result = install_files_pack_to_workspace(
        pack_metadata, source.files_pack_dir, workspace_dir, context,
        allowed_paths=paths_to_install,
    )

    _invalidate_pycache_under(workspace_dir, paths_to_install)

    orphans = _detect_orphans(workspace_dir, pack_metadata, manifest)
    return WorkspaceSyncResult(
        synced_count      = len(install_result.files_written),
        drifted_paths     = drifted,
        orphan_paths      = orphans,
        missing_in_source = missing_in_source,
        errors            = install_result.errors,
        origin            = source.origin,
    )
```

### 7.1 SyncMode

```python
class SyncMode(Enum):
    DRIFT_AWARE = "drift_aware"   # default — only re-copy if sha differs
    FORCE       = "force"          # --force-files-sync — re-copy everything
    SKIP        = "skip"            # --no-files-sync — disable entirely
```

`SyncMode.SKIP` lets an operator say "I only want the launchd plists re-materialised, don't touch workspace files." Useful for emergency rollback when the source is *wrong* and a forced re-sync would make things worse.

### 7.2 `__pycache__` invalidation

Python's import system trusts mtime+size to invalidate `.pyc` files. `os.replace` preserves neither — the new file has new mtime, so Python *should* re-compile on next import. But the Atlas case showed `__pycache__` lingering with the old bytecode, suggesting the bot's process had the old module imported already and never re-imported (a launchd-scheduled fresh-process job won't hit this, but a long-running daemon will).

Practical fix: `_invalidate_pycache_under(workspace_dir, paths_to_install)` walks each directory containing an installed `.py` file and removes `__pycache__/`. Cheap, side-effect-only.

```python
def _invalidate_pycache_under(workspace_dir: Path, paths: set[str]) -> None:
    py_parents = {(workspace_dir / p).parent for p in paths if p.endswith(".py")}
    for parent in py_parents:
        cache_dir = parent / "__pycache__"
        if cache_dir.is_dir():
            try:
                shutil.rmtree(cache_dir)
            except (PermissionError, OSError):
                pass  # log to result.errors; not fatal
```

### 7.3 Orphan detection

```python
def _detect_orphans(
    workspace_dir: Path,
    pack_metadata: FilesPackMetadata,
    manifest: ApplicationManifest,
) -> list[str]:
    """Files in the workspace that were once installed (in manifest.files[])
    but are now absent from the source. Log-only in v1."""
    declared_paths = {(f.get("path") if isinstance(f, dict) else f)
                      for f in (manifest.files or [])}
    source_paths   = {f.path for f in pack_metadata.files}
    declared_but_missing = declared_paths - source_paths
    orphans = []
    for p in declared_but_missing:
        if not isinstance(p, str):
            continue
        if (workspace_dir / p).is_file():
            orphans.append(p)
    return sorted(orphans)
```

A file is an "orphan" when: the manifest declared it, the source no longer has it, but the workspace still does. v1 logs to `workspace_sync.orphan_paths`. v2 may add `--prune-orphans`.

## 8 — Cost on a clean run

For a manifest with N files, drift-aware sync does:

- N file reads (the workspace targets) for sha256 hashing
- N file reads (the source files) for sha256 hashing  
  (avoidable when source has a stamped sha — but synthesised packs don't, so the read happens)
- 1 directory walk for orphan detection (≤ M entries where M = workspace dir size)

For ea-pack (3 files, ~7KB each), that's ~42KB of IO. Atlas (11 files in `scripts/`, mostly < 20KB), ~440KB. On the mini's SSD, sub-50ms total. The cost lever is sha256 over already-cached file contents; on warm pages this is effectively memory-bandwidth-limited.

A `--no-files-sync` escape exists for the unusual case where even this is too much (think: a pod-wide reconcile-actions sweep across 100 apps × 9 bots). Not anticipated as a real concern; flagged for completeness.

## 9 — Failure modes

| Condition | v1 behaviour |
|-----------|--------------|
| `workspace_files_source` declared but path missing | Log; `workspace_sync.skipped` true; reason="source dir missing". apply-actions overall still succeeds (sync is best-effort within a successful apply-actions). |
| Source declared, gallery files-pack ALSO present | Gallery wins (priority order in §5). Operator can `unset workspace_files_source` to make it explicit, but the safer default is gallery. |
| Path traversal escape attempt | `WorkspaceFilesSourceError` propagates out of `apply_actions`; exits 1 with a clear message. Not silent. |
| A file fails to write (PermissionError) | Per-file error recorded in `install_result.errors` → surfaces in `workspace_sync.errors`. Other files still sync. apply-actions exit code unchanged (matches existing per-action best-effort policy). |
| `__pycache__` removal fails | Logged to `workspace_sync.errors`; not fatal. |
| Source file binary content + utf-8 decode would fail | `install_files_pack_to_workspace` already handles this: no placeholders declared → raw bytes; placeholders declared → utf-8 required (raises if not). Synthesised packs declare no placeholders, so the bytes path is used. |
| Concurrent apply-actions on the same (bot, app) | `os.replace` is atomic; a torn write between two callers is impossible. Two callers might compute drift independently and both decide to install; the second is a no-op because the first's write makes sha match. No locking needed. |

## 10 — Out of scope

### 10.1 Pod-wide drift sweep
The substrate makes a cron job trivial: `reconcile-actions --files-sync` would walk every bot × app and run sync. v1 ships without a daemon to keep the trigger explicit (operator runs apply-actions after a PR merge). v2 candidates:

- Extend the existing `reconcile-actions` command's `--apply` mode to include workspace file sync.
- A new `signal-subscriber`-aligned hook that fires sync on every `repo_puller` success Signal.

The Atlas incident would have been caught within 15 minutes of the merge by either approach. Trade-off: silent operator-less sync is exactly the failure mode that bit cost-alerting on 2026-05-20 — a thing that "just works" until it doesn't and nobody notices. Operator gesture stays the v1 contract.

### 10.2 Pruning orphans
Per §6 / §7.3. v1 logs. The v2 flag is `--prune-orphans` on apply-actions or `--prune-orphans` on a separate `reconcile-workspace-files` verb.

### 10.3 Source-file linting
Pre-sync validation that the source files would pass the compliance scanner if installed. Useful, but conflates two passes (sync + audit). Defer to a separate "pre-deploy audit" command. The compliance scanner already runs on the installed workspace post-sync, which catches the regression we care about.

### 10.4 Forge-time integration
Forge install today calls `_maybe_install_via_files_pack` for the initial copy. This spec does NOT change that path. The two paths converge in v2 — `_maybe_install_via_files_pack` becomes `_sync_workspace_files(mode=FORCE)` — but for v1 we keep them separate to limit blast radius.

## 11 — Implementation phasing

### Phase 1 (this PR)
- New manifest field `workspace_files_source` (optional string).
- New manifest field `workspace_sync` (optional dict, stamp).
- New module `packages/admin/evolve_admin/applications/workspace_sync.py` with:
  - `resolve_workspace_files_source`
  - `synthesise_files_pack_metadata`
  - `sync_workspace_files` (the algorithm in §7)
  - `SyncMode` enum
  - `WorkspaceSyncResult` dataclass
- `apply_actions.apply_actions(...)` wires sync in before `_materialize_scheduled_actions`.
- New CLI flag `--no-files-sync` (negation pattern); default behaviour is drift-aware sync.
- New CLI flag `--force-files-sync` for the "re-copy regardless of drift" case.
- The four Atlas manifests at `docs/atlas-app-manifests/atlas-*.json` gain `"workspace_files_source": "docs/atlas-app-manifests/"`.
- Atlas DEPLOY.md is updated to point operators at `sudo evolve-admin apply-actions atlas <app>` for re-syncs.

### Phase 2 (follow-up)
- `reconcile-actions --files-sync` flag — pod-wide drift sweep.
- `--prune-orphans` flag for the cleanup case.
- Admin UI surfacing of `workspace_sync.drifted_paths` per app.

### Phase 3 (eventual)
- Forge install path converges with sync path (`_maybe_install_via_files_pack` → `sync_workspace_files(mode=FORCE)`).
- Atlas-style manifests move to a real gallery layout; `workspace_files_source` field becomes the no-op fallback for any remaining side-loaded apps.

## 12 — Success criteria

After Phase 1 ships:

1. **Reproduces the original failure.** Modify any file under `docs/atlas-app-manifests/scripts/`, run `sudo evolve-admin apply-actions atlas atlas-daily-digest`, confirm `/Users/atlas/.openclaw/workspace/scripts/` matches. Revert: workspace stays at the new content until source reverts and apply-actions is re-run.
2. **No regression on clean runs.** `reconcile-actions` across the existing pod produces no spurious `workspace_sync.drifted_paths` on any bot.
3. **Audit trail.** Each apply-actions run that touches workspace files leaves a `workspace_sync` stamp on the manifest with `last_synced_at`, `synced_count`, `drifted_paths`.
4. **Atlas DEPLOY.md re-deploy procedure shrinks from 30+ lines of scp+cp+chmod+chown to one CLI call.**

## 13 — Risks

- **`workspace_files_source` lets a manifest author point at any in-repo dir.** Mitigated by the path-traversal guard (§5) and the manifest.files[] filter (§6: only declared files sync). A malicious manifest can re-copy files it's declared, but it can already do that at first install via the forge path.
- **__pycache__ purge affects unrelated apps if two apps share a directory.** Atlas's `scripts/atlas_lib/` is shared by four apps (`shared_with` in the manifest's files[]). A sync of `atlas-daily-digest` would purge `scripts/atlas_lib/__pycache__` and any concurrent invocation of `atlas-article-capture` would re-compile on next import. Acceptable: re-compilation is the explicit goal here, and atlas_lib is the only shared dir in practice.
- **Mass drift on first roll-out.** Every existing Atlas bot will see all 11 files as "drifted" the first apply-actions runs against the new code, because the workspace currently has different content than the (post-PR-#2294) source. This IS the bug we want to fix, but it'll feel noisy. Mitigation: log clearly that "first-sync after this PR will appear to drift every file"; the second run will be clean.
