"""Phase 9 of the 2026-06 cost-cap normalization (spec:
internal/spec-cost-caps-2026-06-05.md).

Evo read tools for the graduated cost-cap ladder:
- ``pod_state.cost_caps``           — current cap values + effective resolution
- ``pod_state.cost_remediation_status`` — current tier_downgrade / L1 / L2 state

Both default to "the bot this conversation is on" when called from a
non-primary bot; pod operator can pass ``bot_id`` to inspect any bot.
For Phase 9 minimal scope, authorization is pod_operator-only; the
personal-bot-user / member-bot-user tiers will be wired in a follow-up
once the auth-scope plumbing exists pod-wide.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────


def _be_config(shared_dir: Path):
    """Load better-engine-config; return the loaded object or None."""
    try:
        from better_engine_config import load as _load_be  # type: ignore
        return _load_be(shared_dir)
    except Exception as exc:  # noqa: BLE001
        log.warning("cost_caps tool: BE config load failed: %s", exc)
        return None


def _shared_dir_from_network(network: dict) -> Path:
    return Path(network.get("sharedDir") or "/Users/Shared/evolve")


def _load_network_from(network_path: Path) -> dict | None:
    try:
        from evolve_admin.config import load_network
    except ImportError:
        return None
    try:
        return load_network(network_path)
    except Exception:
        return None


# ─── pod_state.cost_caps ──────────────────────────────────────────────────


def _cost_caps_handler(
    network_path: Path,
    bot_id: str | None = None,
) -> dict[str, Any]:
    """Return the per-bot ladder (6 thresholds) + monthly + TTL +
    effective values after pod-default inheritance + derivation.

    Spec: internal/spec-cost-caps-2026-06-05.md.
    """
    network = _load_network_from(network_path)
    if network is None:
        return {"ok": False, "error": "failed to load network.json"}
    if not bot_id:
        return {"ok": False, "error": "bot_id required"}
    if bot_id not in (network.get("bots") or {}):
        return {"ok": False, "error": f"unknown bot: {bot_id!r}"}

    shared_dir = _shared_dir_from_network(network)
    be = _be_config(shared_dir)
    if be is None:
        return {
            "ok": False,
            "error": "better-engine-config unavailable",
        }

    budget = (be.bots.get(bot_id, {}) or {}).get("budget", {}) or {}
    pod_budget = be.pod_defaults.get("budget", {}) or {}

    def _explicit_or_none(field: str) -> float | None:
        v = budget.get(field)
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    # ── Per-bot explicit values ───────────────────────────────────────
    return {
        "ok": True,
        "bot_id": bot_id,
        # Spec-named fields (operator-facing canonical names)
        "per_bot": {
            "monthly_budget_usd":  _explicit_or_none("per_bot_monthly_cap_usd"),
            "daily_warn_usd":      _explicit_or_none("per_bot_daily_warn_usd"),
            "weekly_warn_usd":     _explicit_or_none("weekly_warn_usd"),
            "tier_downgrade_usd":  _explicit_or_none("tier_downgrade_usd"),
            # l1_breaker is the spec-renamed view of per_bot_daily_hard_usd.
            "l1_breaker_usd":      _explicit_or_none("per_bot_daily_hard_usd"),
            "l2_breaker_usd":      _explicit_or_none("l2_breaker_usd"),
            "per_session_cap_usd": _explicit_or_none("per_bot_session_cost_cap_usd"),
            "cache_retention":     budget.get("per_bot_cache_retention"),
        },
        # ── Pod-default values inherited when per-bot is null ────────
        "pod_defaults": {
            "daily_warn_usd":     pod_budget.get("per_bot_daily_warn_usd"),
            "weekly_warn_usd":    pod_budget.get("weekly_warn_usd"),
            "tier_downgrade_usd": pod_budget.get("tier_downgrade_usd"),
            "l1_breaker_usd":     pod_budget.get("per_bot_daily_hard_usd"),
            "l2_breaker_usd":     pod_budget.get("l2_breaker_usd"),
            "pod_weekly_warn_usd": pod_budget.get("pod_weekly_warn_usd"),
        },
        # ── Effective values (post inheritance + monthly derivation) ─
        # Resolved through ``BetterEngineConfig.resolve_budget_ladder`` — the
        # single reader ``spend_alert`` enforces on — so "what caps does <bot>
        # have?" answers with the caps that will actually fire. Reporting a
        # rung this tool computed on its own is how the operator ends up
        # trusting a cap nothing enforces. ``None`` means no enforcement at
        # that rung.
        #
        # Deliberately NOT ``budget_hard_cap_usd`` (what this used to report):
        # that resolver ends in the compiled $5 product default, which arms the
        # Budget Hawk guardian veto but no breaker. A mature bot with neither an
        # explicit nor a pod-default cap gets no L1 trip on purpose — arming
        # them fleet-wide off a default the operator never set was rejected in
        # the graduated-cap work. So the two chains agree everywhere except
        # "nothing configured at any scope", where this correctly says null.
        "effective": {
            rung + "_usd": value
            for rung, value in be.resolve_budget_ladder(bot_id).items()
        },
    }


COST_CAPS_TOOL = Tool(
    name="pod_state.cost_caps",
    description=(
        "Read a bot's cost-cap configuration: the operator-set per-bot "
        "thresholds (daily_warn / weekly_warn / tier_downgrade / l1_breaker / "
        "l2_breaker / per_session_cap / monthly_budget / cache_retention), the "
        "pod-default inheritance source for each, and the effective value of "
        "every rung after pod-default inheritance, the graduated new-bot "
        "default, and monthly-budget derivation — these are the thresholds "
        "enforcement will actually fire on (null = no enforcement). Use "
        "this to answer 'what caps does <bot> have?', 'why was <bot> "
        "tier-downgraded?', or before recommending a cap change. Spec: "
        "internal/spec-cost-caps-2026-06-05.md."
    ),
    wire_description=(
        "Read a bot's cost-cap config: operator-set per-bot thresholds "
        "(daily/weekly warns, tier_downgrade, l1/l2 breakers, "
        "per-session, monthly), pod-default inheritance, and the "
        "effective value per rung — the thresholds enforcement will "
        "actually fire on (null = no enforcement). Use for 'what caps "
        "does <bot> have?', 'why was <bot> tier-downgraded?', or "
        "before recommending a cap change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": (
                    "Bot to inspect (e.g. 'admin_bot', 'team_bot_a'). "
                    "Required."
                ),
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_cost_caps_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "cost"),
    authorization_scope="admin",
)


register(COST_CAPS_TOOL)


# ─── pod_state.cost_remediation_status ────────────────────────────────────


def _read_breaker_record(
    shared_dir: Path, bot_id: str, breaker_type: str,
) -> dict | None:
    """Read a breaker record (cost / cost_l2) for ``bot_id`` if present."""
    path = shared_dir / "breakers" / bot_id / f"{breaker_type}.json"
    if not path.exists():
        return None
    try:
        rec = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return rec


def _tier_downgrade_active(
    shared_dir: Path, bot_id: str,
) -> tuple[bool, str | None]:
    """Read the tier_downgrade flag file. Returns (active, recorded_date)."""
    flag = shared_dir / "cost_remediations" / bot_id / "tier_downgrade.flag"
    if not flag.exists():
        return False, None
    try:
        recorded = flag.read_text().strip()
    except OSError:
        return False, None
    if not recorded:
        return False, None
    from datetime import date
    today_iso = str(date.today())
    return (recorded == today_iso), recorded


def _cost_remediation_status_handler(
    network_path: Path,
    bot_id: str | None = None,
) -> dict[str, Any]:
    """Read the current state of every remediation tier for ``bot_id``."""
    network = _load_network_from(network_path)
    if network is None:
        return {"ok": False, "error": "failed to load network.json"}
    if not bot_id:
        return {"ok": False, "error": "bot_id required"}
    if bot_id not in (network.get("bots") or {}):
        return {"ok": False, "error": f"unknown bot: {bot_id!r}"}

    shared_dir = _shared_dir_from_network(network)

    tier_active, tier_date = _tier_downgrade_active(shared_dir, bot_id)
    l1_rec = _read_breaker_record(shared_dir, bot_id, "cost")
    l2_rec = _read_breaker_record(shared_dir, bot_id, "cost_l2")

    def _breaker_view(rec: dict | None) -> dict:
        if rec is None:
            return {"tripped": False}
        return {
            "tripped": True,
            "tripped_at": rec.get("tripped_at"),
            "expires_at": rec.get("expires_at"),
            "reason": rec.get("reason"),
            "trip_id": rec.get("trip_id"),
            "initiated_by": rec.get("initiated_by"),
        }

    return {
        "ok": True,
        "bot_id": bot_id,
        "tier_downgrade": {
            "active": tier_active,
            "recorded_date": tier_date,
            "reverts_at": "midnight (date roll)" if tier_active else None,
        },
        "l1_breaker": _breaker_view(l1_rec),
        "l2_breaker": _breaker_view(l2_rec),
    }


COST_REMEDIATION_STATUS_TOOL = Tool(
    name="pod_state.cost_remediation_status",
    description=(
        "Read the current state of every cost-driven remediation tier for "
        "a bot — whether tier_downgrade is active today, whether L1 / L2 "
        "breakers are tripped, and the timestamps + reasons for each. Use "
        "this to answer 'why is <bot> on tier 3?', 'why isn't <bot> "
        "responding?', 'when does <bot>'s remediation revert?'. Spec: "
        "internal/spec-cost-caps-2026-06-05.md §Behavior catalog."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot to inspect. Required.",
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_cost_remediation_status_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "cost"),
    authorization_scope="admin",
)


register(COST_REMEDIATION_STATUS_TOOL)
