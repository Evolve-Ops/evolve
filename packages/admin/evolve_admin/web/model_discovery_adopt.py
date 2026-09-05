"""model_discovery_adopt.py — adopt a discovered model from the AI Optimization
Model Freshness card, without a Proposal.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 12 (2026-06-15).
The ``model_discovery`` generator is signal-only — it no longer authors an
``AdoptModel`` Proposal into the Recommendations queue. Adoption is operator-
driven from the page: the card lists the **gated firing** ``model_discovery``
Signals (recency/modality-filtered at emission, deduped by signature) and this
module turns one-click adopt / ignore into the same back-end edits the proposal
applier used to perform — driving ``arbiter.appliers.adopt_model`` directly from
the model identity, with NO Proposal object in the loop.

Lives outside the route file (the helper-module convention, mirroring
``model_freshness_drift.py``) so the heavily-used ``routes_admin_config.py``
stays thin — the routes are one-liners delegating here.

The Signal is the source of truth for what's adoptable: adopting or ignoring a
model resolves/dismisses its Signal so the count (and the AI Optimization nav
badge) clears immediately, rather than waiting for the next daily discovery run
to notice the model is now in a rung / on the ignore list.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from flask import Flask, Response, jsonify, request

from evolve_config import CANONICAL_SHARED_DIR  # type: ignore

from ..config import load_network

_log = logging.getLogger(__name__)

SIGNAL_PRODUCER = "model_discovery"
SIGNAL_TYPE = "model_discovery"

# Mirror arbiter.appliers.adopt_model._VALID_ROLES + the /act endpoint's
# _ADOPT_VALID_ROLES. "none" = adopt as a dormant catalog entry (no role).
_VALID_ROLES = {"none", "fast", "standard", "power", "max"}
_CAP_ROLES = {"max", "power"}


# ── reads ─────────────────────────────────────────────────────────────────────

def _iter_firing_discoveries(shared_dir: Path):
    """Yield the firing ``model_discovery`` Signals (the real discoveries, not
    the degraded-mode / no-adapter advisories which carry other ``type``s)."""
    from signals import store as signals_store

    for sig in signals_store.iter_active(
        Path(shared_dir), producer=SIGNAL_PRODUCER, state="firing"
    ):
        if getattr(sig, "type", None) == SIGNAL_TYPE:
            yield sig


def _find_firing_discovery(shared_dir: Path, provider: str, model_id: str):
    for sig in _iter_firing_discoveries(shared_dir):
        d = getattr(sig, "details", None) or {}
        if d.get("provider") == provider and d.get("model_id") == model_id:
            return sig
    return None


def _value_line(shared_dir: Path, provider: str, model_id: str) -> str | None:
    """Best-effort decision-grounding value line for the card (the same one the
    retired proposal carried). Fail-open: a value-line miss never blocks the
    model from surfacing — the card just shows no value line."""
    try:
        from generators.model_discovery.value_line import compute_value_line

        vl = compute_value_line(
            provider, model_id, shared_dir=Path(shared_dir),
            provider_display=provider.capitalize() if provider else provider,
        )
        return vl.terse if vl is not None else None
    except Exception:
        return None


# The role picker offers the four general rungs. (The former Judge appendage
# died with the judge role — design-judge-role-collapse-2026-08-21 §5.4.)
_BASE_PICKER_ROLES = ["fast", "standard", "power", "max"]


def picker_roles(network: dict) -> list[str]:
    """The role buttons the adopt card's segmented picker shows: the four
    general rungs (Fast/Standard/Power/Max)."""
    del network  # kept on the signature for the route callers
    return list(_BASE_PICKER_ROLES)


def _all_discovery_rows(shared_dir: Path) -> list[dict]:
    """Every firing ``model_discovery`` Signal as a card row, carrying the
    capability-aware placement verdict (Addendum 13) so the caller can partition
    by ``placement_verdict`` without re-reading the store. Value lines are NOT
    attached here — they're filled only for the rows that actually survive the
    partition (a value-line lookup is best-effort but not free)."""
    out: list[dict] = []
    for sig in _iter_firing_discoveries(shared_dir):
        d = getattr(sig, "details", None) or {}
        provider = d.get("provider")
        model_id = d.get("model_id")
        if not provider or not model_id:
            continue
        qualified = d.get("qualified_id") or f"{provider}/{model_id}"
        out.append({
            "signal_id": getattr(sig, "id", ""),
            "provider": provider,
            "model_id": model_id,
            "qualified_id": qualified,
            # Empty (NOT "new-rung") for an unplaceable / off-ladder discovery —
            # the killed placeholder is never reconstructed; the applier rejects
            # an empty slug.
            "suggested_rung_slug": d.get("suggested_rung_slug") or "",
            "suggested_cost_class": d.get("suggested_cost_class") or "medium",
            "suggested_position": d.get("suggested_position", 0),
            "cost_band_source": d.get("cost_band_source") or "heuristic",
            # Placement verdict (Addendum 13). A missing verdict (older Signal)
            # is treated as ``cannot_place`` — suppressed from the card rather
            # than mis-surfaced as adoptable.
            "placement_verdict": d.get("placement_verdict") or "cannot_place",
            "recommended_role": d.get("recommended_role"),
            "recommended_rung_slug": d.get("recommended_rung_slug"),
            "fit_reason": d.get("fit_reason") or "",
            "fit_confidence": d.get("fit_confidence") or 0.0,
            "evidence": d.get("evidence") or {},
        })
    out.sort(key=lambda r: r["qualified_id"])
    return out


def list_adoptable_discoveries(shared_dir: Path) -> list[dict]:
    """The per-rung adopt rows the card renders, sourced from the gated firing
    ``model_discovery`` Signals.

    Only ``fits_existing`` findings are surfaced — a model that maps to no
    existing role is unroutable, so adopting it is busywork (Addendum 13). Among
    the survivors, collapse to the single BEST model per (provider, role) via the
    provider-neutral generation ranking, so a provider listing several variants
    of a rung the operator already fills shows one row, not many. ``new_tier``
    findings are returned separately by :func:`list_new_tier_discoveries`;
    ``mode_variant`` / ``specialist`` / ``cannot_place`` stay signal-only."""
    rows = [
        r for r in _all_discovery_rows(shared_dir)
        if r["placement_verdict"] == "fits_existing"
    ]
    try:
        import model_discovery as _md

        rows = _md.select_best_per_rung(rows)
    except Exception as exc:  # pragma: no cover — fail-open to the unfiltered list
        _log.debug("model_discovery_adopt: best-per-rung selection failed: %s", exc)
    for r in rows:
        # ``role`` is the routing role the card pre-selects + the applier maps;
        # a fits_existing finding always carries one.
        r["role"] = r.get("recommended_role") or ""
        r["value_line"] = _value_line(shared_dir, r["provider"], r["model_id"])
    return rows


# ── Version freshness — the PRIMARY surface (spec §Addendum 15) ────────────────
# The everyday operator action: ride the latest version of the model class you
# already chose (Sonnet 4-5 → Sonnet 5, Opus 4-7 → 4-8). Computed DETERMINISTIC-
# ally at request time from the persisted listings cache + the live pod config
# (no enumeration, no Signals, no recency/modality gate, no cost band — the
# target rung is already the one the predecessor occupies). One-click apply adds
# the latest model to that rung; the resolver prefers the newest cluster member,
# so the role re-point is a no-op (the AdoptModel applier handles it).

# Role-label order for the row subtitle — the most prominent role a rung serves.
_ROLE_LABEL = {
    "fast": "Fast", "standard": "Standard", "power": "Power", "max": "Max",
}
_ROLE_PROMINENCE = ["max", "power", "standard", "fast"]


def _primary_role_label(roles: list[str]) -> str:
    """The single most-prominent role a rung serves, for the row subtitle.
    Empty for a dormant rung (no role points at it)."""
    for r in _ROLE_PROMINENCE:
        if r in (roles or []):
            return _ROLE_LABEL.get(r, r)
    return ""


def _compute_upgrades(shared_dir: Path, network: dict):
    """Run the deterministic version-upgrade pass off the listings cache + live
    config. Returns the list of ``VersionUpgrade`` (empty when no cache yet)."""
    import model_discovery as _md

    cache = _md.read_listings_cache(Path(shared_dir))
    if not cache:
        return []
    listing_by_provider = _md.hydrate_listing_cache(cache)
    # Pod-sourced models only — a pure code-default seed is not this surface's
    # job (spec §Addendum 15 scope note); the merged catalog still supplies the
    # rung/role location for the models the pod actually configured.
    locations = _md.pod_sourced_model_locations(network)
    return _md.compute_version_upgrades(listing_by_provider, locations)


def auto_upgrade_report(shared_dir: Path, network: dict, upgrades=None):
    """The READ-ONLY auto-upgrade eligibility verdict for this pod's upgrades.

    Spec: internal/spec-model-auto-upgrade-2026-07-30.md Phase 1 — the engine ships
    dark and this is its only surface: a "would this apply on its own?" column
    the operator can watch be right for a week before anything is turned on.
    Nothing here mutates; the engine is a pure function.

    Returns ``None`` when the analyzer engine can't be imported or the run
    raises — the freshness card must keep rendering its (unchanged) upgrade rows
    even if the preview column can't be computed.
    """
    try:
        import model_auto_upgrade as _mau
        import model_discovery as _md
        import model_pricing as _mp
        import model_upgrade_store as _mus

        cache = _md.read_listings_cache(Path(shared_dir)) or {}
        return _mau.compute_auto_upgrades(
            network,
            listing_by_provider=_md.hydrate_listing_cache(cache),
            pricing_cache=_mp.read_pricing_cache(Path(shared_dir)),
            listing_degraded=cache.get("degraded") or [],
            upgrades=upgrades if upgrades is not None
            else _compute_upgrades(shared_dir, network),
            # READ-ONLY: the Phase-3a first-seen ledger, so the preview column
            # can show the veto countdown the apply path will honour. This
            # route never WRITES the ledger — the clock is advanced by the
            # daily model_discovery generator run, because a clock advanced by
            # page views would never start on a pod nobody looks at.
            first_seen=_mus.read_first_seen(Path(shared_dir)),
        )
    except Exception as exc:  # pragma: no cover — fail-open to no column
        # WARNING, not debug: losing the column silently is this phase's own
        # failure mode one level up — the card would simply stop saying anything
        # about automatic updates with no trace anywhere.
        _log.warning("model_discovery_adopt: auto-upgrade preview failed: %s", exc)
        return None


def _auto_upgrade_row(decision) -> dict:
    """The per-row preview payload: the verdict, the reason it was held, and
    which config scopes would carry it."""
    hold = decision.primary_hold
    return {
        "verdict": "would-apply" if decision.would_auto_apply else (
            "eligible" if decision.eligible else "held"
        ),
        "summary": decision.summary,
        "reason_code": hold.code if hold else "",
        "durable": bool(hold and hold.durable),
        "scopes": list(decision.scopes),
        "enabled_scopes": list(decision.enabled_scopes),
    }


def list_version_upgrades(shared_dir: Path, network: dict) -> list[dict]:
    """The version-upgrade rows the card's PRIMARY section renders. Each row:
    ``{provider, family, current_model, latest_model, latest_model_id,
    rung_slug, roles, role_label}``, plus the read-only ``auto_upgrade`` preview
    column (spec-model-auto-upgrade Phase 1 — a verdict + reason, never an
    action). Only upgrades with a real ``rung_slug`` are surfaced — an unlocated
    model has no apply target."""
    return version_upgrade_rows(shared_dir, network)[0]


def version_upgrade_rows(
    shared_dir: Path, network: dict,
) -> "tuple[list[dict], Any]":
    """:func:`list_version_upgrades` plus the auto-upgrade report the rows were
    annotated from, so a caller that wants both (the card endpoint) computes the
    upgrade pass once rather than twice."""
    upgrades = _compute_upgrades(shared_dir, network)
    report = auto_upgrade_report(shared_dir, network, upgrades)
    by_key = report.by_key() if report is not None else {}
    rows: list[dict] = []
    for u in upgrades:
        if not u.rung_slug:
            # No rung to extend → not one-click appliable; skip (don't strand a
            # row whose Update button can't act). The auto-upgrade report still
            # carries it as held-with-reason — never silently dropped.
            continue
        provider, _, latest_bare = u.latest_model.partition("/")
        latest_bare = latest_bare or u.latest_model
        decision = by_key.get(_upgrade_key(u))
        rows.append({
            "provider": u.provider,
            "family": u.family,
            "current_model": u.current_model,
            "latest_model": u.latest_model,
            "latest_model_id": latest_bare,
            "rung_slug": u.rung_slug,
            "roles": list(u.roles or []),
            "role_label": _primary_role_label(u.roles or []),
            "auto_upgrade": _auto_upgrade_row(decision) if decision else None,
        })
    rows.sort(key=lambda r: (r["provider"], r["family"]))
    return rows, report


def _upgrade_key(u) -> str:
    """The engine's own identity for an upgrade row. Built with the engine's
    helper (never a local format string) so the row map and the decision map can
    never drift apart and silently drop the whole column."""
    try:
        import model_auto_upgrade as _mau

        return _mau.upgrade_key(u.provider, u.rung_slug, u.latest_model)
    except Exception:  # pragma: no cover — engine unavailable → no column
        return ""


def auto_upgrade_summary(report) -> dict | None:
    """The card-level auto-upgrade payload: the policy/governance picture, the
    providers that could NOT be checked, and any held item that has no row of
    its own — so a run that applies nothing still says what it considered."""
    if report is None:
        return None
    return {
        "considered": report.considered,
        "eligibleCount": len(report.eligible),
        "heldCount": len(report.held),
        "governance": report.governance(),
        "skippedProviders": [p.to_dict() for p in report.skipped_providers],
        "heldWithoutRow": [
            d.to_dict() for d in report.held if not d.rung_slug
        ],
    }


def _rung_cost_class(network: dict, rung_slug: str) -> str:
    """The costClass of an existing rung (for the AdoptModel action; only used if
    the rung had to be created, which an upgrade never does). Falls back to
    'medium'."""
    for rung in ((network or {}).get("models") or {}).get("rungs") or []:
        if isinstance(rung, dict) and rung.get("id") == rung_slug:
            cc = rung.get("costClass")
            if isinstance(cc, str) and cc:
                return cc
    return "medium"


def apply_upgrade(
    shared_dir: Path, network: dict, *, provider: str, latest_model_id: str,
) -> "tuple[int, dict]":
    """Apply ONE version upgrade: add the latest same-class model to the rung its
    predecessor occupies. The rung_slug is taken from the recomputed upgrade (NOT
    trusted from the client) so a request can't inject an arbitrary rung. Returns
    (http_status, body)."""
    if not provider or not latest_model_id:
        return 400, {"ok": False, "error": "provider and latest_model_id are required"}
    bare = latest_model_id.split("/", 1)[1] if "/" in latest_model_id else latest_model_id
    match = next(
        (
            u for u in _compute_upgrades(shared_dir, network)
            if u.provider == provider
            and u.rung_slug
            and (u.latest_model.split("/", 1)[-1] == bare)
        ),
        None,
    )
    if match is None:
        return 404, {
            "ok": False,
            "error": f"no available upgrade to {provider}/{bare}",
        }
    return _apply_one_upgrade(network, match)


def _apply_one_upgrade(network: dict, upgrade) -> "tuple[int, dict]":
    from schema.proposal import AdoptModel

    provider, _, bare = upgrade.latest_model.partition("/")
    bare = bare or upgrade.latest_model
    action = AdoptModel(
        provider=provider or upgrade.provider,
        model_id=bare,
        rung_slug=upgrade.rung_slug,
        position=0,  # rung already exists → position is unused (extend, not create)
        cost_class=_rung_cost_class(network, upgrade.rung_slug),
        evidence=(upgrade.evidence or {}).get("latest") or {},
        # role_mapping stays 'none': the role already points at this rung, so no
        # re-point is needed. But the resolver routes to the FIRST credentialed
        # member of the cluster, NOT the newest — so the latest version must be
        # spliced in AHEAD of the model it upgrades, or the upgrade would be a
        # silent no-op (the predecessor keeps routing). insert_before names that
        # predecessor; the AdoptModel applier positions the new model just ahead
        # of it and demotes the predecessor to the immediate fallback slot.
        role_mapping="none",
        insert_before=upgrade.current_model,
    )
    result = _applier().apply(action, "<pod>")
    if not result.ok:
        return 400, {"ok": False, "error": result.message, "details": result.details}
    return 200, {
        "ok": True, "message": result.message, "details": result.details,
        "current_model": upgrade.current_model, "latest_model": upgrade.latest_model,
    }


def apply_all_upgrades(shared_dir: Path, network: dict) -> "tuple[int, dict]":
    """Apply EVERY available version upgrade in one pass — the everyday "Update
    all to latest" the operator asked for. Each upgrade extends the rung its
    predecessor occupies; one that fails to apply is reported but never blocks
    the rest."""
    upgrades = [u for u in _compute_upgrades(shared_dir, network) if u.rung_slug]
    if not upgrades:
        return 400, {"ok": False, "error": "no version upgrades available"}
    applied = 0
    failed = 0
    results: list[dict] = []
    for u in upgrades:
        status, body = _apply_one_upgrade(network, u)
        ok = bool(body.get("ok"))
        if ok:
            applied += 1
        else:
            failed += 1
        results.append({
            "current_model": u.current_model, "latest_model": u.latest_model,
            "ok": ok, "message": body.get("message") or body.get("error"),
        })
    return 200, {"ok": failed == 0, "applied": applied, "failed": failed, "results": results}


def list_new_tier_discoveries(shared_dir: Path) -> list[dict]:
    """The ``new_tier`` findings — genuinely-new model lines (e.g. a frontier
    model) that fit NO existing rung. Surfaced in a distinct "create a rung?"
    section, never mixed into the per-rung adopt list (Addendum 13). Collapsed to
    the best model per provider so two frontier variants don't double up."""
    rows = [
        r for r in _all_discovery_rows(shared_dir)
        if r["placement_verdict"] == "new_tier"
    ]
    try:
        import model_discovery as _md

        # No role on new_tier rows → grouping keys on (provider, "") = best per
        # provider, which is the right collapse for a brand-new line.
        rows = _md.select_best_per_rung(rows)
    except Exception as exc:  # pragma: no cover — fail-open to the unfiltered list
        _log.debug("model_discovery_adopt: new-tier selection failed: %s", exc)
    for r in rows:
        r["value_line"] = _value_line(shared_dir, r["provider"], r["model_id"])
    return rows


