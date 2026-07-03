"""arbiter.appliers.content_scan — mutate the content-scan pattern catalog.

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md §3.5.

Single applier — UpdateContentScanCatalog — with three operations:
``add_pattern`` / ``remove_pattern`` / ``set_evolve_markers_allowlist``.

The catalog file at ``{shared_dir}/policy/content-scan-patterns.json``
is owned by the evolve user (under the shared dir's ACL) so writes are
direct — no /tmp staging + sudo dance. No bot-side files touched, no
gateway restart needed — this is the observation surface only.
"""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR

from arbiter.appliers.base import ApplyResult, RevertResult, register_applier
from schema.proposal import UpdateContentScanCatalog


def _shared_dir() -> Path:
    candidates = [os.environ.get("EVOLVE_SHARED_DIR"), str(CANONICAL_SHARED_DIR)]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return CANONICAL_SHARED_DIR


def _load_catalog():
    from content_scan import catalog as _cat
    return _cat.load(_shared_dir())


def _write_catalog(cat) -> None:
    from content_scan import catalog as _cat
    _cat.write(cat, _shared_dir())


class UpdateContentScanCatalogApplier:
    """Mutate {shared_dir}/policy/content-scan-patterns.json."""

    def capture_snapshot(self, action: UpdateContentScanCatalog, bot_id: str) -> dict:
        cat = _load_catalog()
        return {
            "action_kind": "UpdateContentScanCatalog",
            "operation": action.operation,
            "fields": deepcopy(action.fields),
            "prior_catalog": cat.to_dict(),
            "shared_dir": str(_shared_dir()),
        }

    def apply(self, action: UpdateContentScanCatalog, bot_id: str) -> ApplyResult:
        try:
            from content_scan import catalog as _cat
        except ImportError as exc:
            return ApplyResult(
                ok=False, details={"error": str(exc)},
                message=f"content_scan.catalog not importable: {exc}",
            )

        cat = _load_catalog()

        if action.operation == "add_pattern":
            pattern_raw = action.fields.get("pattern") or {}
            if not isinstance(pattern_raw, dict):
                return ApplyResult(
                    ok=False, details={"error": "pattern must be a dict"},
                    message="add_pattern.fields.pattern must be a Pattern dict",
                )
            pid = (pattern_raw.get("id") or "").strip()
            kind = (pattern_raw.get("kind") or "").strip()
            if not pid or not kind:
                return ApplyResult(
                    ok=False, details={"error": "missing_id_or_kind"},
                    message="Pattern dict must have non-empty id and kind",
                )
            if kind not in ("regex", "html_comment", "line_length", "structural"):
                return ApplyResult(
                    ok=False, details={"error": f"unknown_kind: {kind!r}"},
                    message=f"Pattern kind {kind!r} not supported",
                )
            # Replace any existing pattern with same id
            cat.deny_patterns = [p for p in cat.deny_patterns if p.id != pid]
            cat.deny_patterns.append(_cat.Pattern(
                id=pid,
                kind=kind,
                description=str(pattern_raw.get("description") or ""),
                severity=str(pattern_raw.get("severity") or "warn"),
                pattern=str(pattern_raw.get("pattern") or ""),
                threshold=int(pattern_raw.get("threshold") or 0),
                min_size_bytes=int(pattern_raw.get("min_size_bytes") or 0),
                applies_to=list(pattern_raw.get("applies_to") or []),
                flags=list(pattern_raw.get("flags") or []),
            ))

        elif action.operation == "remove_pattern":
            pid = str(action.fields.get("pattern_id") or "").strip()
            if not pid:
                return ApplyResult(
                    ok=False, details={"error": "missing_pattern_id"},
                    message="remove_pattern.fields.pattern_id required",
                )
            before = len(cat.deny_patterns)
            cat.deny_patterns = [p for p in cat.deny_patterns if p.id != pid]
            if len(cat.deny_patterns) == before:
                return ApplyResult(
                    ok=True, details={"no_op": True, "pattern_id": pid},
                    message=f"Pattern {pid!r} not in catalog; no change.",
                )

        elif action.operation == "set_evolve_markers_allowlist":
            markers = action.fields.get("markers")
            if not isinstance(markers, list):
                return ApplyResult(
                    ok=False, details={"error": "markers must be a list"},
                    message="set_evolve_markers_allowlist.fields.markers must be a list",
                )
            cat.evolve_markers_allowlist = [str(m) for m in markers]

        else:
            return ApplyResult(
                ok=False, details={"unknown_operation": action.operation},
                message=f"Unknown UpdateContentScanCatalog operation: {action.operation!r}",
            )

        _write_catalog(cat)
        return ApplyResult(
            ok=True,
            details={
                "operation": action.operation,
                "pattern_count": len(cat.deny_patterns),
                "allowlist_count": len(cat.evolve_markers_allowlist),
            },
            message=f"Updated content-scan catalog ({action.operation}).",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        prior = snapshot.get("prior_catalog")
        if not isinstance(prior, dict):
            return RevertResult(ok=False, message="No prior catalog captured; cannot revert.")
        try:
            from content_scan.catalog import Catalog, Pattern, Scope, write
        except ImportError as exc:
            return RevertResult(ok=False, message=f"content_scan.catalog not importable: {exc}")

        scope_raw = prior.get("scope") or {}
        restored = Catalog(
            version=int(prior.get("version") or 1),
            deny_patterns=[
                Pattern(
                    id=str(p.get("id") or ""),
                    kind=str(p.get("kind") or ""),
                    description=str(p.get("description") or ""),
                    severity=str(p.get("severity") or "warn"),
                    pattern=str(p.get("pattern") or ""),
                    threshold=int(p.get("threshold") or 0),
                    min_size_bytes=int(p.get("min_size_bytes") or 0),
                    applies_to=list(p.get("applies_to") or []),
                    flags=list(p.get("flags") or []),
                )
                for p in (prior.get("deny_patterns") or [])
                if isinstance(p, dict) and p.get("id") and p.get("kind")
            ],
            evolve_markers_allowlist=list(prior.get("evolve_markers_allowlist") or []),
            scope=Scope(
                scanned_files_per_bot=list(scope_raw.get("scanned_files_per_bot") or []),
                scanned_pod_files=list(scope_raw.get("scanned_pod_files") or []),
            ),
            bootstrapped_at=str(prior.get("bootstrapped_at") or ""),
        )
        write(restored, _shared_dir())
        return RevertResult(
            ok=True, details={"operation": snapshot.get("operation")},
            message="Reverted content-scan catalog to prior snapshot.",
        )


register_applier("UpdateContentScanCatalog", UpdateContentScanCatalogApplier())
