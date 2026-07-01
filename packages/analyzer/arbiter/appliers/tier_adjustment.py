"""arbiter.appliers.tier_adjustment — Adjust per-class tier routing.

Budget Hawk emits TierAdjustment proposals when a bot crosses its hard
spend cap (see ``generators/budget_hawk/proposals.py::make_tier_downgrade``).
The action carries:

  - ``bot_id``         — which bot to adjust
  - ``target_class``   — session class whose routing changes
                          ("maintenance", "background", "ambiguous")
  - ``new_tier``       — destination tier; accepts tier IDs ("tier3") or
                          friendly aliases ("haiku", "sonnet", "opus", "judge")

The applier patches ``routing.<class>Tier`` inside the bot's
``~/.openclaw/evolve-tiers.json`` via the existing ``oc_model.py`` API
(invoked through the runtime seam's ``full_config_set`` — today's
``OpenClawRuntime`` so the write happens as the bot user — the admin
server runs as ``evolve``, which has the ``sudo -u {bot} python3
oc_model.py`` grant from setup_wizard.py).

Snapshot captures the full prior routing dict so revert restores the
exact pre-apply state without depending on which fields were touched.
"""

from __future__ import annotations

from typing import Any, Callable

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import TierAdjustment


# target_class (session classification) → routing field name in evolve-tiers.json.
# See oc_model.py:39-45 for the schema.
_TARGET_CLASS_TO_ROUTING_FIELD: dict[str, str] = {
    "maintenance": "maintenanceTier",
    "background": "backgroundTier",
    "ambiguous": "ambiguousTier",
}

# Special target_class that writes the *primary model* field in
# openclaw.json (agents.defaults.model.primary). Distinct from the
# routing targets above — those write tier IDs to evolve-tiers.json;
# this writes the resolved model string to the bot's openclaw.json
# so the runtime's static fallback matches the floor tier its work
# demands. See primary_model_floor_advisor (2026-05-28) for rationale.
TARGET_CLASS_PRIMARY = "primary"

# Friendly model aliases → tier IDs. Generators write the alias for
# readability ("haiku" beats "tier3" in admin-surface text); the applier
# resolves them to canonical tier IDs that the runtime ModelRouter reads.
# See oc_model.py:33-37 for the tier definitions.
_TIER_ALIAS: dict[str, str] = {
    "haiku": "tier3",
    "sonnet": "tier2",
    "opus": "tier1",
    "judge": "tier0",
    "tier0": "tier0",
    "tier1": "tier1",
    "tier2": "tier2",
    "tier3": "tier3",
}


# Module-level injectable IO so tests can stub config read/write without
# spawning sudo subprocesses. Defaults resolve lazily to oc_cli's helpers.
_get_config_fn: Callable[[str], "dict | None"] | None = None
_set_config_fn: Callable[[str, dict], "dict | None"] | None = None


def set_config_io(
    get_fn: Callable[[str], "dict | None"] | None,
    set_fn: Callable[[str, dict], "dict | None"] | None,
) -> None:
    """Override the config getter/setter — for tests."""
    global _get_config_fn, _set_config_fn
    _get_config_fn = get_fn
    _set_config_fn = set_fn


def _read_routing(bot_id: str) -> dict:
    """Return the bot's current routing dict (empty if unset)."""
    global _get_config_fn
    if _get_config_fn is None:
        from runtime.agent_runtime import get_runtime

        _get_config_fn = get_runtime().full_config_get
    config = _get_config_fn(bot_id) or {}
    return dict(config.get("routing") or {})


def _write_routing(bot_id: str, routing: dict) -> "dict | None":
    """Replace the bot's routing dict atomically. Returns None on failure."""
    global _set_config_fn
    if _set_config_fn is None:
        from runtime.agent_runtime import get_runtime

        _set_config_fn = get_runtime().full_config_set
    return _set_config_fn(bot_id, {"routing": routing})


def _normalize(action: TierAdjustment) -> tuple[str | None, str | None]:
    """Return (routing_field, tier_id) or Nones for invalid inputs.

    For target_class="primary" the "field" return is the sentinel
    string "primary" — callers distinguish this from the routing
    target classes and route to the openclaw.json writer instead of
    the routing-config setter.
    """
    target = action.target_class.strip().lower()
    if target == TARGET_CLASS_PRIMARY:
        # primary uses a different write path; signal that with the
        # sentinel value. Tier resolution still happens normally.
        tier = _TIER_ALIAS.get(action.new_tier.strip().lower())
        return TARGET_CLASS_PRIMARY, tier
    field = _TARGET_CLASS_TO_ROUTING_FIELD.get(target)
    tier = _TIER_ALIAS.get(action.new_tier.strip().lower())
    return field, tier


