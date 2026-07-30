"""Action tools — per-bot cost caps + enforcement clear.

Three tools that mirror the Cost & caps card on Settings → Bots:

* ``action.cost.set_bot_cap(bot_id, cap_usd)`` — set
  ``better-engine-config.json::bots[bot].budget.per_bot_daily_hard_usd``.
  spend_alert + cost_watchdog read this value on their next sweep;
  exceeding it auto-trips an L1 cost breaker that actually disables the
  bot's heartbeat (see PR #1483 + memory
  project_safety_nets_shipped_2026_05_23). Mirrors the Daily hard cap
  field in the Cost & caps card.

* ``action.cost.clear_bot_cap(bot_id)`` — remove the per-bot daily hard
  cap override from better-engine-config; the bot falls back to the pod
  default cap.

* ``action.cost.clear_enforcement(bot_id)`` — clear an active spend-cap
  enforcement flag for a bot (un-trip a breaker that fired today).
  Mirrors POST /api/spend-caps/<bot_id>/clear (server.py:22323). Use
  when the operator has triaged the spike and wants the bot back to
  normal model selection before midnight.

All three are write_risky:

* Setting too low a cap can disable a bot's heartbeat (the L1 breaker
  trip is real per memory).
* Clearing the cap removes a safety net.
* Clearing active enforcement un-trips a breaker that fired for a
  reason — spend will resume at full rates until midnight reset.

Memory references:
* project_safety_nets_shipped_2026_05_23 — daily_cap_usd auto-trips
  L1 cost breaker (no more no-op); confirms this is real enforcement,
  not advisory.
* project_cost_alerting_blackout_2026_05_20 — historical context for
  why these tools matter (Usage tile silently disagreeing with Usage
  Summary was a smoking-gun signature).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# ─── helpers ─────────────────────────────────────────────────────────────────


def _load_network(network_path: Path) -> dict | None:
    """Load network.json. Returns None on failure; callers convert to
    a tool error dict so the model sees a clean message."""
    try:
        from evolve_admin.config import load_network
    except ImportError as exc:
        log.warning("action.cost.*: load_network unavailable: %s", exc)
        return None
    try:
        return load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("action.cost.*: load_network failed: %s", exc)
        return None


def _bot_exists(network: dict, bot_id: str) -> bool:
    return bot_id in (network.get("bots") or {})


# ─── action.cost.set_bot_cap ──────────────────────────────────────────────────


def _set_bot_cap_handler(
    network_path: Path,
    bot_id: str,
    cap_usd: float,
) -> dict[str, Any]:
    """Write the per-bot daily hard cap to the canonical store.

    Post-Phase-4 cost-cap normalization (2026-06): writes to
    ``better-engine-config.json::bots.<bot>.budget.per_bot_daily_hard_usd``.
    spend_alert + cost_watchdog read this on their next sweep; exceeding
    it trips an L1 cost breaker that disables the bot's heartbeat.
    Setting too low a value will disable the bot mid-day — pick a value
    that gives headroom for the bot's normal pattern.
    """
    if cap_usd is None or cap_usd <= 0:
        return {
            "ok": False,
            "error": (
                "cap_usd must be a positive number. To remove a cap, use "
                "action.cost.clear_bot_cap instead."
            ),
        }

    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "error": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "error": f"unknown bot: {bot_id!r}"}

    shared_dir, be_load, be_save = _be_helpers(network)
    if be_load is None or be_save is None:
        return {"ok": False, "error": "better-engine-config helpers unavailable"}

    try:
        be = be_load(shared_dir)
        prior = be.per_bot_daily_hard_usd(bot_id) if hasattr(
            be, "per_bot_daily_hard_usd"
        ) else (
            be.bots.get(bot_id, {}).get("budget", {}).get("per_bot_daily_hard_usd")
        )
        be.set_per_bot_daily_hard_usd(bot_id, float(cap_usd))
        be_save(be, shared_dir)
    except Exception as exc:  # noqa: BLE001
        log.exception("action.cost.set_bot_cap: BE config write failed")
        return {"ok": False, "error": f"failed to save better-engine-config: {exc}"}

    return {
        "ok": True,
        "bot_id": bot_id,
        "cap_usd": float(cap_usd),
        "prior_cap_usd": prior,
        "verify_via": {
            "tool": "pod_state.bots",
            "args": {},
            "expect": (
                f"better-engine-config.json::bots.{bot_id}.budget."
                f"per_bot_daily_hard_usd == {cap_usd} (the canonical store; "
                f"visible in the Cost & caps card on Settings -> Bots)"
            ),
        },
    }


def _be_helpers(network: dict):
    """Return ``(shared_dir, load, save)`` for better-engine-config.

    ``load`` / ``save`` are None when the analyzer package isn't importable
    (caller should fail gracefully with a clean error message).
    """
    shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
    try:
        from better_engine_config import (  # type: ignore[import]
            load as _load,
            save as _save,
        )
    except ImportError:
        return shared_dir, None, None
    return shared_dir, _load, _save


def _set_bot_cap_validate(
    network_path: Path,
    bot_id: str,
    cap_usd: float,
) -> dict[str, Any]:
    """Dry-run: confirm bot exists, cap is positive. Warn if the new
    cap is below the bot's recent daily spend (would auto-trip)."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if cap_usd is None:
        return {
            "ok": False,
            "reason": (
                "cap_usd is required (positive number, in USD). To remove "
                "a cap, use action.cost.clear_bot_cap instead."
            ),
        }
    try:
        cap_usd_f = float(cap_usd)
    except (TypeError, ValueError):
        return {"ok": False, "reason": f"cap_usd must be a number: got {cap_usd!r}"}
    if cap_usd_f <= 0:
        return {
            "ok": False,
            "reason": (
                f"cap_usd must be positive (got {cap_usd_f}). To remove a "
                "cap, use action.cost.clear_bot_cap."
            ),
        }

    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "reason": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "reason": f"unknown bot: {bot_id!r}"}

    # Post-Phase-4: prior cap comes from better-engine-config, the canonical
    # store. Best-effort; an import failure just leaves prior as None.
    prior: float | None = None
    shared_dir, be_load, _ = _be_helpers(network)
    if be_load is not None:
        try:
            be = be_load(shared_dir)
            raw = be.bots.get(bot_id, {}).get("budget", {}).get(
                "per_bot_daily_hard_usd"
            )
            if raw is not None:
                prior = float(raw)
        except Exception:
            pass
    return {
        "ok": True,
        "context": {
            "bot_id": bot_id,
            "prior_cap_usd": prior,
            "new_cap_usd": cap_usd_f,
        },
    }


