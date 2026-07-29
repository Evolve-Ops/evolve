"""
reconcile_actions — pod-wide drift detection between installed manifests
and current gallery sources, for ``scheduled_actions[]`` specifically.

Why this exists
---------------

``apply_actions`` (the sibling module) is the per-(bot, app) primitive.
It's the right tool when you already know what needs fixing. But the
2026-06-04 Atlas incident showed the upstream gap: nothing was watching
for drift, so the operator only noticed "no digest messages ever fired"
days after the fact.

This module is the answer: walk every bot, compare each installed app's
``scheduled_actions[]`` to the current gallery package, classify the
difference, and (optionally) apply ``apply_actions(--from-gallery)`` to
each drifted entry. The same primitive is reachable via
``evolve-admin reconcile-actions`` for the operator, and is the
substrate a future scheduled-audit daemon would call.

Drift classification
--------------------

For each (bot, app) pair:

  OK
      The installed manifest's scheduled_actions[] is structurally
      identical to the gallery package's. No action needed.

  SHAPE_DRIFT
      Same action ids in both, but at least one ``install`` block (or
      mechanism, schedule, etc.) differs. The 2026-06-04 namespace
      rename (com.${bot_id}.* → ai.evolve.${bot_id}.*) lands here.
      --apply fixes via apply_actions(--from-gallery).

  MISSING_IN_INSTALLED
      Gallery has action ids the installed manifest lacks. The
      2026-06-04 first migration (Atlas, the 8 broken apps) lands here
      — installed manifests had scheduled_actions=[], gallery now has
      structured entries. --apply fixes.

  MISSING_IN_GALLERY
      Installed has action ids the gallery lacks. Either the gallery
      package was edited to remove a daemon (rare) or the manifest was
      manually augmented post-install. We surface this without
      auto-removing — operator must decide whether to keep the manual
      addition or sync.

  SKIPPED_NO_PKG_ID
      Manifest has no pkg_id. Forge-built custom apps that were never
      tied to a gallery package. Can't compare; report and move on.

  SKIPPED_SIDE_LOADED
      pkg_id is set but the gallery loader can't find it. Atlas was in
      this state before being moved into the gallery. Report and move on.

  SKIPPED_NO_DAEMON
      Neither the installed manifest nor the gallery package declares
      any scheduled_actions[]. No daemon to install, no drift possible.
      Quiet-skip in normal output, surfaces in --json.

  ERROR
      Manifest unreadable or gallery loader raised. Wrapped with the
      exception detail so the operator can investigate.

Per-bot best-effort: one bot's read failure doesn't abort the others.
Per-app best-effort within --apply: one apply_actions failure doesn't
stop the rest. The summary lists every outcome so the operator gets a
single coherent report.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Classification constants ─────────────────────────────────────────────────

CLASS_OK                   = "ok"
CLASS_SHAPE_DRIFT          = "shape_drift"
CLASS_MISSING_IN_INSTALLED = "missing_in_installed"
CLASS_MISSING_IN_GALLERY   = "missing_in_gallery"
CLASS_SKIPPED_NO_PKG_ID    = "skipped_no_pkg_id"
CLASS_SKIPPED_SIDE_LOADED  = "skipped_side_loaded"
CLASS_SKIPPED_NO_DAEMON    = "skipped_no_daemon"
CLASS_ERROR                = "error"

# Classifications that --apply will attempt to remediate. The others are
# either OK, ambiguous (MISSING_IN_GALLERY — operator intent unknown), or
# infrastructure-impossible (SKIPPED_* — nothing for apply_actions to do).
_REMEDIABLE: frozenset[str] = frozenset({
    CLASS_SHAPE_DRIFT,
    CLASS_MISSING_IN_INSTALLED,
})


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class AppDriftReport:
    """One (bot, app) entry in the reconciliation result."""

    bot_id:           str
    app_id:           str
    pkg_id:           str
    classification:   str
    detail:           str = ""
    # Sub-counts for drift cases. Zero for non-drift classifications.
    installed_action_ids: list[str] = field(default_factory=list)
    gallery_action_ids:   list[str] = field(default_factory=list)
    drifted_action_ids:   list[str] = field(default_factory=list)
    # Populated when --apply ran against this entry.
    applied:          bool = False
    apply_summary:    dict | None = None
    apply_error:      str = ""

    def to_dict(self) -> dict:
        return {
            "bot_id":               self.bot_id,
            "app_id":               self.app_id,
            "pkg_id":               self.pkg_id,
            "classification":       self.classification,
            "detail":               self.detail,
            "installed_action_ids": self.installed_action_ids,
            "gallery_action_ids":   self.gallery_action_ids,
            "drifted_action_ids":   self.drifted_action_ids,
            "applied":              self.applied,
            "apply_summary":        self.apply_summary,
            "apply_error":          self.apply_error,
        }


@dataclass
class ReconcileResult:
    """Aggregate result returned by ``reconcile_actions``."""

    reports: list[AppDriftReport] = field(default_factory=list)
    applied: bool = False  # whether --apply was set for this run

    @property
    def by_classification(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.reports:
            counts[r.classification] = counts.get(r.classification, 0) + 1
        return counts

    @property
    def drifted_count(self) -> int:
        """Reports that need attention (drift classifications, not skips
        and not OK)."""
        return sum(
            1 for r in self.reports
            if r.classification not in (
                CLASS_OK, CLASS_SKIPPED_NO_PKG_ID,
                CLASS_SKIPPED_SIDE_LOADED, CLASS_SKIPPED_NO_DAEMON,
            )
        )

    @property
    def apply_succeeded_count(self) -> int:
        return sum(
            1 for r in self.reports
            if r.applied and r.apply_summary
            and r.apply_summary.get("counts", {}).get("failed", 0) == 0
            and not r.apply_error
        )

    @property
    def apply_failed_count(self) -> int:
        return sum(
            1 for r in self.reports
            if r.applied and (
                r.apply_error
                or (r.apply_summary or {}).get("counts", {}).get("failed", 0) > 0
            )
        )

    def to_dict(self) -> dict:
        return {
            "applied":   self.applied,
            "reports":   [r.to_dict() for r in self.reports],
            "summary":   {
                "by_classification":     self.by_classification,
                "drifted":               self.drifted_count,
                "apply_succeeded":       self.apply_succeeded_count,
                "apply_failed":          self.apply_failed_count,
                "total":                 len(self.reports),
            },
        }


# ── Classification helpers ───────────────────────────────────────────────────


def _normalize_action_for_compare(action: dict) -> dict:
    """Return an ``action`` dict with the in-place stamp fields stripped.

    ``installed_at`` / ``installed_by`` / ``installed_artifact`` are
    stamped by Phase 4.5 on every successful install. They're audit
    trail, not shape. Including them in the comparison would flag every
    installed action as "drifted" relative to the unstamped gallery
    source — false-positives that would drown the real drift in noise.
    """
    if not isinstance(action, dict):
        return {}
    return {k: v for k, v in action.items()
            if k not in ("installed_at", "installed_by", "installed_artifact")}


def _classify(
    installed_actions: list, gallery_actions: list,
) -> tuple[str, str, list[str]]:
    """Classify drift between an installed manifest's scheduled_actions[]
    and a gallery package's scheduled_actions[].

    Returns ``(classification, detail, drifted_action_ids)``.

    The classification cascade is ordered by remediation specificity:
    drifted-shape is the most actionable; missing-in-installed is the
    canonical install-new-daemons case; missing-in-gallery is ambiguous
    and surfaced for operator decision.
    """
    inst_by_id: dict[str, dict] = {
        a["id"]: a for a in installed_actions
        if isinstance(a, dict) and a.get("id")
    }
    gal_by_id: dict[str, dict] = {
        a["id"]: a for a in gallery_actions
        if isinstance(a, dict) and a.get("id")
    }

    inst_ids = set(inst_by_id.keys())
    gal_ids = set(gal_by_id.keys())

    if not inst_ids and not gal_ids:
        return (CLASS_SKIPPED_NO_DAEMON,
                "no scheduled_actions in either side", [])

    in_both = inst_ids & gal_ids
    drifted_in_both: list[str] = []
    for aid in sorted(in_both):
        if _normalize_action_for_compare(inst_by_id[aid]) \
                != _normalize_action_for_compare(gal_by_id[aid]):
            drifted_in_both.append(aid)

    only_in_gallery = sorted(gal_ids - inst_ids)
    only_in_installed = sorted(inst_ids - gal_ids)

    if drifted_in_both:
        # Pick this BEFORE missing-in-installed because shape drift is
        # the more specific signal — when both are present, even if
        # additional gallery entries exist, the "shape changed" detail
        # is what the operator needs to act on first. (--apply re-seeds
        # the whole list anyway, picking up the missing entries too.)
        detail = (
            f"{len(drifted_in_both)} action(s) changed shape: "
            f"{', '.join(drifted_in_both)}"
        )
        if only_in_gallery:
            detail += (
                f"; +{len(only_in_gallery)} new action(s) in gallery: "
                f"{', '.join(only_in_gallery)}"
            )
        if only_in_installed:
            detail += (
                f"; {len(only_in_installed)} action(s) only in installed: "
                f"{', '.join(only_in_installed)}"
            )
        return CLASS_SHAPE_DRIFT, detail, drifted_in_both + only_in_gallery + only_in_installed

    if only_in_gallery and not only_in_installed:
        detail = (
            f"gallery has {len(only_in_gallery)} action(s) not in installed: "
            f"{', '.join(only_in_gallery)}"
        )
        return CLASS_MISSING_IN_INSTALLED, detail, only_in_gallery

    if only_in_installed and not only_in_gallery:
        detail = (
            f"installed has {len(only_in_installed)} action(s) not in gallery: "
            f"{', '.join(only_in_installed)}"
        )
        return CLASS_MISSING_IN_GALLERY, detail, only_in_installed

    if only_in_gallery and only_in_installed:
        # Both sides have unique ids — treat as missing-in-installed
        # because --apply will sync from gallery, which is the more
        # common operator intent. Surface both lists in detail so the
        # operator can see what installed-only would be removed.
        detail = (
            f"gallery has {len(only_in_gallery)} new action(s): "
            f"{', '.join(only_in_gallery)}; installed has "
            f"{len(only_in_installed)} extra action(s) not in gallery: "
            f"{', '.join(only_in_installed)}"
        )
        return CLASS_MISSING_IN_INSTALLED, detail, only_in_gallery + only_in_installed

    return CLASS_OK, "scheduled_actions[] match gallery", []


# ── Manifest iteration ──────────────────────────────────────────────────────


def _iter_bot_manifests(bot_id: str, bot_info: dict) -> list:
    """Best-effort iteration over a bot's installed manifests.

    Yields ApplicationManifest objects. Returns ``[]`` (silently) if the
    bot's manifests/ directory doesn't exist — a freshly-added bot that
    hasn't installed anything yet is not an error.
    """
    from .manifest import list_manifests
    from .. import config as config_mod

    user = bot_info.get("user", bot_id)
    workspace = config_mod.get_bot_workspace(bot_id, user=user)
    if workspace is None:
        workspace = Path(f"/Users/{user}/.openclaw/workspace")
    if not (workspace / "manifests").is_dir():
        return []

    # list_manifests needs shared_dir for the loader; pass the bot's
    # workspace under its manifests/ dir directly. Looking at the
    # implementation: it iterates workspace/manifests/*.json.
    # We can either call list_manifests with a shared_dir (it doesn't
    # use it for the bot-local manifests path) or iterate manually.
    try:
        manifests = list_manifests(Path("/Users/Shared/evolve"), bot_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "reconcile_actions: failed to list manifests for %s: %s",
            bot_id, exc,
        )
        return []
    return manifests


# ── Top-level entry ─────────────────────────────────────────────────────────


def reconcile_actions(
    shared_dir: Path,
    *,
    bot_filter: str | None = None,
    app_filter: str | None = None,
    apply: bool = False,
    network: dict | None = None,
) -> ReconcileResult:
    """Walk the pod, classify drift, optionally apply fixes.

    Args:
        shared_dir: pod-wide shared dir, passed through to apply_actions
            and the gallery loader.
        bot_filter: when set, only reconcile this bot. Useful for
            targeted reruns.
        app_filter: when set, only reconcile this app_id (across all
            bots, modulated by bot_filter). Useful when chasing one
            known migration.
        apply: when True, call ``apply_actions(--from-gallery)`` against
            each remediable drift entry (SHAPE_DRIFT and
            MISSING_IN_INSTALLED). Stamp preservation in apply_actions
            keeps the audit trail continuous.
        network: pre-loaded network dict; loaded from disk if None.

    Returns:
        ReconcileResult with one AppDriftReport per (bot, app) examined.
    """
    from ..config import load_network as _load_network
    from .apply_actions import apply_actions as _apply, ApplyActionsError
    from .gallery import load_gallery_package

    net = network if network is not None else _load_network()
    bots = net.get("bots") or {}

    result = ReconcileResult(applied=apply)

    bot_ids = sorted(bots.keys())
    if bot_filter:
        bot_ids = [b for b in bot_ids if b == bot_filter]
        if not bot_ids:
            # Filter matched nothing — surface a single ERROR entry so
            # the CLI can distinguish "no drift" from "filter typo'd
            # the bot name".
            result.reports.append(AppDriftReport(
                bot_id=bot_filter, app_id="?", pkg_id="",
                classification=CLASS_ERROR,
                detail=(
                    f"--bot {bot_filter!r} matched no registered bot "
                    f"(known: {', '.join(sorted(bots.keys())) or '(none)'})"
                ),
            ))
            return result

    for bot_id in bot_ids:
        bot_info = bots[bot_id]
        for manifest in _iter_bot_manifests(bot_id, bot_info):
            if app_filter and manifest.id != app_filter:
                continue

            pkg_id = manifest.pkg_id or ""
            installed_actions = manifest.scheduled_actions or []

            # No pkg_id → can't look up a gallery source. This is the
            # common case for custom-built apps; report and move on.
            if not pkg_id:
                result.reports.append(AppDriftReport(
                    bot_id=bot_id, app_id=manifest.id, pkg_id="",
                    classification=CLASS_SKIPPED_NO_PKG_ID,
                    detail="manifest has no pkg_id (likely a forge-only custom app)",
                    installed_action_ids=[
                        a.get("id", "") for a in installed_actions
                        if isinstance(a, dict)
                    ],
                ))
                continue

            try:
                pkg = load_gallery_package(pkg_id, shared_dir)
            except Exception as exc:  # noqa: BLE001
                result.reports.append(AppDriftReport(
                    bot_id=bot_id, app_id=manifest.id, pkg_id=pkg_id,
                    classification=CLASS_ERROR,
                    detail=f"gallery load raised: {type(exc).__name__}: {exc}",
                ))
                continue

            if pkg is None:
                # Side-loaded apps (Atlas pre-gallery-move) land here.
                # We can't auto-reconcile because there's no source of
                # truth in the gallery to sync from.
                result.reports.append(AppDriftReport(
                    bot_id=bot_id, app_id=manifest.id, pkg_id=pkg_id,
                    classification=CLASS_SKIPPED_SIDE_LOADED,
                    detail=(
                        f"pkg_id {pkg_id!r} not found in gallery — likely "
                        f"side-loaded; reconcile by editing the on-disk "
                        f"manifest or moving the package into gallery/"
                    ),
                    installed_action_ids=[
                        a.get("id", "") for a in installed_actions
                        if isinstance(a, dict)
                    ],
                ))
                continue

            gallery_actions = pkg.get("scheduled_actions") or []
            classification, detail, drifted_ids = _classify(
                installed_actions, gallery_actions,
            )

            report = AppDriftReport(
                bot_id=bot_id, app_id=manifest.id, pkg_id=pkg_id,
                classification=classification, detail=detail,
                installed_action_ids=sorted(
                    a.get("id", "") for a in installed_actions
                    if isinstance(a, dict)
                ),
                gallery_action_ids=sorted(
                    a.get("id", "") for a in gallery_actions
                    if isinstance(a, dict)
                ),
                drifted_action_ids=drifted_ids,
            )

            # Run --apply against remediable drifts. Per-app best-effort:
            # one failure doesn't stop the rest.
            if apply and classification in _REMEDIABLE:
                try:
                    apply_result = _apply(
                        bot_id, manifest.id, shared_dir,
                        from_gallery=True, network=net,
                    )
                    report.applied = True
                    report.apply_summary = apply_result.to_dict()
                except ApplyActionsError as exc:
                    report.applied = True
                    report.apply_error = str(exc)
                except Exception as exc:  # noqa: BLE001
                    report.applied = True
                    report.apply_error = f"{type(exc).__name__}: {exc}"

            result.reports.append(report)

    return result