# ── writes ─────────────────────────────────────────────────────────────────────

def _build_action(details: dict, network: dict):
    """Reconstruct the ``AdoptModel`` action from a firing Signal's details,
    recomputing the cost-rank insertion position against the LIVE pod rungs
    (the stored hint dates to when the Signal was first observed)."""
    from schema.proposal import AdoptModel

    provider = str(details.get("provider") or "")
    model_id = str(details.get("model_id") or "")
    # Empty (NOT "new-rung"): the applier rejects an empty/placeholder slug, so
    # adopting an unplaceable model fails cleanly instead of minting a dead tier.
    rung_slug = details.get("suggested_rung_slug") or ""
    cost_class = details.get("suggested_cost_class") or "medium"
    evidence = details.get("evidence") or {}
    position = details.get("suggested_position", 0)
    try:
        import model_discovery as _md

        rungs = ((network or {}).get("models") or {}).get("rungs") or []
        position = _md.suggest_rung_position(rungs, cost_class)
    except Exception as exc:
        # Applier re-clamps; the stored hint is a safe fallback.
        _log.debug("model_discovery_adopt: rung-position recompute failed: %s", exc)
    return AdoptModel(
        provider=provider, model_id=model_id, rung_slug=rung_slug,
        position=int(position), cost_class=cost_class, evidence=evidence,
    )