# Module-level helpers for the primary-model write path. Tests inject
# fakes via set_primary_io; production resolves to the real readers.
_get_oc_full_fn: Callable[[str], "dict | None"] | None = None
_get_tier_models_fn: Callable[[str, str], "list[str]"] | None = None
_write_primary_fn: Callable[[str, str], "tuple[bool, str]"] | None = None


def set_primary_io(
    get_oc_full_fn: Callable[[str], "dict | None"] | None = None,
    get_tier_models_fn: Callable[[str, str], "list[str]"] | None = None,
    write_primary_fn: Callable[[str, str], "tuple[bool, str]"] | None = None,
) -> None:
    """Override the primary-model IO helpers — for tests."""
    global _get_oc_full_fn, _get_tier_models_fn, _write_primary_fn
    _get_oc_full_fn = get_oc_full_fn
    _get_tier_models_fn = get_tier_models_fn
    _write_primary_fn = write_primary_fn


def _read_primary_model(bot_id: str) -> str:
    """Return the bot's current ``agents.defaults.model.primary`` string."""
    global _get_oc_full_fn
    if _get_oc_full_fn is None:
        from runtime.agent_runtime import get_runtime
        _get_oc_full_fn = get_runtime().full_config_get
    config = _get_oc_full_fn(bot_id) or {}
    agents = config.get("agents") or {}
    defaults = agents.get("defaults") or {}
    model = defaults.get("model") or {}
    return str(model.get("primary") or "")


def _resolve_tier_to_model(bot_id: str, tier_id: str) -> str:
    """Return the first model string registered for the given tier.

    Reads ``evolve-tiers.json`` (per-bot) via the same runtime-seam
    surface the routing path uses. Returns the empty string when the tier isn't
    defined or has no models — the caller treats empty as a hard failure
    and refuses to write.
    """
    global _get_tier_models_fn
    if _get_tier_models_fn is None:
        from runtime.agent_runtime import get_runtime
        full_config_get = get_runtime().full_config_get

        def _default(bot_id: str, tier_id: str) -> list[str]:
            config = full_config_get(bot_id) or {}
            tiers = config.get("tiers") or {}
            tier = tiers.get(tier_id) or {}
            models = tier.get("models") or []
            return [str(m) for m in models if m]

        _get_tier_models_fn = _default
    models = _get_tier_models_fn(bot_id, tier_id) or []
    return models[0] if models else ""


def _write_primary_model(bot_id: str, model: str) -> tuple[bool, str]:
    """Write ``agents.defaults.model.primary = model`` to the bot's openclaw.json.

    Uses the existing L2 path (``permissions.writer.write_openclaw_fields``
    — /tmp + sudo + chmod + gateway kickstart). Returns ``(ok, message)``.
    """
    global _write_primary_fn
    if _write_primary_fn is None:
        from permissions.writer import write_openclaw_fields

        def _default(bot_id: str, model: str) -> tuple[bool, str]:
            return write_openclaw_fields(
                bot_id,
                {"agents.defaults.model.primary": model},
                kickstart=True,
            )

        _write_primary_fn = _default
    return _write_primary_fn(bot_id, model)


