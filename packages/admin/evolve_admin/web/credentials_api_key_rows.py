"""Row builder for single-value credentials (``api_key`` / ``identifier``).

Lifted verbatim out of ``routes_admin.api_admin_get_keys``'s registry loop
(the frozen hot file is under a no-growth line cap, 4.1a) and then extended
with the ONE thing it was missing: OC's per-agent runtime store.

The winner cascade for a single-value row, highest first:

1. ``WizardAuthProfilesProbe`` — the canonical Evolve-managed
   ``auth-profiles.json`` entry. Owns the row's ``profile_id`` and its
   ``order`` metadata when present.
2. ``OcAgentCatalogKeyProbe`` — OC's per-agent runtime store (storage shape
   S8). This is WHERE THE RUNTIME READS, so it owns the row's ``storage``
   and therefore where Rotate writes, even when (1) also matched. Decision
   B: an affordance may not break a working integration, and writing only
   to auth-profiles while the gateway keeps reading ``catalog.json`` is
   exactly the silent no-op this whole change exists to remove.
3. The inline brave special-case (openclaw.json, canonical + legacy paths).

When both (1) and (2) match, the row is ACTIVE, keeps the auth-profiles
``profile_id``, and carries ``oc_drift = True`` if the two stores disagree —
the same signal brave already surfaces, and the one that tells an operator
their last rotation only landed on half the box.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .credentials_oc import brave_key_from_oc_config
from .oc_agent_keys import (
    RUNTIME_STORAGE_ID,
    agents_for_provider,
    storage_locations_for_provider,
)


def build_api_key_row(
    *,
    base: dict,
    provider: str,
    key_type: str,
    oc_cfg: dict,
    wizard_match: Any | None,
    runtime_match: Any | None,
    bot_id: str,
    network_path: Path,
    canonical_profile_id: Callable[[str, str], str],
    mask_key: Callable[[str], str],
    actions_for_winner: Callable[..., list],
) -> dict:
    """Build one single-value credential row. Never raises."""
    # Brave special-case: detect intentional opt-out via
    # tools.web.search.provider != null/"brave" (per v3 design), AND surface
    # keys that live in openclaw.json but not in auth-profiles (drift from
    # CLI / hand-edits). Kept inline because both signals come from
    # openclaw.json.
    opted_out_reason = None
    oc_brave_key = None
    if provider == "brave":
        # Honour canonical + legacy openclaw.json key locations (see
        # brave_key_from_oc_config) so a key set by either path reads ACTIVE,
        # not a mismatched "Setup required".
        oc_brave_key = brave_key_from_oc_config(oc_cfg)
        current_provider = (
            oc_cfg.get("tools", {}).get("web", {}).get("search", {}) or {}
        ).get("provider")
        if current_provider not in (None, "", "brave"):
            opted_out_reason = f"tools.web.search.provider = '{current_provider}'"

    if wizard_match is not None:
        ext = wizard_match.extras
        row = {
            **base,
            "profile_id": ext["profile_id"],
            "masked": ext["masked"],
            "status": "active",
            "order": ext["order"],
            "order_total": ext["order_total"],
            "has_prev": ext["has_prev"],
        }
        if provider == "brave" and oc_brave_key and oc_brave_key != ext["value"]:
            row["oc_drift"] = True
        if runtime_match is not None:
            _apply_runtime_evidence(
                row, runtime_match,
                bot_id=bot_id, provider=provider, network_path=network_path,
                actions_for_winner=actions_for_winner,
            )
            if runtime_match.extras.get("value") != ext["value"]:
                # auth-profiles and the runtime store hold different keys.
                # The runtime one is what bills, so the row shows THAT masked
                # value and rotation targets it — but the operator needs to
                # know the two disagree.
                row["oc_drift"] = True
                row["masked"] = runtime_match.extras["masked"]
    elif runtime_match is not None:
        row = {
            **base,
            "profile_id": canonical_profile_id(provider, key_type),
            "masked": runtime_match.extras["masked"],
            "status": "active",
            "oc_only": True,
            "has_prev": False,
        }
        _apply_runtime_evidence(
            row, runtime_match,
            bot_id=bot_id, provider=provider, network_path=network_path,
            actions_for_winner=actions_for_winner,
        )
    elif provider == "brave" and oc_brave_key:
        # auth-profiles missing but openclaw.json carries the key — bot was
        # configured outside the wizard. Treat as active so the onboarding
        # banner / per-bot Set-up button skip it.
        row = {
            **base,
            "profile_id": canonical_profile_id(provider, key_type),
            "masked": mask_key(oc_brave_key),
            "status": "active",
            "oc_only": True,
            "has_prev": False,
        }
    else:
        row = {
            **base,
            "profile_id": canonical_profile_id(provider, key_type),
            "masked": None,
            "status": "missing",
            "has_prev": False,
        }

    if opted_out_reason:
        row["status"] = "opted_out"
        row["opted_out_reason"] = opted_out_reason
    return row


def runtime_only_rows(
    scan: Callable[..., Any],
    bot_id: str,
    seen_providers: set,
) -> list[dict]:
    """Rows for providers the runtime store carries that the registry doesn't.

    The scan lists ``agents/<a>/agent/plugins/`` rather than guessing eleven
    provider ids, so it finds a catalog for a vendor Evolve has never heard
    of. Rows, though, are minted from ``_KEY_REGISTRY`` and probes are built
    for ``KNOWN_LLM_PROVIDERS`` — so before this, such a key was located and
    then dropped on the floor. That contradicts the operator's rule (stated
    2026-09-04): **if it has a key loaded, it is viewable, used or not.**

    The row is deliberately NOT rotatable (``rotatable = False``). Rotation
    here means writing the key the gateway is live on and then proving the
    write with a provider auth call; ``llm_key_verify`` has no call for a
    vendor it does not know, so a rotation would be an unverifiable write to
    a working integration — the exact thing decision B forbids. Wire the
    provider's verify call (and a registry entry) and it becomes rotatable
    through the ordinary path.

    Providers already emitted are skipped via *seen_providers*, which this
    adds to — the same convention as the profile-discovery loop above.
    It runs LAST among the keys API's row producers for that reason: by then
    the named sections (github, discord, whatsapp) have claimed their ids, so
    a runtime-store directory that happens to share one of those names cannot
    shadow the row that section owns.

    *scan* is passed as a callable, not a list, so a scan that raises — a pod
    whose sudoers grants have not been refreshed, say — costs no rows rather
    than 500ing the Credentials page.
    """
    try:
        located = list(scan(bot_id) or [])
    except Exception:  # noqa: BLE001 — discovery never 500s the keys page
        return []
    rows: list[dict] = []
    for hit in located:
        provider = hit.provider
        if provider in seen_providers:
            continue
        seen_providers.add(provider)
        mine = [h for h in located if h.provider == provider]
        mtimes = [h.mtime for h in mine if h.mtime is not None]
        rows.append({
            "provider": provider,
            "display": provider.replace("_", " ").title(),
            "type": "api_key",
            "type_label": "API key",
            "credential_class": "api_key",
            # The store it was found in is an LLM key store, so the row
            # belongs beside the other LLM providers, not under "custom".
            "category": "llm",
            "profile_id": f"{provider}:api_key",
            "masked": hit.masked,
            "status": "active",
            "order": None,
            "order_total": None,
            "has_prev": False,
            "plugin_enabled": False,
            "oc_only": True,
            "rotatable": False,
            "storage": RUNTIME_STORAGE_ID,
            "flavor": "oc_agent_store",
            "auth_model": "api_key",
            "storage_locations": storage_locations_for_provider(located, provider),
            "agents": agents_for_provider(located, provider),
            "runtime_stores": sorted({h.store for h in mine}),
            "runtime_location_count": len(mine),
            "runtime_mtime": max(mtimes) if mtimes else None,
        })
    return rows


def _apply_runtime_evidence(
    row: dict,
    runtime_match: Any,
    *,
    bot_id: str,
    provider: str,
    network_path: Path,
    actions_for_winner: Callable[..., list],
) -> None:
    """Stamp the S8 storage facts onto *row*, in place.

    ``agents`` is what makes the mirroring legible: "main · email-reader"
    chips tell the operator that ONE key is duplicated across N agents and
    that a rotation covered all of them. Without it, "rotate" on a two-agent
    bot looks like it touched one file.
    """
    ext = runtime_match.extras
    row["storage"] = RUNTIME_STORAGE_ID
    row["flavor"] = "oc_agent_store"
    row["auth_model"] = "api_key"
    row["storage_locations"] = list(runtime_match.storage_locations)
    row["agents"] = list(ext.get("agents") or [])
    row["runtime_stores"] = list(ext.get("stores") or [])
    row["runtime_location_count"] = ext.get("location_count", 0)
    row["runtime_mtime"] = ext.get("latest_mtime")
    row["actions"] = actions_for_winner(
        runtime_match, bot_id=bot_id, provider=provider,
    )
    prev_at, verify = _last_rotation(bot_id, provider, network_path)
    if prev_at:
        row["has_prev"] = True
        row["prev_rotated_at"] = prev_at
    if verify:
        # Tri-state on the row, matching the verify contract: accepted /
        # rejected / unproven. "Unproven" is never rendered as a failure —
        # a provider we could not reach has not disproved anything.
        if verify.get("ok"):
            row["last_verified"] = verify.get("at") or ""
        elif not verify.get("skipped"):
            row["last_verify_error"] = verify.get("detail") or ""


def _last_rotation(
    bot_id: str, provider: str, network_path: Path,
) -> tuple[str | None, dict | None]:
    """``(when the key was last rotated here, the verdict that rotation got)``.

    Import is local: ``oc_agent_keys_io`` reaches into ``server`` lazily and
    this module is imported from the keys API at request time, so keeping the
    dependency inside the call keeps the row builder importable on its own
    (the unit tests build rows with no server module loaded at all).
    """
    try:
        from .oc_agent_keys_io import last_verification, previous_key_rotated_at
        return (
            previous_key_rotated_at(bot_id, provider, network_path=network_path),
            last_verification(bot_id, provider, network_path=network_path),
        )
    except Exception:  # noqa: BLE001 — an undo hint never fails the page
        return None, None


__all__ = ["build_api_key_row", "runtime_only_rows"]
