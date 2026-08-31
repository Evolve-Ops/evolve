"""model_freshness_drift.py — credential-gap enrichment for catalog-drift findings.

Lives outside routes_admin.py / routes_admin_config.py (frozen no-growth
hot-hazard files) per the helper-module convention. The freshness route
(`/api/models/check-freshness`) builds raw drift findings (`asdict` of
`model_catalog.CatalogDriftFinding`); this module attaches the
`borrow_candidates` the UI needs to render the missing-credentials remedy for
credential-gap (Type-1) drift (§B), and classifies each bot's roles into the
hard-break / dormant severity split the UI renders prominently vs quietly (§C).

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 10 §B + §C.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def bot_providers_and_source(bot_id: str) -> "tuple[set, str | None]":
    """Return (credentialed providers, store source) for a bot. ``source`` is
    the presence reader's ladder value (sqlite | legacy_json | bak | none);
    ``"none"`` is the loud-fail marker (no readable store at all), NOT a readable
    store holding zero providers. A hard subprocess crash yields source ``None``
    (not ``"none"``) — already loud in the reader's log.
    """
    from runtime.agent_runtime import get_runtime
    keys = get_runtime().keys_get(bot_id) or {}
    providers: set = {
        prov.lower()
        for prov, pdata in (keys.get("keys") or {}).items()
        if pdata.get("api_key") or pdata.get("token")
    }
    return providers, keys.get("source")


def attach_credential_store_unreadable(
    summary: dict, cred_sources: dict, bots: list,
) -> None:
    """Attach the ``credential_store_unreadable`` loud-fail advisory.

    ``cred_sources`` is ``{bot_id: source}`` from the presence reader's source
    ladder (``sqlite`` | ``legacy_json`` | ``bak`` | ``none``). A ``"none"`` bot
    had NO readable OpenClaw credential store at all (sqlite + legacy json + bak
    all absent) — typically the store schema moved under us. That must NOT
    masquerade as "zero credentialed providers" (which silently empties every
    tier via #3134's credential-match filter). Surface it as a visible advisory
    so the NEXT storage migration fails loud, not silent-for-days. A readable
    store that simply holds no keys (any other source) does not trip this.
    Mutates ``summary`` in place; no-op when every bot is readable.
    """
    unreadable = sorted(b for b, s in (cred_sources or {}).items() if s == "none")
    if not unreadable:
        return
    summary["credential_store_unreadable"] = {
        # Pod-wide blindness (every checked bot) is the load-bearing failure —
        # critical; a single bot is a warning.
        "severity": "critical" if bots and len(unreadable) == len(bots) else "warning",
        "bots": unreadable,
        "message": (
            "Evolve can't read OpenClaw's credential store for "
            f"{len(unreadable)} bot{'' if len(unreadable) == 1 else 's'}. The "
            "store format may have changed — credentialed-provider detection is "
            "degraded until the reader is updated. Routing is unaffected (the "
            "gateway reads its own store)."
        ),
    }
    _log.warning(
        "freshness: no readable OpenClaw credential store for %s — "
        "credentialed-provider detection degraded",
        ", ".join(unreadable),
    )


def attach_drift_borrow_candidates(
    drift_findings: list[dict], network: dict,
) -> None:
    """Mutate `drift_findings` in place, attaching `borrow_candidates` to each
    credential-gap finding (``provider_credentialed`` is False).

    A credential-gap drift can't be fixed by "Reconcile catalog" (the server
    skips uncredentialed providers), so the UI offers "Copy <provider> from
    <bot>" instead. The candidate list is the bots that hold the provider key,
    minus the drifting bot itself — the same shape and source the provider-
    diversity card uses (`wizard_routes.borrow_candidates`). Best-effort: a
    lookup failure leaves an empty candidate list so the row still offers the
    remove-from-tier / add-manually paths.

    Reconcilable findings, ``recommended_missing``, and unknown-provider
    findings are left untouched (no ``borrow_candidates`` key, or an empty
    list for the unknown-provider case where no provider can be copied).
    """
    from .wizard_routes import borrow_candidates

    cache: dict[str, list[dict]] = {}
    for finding in drift_findings:
        if finding.get("provider_credentialed") is not False:
            continue
        provider = finding.get("provider")
        if not provider or provider == "unknown":
            finding["borrow_candidates"] = []
            continue
        if provider not in cache:
            try:
                cache[provider] = borrow_candidates(provider, network)
            except Exception as exc:  # never let enrichment break the check
                _log.warning(
                    "drift enrich: borrow_candidates failed for %s: %s",
                    provider, exc,
                )
                cache[provider] = []
        finding["borrow_candidates"] = [
            c for c in cache[provider]
            if c.get("bot_id") != finding.get("bot_id")
        ]


def attach_behavior_pins(summary: dict, network_path) -> None:
    """Tag each tier advisory whose recommended model is behavior-pinned.

    A pinned advisory is one whose one-click Apply would reintroduce a
    (bot, tier, model) that ``evolve-admin models rollback`` rejected for
    behavior — the tier-write endpoints refuse it without an explicit
    override (the 2026-08-21 recurrence: a routine "Apply All" silently
    re-swapped the model a rollback had just backed out). Tagging here lets
    the UI render the pin and route the row through the override confirm
    instead of offering an Apply that will 409.

    Mutates ``summary`` in place: sets ``advisories[i]['behavior_pin']`` to
    the pin record for pinned rows, and ``summary['behavior_pin_error']``
    when pin state is unknown (ledger present but unreadable) — in that
    state the endpoints refuse every apply, and the UI must say why. Called
    from BOTH freshness endpoints (check + persisted-status read): pins can
    change between a check and a later page load, and the persisted summary
    deliberately does not store pin state.
    """
    from .model_tier_apply import behavior_pins, pin_lookup

    pins, err = behavior_pins(network_path)
    if err:
        summary["behavior_pin_error"] = err
        _log.warning("freshness: %s", err)
    for advisory in summary.get("advisories") or []:
        pin = pin_lookup(pins, advisory.get("bot_id"), advisory.get("tier"),
                         advisory.get("recommended_model"))
        if pin is not None:
            advisory["behavior_pin"] = pin


def _finding_role(finding: dict, tier_to_role: dict) -> str:
    """The role a tier/role drift finding pertains to.

    ``role_resolves_outside_catalog`` already carries a role id in ``tier``;
    ``tier_member_missing`` carries a legacy ``tierN`` key, mapped via
    ``primary_bot.TIER_TO_ROLE`` (the canonical table the rung migration uses).
    Unknown keys pass through unchanged.
    """
    tier = finding.get("tier") or ""
    if finding.get("kind") == "tier_member_missing":
        return tier_to_role.get(tier, tier)
    return tier


def attach_tier_severity(
    summary: dict, network: dict, bot_sev_input: dict,
) -> None:
    """Classify each bot's roles (hard_break / advisory / dormant / ok) and
    surface the severity split (spec §Addendum 10 §C). Mutates ``summary`` in
    place:

      1. attaches ``summary['hard_break_tiers']`` — every hard-broken role
         across bots (a tier whose whole chain has no credentialed model), with
         the providers it needs, per-provider borrow candidates (the copy-creds
         remedy), and the offending uncredentialed tier entries (so the
         prominent panel can offer copy AND remove). Pure-resolution, so it
         catches the in-catalog hard break (e.g. judge with no credentialed
         model at all) that no drift finding flags.
      2. attaches ``summary['advisory_tiers']`` — every judge that ROUTES but on
         the same vendor as standard (severity ``advisory``). Provider diversity
         is a recommendation, not a requirement (2026-06-19), so this is a soft
         amber nudge ("add a cross-vendor model"), NOT a hard break. Each entry
         carries the doubled-up provider + borrow candidates for the copy.
      3. tags each credential-gap drift finding (``provider_credentialed`` is
         False) with ``tier_severity`` ('hard_break' | 'dormant') from its
         role's classification, so the UI renders the role's inert fallbacks
         quietly and folds genuine breaks into the prominent panel.

    ``bot_sev_input`` is ``{bot_id: (bot_tiers, credentialed_providers)}`` —
    pre-collected by the route loop so this post-pass adds no extra config
    reads. Best-effort per bot: a classification failure leaves that bot
    unclassified rather than breaking the freshness check.
    """
    summary.setdefault("hard_break_tiers", [])
    summary.setdefault("advisory_tiers", [])
    try:
        from primary_bot import (  # type: ignore
            classify_bot_tier_severities, TIER_TO_ROLE,
        )
    except Exception as exc:
        _log.warning("tier severity: analyzer unavailable: %s", exc)
        return

    pod_models = (network or {}).get("models")
    drift = summary.get("drift_findings") or []

    sev_by_bot: dict[str, dict] = {}
    for bot_id, (bot_tiers, providers) in (bot_sev_input or {}).items():
        try:
            sev_by_bot[bot_id] = classify_bot_tier_severities(
                pod_models, bot_tiers, providers,
            )
        except Exception as exc:
            _log.warning("tier severity: classify failed for %s: %s", bot_id, exc)
            sev_by_bot[bot_id] = {}

    # Tag credential-gap findings with their role's severity (default dormant —
    # an unclassified role still routes, by the principle that non-cred entries
    # are inert until a key is added).
    for f in drift:
        if f.get("provider_credentialed") is not False:
            continue
        cls = (sev_by_bot.get(f.get("bot_id")) or {}).get(
            _finding_role(f, TIER_TO_ROLE),
        )
        f["tier_severity"] = cls.get("severity") if cls else "dormant"

    # Build the hard-break panel. Borrow candidates only for providers the bot
    # LACKS — no point offering to copy a key it already holds.
    hard_break: list[dict] = []
    cand_cache: dict[str, list[dict]] = {}
    for bot_id, (bot_tiers, providers) in (bot_sev_input or {}).items():
        creds = providers or set()
        for role, cls in (sev_by_bot.get(bot_id) or {}).items():
            if cls.get("severity") != "hard_break":
                continue
            needed = [
                p for p in (cls.get("providers") or []) if p and p not in creds
            ]
            borrow: dict[str, list[dict]] = {}
            for prov in needed:
                if prov not in cand_cache:
                    try:
                        from .wizard_routes import borrow_candidates
                        cand_cache[prov] = borrow_candidates(prov, network)
                    except Exception as exc:
                        _log.warning(
                            "tier severity: borrow_candidates failed for %s: %s",
                            prov, exc,
                        )
                        cand_cache[prov] = []
                borrow[prov] = [
                    c for c in cand_cache[prov] if c.get("bot_id") != bot_id
                ]
            # Offending uncredentialed tier entries for this role — carry the
            # tierN key the remove-from-tier action needs. Empty for an
            # in-catalog-only hard break (copy is the only remedy; there is no
            # dead tier entry to strip).
            offending = [
                {"model_id": f.get("model_id"), "tier": f.get("tier"),
                 "kind": f.get("kind")}
                for f in drift
                if f.get("bot_id") == bot_id
                and f.get("provider_credentialed") is False
                and _finding_role(f, TIER_TO_ROLE) == role
            ]
            hard_break.append({
                "bot_id": bot_id, "role": role, "reason": cls.get("reason"),
                "needed_providers": needed, "borrow_candidates": borrow,
                "offending_models": offending,
            })
    summary["hard_break_tiers"] = hard_break

    # Build the soft-advisory panel: judges that route but on standard's vendor.
    # Borrow candidates are for providers OTHER than the doubled-up one (a
    # cross-vendor model is what makes diversity satisfiable), minus this bot.
    advisory: list[dict] = []
    for bot_id, (bot_tiers, providers) in (bot_sev_input or {}).items():
        creds = providers or set()
        for role, cls in (sev_by_bot.get(bot_id) or {}).items():
            if cls.get("severity") != "advisory":
                continue
            same_vendor = cls.get("advisory_provider")
            # Suggested cross-vendor providers: rung providers that are NOT the
            # doubled-up vendor (any of them would restore diversity).
            cross = [
                p for p in (cls.get("providers") or [])
                if p and p != same_vendor
            ]
            borrow: dict[str, list[dict]] = {}
            for prov in cross:
                if prov in creds:
                    continue  # already holds this key — no copy needed
                if prov not in cand_cache:
                    try:
                        from .wizard_routes import borrow_candidates
                        cand_cache[prov] = borrow_candidates(prov, network)
                    except Exception as exc:
                        _log.warning(
                            "tier severity: borrow_candidates failed for %s: %s",
                            prov, exc,
                        )
                        cand_cache[prov] = []
                borrow[prov] = [
                    c for c in cand_cache[prov] if c.get("bot_id") != bot_id
                ]
            advisory.append({
                "bot_id": bot_id, "role": role, "reason": cls.get("reason"),
                "same_vendor_provider": same_vendor,
                "cross_vendor_providers": cross,
                "borrow_candidates": borrow,
            })
    summary["advisory_tiers"] = advisory
