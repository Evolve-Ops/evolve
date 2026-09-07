"""
apply_actions — operator entry point to materialize an installed app's
scheduled_actions[] on a bot, idempotently.

Why this exists
---------------

When an app is forge-installed, the bot's manifest is a COPY of the
gallery package at install time. Subsequent changes to the gallery
(e.g. the 2026-06-04 scheduled_actions[] migration) do NOT propagate
automatically — the decoupling is deliberate (manifest changes might
be breaking; operators should opt in), but it creates a gap where
post-install fixes need a way to be reapplied to existing bots.

Pre-this-module, the only ways to reapply scheduled_actions[] to an
already-installed app were:

  - Forge improvement run via the admin UI — runs the full LLM
    build/critique/test cycle. Massive overkill just to re-materialize
    daemons whose manifest entries are already correct.
  - Hand-rolled `sudo -u evolve python3 -c "..."` invocations. Brittle
    and per-host.

This module wraps ``forge_engine._materialize_scheduled_actions`` with
a thin operator surface:

  evolve-admin apply-actions <bot_id> <app_id>
      → materialize whatever scheduled_actions[] currently sits in the
        bot's installed manifest. Idempotent: re-running on a
        fully-installed app is a no-op (Phase 4.5's already-installed
        guard fires).

  evolve-admin apply-actions <bot_id> <app_id> --from-gallery
      → re-seed scheduled_actions[] from the current gallery package
        BEFORE materializing. Preserves installed_* stamps from any
        prior install so the audit trail stays continuous. Use after
        a gallery-side migration changes scheduled_actions[] shape
        (the 2026-06-04 Atlas-style case).

Failure semantics
-----------------

Per-action best-effort, mirroring Phase 4.5: a failure on one action
records the error in the summary but does NOT abort the run. The
caller (CLI) inspects the summary and exits with a non-zero code if
any action failed. The manifest is always saved with whatever
installed_* stamps did land.

Bot not registered, manifest missing, gallery package missing →
raised as ``ApplyActionsError`` so the CLI can surface a clean
error without a traceback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class ApplyActionsError(Exception):
    """Raised when the apply-actions precondition isn't met.

    The CLI catches this and prints the message without a traceback.
    Subclasses by failure category so callers can branch if needed.
    """


class BotNotRegisteredError(ApplyActionsError):
    pass


class ManifestNotFoundError(ApplyActionsError):
    pass


class GalleryPackageNotFoundError(ApplyActionsError):
    pass


@dataclass
class ApplyActionsResult:
    """Structured result returned by ``apply_actions`` for the CLI to render.

    The summary list mirrors ``_materialize_scheduled_actions``'s shape:
    one entry per scheduled_actions[] entry, with status ∈
    {"ok", "skipped", "failed"}. Counts are pre-computed for the CLI's
    pretty-printer.

    ``workspace_sync`` carries the result of the workspace file sync pass
    (spec: internal/spec-workspace-file-sync-2026-06-07.md). When the
    operator passes ``--no-files-sync`` or the manifest has no source the
    resolver can find, ``workspace_sync.skipped`` is True and nothing was
    copied. When files genuinely drifted, ``workspace_sync.synced_count``
    > 0 and ``drifted_paths`` lists them.
    """

    bot_id: str
    app_id: str
    pkg_id: str
    summary: list[dict] = field(default_factory=list)
    synced_from_gallery: bool = False
    workspace_sync: dict = field(default_factory=dict)

    @property
    def ok_count(self) -> int:
        return sum(1 for e in self.summary if e.get("status") == "ok")

    @property
    def skipped_count(self) -> int:
        return sum(1 for e in self.summary if e.get("status") == "skipped")

    @property
    def failed_count(self) -> int:
        return sum(1 for e in self.summary if e.get("status") == "failed")

    def to_dict(self) -> dict:
        return {
            "bot_id":             self.bot_id,
            "app_id":             self.app_id,
            # identity: see resolve_app_id — ``app_id`` and ``pkg_id`` are two
            # DIFFERENT strings on this result and both are load-bearing:
            # ``app_id`` is the manifest filename stem the CLI was invoked with,
            # ``pkg_id`` is the gallery catalog key (AL-1.4 §6 delta 12). The
            # resolver returns the latter for a gallery install, so collapsing
            # them would silently drop the stem.
            "pkg_id":             self.pkg_id,
            "synced_from_gallery": self.synced_from_gallery,
            "workspace_sync":     self.workspace_sync,
            "summary":            self.summary,
            "counts": {
                "ok":      self.ok_count,
                "skipped": self.skipped_count,
                "failed":  self.failed_count,
            },
        }


def _sync_scheduled_actions_from_gallery(
    manifest, gallery_pkg: dict,
) -> int:
    """Replace ``manifest.scheduled_actions[]`` with the gallery's, preserving
    ``installed_at`` / ``installed_by`` / ``installed_artifact`` stamps on
    actions whose ``id`` matches.

    Stamp preservation matters for the audit trail — without it, a re-sync
    would lose the record of when the action was first installed (and by
    which forge job). For an action that didn't previously exist (new id
    introduced by the migration), no stamps to carry forward; Phase 4.5
    will stamp it fresh on first materialize.

    Returns the number of action ids that gained / changed shape. Zero
    means the gallery shape and the installed shape were already
    structurally identical.
    """
    gallery_actions = gallery_pkg.get("scheduled_actions") or []
    if not isinstance(gallery_actions, list):
        gallery_actions = []

    existing_by_id: dict[str, dict] = {}
    for a in (manifest.scheduled_actions or []):
        if isinstance(a, dict) and a.get("id"):
            existing_by_id[a["id"]] = a

    new_actions: list[dict] = []
    changed = 0
    for gal in gallery_actions:
        if not isinstance(gal, dict):
            continue
        gal_copy = dict(gal)  # don't mutate the gallery copy
        old = existing_by_id.get(gal_copy.get("id", ""), {})
        # Preserve stamps from prior install so the trail stays continuous.
        for stamp_key in ("installed_at", "installed_by", "installed_artifact"):
            if stamp_key in old:
                gal_copy[stamp_key] = old[stamp_key]
        # Crude drift check: was anything other than the preserved stamps
        # different between gallery and installed? Used for the changed count.
        old_minus_stamps = {k: v for k, v in old.items()
                            if k not in ("installed_at", "installed_by", "installed_artifact")}
        gal_minus_stamps = {k: v for k, v in gal_copy.items()
                            if k not in ("installed_at", "installed_by", "installed_artifact")}
        if old_minus_stamps != gal_minus_stamps:
            changed += 1
        new_actions.append(gal_copy)

    manifest.scheduled_actions = new_actions
    return changed


def _build_synthetic_job(bot_id: str, app_id: str, pkg_id: str):
    """Construct a ForgeJob shell so ``_materialize_scheduled_actions`` has
    something to stamp ``installed_by = "forge:<job_id>"`` against.

    Apply-actions isn't a real forge run — there's no build phase, no
    test, no critique — but Phase 4.5 reads ``job.job_id`` to stamp
    provenance on each installed action. We make the synthetic job_id
    distinguishable from a real forge run (prefix ``apply-actions-``)
    so audit log readers can tell the difference.
    """
    from .forge_jobs import ForgeJob
    # Avoid Math.random / Date.now patterns — use timestamp + monotonic
    # for unique-enough ids. Local-only side effect; not a security
    # boundary.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ForgeJob(
        job_id              = f"apply-actions-{ts}",
        run_id              = f"r-apply-{ts}",
        job_type            = "apply",
        pkg_id              = pkg_id,
        app_id              = app_id,
        bot_id              = bot_id,
        pkg_version_before  = None,
        gallery_version     = None,
    )


def apply_actions(
    bot_id: str,
    app_id: str,
    shared_dir: Path,
    *,
    from_gallery: bool = False,
    files_sync_mode: "str | None" = None,
    network: dict | None = None,
) -> ApplyActionsResult:
    """Materialize an installed app's ``scheduled_actions[]`` on a bot,
    and re-sync any drifted workspace files from their canonical source.

    The workspace-file-sync pass runs BEFORE
    ``_materialize_scheduled_actions`` so a launchd plist pointing at
    ``scripts/atlas_digest.py`` materialises against the freshly-synced
    file rather than a stale one. Spec:
    internal/spec-workspace-file-sync-2026-06-07.md.

    Args:
        bot_id, app_id: identify the installed manifest at
            ``{bot_workspace}/manifests/{app_id}.json``.
        shared_dir: pod-wide shared dir, used by the manifest loader and
            by Phase 4.5's audit-log writes.
        from_gallery: when True, re-seed ``manifest.scheduled_actions[]``
            from the current gallery package's entries (preserving
            stamps where ids match) BEFORE materializing. Use this after
            a gallery-side migration that changed shape; otherwise the
            existing installed shape is what gets materialized. Default
            False so the default behaviour is a pure "redo whatever the
            manifest currently says" — the safest primitive.
        files_sync_mode: ``"drift_aware"`` (default) re-copies only files
            whose source sha256 disagrees with the workspace; ``"force"``
            re-copies regardless; ``"skip"`` disables the sync pass. The
            CLI surfaces these as ``(default)``, ``--force-files-sync``,
            and ``--no-files-sync`` respectively. ``None`` is treated as
            the default.
        network: pre-loaded network dict; loaded from disk if None.

    Returns:
        ApplyActionsResult with per-action summary and counts. The
        ``workspace_sync`` field carries the sync stamp (also persisted
        on the manifest).

    Raises:
        BotNotRegisteredError: bot_id absent from network.json.
        ManifestNotFoundError: no installed manifest for ``app_id`` on
            this bot.
        GalleryPackageNotFoundError: ``from_gallery=True`` and the
            manifest's ``pkg_id`` can't be found in the gallery loader
            (this is the Atlas-in-docs case where the package was
            side-loaded outside the gallery dir).
        WorkspaceFilesSourceError: ``manifest.workspace_files_source`` is
            set but resolves outside the repo root (path traversal
            escape). The error propagates so the CLI surfaces it
            clearly rather than silently skipping sync.
    """
    from ..config import bot_home, get_bot_user, load_network as _load_network
    from .manifest import load_manifest, save_manifest

    net = network if network is not None else _load_network()
    if bot_id not in (net.get("bots") or {}):
        raise BotNotRegisteredError(
            f"bot {bot_id!r} is not registered in network.json — "
            f"add it via `evolve-admin add-bot {bot_id}` first"
        )

    manifest = load_manifest(app_id, bot_id, shared_dir)
    if manifest is None:
        raise ManifestNotFoundError(
            f"no installed manifest for app {app_id!r} on bot {bot_id!r} "
            f"— install it via the admin UI's Gallery page first"
        )

    # The materializer reads bot_id off the manifest in some branches;
    # the load path sets it from the manifests/ directory but a
    # defensive set here keeps sync's resolver honest.
    if not getattr(manifest, "bot_id", ""):
        manifest.bot_id = bot_id

    # identity: see resolve_app_id — GALLERY CATALOG KEY, not the canonical id.
    # It is handed straight to ``load_gallery_package``, which indexes
    # ``gallery/<app-name>/<pkg_id>.json`` and the ``LEGACY_KEY_MIGRATION``
    # alias table; both are keyed on ``pkg_id``. An empty value is also the
    # documented "side-loaded, not from the gallery" signal below, which a
    # resolver fallback to the manifest stem would erase.
    pkg_id = manifest.pkg_id or ""

    synced = False
    if from_gallery:
        from .gallery import load_gallery_package
        pkg = load_gallery_package(pkg_id, shared_dir) if pkg_id else None
        if pkg is None:
            raise GalleryPackageNotFoundError(
                f"gallery package {pkg_id!r} not found — cannot sync. "
                f"If this is a side-loaded app (e.g. authored outside "
                f"gallery/), patch the on-disk manifest directly and "
                f"re-run without --from-gallery."
            )
        changed = _sync_scheduled_actions_from_gallery(manifest, pkg)
        synced = True
        save_manifest(manifest, shared_dir)
        logger.info(
            "apply_actions: synced scheduled_actions[] from gallery for "
            "bot=%s app=%s changed_entries=%d",
            bot_id, app_id, changed,
        )

    # Workspace file sync. Runs BEFORE _materialize_scheduled_actions so
    # the launchd plists materialise against the freshly-synced files.
    from .workspace_sync import SyncMode, sync_workspace_files

    raw_mode = (files_sync_mode or SyncMode.DRIFT_AWARE.value).strip().lower()
    try:
        sync_mode = SyncMode(raw_mode)
    except ValueError:
        # Caller (the CLI) is responsible for restricting to known
        # values; defend against typos in library callers by falling
        # back to drift_aware with a log.
        logger.warning(
            "apply_actions: unknown files_sync_mode %r — defaulting to drift_aware",
            files_sync_mode,
        )
        sync_mode = SyncMode.DRIFT_AWARE

    bot_user = get_bot_user(bot_id, net)
    workspace_dir = bot_home(bot_id, net) / ".openclaw" / "workspace"

    sync_result = sync_workspace_files(
        manifest,
        shared_dir,
        bot_user      = bot_user,
        workspace_dir = workspace_dir,
        mode          = sync_mode,
    )

    sync_stamp = sync_result.to_stamp()
    manifest.workspace_sync = sync_stamp

    if sync_result.synced_count or sync_result.drifted_paths:
        logger.info(
            "apply_actions: synced %d workspace file(s) on bot=%s app=%s "
            "(drifted=%d, source=%s)",
            sync_result.synced_count, bot_id, app_id,
            len(sync_result.drifted_paths), sync_result.origin or "n/a",
        )

    # Run Phase 4.5 against the (possibly synced) manifest. The materializer
    # stamps installed_at / installed_by / installed_artifact in-place on
    # each action; we save the manifest afterwards so those stamps persist.
    from .forge_engine import (
        _materialize_scheduled_actions,
        _record_scheduled_action_outcomes,
    )

    job = _build_synthetic_job(bot_id, app_id, pkg_id)
    summary = _materialize_scheduled_actions(job, manifest)

    # Failure-visibility parity with forge Phase 4.5 (audit slate S2): this
    # CLI remediation path must apply the SAME stamps + Signal transitions,
    # or a successful re-materialize here would leave install_error stamped
    # and the failure Signal firing forever — violating the manifest.py
    # "cleared when a later attempt installs" contract. Records failures AND
    # clears/resolves on success; runs before save_manifest so stamps persist.
    _record_scheduled_action_outcomes(job, manifest, summary, shared_dir)

    save_manifest(manifest, shared_dir)

    return ApplyActionsResult(
        bot_id              = bot_id,
        app_id              = app_id,
        pkg_id              = pkg_id,
        summary             = summary,
        synced_from_gallery = synced,
        workspace_sync      = sync_stamp,
    )
