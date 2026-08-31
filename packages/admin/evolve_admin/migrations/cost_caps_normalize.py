"""cost_caps_normalize — one-shot migration to the canonical cost-cap store.

Phase 2 of the cost-cap normalization audit. Moves per-bot cost-cap values
from their legacy locations into ``better-engine-config.json``, then strips
the legacy keys so the canonical store is the only place these settings
live.

Legacy → canonical mapping:

    network.json::bots.<bot>.daily_cap_usd
        → better-engine-config.json::bots.<bot>.budget.per_bot_daily_hard_usd

    sandbox/overrides/<bot>.json::openclaw.agents.defaults.sessionBudgetCapUsd
        → better-engine-config.json::bots.<bot>.budget.per_bot_session_cost_cap_usd

    sandbox/overrides/<bot>.json::openclaw.agents.defaults.models.cacheRetention
        → better-engine-config.json::bots.<bot>.budget.per_bot_cache_retention

Resolution rule when both legacy and canonical carry a value: canonical
wins (operator's most recent write via the new bot-setup endpoint
supersedes anything older). The legacy key is stripped in either case.

Idempotent: re-running with no legacy keys is a no-op. Hooked into the
admin server's ``create_app`` so it runs on every boot; safe to invoke
manually via ``python3 -m evolve_admin.migrations.cost_caps_normalize``
for ad-hoc cleanup.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platform_profile import get_profile

_log = logging.getLogger(__name__)


# Schema paths in the sandbox-overrides store that this migration drains.
_SANDBOX_SESSION_CAP_KEY = "openclaw.agents.defaults.sessionBudgetCapUsd"
_SANDBOX_CACHE_RETENTION_KEY = "openclaw.agents.defaults.models.cacheRetention"


@dataclass
class MigrationResult:
    """Summary of one migration pass. Returned to caller for logging/Signals."""

    bots_inspected: int = 0
    daily_hard_cap_migrated: list[str] = field(default_factory=list)
    daily_hard_cap_stripped: list[str] = field(default_factory=list)
    session_cap_migrated: list[str] = field(default_factory=list)
    session_cap_stripped: list[str] = field(default_factory=list)
    cache_retention_migrated: list[str] = field(default_factory=list)
    cache_retention_stripped: list[str] = field(default_factory=list)
    # Phase 8 — pod-wide thresholds migrated from network.json into
    # better-engine-config.json::pod_defaults.budget. Boolean flags
    # because each migration is at most one move (the pod has one of
    # each value).
    pod_daily_hard_migrated: bool = False
    pod_daily_warn_migrated: bool = False
    pod_weekly_warn_migrated: bool = False
    pod_tier_downgrade_migrated: bool = False
    pod_l2_breaker_migrated: bool = False
    pod_thresholds_stripped: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return (
            len(self.daily_hard_cap_migrated)
            + len(self.daily_hard_cap_stripped)
            + len(self.session_cap_migrated)
            + len(self.session_cap_stripped)
            + len(self.cache_retention_migrated)
            + len(self.cache_retention_stripped)
            + (1 if self.pod_daily_hard_migrated else 0)
            + (1 if self.pod_daily_warn_migrated else 0)
            + (1 if self.pod_weekly_warn_migrated else 0)
            + (1 if self.pod_tier_downgrade_migrated else 0)
            + (1 if self.pod_l2_breaker_migrated else 0)
            + (1 if self.pod_thresholds_stripped else 0)
        )

    def summary_line(self) -> str:
        if self.total_changes == 0 and not self.errors:
            return f"cost_caps_normalize: no-op ({self.bots_inspected} bots inspected)"
        parts = [f"{self.bots_inspected} bots inspected"]
        if self.daily_hard_cap_migrated:
            parts.append(
                f"daily_hard_cap migrated for {len(self.daily_hard_cap_migrated)}"
            )
        if self.daily_hard_cap_stripped:
            parts.append(
                f"daily_cap_usd stripped from {len(self.daily_hard_cap_stripped)}"
            )
        if self.session_cap_migrated:
            parts.append(
                f"session_cap migrated for {len(self.session_cap_migrated)}"
            )
        if self.session_cap_stripped:
            parts.append(
                f"sandbox session_cap stripped from {len(self.session_cap_stripped)}"
            )
        if self.cache_retention_migrated:
            parts.append(
                f"cache_retention migrated for {len(self.cache_retention_migrated)}"
            )
        if self.cache_retention_stripped:
            parts.append(
                f"sandbox cache_retention stripped from {len(self.cache_retention_stripped)}"
            )
        pod_moves: list[str] = []
        if self.pod_daily_hard_migrated:
            pod_moves.append("daily_hard")
        if self.pod_daily_warn_migrated:
            pod_moves.append("daily_warn")
        if self.pod_weekly_warn_migrated:
            pod_moves.append("weekly_warn")
        if self.pod_tier_downgrade_migrated:
            pod_moves.append("tier_downgrade")
        if self.pod_l2_breaker_migrated:
            pod_moves.append("l2_breaker")
        if pod_moves:
            parts.append("pod_defaults migrated: " + ", ".join(pod_moves))
        if self.pod_thresholds_stripped:
            parts.append("network.json thresholds stripped")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return "cost_caps_normalize: " + ", ".join(parts)


def run(shared_dir: Path, network_path: Path) -> MigrationResult:
    """Migrate legacy cost-cap values into better-engine-config and strip.

    Safe to call repeatedly; one-shot per server boot is the production
    cadence. Errors on individual bots are logged but do not abort the
    pass — every bot gets its own try/except so one corrupt sandbox file
    can't block the others.
    """
    result = MigrationResult()

    # Lazy imports — ``better_engine_config`` comes from the installed
    # evolve-analyzer package. Keep the dependency local so a tooling
    # check that imports this module outside that context doesn't trip
    # ImportError at collection time.
    try:
        from better_engine_config import (  # type: ignore[import]
            load as load_be_config,
            save as save_be_config,
        )
    except ImportError as exc:
        result.errors.append(f"better_engine_config import failed: {exc}")
        return result

    from ..config import load_network, save_network
    from ..config_sandbox.overrides import (
        delete_override,
        read_bot_overrides,
    )

    try:
        network = load_network(network_path)
    except Exception as exc:
        result.errors.append(f"network.json load failed: {exc}")
        return result

    bots_cfg: dict[str, Any] = network.get("bots") or {}
    if not isinstance(bots_cfg, dict):
        result.errors.append("network.json::bots is not a dict; skipping migration")
        return result

    try:
        be_config = load_be_config(shared_dir)
    except Exception as exc:
        result.errors.append(f"better-engine-config load failed: {exc}")
        return result

    network_changed = False
    be_changed = False

    for bot_id, bot_entry in list(bots_cfg.items()):
        result.bots_inspected += 1
        if not isinstance(bot_entry, dict):
            continue
        try:
            # ── Daily hard cap: network.json → BE config ──────────────────
            legacy_cap = bot_entry.get("daily_cap_usd")
            if legacy_cap is not None:
                try:
                    legacy_val = float(legacy_cap)
                except (TypeError, ValueError):
                    legacy_val = None
                # Only copy if BE config doesn't already carry a value
                # (operator's most recent write wins).
                if (
                    legacy_val is not None
                    and legacy_val > 0
                    and _be_daily_hard_cap_unset(be_config, bot_id)
                ):
                    be_config.set_per_bot_daily_hard_usd(bot_id, legacy_val)
                    be_changed = True
                    result.daily_hard_cap_migrated.append(bot_id)
                # Strip the legacy key regardless — canonical store now owns
                # this field, and leaving the legacy key risks drift.
                del bot_entry["daily_cap_usd"]
                network_changed = True
                result.daily_hard_cap_stripped.append(bot_id)

            # ── Per-session cap + cache TTL: sandbox overrides → BE config ─
            try:
                bot_overrides = read_bot_overrides(shared_dir, bot_id)
            except Exception as exc:
                _log.warning(
                    "cost_caps_normalize: read_bot_overrides(%s) failed: %s; "
                    "skipping sandbox→BE migration for this bot",
                    bot_id, exc,
                )
                bot_overrides = None

            if bot_overrides is not None:
                # Session cap.
                sess_entry = bot_overrides.overrides.get(_SANDBOX_SESSION_CAP_KEY)
                if sess_entry is not None:
                    try:
                        sess_val = float(sess_entry.value)
                    except (TypeError, ValueError):
                        sess_val = None
                    if (
                        sess_val is not None
                        and sess_val > 0
                        and be_config.per_bot_session_cost_cap_usd(bot_id) is None
                    ):
                        be_config.set_per_bot_session_cost_cap_usd(bot_id, sess_val)
                        be_changed = True
                        result.session_cap_migrated.append(bot_id)
                    try:
                        delete_override(shared_dir, bot_id, _SANDBOX_SESSION_CAP_KEY)
                        result.session_cap_stripped.append(bot_id)
                    except Exception as exc:
                        result.errors.append(
                            f"{bot_id}: delete_override(session_cap) failed: {exc}"
                        )

                # Cache retention.
                cache_entry = bot_overrides.overrides.get(_SANDBOX_CACHE_RETENTION_KEY)
                if cache_entry is not None:
                    cache_val = cache_entry.value if isinstance(cache_entry.value, str) else None
                    if (
                        cache_val in ("short", "long")
                        and be_config.per_bot_cache_retention(bot_id) is None
                    ):
                        be_config.set_per_bot_cache_retention(bot_id, cache_val)
                        be_changed = True
                        result.cache_retention_migrated.append(bot_id)
                    try:
                        delete_override(shared_dir, bot_id, _SANDBOX_CACHE_RETENTION_KEY)
                        result.cache_retention_stripped.append(bot_id)
                    except Exception as exc:
                        result.errors.append(
                            f"{bot_id}: delete_override(cache_retention) failed: {exc}"
                        )
        except Exception as exc:
            result.errors.append(f"{bot_id}: unexpected error: {exc}")

    # ── Phase 8: pod-wide thresholds (network.json::thresholds.*) ─────────
    # Migrate the legacy pod-level cost knobs into BE config pod_defaults.
    # Mapping (per spec internal/spec-cost-caps-2026-06-05.md §"Migration"):
    #   thresholds.dailySpendCapUsd      → pod_defaults.budget.per_bot_daily_hard_usd
    #   thresholds.dailySpendAlertUsd    → pod_defaults.budget.per_bot_daily_warn_usd
    #   thresholds.weeklySpendAlertUsd   → pod_defaults.budget.pod_weekly_warn_usd
    #   thresholds.spendCapAction:
    #     "downgrade-tier" → copy daily_hard_usd value to tier_downgrade_usd
    #     "suspend-bot"    → copy to pod_defaults.budget.l2_breaker_usd
    #     "pause-crons"    → drop (covered by L1 in new spec)
    #     "alert-only"     → no-op (alert fires from daily_warn_usd)
    #
    # Stripping is conditional: keep the legacy thresholds key when there
    # are non-cost fields under it (other migrations might add to it).
    # If only cost fields are present, remove the whole key after migrating.
    thresholds = network.get("thresholds")
    if isinstance(thresholds, dict):
        legacy_daily_hard = _pos_float(thresholds.get("dailySpendCapUsd"))
        legacy_daily_warn = _pos_float(thresholds.get("dailySpendAlertUsd"))
        legacy_weekly_warn = _pos_float(thresholds.get("weeklySpendAlertUsd"))
        spend_cap_action = thresholds.get("spendCapAction")

        pod_budget = be_config.pod_defaults.setdefault("budget", {})

        if legacy_daily_hard is not None and pod_budget.get("per_bot_daily_hard_usd") in (None, 5.00):
            pod_budget["per_bot_daily_hard_usd"] = legacy_daily_hard
            be_changed = True
            result.pod_daily_hard_migrated = True

        if legacy_daily_warn is not None and pod_budget.get("per_bot_daily_warn_usd") in (None, 2.00):
            pod_budget["per_bot_daily_warn_usd"] = legacy_daily_warn
            be_changed = True
            result.pod_daily_warn_migrated = True

        if legacy_weekly_warn is not None and pod_budget.get("pod_weekly_warn_usd") is None:
            pod_budget["pod_weekly_warn_usd"] = legacy_weekly_warn
            be_changed = True
            result.pod_weekly_warn_migrated = True

        # spendCapAction mapping. Only fires when daily_hard is set
        # (the action's threshold is the daily_hard value).
        if legacy_daily_hard is not None and isinstance(spend_cap_action, str):
            if spend_cap_action == "downgrade-tier" and pod_budget.get("tier_downgrade_usd") is None:
                pod_budget["tier_downgrade_usd"] = legacy_daily_hard
                be_changed = True
                result.pod_tier_downgrade_migrated = True
            elif spend_cap_action == "suspend-bot" and pod_budget.get("l2_breaker_usd") is None:
                pod_budget["l2_breaker_usd"] = legacy_daily_hard
                be_changed = True
                result.pod_l2_breaker_migrated = True
            # "pause-crons" and "alert-only" intentionally drop (no-op).

        # Strip the migrated cost keys. Keep the thresholds dict if it has
        # non-cost fields; remove it entirely if it'd be empty afterwards.
        cost_keys = {
            "dailySpendCapUsd", "dailySpendAlertUsd",
            "weeklySpendAlertUsd", "spendCapAction",
        }
        stripped_any = False
        for k in list(thresholds.keys()):
            if k in cost_keys:
                del thresholds[k]
                stripped_any = True
        if stripped_any:
            result.pod_thresholds_stripped = True
            network_changed = True
            # Only delete the thresholds key when it becomes empty —
            # the wider key may carry burst / velocity-forecast fields
            # owned by other code.
            if not thresholds:
                del network["thresholds"]

    # Persist changes — both files via their normal atomic-write paths.
    if be_changed:
        try:
            save_be_config(be_config, shared_dir)
        except Exception as exc:
            result.errors.append(f"better-engine-config save failed: {exc}")

    if network_changed:
        try:
            save_network(network, network_path)
        except Exception as exc:
            result.errors.append(f"network.json save failed: {exc}")

    return result


def _pos_float(v):
    """Return ``v`` as a positive float, or None if unset / non-positive."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _be_daily_hard_cap_unset(be_config: Any, bot_id: str) -> bool:
    """True iff BE config has no explicit per-bot daily_hard override.

    Reads the raw bot dict (not the resolved value) so the pod default
    doesn't mask an absent override. Mirrors what the bot-setup GET
    endpoint surfaces as ``daily_hard_usd: null``.
    """
    bot = (be_config.bots.get(bot_id) or {}) if hasattr(be_config, "bots") else {}
    budget = bot.get("budget") or {}
    return budget.get("per_bot_daily_hard_usd") is None