def _applier():
    # Importing the module registers the applier; instantiate directly (the
    # applier is stateless) to avoid registry import-order coupling.
    from arbiter.appliers.adopt_model import AdoptModelApplier

    return AdoptModelApplier()


def _resolve_signal(
    shared_dir: Path, sig, *, to_state: "Literal['resolved', 'dismissed']", reason: str,
) -> None:
    """Transition the Signal so the card + nav badge clear immediately. Fail-
    open: a transition miss just leaves the Signal to auto-resolve on the next
    daily run (the model is now in a rung / on the ignore list)."""
    try:
        from signals import store as signals_store

        signals_store.apply_transition(
            sig, to_state, Path(shared_dir), actor="operator", reason=reason,
        )
    except Exception as exc:
        # Fail-open: the model is now in a rung / on the ignore list, so the
        # next daily discovery run auto-resolves the Signal regardless.
        _log.warning(
            "model_discovery_adopt: signal %s -> %s transition failed: %s",
            getattr(sig, "id", "?"), to_state, exc,
        )


def _validate_role_cap(role: str, cap: Any) -> "tuple[str, int | None, str | None]":
    """Returns (normalized_role, cap_val, error). Mirrors the /act endpoint's
    _apply_adopt_model_choices: a cap only applies to max/power."""
    role = str(role or "none").strip().lower()
    if role not in _VALID_ROLES:
        return role, None, (
            f"invalid role {role!r}; expected one of {sorted(_VALID_ROLES)}"
        )
    cap_val: int | None = None
    if cap not in (None, ""):
        try:
            cap_val = int(cap)
        except (TypeError, ValueError):
            return role, None, f"cap must be an integer; got {cap!r}"
        if cap_val < 1:
            return role, None, f"cap must be >= 1; got {cap_val}"
    if role not in _CAP_ROLES:
        cap_val = None
    return role, cap_val, None


