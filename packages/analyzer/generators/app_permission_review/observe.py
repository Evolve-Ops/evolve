"""generators.app_permission_review.observe — entry point.

Loads every active manifest on the bot, runs the first-pass static
checks (review.py), runs the pod-aware consolidation pass (consolidation.py),
and emits one Investigation proposal per consolidated finding.

Two invocation paths land here:

1. **Scheduled** — generator_runner runs us on cadence (weekly).
2. **On-demand** — scan_workspace_pipeline calls run_one_generator
   after manifest generation, so operator-initiated scans surface
   findings immediately.

Same code path for both — the scan invocation just bypasses the
cadence-due check.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generators.app_permission_review.consolidation import consolidate
from generators.app_permission_review.proposals import (
    build_proposal,
    dismiss_signature_for_finding,
)
from generators.app_permission_review.review import find_per_app_candidates
from schema.proposal import Proposal


GENERATOR_ID = "app_permission_review"
DIMENSION = "safety"


@dataclass
class AppPermissionReviewContext:
    """Per-bot run context.

    One bot per ``observe()`` call. The generator loads every manifest
    on the bot in one go — the pod-aware second pass needs the full
    set in memory, so per-app invocation doesn't make sense.

    ``home_override`` is a test hook (same pattern as B.1 + the
    bootstrapper).
    """

    bot_id: str
    shared_dir: Path
    home_override: Path | None = None
    # Phase A.5 — universal dismiss suppression. Skip emissions whose
    # per-finding signature is actively dismissed for this bot (or
    # pod-wide). Granularity is per (kind, app_id, entry_kind,
    # entry_value).
    consult_dismissals: bool = True


def observe(ctx: AppPermissionReviewContext) -> list[Proposal]:
    """Run the two-pass review on this bot and return Investigation proposals."""
    # Lazy imports — admin-side helpers needed for path resolution +
    # reconciler helpers for manifest discovery.
    try:
        from evolve_admin.config import bot_home, load_network
        from evolve_admin.app_permissions.reconciler import (
            _iter_manifest_files,
            _read_manifest_raw,
        )
    except ImportError:
        return []

    home = ctx.home_override
    if home is None:
        try:
            network = load_network()
            home = bot_home(ctx.bot_id, network)
        except Exception:
            return []

    workspace = home / ".openclaw" / "workspace"
    manifests_dir = workspace / "manifests"
    if not manifests_dir.exists():
        return []

    # Load every active manifest. Hidden / deprecated apps are skipped
    # (consistent with B.1 + bootstrapper).
    manifests: list[dict] = []
    for mpath in _iter_manifest_files(manifests_dir):
        try:
            raw, err = _read_manifest_raw(mpath)
        except Exception:
            continue
        if raw is None:
            continue
        status = (raw.get("status") or "").lower()
        if status in ("hidden", "deprecated"):
            continue
        manifests.append(raw)

    if not manifests:
        return []

    # First pass — per-app candidate findings.
    all_candidates = []
    for m in manifests:
        try:
            all_candidates.extend(find_per_app_candidates(m, workspace, ctx.bot_id))
        except Exception:
            # One bad manifest doesn't break the rest.
            continue

    if not all_candidates:
        return []

    # Second pass — pod-aware consolidation.
    consolidated = consolidate(all_candidates, manifests, workspace)

    # Preload the active dismiss signatures for this bot — one read
    # per observe() call, then per-finding membership lookups are
    # O(1). Fail-open on any read failure.
    suppressed_signatures = _load_suppressed_signatures(
        ctx.shared_dir, ctx.bot_id, consult=ctx.consult_dismissals,
    )

    # Build proposals.
    proposals: list[Proposal] = []
    for cf in consolidated:
        # Phase A.5 dismiss-suppression gate. Per-finding signature
        # (kind:app:entry_kind:entry_value), so dismissing one
        # specific path/host/command does NOT suppress findings on
        # other paths/hosts/commands for the same app + kind.
        sig = dismiss_signature_for_finding(
            kind=cf.finding.kind,
            app_id=cf.finding.app_id,
            entry_kind=cf.finding.entry_kind,
            entry_value=cf.finding.entry_value,
        )
        if sig in suppressed_signatures:
            continue
        try:
            proposals.append(build_proposal(cf))
        except Exception:
            continue
    return proposals


def _load_suppressed_signatures(
    shared_dir: Path, bot_id: str, *, consult: bool,
) -> set[str]:
    """Thin wrapper around :func:`arbiter.dismissals.preload_suppressed_signatures`
    that honors the local ``consult`` test hook."""
    if not consult:
        return set()
    from arbiter.dismissals import preload_suppressed_signatures
    return preload_suppressed_signatures(shared_dir, bot_id)
