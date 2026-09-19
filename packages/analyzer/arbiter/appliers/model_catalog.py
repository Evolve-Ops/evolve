"""arbiter.appliers.model_catalog — apply ReconcileModelCatalog actions.

The applier:
  1. Reads the bot's current catalog via `oc_full_config_get`
  2. Appends the requested missing models (deduped, preserves order)
  3. Writes back via `oc_full_config_set`

Never replaces existing catalog entries — operators' manual additions
are preserved. This is the "auto-fill the gaps" applier, not a
"rewrite the catalog" applier.

Spec / context: internal/spec-model-catalog-reconcile-2026-05-28 (PR #1703
established the helpers + UI; this generator+applier closes the RSI
loop so drift gets caught proactively by a scheduled monitor rather
than only on Check-Now or tier-write).
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from arbiter.appliers.base import ApplyResult, RevertResult, register_applier
from schema.proposal import ReconcileModelCatalog


# Sentinel — when no models to add, we still emit ApplyResult(ok=True)
# but mark it idempotent so the proposal record reflects "no changes
# needed" rather than "applied 0 models" (which reads as broken).


def _config_fns():
    """Resolve the runtime seam's full-config read/write — kept lazy to hold
    the analyzer-only import surface tight."""
    from runtime.agent_runtime import get_runtime  # type: ignore
    rt = get_runtime()
    return rt.full_config_get, rt.full_config_set


def _normalize_catalog_entries(catalog: list) -> list[dict]:
    """Normalize a mixed-shape catalog (strings and/or dicts) to a list
    of dict entries with at least `id` set.

    Mirrors evolve_admin.model_catalog._model_in_catalog tolerance
    but lives here so the applier doesn't import admin code.
    """
    normalized: list[dict] = []
    for entry in catalog or []:
        if isinstance(entry, str):
            normalized.append({"id": entry})
        elif isinstance(entry, dict):
            mid = entry.get("id") or entry.get("model")
            if not mid:
                continue
            new_entry = dict(entry)
            new_entry["id"] = mid
            normalized.append(new_entry)
    return normalized


def _catalog_has(catalog: list[dict], model_id: str) -> bool:
    for e in catalog:
        if e.get("id") == model_id:
            return True
    return False


def _provider_of(model_id: str) -> str | None:
    if not model_id or "/" not in model_id:
        return None
    return model_id.split("/", 1)[0].lower()


class ReconcileModelCatalogApplier:
    """Apply / revert ReconcileModelCatalog proposals.

    Apply: reads current catalog, appends missing models, writes back.
    Revert: writes the snapshot catalog (the pre-apply state).
    """

    def capture_snapshot(self, action: ReconcileModelCatalog, bot_id: str) -> dict:
        """Snapshot the current catalog so we can revert later."""
        try:
            oc_full_config_get, _ = _config_fns()
        except Exception as exc:
            return {"error": f"runtime unavailable: {exc}"}
        cfg = oc_full_config_get(action.bot_id) or {}
        return {
            "bot_id": action.bot_id,
            "catalog_before": cfg.get("catalog") or [],
        }

    def apply(self, action: ReconcileModelCatalog, bot_id: str) -> ApplyResult:
        if not action.add_models:
            return ApplyResult(
                ok=True,
                details={"added": [], "reason": "no models to add"},
                message="Nothing to reconcile (no missing models).",
            )

        try:
            oc_full_config_get, oc_full_config_set = _config_fns()
        except Exception as exc:
            return ApplyResult(
                ok=False, details={"error": str(exc)},
                message=f"runtime unavailable: {exc}",
            )

        cfg = oc_full_config_get(action.bot_id)
        if cfg is None:
            return ApplyResult(
                ok=False, details={},
                message=f"Could not read openclaw.json for {action.bot_id}",
            )

        current_catalog = _normalize_catalog_entries(cfg.get("catalog") or [])
        added: list[str] = []
        for model_id in action.add_models:
            if not isinstance(model_id, str) or not model_id:
                continue
            if _catalog_has(current_catalog, model_id):
                continue
            entry: dict[str, Any] = {"id": model_id}
            prov = _provider_of(model_id)
            if prov:
                entry["provider"] = prov
            current_catalog.append(entry)
            added.append(model_id)

        if not added:
            return ApplyResult(
                ok=True,
                details={"added": [], "reason": "all requested models already in catalog"},
                message="Catalog already in sync — no changes needed.",
            )

        write_result = oc_full_config_set(action.bot_id, {"catalog": current_catalog})
        if write_result is None:
            return ApplyResult(
                ok=False, details={"added": added},
                message="oc_full_config_set returned None — check server logs",
            )

        return ApplyResult(
            ok=True,
            details={
                "added": added,
                "catalog_count": len(current_catalog),
            },
            message=(
                f"Added {len(added)} model{'s' if len(added) != 1 else ''} "
                f"to {action.bot_id}'s catalog: {', '.join(added)}. "
                f"Gateway restart required."
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        catalog_before = snapshot.get("catalog_before")
        snap_bot = snapshot.get("bot_id") or bot_id
        if catalog_before is None:
            return RevertResult(
                ok=False, details=snapshot,
                message="Snapshot missing catalog_before — cannot revert",
            )

        try:
            _, oc_full_config_set = _config_fns()
        except Exception as exc:
            return RevertResult(
                ok=False, details={"error": str(exc)},
                message=f"runtime unavailable: {exc}",
            )

        write_result = oc_full_config_set(snap_bot, {"catalog": catalog_before})
        if write_result is None:
            return RevertResult(
                ok=False, details=snapshot,
                message="oc_full_config_set returned None during revert",
            )
        return RevertResult(
            ok=True,
            details={"catalog_count": len(catalog_before)},
            message=f"Reverted {snap_bot}'s catalog to pre-apply state ({len(catalog_before)} entries).",
        )


register_applier("ReconcileModelCatalog", ReconcileModelCatalogApplier())