def adopt_discovery(
    shared_dir: Path, network: dict, *, provider: str, model_id: str,
    role: str = "none", cap: Any = None,
) -> "tuple[int, dict]":
    """Adopt ONE discovered model into the pod catalog with the operator's role
    + cap choice. Returns (http_status, body)."""
    if not provider or not model_id:
        return 400, {"ok": False, "error": "provider and model_id are required"}
    role, cap_val, err = _validate_role_cap(role, cap)
    if err is not None:
        return 400, {"ok": False, "error": err}

    sig = _find_firing_discovery(shared_dir, provider, model_id)
    if sig is None:
        return 404, {
            "ok": False,
            "error": f"no firing discovery for {provider}/{model_id}",
        }
    action = _build_action(getattr(sig, "details", None) or {}, network)
    action.role_mapping = role
    action.cap_per_day = cap_val

    result = _applier().apply(action, "<pod>")
    if not result.ok:
        return 400, {"ok": False, "error": result.message, "details": result.details}

    _resolve_signal(
        shared_dir, sig, to_state="resolved",
        reason="model adopted from AI Optimization Model Freshness card",
    )
    return 200, {"ok": True, "message": result.message, "details": result.details}


def adopt_all_dormant(shared_dir: Path, network: dict) -> "tuple[int, dict]":
    """Adopt EVERY firing discovery as a dormant catalog entry (role 'none',
    no cap). Successfully adopted models' Signals resolve; any that fail to
    apply stay firing (remain on the card) for retry."""
    sigs = list(_iter_firing_discoveries(shared_dir))
    if not sigs:
        return 400, {"ok": False, "error": "no discovered models to adopt"}
    applier = _applier()
    adopted = 0
    failed = 0
    results: list[dict] = []
    for sig in sigs:
        d = getattr(sig, "details", None) or {}
        provider = d.get("provider")
        model_id = d.get("model_id")
        if not provider or not model_id:
            continue
        action = _build_action(d, network)
        action.role_mapping = "none"
        action.cap_per_day = None
        result = applier.apply(action, "<pod>")
        qualified = d.get("qualified_id") or f"{provider}/{model_id}"
        if result.ok:
            adopted += 1
            _resolve_signal(
                shared_dir, sig, to_state="resolved",
                reason="adopted as dormant from AI Optimization card",
            )
        else:
            failed += 1
        results.append({
            "qualified_id": qualified, "ok": result.ok, "message": result.message,
        })
    return 200, {"ok": failed == 0, "adopted": adopted, "failed": failed, "results": results}