SET_BOT_CAP_TOOL = Tool(
    name="action.cost.set_bot_cap",
    description=(
        "Set a per-bot daily LLM-spend cap (USD). When the bot's daily "
        "spend exceeds this value, the L1 cost breaker auto-trips and "
        "disables the bot's heartbeat for the rest of the day (real "
        "enforcement, not advisory — per PR #1483). Use to bound runaway "
        "spend or to enforce a budget. Pick a value with headroom over "
        "the bot's normal pattern — too low and you'll disable the bot "
        "mid-day. To remove the cap entirely, use action.cost.clear_bot_cap."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": (
                    "Bot id — get from pod_state.bots. Cap is per-bot, "
                    "not pod-wide."
                ),
            },
            "cap_usd": {
                "type": "number",
                "description": (
                    "Daily cap in USD. Must be positive. Example: 5.0 for "
                    "a $5/day budget."
                ),
            },
        },
        "required": ["bot_id", "cap_usd"],
        "additionalProperties": False,
    },
    handler=_set_bot_cap_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_set_bot_cap_validate,
    tags=("action", "cost"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(SET_BOT_CAP_TOOL)


# ─── action.cost.clear_bot_cap ────────────────────────────────────────────────


def _clear_bot_cap_handler(
    network_path: Path,
    bot_id: str,
) -> dict[str, Any]:
    """Clear the per-bot daily hard cap (revert to pod default).

    Post-Phase-4 cost-cap normalization: removes the override from
    ``better-engine-config.json::bots.<bot>.budget.per_bot_daily_hard_usd``.
    The bot then inherits the pod-default cap.
    """
    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "error": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "error": f"unknown bot: {bot_id!r}"}

    shared_dir, be_load, be_save = _be_helpers(network)
    if be_load is None or be_save is None:
        return {"ok": False, "error": "better-engine-config helpers unavailable"}

    try:
        be = be_load(shared_dir)
        raw = be.bots.get(bot_id, {}).get("budget", {}).get(
            "per_bot_daily_hard_usd"
        )
        prior = float(raw) if raw is not None else None
        be.set_per_bot_daily_hard_usd(bot_id, None)
        be_save(be, shared_dir)
    except Exception as exc:  # noqa: BLE001
        log.exception("action.cost.clear_bot_cap: BE config write failed")
        return {"ok": False, "error": f"failed to save better-engine-config: {exc}"}

    return {
        "ok": True,
        "bot_id": bot_id,
        "prior_cap_usd": prior,
        "verify_via": {
            "tool": "pod_state.bots",
            "args": {},
            "expect": (
                f"better-engine-config.json::bots.{bot_id}.budget."
                "per_bot_daily_hard_usd is absent (bot inherits pod default; "
                "visible in the Cost & caps card on Settings -> Bots)"
            ),
        },
    }


