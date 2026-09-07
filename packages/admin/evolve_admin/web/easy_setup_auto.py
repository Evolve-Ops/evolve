"""Easy-setup + top-level auto-upgrade helpers (spec-model-auto-upgrade-2026-07-30).

Helpers for the ``/api/models/easy-setup`` and ``/api/models/auto-upgrade``
routes, kept out of ``routes_admin_config.py`` (no-growth-capped file):

- :func:`preview_extras` — the preview payload's auto-upgrade display prefill
  (the scope's CURRENT resolved policy), the qualified-id → family-stem map
  (version-less "latest <family>" chips when auto mode is on), and the
  pod-scope governance split (spec §migration-era hazard: the pod toggle must
  state which bots it actually reaches — Custom bots keep their own setting).
- :func:`scope_auto_upgrade_state` — the GET payload for the TOP-LEVEL toggle
  on the tier-definition cards (spec §Scope: the pod-defaults card and each
  Custom bot's tier card own the toggle; the easy-setup modal only displays
  the current posture). Carries the same governance split plus a family map
  wide enough for the tier editors' consolidated "family · latest" rows.
- :func:`pod_auto_upgrade_block` — the ``models.autoUpgrade`` block for a pod
  apply, merging ``enabled`` into the existing block so hand-set subordinate
  knobs (applyDay, requireGA, …) survive an enabled-only flip.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


def preview_extras(net: dict[str, Any], scope: str, catalog: dict[str, Any]) -> dict[str, Any]:
    """Auto-upgrade extras for the easy-setup preview response.

    Best-effort: a policy/family failure must never break the tier preview the
    operator is already relying on — degraded keys come back empty/None.
    """
    auto_upgrade: dict | None = None
    families: dict[str, str] = {}
    governed: list[str] = []
    excluded: list[str] = []
    try:
        import model_auto_upgrade as _mau
        from model_discovery import _bare_id, _family_of
        from primary_bot import bot_has_custom_tiers, read_bot_tiers_doc
        pod_pol = _mau.pod_policy(net)
        if scope == "pod":
            auto_upgrade = pod_pol.to_dict()
            for b in sorted((net.get("bots") or {}).keys()):
                (excluded if bot_has_custom_tiers(net, b) else governed).append(b)
        else:
            auto_upgrade = _mau.bot_policy(
                pod_pol, read_bot_tiers_doc(net, scope),
                custom=bot_has_custom_tiers(net, scope),
            ).to_dict()
        for rung in (catalog.get("rungs") or []):
            for m in (rung.get("models") or []):
                if isinstance(m, str) and m and m not in families:
                    families[m] = _family_of(_bare_id(m))
    except Exception as exc:
        _log.warning("easy-setup preview extras failed (scope=%s): %s", scope, exc)
    return {
        "auto_upgrade": auto_upgrade,
        "families": families,
        "auto_upgrade_governed": governed,
        "auto_upgrade_excluded": excluded,
    }


def _scope_family_map(net: dict[str, Any], scope: str, shared_dir: Path) -> dict[str, str]:
    """Qualified-id → family stem for every model the scope's tier editor can
    show or offer: the listings cache (the picker's candidate pool) plus the
    scope's current merged catalog (which may predate the cache). Best-effort —
    a failure degrades to a partial/empty map, never an error (the editor falls
    back to a bare-id stem client-side, which only costs grouping precision).
    """
    families: dict[str, str] = {}

    def _add(model: Any) -> None:
        if isinstance(model, str) and model and model not in families:
            from model_discovery import _bare_id, _family_of
            families[model] = _family_of(_bare_id(model))

    try:
        from model_discovery import read_listings_cache
        cache = read_listings_cache(shared_dir) or {}
        for models in (cache.get("providers") or {}).values():
            for rec in models or []:
                if isinstance(rec, dict):
                    _add(rec.get("qualified_id") or rec.get("model_id"))
    except Exception as exc:
        _log.warning("auto-upgrade family map: listings sweep failed: %s", exc)
    try:
        from primary_bot import pod_default_catalog_view, read_bot_tiers_doc
        for rung in (pod_default_catalog_view(net).get("rungs") or []):
            for m in (rung.get("models") or []):
                _add(m)
        if scope != "pod":
            for rung in (read_bot_tiers_doc(net, scope).get("rungs") or []):
                if isinstance(rung, dict):
                    for m in (rung.get("models") or []):
                        _add(m)
    except Exception as exc:
        _log.warning("auto-upgrade family map: catalog sweep failed: %s", exc)
    return families


def scope_auto_upgrade_state(
    net: dict[str, Any], scope: str, shared_dir: Path,
) -> dict[str, Any]:
    """The GET payload for the top-level auto-upgrade toggle on a tier card.

    ``auto_upgrade`` is the scope's CURRENT resolved policy (a bot scope
    resolves per-bot → pod → code default, so a Use-defaults bot reports the
    pod posture it follows). ``custom_tiers`` tells the UI whether the bot
    owns its toggle (spec §Scope) — the pod scope is always its own owner.
    Governance lists ride pod scope only (spec §migration-era hazard).
    """
    import model_auto_upgrade as _mau
    from primary_bot import bot_has_custom_tiers, read_bot_tiers_doc

    pod_pol = _mau.pod_policy(net)
    governed: list[str] = []
    excluded: list[str] = []
    custom = False
    if scope == "pod":
        policy = pod_pol
        for b in sorted((net.get("bots") or {}).keys()):
            (excluded if bot_has_custom_tiers(net, b) else governed).append(b)
    else:
        custom = bot_has_custom_tiers(net, scope)
        policy = _mau.bot_policy(
            pod_pol, read_bot_tiers_doc(net, scope), custom=custom,
        )
    return {
        "ok": True,
        "scope": scope,
        "auto_upgrade": policy.to_dict(),
        "custom_tiers": custom,
        "auto_upgrade_governed": governed,
        "auto_upgrade_excluded": excluded,
        "families": _scope_family_map(net, scope, shared_dir),
    }


def pod_auto_upgrade_block(net: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """``models.autoUpgrade`` for a pod apply: merge ``enabled`` into the
    existing block so hand-set subordinate knobs survive the flip."""
    existing = (net.get("models") or {}).get("autoUpgrade")
    block = dict(existing) if isinstance(existing, dict) else {}
    block["enabled"] = enabled
    return block