def _ignore_list_path(shared_dir: Path) -> Path:
    # Same location model_discovery.load_ignore_list reads.
    return Path(shared_dir) / "model-freshness" / "discovery-ignore.json"


def ignore_discovery(
    shared_dir: Path, *, provider: str, model_id: str,
) -> "tuple[int, dict]":
    """Add a discovered model to the operator ignore list (so it never surfaces
    again) and dismiss its Signal. Returns (http_status, body)."""
    if not provider or not model_id:
        return 400, {"ok": False, "error": "provider and model_id are required"}
    qualified = f"{provider}/{model_id}"

    path = _ignore_list_path(shared_dir)
    data: dict = {"ignore": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {"ignore": []}
    ignore = data.get("ignore")
    if not isinstance(ignore, list):
        ignore = []
    if qualified not in ignore:
        ignore.append(qualified)
    data["ignore"] = ignore

    # Atomic temp+rename — {shared_dir} is evolve-owned (no sudo/staging).
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".discovery-ignore-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError as exc:
                # Best-effort cleanup; a stray .tmp is harmless (overwritten
                # on the next write).
                _log.debug("model_discovery_adopt: temp cleanup failed: %s", exc)

    sig = _find_firing_discovery(shared_dir, provider, model_id)
    if sig is not None:
        _resolve_signal(
            shared_dir, sig, to_state="dismissed",
            reason="ignored from AI Optimization Model Freshness card",
        )
    return 200, {"ok": True, "ignored": qualified}


# ── routes ──────────────────────────────────────────────────────────────────────

def register_model_discovery_routes(app: Flask, network_path: Path) -> None:
    """Register the AI Optimization model-discovery adopt/ignore routes.

    Split out of ``routes_admin_config.py`` (the ``register_*_routes`` helper-
    module convention, wired from ``server.py`` alongside the other route
    groups) so that hot-hazard route file stays thin — the routes here are
    one-liners delegating to the adopt/ignore functions above. The
    ``model_discovery`` generator is signal-only; adoption drives the AdoptModel
    applier directly from the firing Signal — no Proposal (spec §Addendum 12).
    """

    @app.get("/api/models/discoveries")
    def api_models_discoveries() -> Response:
        """The Model Freshness card's data source. ``upgrades`` is the PRIMARY
        section (spec §Addendum 15) — models the pod runs with a newer same-class
        version available, computed deterministically off the listings cache.
        ``discoveries`` is the secondary fits_existing adopt list (best-per-rung);
        ``new_tiers`` the separate "create a rung?" list. ``count`` is the total
        the nav badge reflects. ``auto_upgrade`` is the READ-ONLY Phase-1
        eligibility preview (governance + unchecked providers + held items with
        no row of their own); each upgrade row carries its own verdict inline.
        Nothing in it is actionable — the engine ships dark."""
        cfg = load_network(network_path)
        shared_dir = Path(cfg.get("sharedDir", CANONICAL_SHARED_DIR))
        upgrades, auto_report = version_upgrade_rows(shared_dir, cfg)
        rows = list_adoptable_discoveries(shared_dir)
        new_tiers = list_new_tier_discoveries(shared_dir)
        return jsonify({
            "upgrades": upgrades,
            "discoveries": rows,
            "new_tiers": new_tiers,
            "roles": picker_roles(cfg),
            "auto_upgrade": auto_upgrade_summary(auto_report),
            "count": len(upgrades) + len(rows) + len(new_tiers),
        })

    @app.post("/api/models/apply-upgrade")
    def api_models_apply_upgrade() -> "Response | tuple[Response, int]":
        """Apply ONE version upgrade — add the latest same-class model to the
        rung its predecessor occupies. Body: {provider, latest_model_id}."""
        cfg = load_network(network_path)
        shared_dir = Path(cfg.get("sharedDir", CANONICAL_SHARED_DIR))
        body = request.get_json(silent=True) or {}
        status, payload = apply_upgrade(
            shared_dir, cfg,
            provider=str(body.get("provider", "") or "").strip(),
            latest_model_id=str(body.get("latest_model_id", "") or "").strip(),
        )
        return jsonify(payload), status

    @app.post("/api/models/apply-all-upgrades")
    def api_models_apply_all_upgrades() -> "Response | tuple[Response, int]":
        """Apply EVERY available version upgrade in one pass — "Update all to
        latest"."""
        cfg = load_network(network_path)
        shared_dir = Path(cfg.get("sharedDir", CANONICAL_SHARED_DIR))
        status, payload = apply_all_upgrades(shared_dir, cfg)
        return jsonify(payload), status

    @app.post("/api/models/adopt-discovery")
    def api_models_adopt_discovery() -> "Response | tuple[Response, int]":
        """Adopt one discovered model with the operator's role/cap choice.
        Body: {provider, model_id, role?, cap?}."""
        cfg = load_network(network_path)
        shared_dir = Path(cfg.get("sharedDir", CANONICAL_SHARED_DIR))
        body = request.get_json(silent=True) or {}
        status, payload = adopt_discovery(
            shared_dir, cfg,
            provider=str(body.get("provider", "") or "").strip(),
            model_id=str(body.get("model_id", "") or "").strip(),
            role=body.get("role", "none"),
            cap=body.get("cap"),
        )
        return jsonify(payload), status

    @app.post("/api/models/adopt-all-discoveries-dormant")
    def api_models_adopt_all_discoveries_dormant() -> "Response | tuple[Response, int]":
        """Adopt every discovered model as a dormant catalog entry."""
        cfg = load_network(network_path)
        shared_dir = Path(cfg.get("sharedDir", CANONICAL_SHARED_DIR))
        status, payload = adopt_all_dormant(shared_dir, cfg)
        return jsonify(payload), status

    @app.post("/api/models/ignore-discovery")
    def api_models_ignore_discovery() -> "Response | tuple[Response, int]":
        """Add a discovered model to the ignore list + dismiss its Signal.
        Body: {provider, model_id}."""
        cfg = load_network(network_path)
        shared_dir = Path(cfg.get("sharedDir", CANONICAL_SHARED_DIR))
        body = request.get_json(silent=True) or {}
        status, payload = ignore_discovery(
            shared_dir,
            provider=str(body.get("provider", "") or "").strip(),
            model_id=str(body.get("model_id", "") or "").strip(),
        )
        return jsonify(payload), status