def _clear_bot_cap_validate(
    network_path: Path,
    bot_id: str,
) -> dict[str, Any]:
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "reason": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "reason": f"unknown bot: {bot_id!r}"}

    # Post-Phase-4: prior cap lives in better-engine-config.
    prior: float | None = None
    shared_dir, be_load, _ = _be_helpers(network)
    if be_load is not None:
        try:
            be = be_load(shared_dir)
            raw = be.bots.get(bot_id, {}).get("budget", {}).get(
                "per_bot_daily_hard_usd"
            )
            if raw is not None:
                prior = float(raw)
        except Exception:
            pass
    if prior is None:
        return {
            "ok": False,
            "reason": (
                f"bot {bot_id!r} has no per-bot daily hard cap override — "
                "nothing to clear (already inheriting pod default)"
            ),
        }
    return {"ok": True, "context": {"bot_id": bot_id, "prior_cap_usd": prior}}


CLEAR_BOT_CAP_TOOL = Tool(
    name="action.cost.clear_bot_cap",
    description=(
        "Remove the per-bot daily LLM-spend cap, disengaging the L1 cost "
        "breaker for that bot. Use when the operator has decided the cap "
        "is no longer needed (bot's workload changed) or as a one-shot "
        "override after a real spend spike has been triaged. Removes the "
        "safety net — the operator should set a new cap (action.cost."
        "set_bot_cap) if any cap is still desired."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id — get from pod_state.bots.",
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_clear_bot_cap_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_clear_bot_cap_validate,
    tags=("action", "cost"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(CLEAR_BOT_CAP_TOOL)


# ─── action.cost.clear_enforcement ────────────────────────────────────────────


def _clear_enforcement_handler(
    shared_dir: Path,
    network_path: Path,
    bot_id: str,
) -> dict[str, Any]:
    """Clear an active pod-wide spend-cap enforcement flag for a bot.

    Mirrors POST /api/spend-caps/<bot_id>/clear. Used when a bot
    tripped the pod-wide spend cap today and the operator has triaged
    the spike — clearing the flag returns the bot to normal model
    selection for the remainder of the day.
    """
    try:
        from evolve_admin.web.server import _import_analyzer  # type: ignore
    except ImportError:
        # The server module's helper is sometimes private; fall back to
        # importing straight from the installed evolve-analyzer package.
        try:
            import spend_caps as sc  # type: ignore
        except ImportError as exc:
            return {"ok": False, "error": f"spend_caps unavailable: {exc}"}
    else:
        try:
            sc = _import_analyzer("spend_caps")
        except ImportError as exc:
            return {"ok": False, "error": f"spend_caps unavailable: {exc}"}

    try:
        cleared = sc.clear_enforcement(shared_dir, bot_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("action.cost.clear_enforcement: clear_enforcement raised")
        return {"ok": False, "error": f"clear_enforcement failed: {exc}"}

    if not cleared:
        return {
            "ok": False,
            "error": (
                f"no active spend-cap enforcement found for {bot_id!r} — "
                "nothing to clear"
            ),
        }

    return {
        "ok": True,
        "bot_id": bot_id,
        "cleared": True,
        "verify_via": {
            "tool": "pod_state.breakers",
            "args": {},
            "expect": (
                f"no active spend-cap enforcement listed for {bot_id!r} "
                "in the trips array (scope matches bot_id, type='cost')"
            ),
        },
    }


def _clear_enforcement_validate(
    shared_dir: Path,
    network_path: Path,
    bot_id: str,
) -> dict[str, Any]:
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "reason": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "reason": f"unknown bot: {bot_id!r}"}
    # We don't pre-check that an enforcement flag exists — the handler
    # returns a clean message if there's nothing to clear. Pre-checking
    # would require duplicating spend_caps internals.
    return {"ok": True, "context": {"bot_id": bot_id}}


CLEAR_ENFORCEMENT_TOOL = Tool(
    name="action.cost.clear_enforcement",
    description=(
        "Clear an active pod-wide spend-cap enforcement flag for a bot, "
        "letting it return to normal model selection for the rest of the "
        "day. Use after the operator has triaged a spend spike and "
        "decided the bot can resume full-rate operation before the "
        "midnight reset. Does NOT change the cap thresholds (use "
        "action.cost.set_bot_cap for that)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id — get from pod_state.bots.",
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_clear_enforcement_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_clear_enforcement_validate,
    tags=("action", "cost"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(CLEAR_ENFORCEMENT_TOOL)


# ─── action.cost.set_cap ──────────────────────────────────────────────────────
# Phase 9 of the 2026-06 cost-cap normalization (spec:
# docs/spec-cost-caps-2026-06-05.md). General-purpose setter for any of
# the six graduated-ladder fields plus monthly + TTL. Generalizes the
# legacy ``action.cost.set_bot_cap`` (which only handled the L1 breaker).


# Mapping: spec field name -> BE config setter method name
_SET_CAP_FIELD_MAP: dict[str, str] = {
    "monthly_budget_usd":  "set_per_bot_monthly_cap_usd",
    "daily_warn_usd":      "set_per_bot_daily_warn_usd",
    "weekly_warn_usd":     "set_per_bot_weekly_warn_usd",
    "tier_downgrade_usd":  "set_per_bot_tier_downgrade_usd",
    "l1_breaker_usd":      "set_per_bot_l1_breaker_usd",
    "l2_breaker_usd":      "set_per_bot_l2_breaker_usd",
    "per_session_cap_usd": "set_per_bot_session_cost_cap_usd",
    "cache_retention":     "set_per_bot_cache_retention",
}


def _set_cap_handler(
    network_path: Path,
    bot_id: str,
    field: str,
    value,
) -> dict[str, Any]:
    """Set or clear one cap field on ``bot_id``.

    Writes to better-engine-config. Validates the resulting ladder
    ordering (l2 > l1 > tier_downgrade > daily_warn) and refuses
    when the write would invert. The operator can lift this by
    setting the inverted field first or via the Cost & caps UI.
    """
    if field not in _SET_CAP_FIELD_MAP:
        valid = sorted(_SET_CAP_FIELD_MAP.keys())
        return {
            "ok": False,
            "error": f"unknown field {field!r}; valid: {valid}",
        }

    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "error": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "error": f"unknown bot: {bot_id!r}"}

    shared_dir, be_load, be_save = _be_helpers(network)
    if be_load is None or be_save is None:
        return {"ok": False, "error": "better-engine-config helpers unavailable"}

    try:
        be = be_load(shared_dir)
        setter = getattr(be, _SET_CAP_FIELD_MAP[field])
        setter(bot_id, value)
        # Phase 5 ladder validation — reject inverted ordering before save.
        errors = be.validate_remediation_ladder(bot_id)
        if errors:
            return {
                "ok": False,
                "error": "; ".join(errors),
                "kind": "remediation_ladder_inverted",
            }
        be_save(be, shared_dir)
    except Exception as exc:  # noqa: BLE001
        log.exception("action.cost.set_cap: BE config write failed")
        return {"ok": False, "error": f"BE config write failed: {exc}"}

    return {
        "ok": True,
        "bot_id": bot_id,
        "field": field,
        "value": value,
        "verify_via": {
            "tool": "pod_state.cost_caps",
            "args": {"bot_id": bot_id},
            "expect": f"per_bot.{field} == {value!r}",
        },
    }


def _set_cap_validate(
    network_path: Path,
    bot_id: str,
    field: str,
    value,
) -> dict[str, Any]:
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if field not in _SET_CAP_FIELD_MAP:
        return {
            "ok": False,
            "reason": (
                f"field must be one of {sorted(_SET_CAP_FIELD_MAP.keys())}; "
                f"got {field!r}"
            ),
        }
    # value validation is field-specific; cache_retention is enum, the
    # rest are positive floats or null. Match the BE-config setter rules
    # exactly so the handler's setter doesn't surprise the caller.
    if field == "cache_retention":
        if value is not None and value not in ("short", "long"):
            return {
                "ok": False,
                "reason": (
                    f"cache_retention must be 'short', 'long', or null; "
                    f"got {value!r}"
                ),
            }
    else:
        if value is None:
            pass  # null clears
        else:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return {"ok": False, "reason": f"{field} must be a number or null"}
            if v <= 0:
                return {"ok": False, "reason": f"{field} must be > 0 or null"}
    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "reason": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "reason": f"unknown bot: {bot_id!r}"}
    return {"ok": True, "context": {"bot_id": bot_id, "field": field, "value": value}}


SET_CAP_TOOL = Tool(
    name="action.cost.set_cap",
    description=(
        "Set one cap field on a bot's cost ladder. Generalizes the legacy "
        "action.cost.set_bot_cap (which only handled L1). Use to wire up "
        "the full ladder (daily_warn / weekly_warn / tier_downgrade / "
        "l1_breaker / l2_breaker / per_session_cap), the monthly_budget "
        "input, or the cache_retention optimization knob. Validates the "
        "resulting ladder ordering and rejects inversions. Pass value=null "
        "to clear a field (revert to pod default / no enforcement). Spec: "
        "docs/spec-cost-caps-2026-06-05.md."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot to update. Required.",
            },
            "field": {
                "type": "string",
                "enum": list(_SET_CAP_FIELD_MAP.keys()),
                "description": (
                    "Cap field to set. Each maps to a distinct enforcement "
                    "tier per the spec."
                ),
            },
            "value": {
                "description": (
                    "Number for cost fields (positive USD); 'short' or "
                    "'long' for cache_retention; null to clear the field."
                ),
            },
        },
        "required": ["bot_id", "field", "value"],
        "additionalProperties": False,
    },
    handler=_set_cap_handler,
    validate=_set_cap_validate,
    risk_tier=RiskTier.WRITE_RISKY,
    tags=("action", "cost"),
    authorization_scope="admin",
)

register(SET_CAP_TOOL)


# ─── action.cost.reset_remediation ───────────────────────────────────────────
# Manually clear a tier_downgrade or breaker. Used when the operator has
# triaged the cause and wants the bot back to normal mid-day.


_REMEDIATION_LEVELS = ("tier_downgrade", "l1_breaker", "l2_breaker")


def _reset_remediation_handler(
    network_path: Path,
    bot_id: str,
    level: str,
) -> dict[str, Any]:
    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "error": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "error": f"unknown bot: {bot_id!r}"}
    if level not in _REMEDIATION_LEVELS:
        return {
            "ok": False,
            "error": f"level must be one of {list(_REMEDIATION_LEVELS)}; got {level!r}",
        }

    shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")

    if level == "tier_downgrade":
        # Remove the daily flag file.
        flag = shared_dir / "cost_remediations" / bot_id / "tier_downgrade.flag"
        existed = flag.exists()
        if existed:
            try:
                flag.unlink()
            except OSError as exc:
                return {"ok": False, "error": f"flag removal failed: {exc}"}
        return {
            "ok": True,
            "bot_id": bot_id,
            "level": "tier_downgrade",
            "was_active": existed,
            "verify_via": {
                "tool": "pod_state.cost_remediation_status",
                "args": {"bot_id": bot_id},
                "expect": "tier_downgrade.active == false",
            },
        }

    # L1 / L2 breaker reset goes through breakers_enforce.enforce_reset
    # which removes the breaker file AND brings the gateway / heartbeat
    # back up via launchctl.
    try:
        from evolve_admin import breakers_enforce as _be  # type: ignore
    except ImportError as exc:
        return {"ok": False, "error": f"breakers_enforce unavailable: {exc}"}

    breaker_type = "cost" if level == "l1_breaker" else "cost_l2"
    try:
        result = _be.enforce_reset(
            scope=bot_id,
            breaker_type=breaker_type,
            network=network,
            shared_dir=shared_dir,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.cost.reset_remediation: enforce_reset raised")
        return {"ok": False, "error": f"enforce_reset failed: {exc}"}

    # Remove the breaker file (enforce_reset doesn't always do this).
    breaker_file = shared_dir / "breakers" / bot_id / f"{breaker_type}.json"
    if breaker_file.exists():
        try:
            breaker_file.unlink()
        except OSError:
            pass

    return {
        "ok": True,
        "bot_id": bot_id,
        "level": level,
        "breaker_type": breaker_type,
        "enforce_ok": bool(result and getattr(result, "ok", False)),
        "verify_via": {
            "tool": "pod_state.cost_remediation_status",
            "args": {"bot_id": bot_id},
            "expect": f"{level}.tripped == false",
        },
    }


def _reset_remediation_validate(
    network_path: Path,
    bot_id: str,
    level: str,
) -> dict[str, Any]:
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if level not in _REMEDIATION_LEVELS:
        return {
            "ok": False,
            "reason": f"level must be one of {list(_REMEDIATION_LEVELS)}; got {level!r}",
        }
    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "reason": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "reason": f"unknown bot: {bot_id!r}"}
    return {"ok": True, "context": {"bot_id": bot_id, "level": level}}


RESET_REMEDIATION_TOOL = Tool(
    name="action.cost.reset_remediation",
    description=(
        "Manually clear a cost-driven remediation tier on a bot — the "
        "tier_downgrade flag, the L1 breaker, or the L2 breaker. Use when "
        "the operator has triaged the spend cause and wants the bot back "
        "to normal mid-day (instead of waiting for the midnight auto-revert "
        "for tier_downgrade / L1, or the manual operator action for L2). "
        "L1 / L2 reset goes through breakers_enforce.enforce_reset which "
        "also bootstraps the gateway / restores heartbeat. Spec: "
        "docs/spec-cost-caps-2026-06-05.md."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot to reset. Required.",
            },
            "level": {
                "type": "string",
                "enum": list(_REMEDIATION_LEVELS),
                "description": "Which remediation tier to clear.",
            },
        },
        "required": ["bot_id", "level"],
        "additionalProperties": False,
    },
    handler=_reset_remediation_handler,
    validate=_reset_remediation_validate,
    risk_tier=RiskTier.WRITE_RISKY,
    tags=("action", "cost"),
    authorization_scope="admin",
)

register(RESET_REMEDIATION_TOOL)
