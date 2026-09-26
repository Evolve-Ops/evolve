"""Tests for breakers.suppression — the "don't fight the breaker" helper."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from breakers import store, suppression
from breakers.suppression import (
    SUPPRESSION_CATEGORIES,
    find_suppressing_breaker,
    get_active_breakers,
    is_config_change_suppressed,
    is_cost_suppressed,
    suppression_tag,
)


def _trip(
    shared_dir: Path, scope: str, breaker_type: str, *,
    duration_hours: int | None = 24,
) -> store.BreakerRecord:
    return store.trip(
        shared_dir=shared_dir,
        scope=scope,
        breaker_type=breaker_type,
        duration=timedelta(hours=duration_hours) if duration_hours else None,
        initiated_by="test",
        reason="test",
    )


# ─────────────────────────────────────────────────────────────────────────────
# get_active_breakers
# ─────────────────────────────────────────────────────────────────────────────


class TestGetActiveBreakers:
    def test_returns_empty_when_no_trips(self, tmp_path: Path) -> None:
        assert get_active_breakers(tmp_path, "team_bot_a") == []

    def test_returns_per_bot_trip(self, tmp_path: Path) -> None:
        _trip(tmp_path, "team_bot_a", "cost")
        active = get_active_breakers(tmp_path, "team_bot_a")
        assert len(active) == 1
        assert active[0].bot_id == "team_bot_a"
        assert active[0].type == "cost"

    def test_returns_pod_scope_trip_for_any_bot(self, tmp_path: Path) -> None:
        _trip(tmp_path, "pod", "full")
        active = get_active_breakers(tmp_path, "team_bot_a")
        assert len(active) == 1
        assert active[0].bot_id == "pod"

    def test_excludes_other_bots(self, tmp_path: Path) -> None:
        _trip(tmp_path, "security_bot", "cost")
        active = get_active_breakers(tmp_path, "team_bot_a")
        assert active == []

    def test_returns_both_per_bot_and_pod_scope(self, tmp_path: Path) -> None:
        _trip(tmp_path, "team_bot_a", "cost")
        _trip(tmp_path, "pod", "full")
        active = get_active_breakers(tmp_path, "team_bot_a")
        scopes = {r.bot_id for r in active}
        assert scopes == {"team_bot_a", "pod"}

    def test_fail_open_on_store_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(*a, **kw):
            raise IOError("disk gone")
        monkeypatch.setattr(suppression._store, "list_active", _boom)
        assert get_active_breakers(tmp_path, "team_bot_a") == []


# ─────────────────────────────────────────────────────────────────────────────
# find_suppressing_breaker — category routing
# ─────────────────────────────────────────────────────────────────────────────


class TestCostCategory:
    def test_cost_breaker_suppresses_cost(self, tmp_path: Path) -> None:
        _trip(tmp_path, "team_bot_a", "cost")
        rec = find_suppressing_breaker(tmp_path, "team_bot_a", category="cost")
        assert rec is not None
        assert rec.type == "cost"

    def test_full_breaker_also_suppresses_cost(self, tmp_path: Path) -> None:
        _trip(tmp_path, "team_bot_a", "full")
        rec = find_suppressing_breaker(tmp_path, "team_bot_a", category="cost")
        assert rec is not None
        assert rec.type == "full"

    def test_pod_cost_breaker_suppresses_per_bot_cost(self, tmp_path: Path) -> None:
        _trip(tmp_path, "pod", "cost")
        rec = find_suppressing_breaker(tmp_path, "team_bot_a", category="cost")
        assert rec is not None
        assert rec.bot_id == "pod"

    def test_no_trip_no_suppression(self, tmp_path: Path) -> None:
        assert find_suppressing_breaker(tmp_path, "team_bot_a", category="cost") is None

    def test_other_bot_trip_does_not_suppress(self, tmp_path: Path) -> None:
        _trip(tmp_path, "security_bot", "cost")
        assert find_suppressing_breaker(tmp_path, "team_bot_a", category="cost") is None


class TestConfigChangeCategory:
    def test_full_breaker_suppresses_config_change(self, tmp_path: Path) -> None:
        _trip(tmp_path, "team_bot_a", "full")
        rec = find_suppressing_breaker(tmp_path, "team_bot_a", category="config_change")
        assert rec is not None

    def test_cost_breaker_does_NOT_suppress_config_change(
        self, tmp_path: Path,
    ) -> None:
        # L1 trips leave the gateway up; config writes still work.
        _trip(tmp_path, "team_bot_a", "cost")
        assert (
            find_suppressing_breaker(tmp_path, "team_bot_a", category="config_change")
            is None
        )

    def test_pod_full_suppresses_config_change(self, tmp_path: Path) -> None:
        _trip(tmp_path, "pod", "full")
        rec = find_suppressing_breaker(tmp_path, "team_bot_a", category="config_change")
        assert rec is not None
        assert rec.bot_id == "pod"


class TestGatewayAlertCategory:
    def test_full_breaker_suppresses_gateway_alert(self, tmp_path: Path) -> None:
        _trip(tmp_path, "team_bot_a", "full")
        rec = find_suppressing_breaker(tmp_path, "team_bot_a", category="gateway_alert")
        assert rec is not None

    def test_cost_breaker_does_NOT_suppress_gateway_alert(
        self, tmp_path: Path,
    ) -> None:
        _trip(tmp_path, "team_bot_a", "cost")
        assert (
            find_suppressing_breaker(tmp_path, "team_bot_a", category="gateway_alert")
            is None
        )


class TestAutomationCategory:
    def test_cost_breaker_suppresses_automation(self, tmp_path: Path) -> None:
        _trip(tmp_path, "team_bot_a", "cost")
        rec = find_suppressing_breaker(tmp_path, "team_bot_a", category="automation")
        assert rec is not None

    def test_full_breaker_suppresses_automation(self, tmp_path: Path) -> None:
        _trip(tmp_path, "team_bot_a", "full")
        rec = find_suppressing_breaker(tmp_path, "team_bot_a", category="automation")
        assert rec is not None


class TestUnknownCategory:
    def test_unknown_category_returns_none(self, tmp_path: Path) -> None:
        # Fail-open: a typo'd category is safer as "no suppression"
        # (alerts fire as normal) than as "suppress everything".
        _trip(tmp_path, "team_bot_a", "full")
        assert (
            find_suppressing_breaker(tmp_path, "team_bot_a", category="bogus")
            is None
        )


# ─────────────────────────────────────────────────────────────────────────────
# Precedence — per-bot trip preferred over pod-wide when both apply
# ─────────────────────────────────────────────────────────────────────────────


class TestPrecedence:
    def test_per_bot_trip_returned_before_pod_scope(self, tmp_path: Path) -> None:
        per_bot = _trip(tmp_path, "team_bot_a", "cost")
        pod = _trip(tmp_path, "pod", "cost")
        rec = find_suppressing_breaker(tmp_path, "team_bot_a", category="cost")
        assert rec is not None
        # The per-bot trip wins so the caller's tag carries the
        # most-specific trip_id.
        assert rec.trip_id == per_bot.trip_id
        assert rec.trip_id != pod.trip_id


# ─────────────────────────────────────────────────────────────────────────────
# suppression_tag — output shape
# ─────────────────────────────────────────────────────────────────────────────


class TestSuppressionTag:
    def test_includes_trip_id_type_and_scope(self, tmp_path: Path) -> None:
        rec = _trip(tmp_path, "team_bot_a", "cost")
        tag = suppression_tag(rec)
        assert tag["suppressed_by_breaker"] == rec.trip_id
        assert tag["suppressed_breaker_type"] == "cost"
        assert tag["suppressed_breaker_scope"] == "team_bot_a"

    def test_pod_scope_recorded(self, tmp_path: Path) -> None:
        rec = _trip(tmp_path, "pod", "full")
        tag = suppression_tag(rec)
        assert tag["suppressed_breaker_scope"] == "pod"
        assert tag["suppressed_breaker_type"] == "full"


# ─────────────────────────────────────────────────────────────────────────────
# Convenience shortcuts
# ─────────────────────────────────────────────────────────────────────────────


class TestShortcuts:
    def test_is_cost_suppressed_fires_on_cost_trip(self, tmp_path: Path) -> None:
        _trip(tmp_path, "team_bot_a", "cost")
        assert is_cost_suppressed(tmp_path, "team_bot_a") is not None

    def test_is_cost_suppressed_none_when_clear(self, tmp_path: Path) -> None:
        assert is_cost_suppressed(tmp_path, "team_bot_a") is None

    def test_is_config_change_suppressed_fires_on_full_only(
        self, tmp_path: Path,
    ) -> None:
        _trip(tmp_path, "team_bot_a", "cost")
        assert is_config_change_suppressed(tmp_path, "team_bot_a") is None
        _trip(tmp_path, "team_bot_a", "full")
        assert is_config_change_suppressed(tmp_path, "team_bot_a") is not None


# ─────────────────────────────────────────────────────────────────────────────
# Exposed categories
# ─────────────────────────────────────────────────────────────────────────────


class TestExposedConstants:
    def test_categories_set_includes_expected(self) -> None:
        assert "cost" in SUPPRESSION_CATEGORIES
        assert "config_change" in SUPPRESSION_CATEGORIES
        assert "gateway_alert" in SUPPRESSION_CATEGORIES
        assert "automation" in SUPPRESSION_CATEGORIES