def _cli_entrypoint() -> int:
    """Ad-hoc CLI: ``python3 -m evolve_admin.migrations.cost_caps_normalize``.

    Reads ``EVOLVE_SHARED_DIR`` env var (default ``/Users/Shared/evolve``)
    and the canonical network.json path. Prints the summary line and a
    machine-readable JSON blob; exits 0 on success, 1 if errors.
    """
    import json as _json
    import os as _os

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    shared_dir = Path(_os.environ.get("EVOLVE_SHARED_DIR", "/Users/Shared/evolve"))
    _default_network = str(
        Path(get_profile().deploy_checkout_default) / "config" / "network.json"
    )
    network_path = Path(_os.environ.get("EVOLVE_NETWORK_PATH", _default_network))
    result = run(shared_dir, network_path)
    print(result.summary_line())
    print(_json.dumps({
        "bots_inspected": result.bots_inspected,
        "daily_hard_cap_migrated": result.daily_hard_cap_migrated,
        "daily_hard_cap_stripped": result.daily_hard_cap_stripped,
        "session_cap_migrated": result.session_cap_migrated,
        "session_cap_stripped": result.session_cap_stripped,
        "cache_retention_migrated": result.cache_retention_migrated,
        "cache_retention_stripped": result.cache_retention_stripped,
        "pod_daily_hard_migrated": result.pod_daily_hard_migrated,
        "pod_daily_warn_migrated": result.pod_daily_warn_migrated,
        "pod_weekly_warn_migrated": result.pod_weekly_warn_migrated,
        "pod_tier_downgrade_migrated": result.pod_tier_downgrade_migrated,
        "pod_l2_breaker_migrated": result.pod_l2_breaker_migrated,
        "pod_thresholds_stripped": result.pod_thresholds_stripped,
        "errors": result.errors,
    }, indent=2))
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(_cli_entrypoint())