class TierAdjustmentApplier:
    def capture_snapshot(self, action: TierAdjustment, bot_id: str) -> dict:
        field, _ = _normalize(action)
        # The primary path snapshots the prior model string from
        # openclaw.json; the routing path keeps capturing the full
        # routing dict.
        if field == TARGET_CLASS_PRIMARY:
            return {
                "action_kind": "TierAdjustment",
                "bot_id": bot_id,
                "target_class": action.target_class,
                "routing_field": TARGET_CLASS_PRIMARY,
                "prior_primary_model": _read_primary_model(bot_id),
            }
        prior_routing = _read_routing(bot_id)
        return {
            "action_kind": "TierAdjustment",
            "bot_id": bot_id,
            "target_class": action.target_class,
            "routing_field": field,
            "prior_routing": prior_routing,
        }

    def apply(self, action: TierAdjustment, bot_id: str) -> ApplyResult:
        field, tier = _normalize(action)
        if field is None:
            return ApplyResult(
                ok=False,
                details={"target_class": action.target_class},
                message=(
                    f"unknown target_class {action.target_class!r}; expected one "
                    f"of {sorted(_TARGET_CLASS_TO_ROUTING_FIELD) + [TARGET_CLASS_PRIMARY]}"
                ),
            )
        if tier is None:
            return ApplyResult(
                ok=False,
                details={"new_tier": action.new_tier},
                message=(
                    f"unknown new_tier {action.new_tier!r}; expected a tier ID "
                    f"(tier0..tier3) or alias (haiku/sonnet/opus/judge)"
                ),
            )

        if field == TARGET_CLASS_PRIMARY:
            # Resolve the tier to its first registered model in the
            # bot's tiers config. If the tier isn't defined or has no
            # models, refuse to write — better to fail loudly than to
            # blank the primary field.
            new_model = _resolve_tier_to_model(bot_id, tier)
            if not new_model:
                return ApplyResult(
                    ok=False,
                    details={"bot_id": bot_id, "tier": tier},
                    message=(
                        f"tier {tier!r} has no registered models in "
                        f"{bot_id}'s tiers config; refusing to blank "
                        f"primary"
                    ),
                )
            current_primary = _read_primary_model(bot_id)
            if current_primary == new_model:
                return ApplyResult(
                    ok=False,
                    details={
                        "bot_id": bot_id, "field": "primary", "model": new_model,
                    },
                    message=(
                        f"no-op: agents.defaults.model.primary is already "
                        f"{new_model!r} for {bot_id}"
                    ),
                )
            ok, msg = _write_primary_model(bot_id, new_model)
            if not ok:
                return ApplyResult(
                    ok=False,
                    details={
                        "bot_id": bot_id, "tier": tier, "model": new_model,
                    },
                    message=f"failed to write primary model: {msg}",
                )
            return ApplyResult(
                ok=True,
                details={
                    "bot_id": bot_id, "field": "primary",
                    "tier": tier, "model": new_model,
                    "prior_model": current_primary,
                },
                message=(
                    f"set agents.defaults.model.primary={new_model!r} "
                    f"for {bot_id} (tier={tier})"
                ),
            )

        # Read-modify-write: oc_full_config_set replaces the entire routing
        # dict (oc_model.py:407), so we must merge in-process to preserve
        # other routing fields (enabled, confidenceThreshold, etc).
        routing = _read_routing(bot_id)
        if routing.get(field) == tier:
            return ApplyResult(
                ok=False,
                details={"bot_id": bot_id, "field": field, "tier": tier},
                message=(
                    f"no-op: routing.{field} is already {tier!r} for {bot_id}; "
                    "routing config is correct — check classifier recall instead"
                ),
            )
        routing[field] = tier
        result = _write_routing(bot_id, routing)
        if result is None:
            return ApplyResult(
                ok=False,
                details={"bot_id": bot_id, "field": field, "tier": tier},
                message=f"failed to write tier routing for {bot_id}",
            )
        return ApplyResult(
            ok=True,
            details={"bot_id": bot_id, "field": field, "tier": tier},
            message=f"set routing.{field}={tier!r} for {bot_id}",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        # Primary-model revert: restore the stashed model string via
        # the same writer the apply path used.
        if snapshot.get("routing_field") == TARGET_CLASS_PRIMARY:
            prior = str(snapshot.get("prior_primary_model") or "")
            if not prior:
                return RevertResult(
                    ok=False,
                    details={"bot_id": bot_id},
                    message="snapshot missing prior_primary_model",
                )
            ok, msg = _write_primary_model(bot_id, prior)
            if not ok:
                return RevertResult(
                    ok=False,
                    details={"bot_id": bot_id, "model": prior},
                    message=f"failed to restore primary model: {msg}",
                )
            return RevertResult(
                ok=True,
                details={"bot_id": bot_id, "model": prior},
                message=f"restored agents.defaults.model.primary={prior!r}",
            )
        if "prior_routing" not in snapshot:
            return RevertResult(
                ok=False, message="snapshot missing prior_routing"
            )
        prior_routing = dict(snapshot.get("prior_routing") or {})
        field = snapshot.get("routing_field") or "<unknown>"
        result = _write_routing(bot_id, prior_routing)
        if result is None:
            return RevertResult(
                ok=False,
                details={"bot_id": bot_id, "field": field},
                message=f"failed to restore tier routing for {bot_id}",
            )
        return RevertResult(
            ok=True,
            details={"bot_id": bot_id, "field": field},
            message=f"restored routing.{field} for {bot_id}",
        )


register_applier("TierAdjustment", TierAdjustmentApplier())
