"""Tests for the evo breaker tools (Phase 4a).

Covers action.{bot,pod}.{trip,reset}_breaker plus the pod_state.breakers
read companion. Each action tool has two surfaces under test — the
handler (real side effect via store + enforce) and the validate
(dry-run preflight that gates button rendering).

Tests mirror test_cli_breaker.py: monkeypatch breakers_enforce so we
don't actually call launchctl; verify state changes via the real
breakers.store. Network is stubbed via tmp network.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))


from evolve_admin import breakers_enforce  # noqa: E402
from evolve_admin.evo.tools import action_breakers, lookup  # noqa: E402
from breakers import store as breakers_store  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    """Write a tiny but valid network.json with two bots into tmp_path."""
    path = tmp_path / "network.json"
    path.write_text(json.dumps({
        "primary": "team_bot_a",
        "members": ["team_bot_a", "security_bot"],
        "bots": {
            "team_bot_a": {"user": "team_bot_a"},
            "security_bot": {"user": "security_bot"},
        },
        "shared_dir": str(tmp_path / "shared"),
    }))
    (tmp_path / "shared").mkdir()
    return path


@pytest.fixture
def shared_dir(network_path: Path) -> Path:
    return network_path.parent / "shared"


@pytest.fixture(autouse=True)
def stub_enforce(monkeypatch: pytest.MonkeyPatch):
    """Stub breakers_enforce so tests don't touch real launchctl."""

    def _ok(*, scope, breaker_type, network, dry_run=False, **_kwargs):
        from evolve_admin.recovery import PerBotResult
        bots = (network or {}).get("bots") or {}
        per_bot = []
        if breaker_type == "full":
            if scope == "pod":
                per_bot = [
                    PerBotResult(
                        bot_id=b, label=f"ai.openclaw.{b}-gateway",
                        ok=True, rc=0, stdout="", stderr="", elapsed_ms=1,
                    ) for b in bots
                ]
            else:
                per_bot = [
                    PerBotResult(
                        bot_id=scope, label=f"ai.openclaw.{scope}-gateway",
                        ok=True, rc=0, stdout="", stderr="", elapsed_ms=1,
                    )
                ]
        return breakers_enforce.EnforceResult(
            action="trip", scope=scope, breaker_type=breaker_type,
            ok=True, no_op=(breaker_type == "cost"),
            no_op_reason="stubbed for tests",
            per_bot=per_bot, dry_run=dry_run, elapsed_ms=0,
        )

    monkeypatch.setattr(breakers_enforce, "enforce_trip", _ok)
    monkeypatch.setattr(breakers_enforce, "enforce_reset", _ok)


# ─────────────────────────────────────────────────────────────────────────────
# Registry shape
# ─────────────────────────────────────────────────────────────────────────────


class TestRegistry:
    @pytest.mark.parametrize("name,tier", [
        ("action.bot.trip_breaker", "destructive"),
        ("action.bot.reset_breaker", "write_risky"),
        ("action.pod.trip_breaker", "destructive"),
        ("action.pod.reset_breaker", "write_risky"),
        ("pod_state.breakers", "read"),
    ])
    def test_tool_registered_with_expected_tier(self, name: str, tier: str) -> None:
        t = lookup(name)
        assert t is not None, f"tool {name!r} not registered"
        assert t.risk_tier.value == tier

    def test_destructive_trip_tools_require_confirm_in_schema(self) -> None:
        for name in ("action.bot.trip_breaker", "action.pod.trip_breaker"):
            t = lookup(name)
            assert "confirm" in t.input_schema["properties"]
            assert t.input_schema["properties"]["confirm"]["default"] is False

    def test_read_tool_has_no_validate(self) -> None:
        # Framework invariant: read-tier tools must NOT define validate.
        t = lookup("pod_state.breakers")
        assert t.validate is None


# ─────────────────────────────────────────────────────────────────────────────
# action.bot.trip_breaker
# ─────────────────────────────────────────────────────────────────────────────


class TestTripBot:
    def test_trip_without_confirm_rejected(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="x",
        )
        assert r["ok"] is False
        assert "confirm" in r["error"].lower()
        # And no state was written.
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is None

    def test_trip_unknown_bot_rejected(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="ghost",
            breaker_type="cost", reason="x", confirm=True,
        )
        assert r["ok"] is False
        assert "ghost" in r["error"]
        assert breakers_store.read_trip(shared_dir, "ghost", "cost") is None

    def test_trip_bad_type_rejected(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="security", reason="x", confirm=True,
        )
        assert r["ok"] is False

    def test_trip_pod_reserved_via_bot_handler(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="pod",
            breaker_type="full", reason="x", confirm=True,
        )
        assert r["ok"] is False
        assert "reserved scope" in r["error"]

    def test_trip_cost_writes_state(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="testing", confirm=True,
        )
        assert r["ok"] is True
        rec = breakers_store.read_trip(shared_dir, "team_bot_a", "cost")
        assert rec is not None
        assert rec.bot_id == "team_bot_a"
        assert rec.reason == "testing"

    def test_trip_full_writes_state_and_returns_per_bot(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="full", reason="halt", confirm=True,
            duration="1h",
        )
        assert r["ok"] is True
        assert r["enforce"]["no_op"] is False  # L2 has real bootout
        assert any(p["bot_id"] == "team_bot_a" for p in r["enforce"]["per_bot"])
        rec = breakers_store.read_trip(shared_dir, "team_bot_a", "full")
        assert rec is not None

    def test_trip_bad_duration_rejected(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="x", confirm=True,
            duration="garbage",
        )
        assert r["ok"] is False

    def test_trip_indefinite_duration_supported(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="x", confirm=True,
            duration="indefinite",
        )
        assert r["ok"] is True
        rec = breakers_store.read_trip(shared_dir, "team_bot_a", "cost")
        assert rec.expires_at is None


# ─────────────────────────────────────────────────────────────────────────────
# action.bot.trip_breaker — validate
# ─────────────────────────────────────────────────────────────────────────────


class TestTripBotValidate:
    def test_validate_blocks_without_confirm(self, network_path: Path) -> None:
        r = action_breakers._trip_bot_validate(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="x",
        )
        assert r["ok"] is False
        assert "confirm" in r["reason"].lower()

    def test_validate_blocks_unknown_bot(self, network_path: Path) -> None:
        r = action_breakers._trip_bot_validate(
            network_path=network_path, bot_id="ghost",
            breaker_type="cost", reason="x", confirm=True,
        )
        assert r["ok"] is False

    def test_validate_ok_with_context(self, network_path: Path) -> None:
        r = action_breakers._trip_bot_validate(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="x", confirm=True,
            duration="24h",
        )
        assert r["ok"] is True
        assert "breaker_type" in r["context"]
        assert "effect" in r["context"]
        assert "background activity" in r["context"]["effect"]

    def test_validate_full_describes_effect(self, network_path: Path) -> None:
        r = action_breakers._trip_bot_validate(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="full", reason="x", confirm=True,
        )
        assert "gateway" in r["context"]["effect"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# action.bot.reset_breaker
# ─────────────────────────────────────────────────────────────────────────────


class TestResetBot:
    def test_reset_when_not_tripped_is_noop_ok(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._reset_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost",
        )
        assert r["ok"] is True
        assert r["was_tripped"] is False

    def test_reset_clears_existing_trip(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        # Trip first
        action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="x", confirm=True,
        )
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is not None

        r = action_breakers._reset_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost",
        )
        assert r["ok"] is True
        assert r["was_tripped"] is True
        assert "prior_trip_id" in r
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is None

    def test_reset_unknown_bot_rejected(
        self, network_path: Path,
    ) -> None:
        r = action_breakers._reset_bot_handler(
            network_path=network_path, bot_id="ghost",
            breaker_type="cost",
        )
        assert r["ok"] is False
        assert "ghost" in r["error"]


# ─────────────────────────────────────────────────────────────────────────────
# action.pod.trip_breaker + reset_breaker
# ─────────────────────────────────────────────────────────────────────────────


class TestPodScope:
    def test_pod_trip_without_confirm_rejected(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_pod_handler(
            network_path=network_path,
            breaker_type="full", reason="x",
        )
        assert r["ok"] is False
        assert "confirm" in r["error"].lower()
        assert breakers_store.read_trip(shared_dir, "pod", "full") is None

    def test_pod_trip_full_writes_state_and_affects_every_bot(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        r = action_breakers._trip_pod_handler(
            network_path=network_path,
            breaker_type="full", reason="panic", confirm=True,
        )
        assert r["ok"] is True
        per_bot_ids = {p["bot_id"] for p in r["enforce"]["per_bot"]}
        assert per_bot_ids == {"team_bot_a", "security_bot"}
        rec = breakers_store.read_trip(shared_dir, "pod", "full")
        assert rec is not None

    def test_pod_reset_full_clears_state_and_restarts(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        action_breakers._trip_pod_handler(
            network_path=network_path,
            breaker_type="full", reason="panic", confirm=True,
        )
        r = action_breakers._reset_pod_handler(
            network_path=network_path,
            breaker_type="full",
        )
        assert r["ok"] is True
        assert r["was_tripped"] is True
        assert breakers_store.read_trip(shared_dir, "pod", "full") is None

    def test_pod_validate_surfaces_bot_count(
        self, network_path: Path,
    ) -> None:
        r = action_breakers._trip_pod_validate(
            network_path=network_path,
            breaker_type="full", reason="x", confirm=True,
        )
        assert r["ok"] is True
        assert r["context"]["estimated_bots_affected"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.breakers (read companion)
# ─────────────────────────────────────────────────────────────────────────────


class TestBreakersState:
    def test_empty_when_no_trips(self, network_path: Path) -> None:
        r = action_breakers._breakers_state_handler(network_path=network_path)
        assert r["ok"] is True
        assert r["active_count"] == 0
        assert r["trips"] == []

    def test_lists_per_bot_and_pod_trips(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="alpha", confirm=True,
        )
        action_breakers._trip_pod_handler(
            network_path=network_path,
            breaker_type="full", reason="bravo", confirm=True,
        )
        r = action_breakers._breakers_state_handler(network_path=network_path)
        assert r["ok"] is True
        assert r["active_count"] == 2
        scopes = sorted(t["scope"] for t in r["trips"])
        assert scopes == ["pod", "team_bot_a"]
        # Each entry carries the expected fields.
        for t in r["trips"]:
            assert "trip_id" in t
            assert "tripped_at" in t
            assert "reason" in t
            assert t["expired"] is False

    def test_include_expired_flag(
        self, network_path: Path, shared_dir: Path,
    ) -> None:
        """Verify the flag toggles list_all vs list_active."""
        # Write a directly-expired record via the store (skip the handler
        # so we can control expires_at precisely).
        from datetime import timedelta
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(seconds=-1), initiated_by="test",
            reason="already-expired",
        )
        # Default: excludes expired.
        r_default = action_breakers._breakers_state_handler(
            network_path=network_path,
        )
        assert r_default["active_count"] == 0
        assert r_default["trips"] == []
        # include_expired=True: includes it, marked expired.
        r_all = action_breakers._breakers_state_handler(
            network_path=network_path, include_expired=True,
        )
        assert len(r_all["trips"]) == 1
        assert r_all["trips"][0]["expired"] is True
        assert r_all["active_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Verify_via pointers
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifyVia:
    def test_trip_handlers_point_at_pod_state_breakers(
        self, network_path: Path,
    ) -> None:
        r = action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="x", confirm=True,
        )
        assert r["verify_via"]["tool"] == "pod_state.breakers"

        r = action_breakers._trip_pod_handler(
            network_path=network_path,
            breaker_type="cost", reason="x", confirm=True,
        )
        assert r["verify_via"]["tool"] == "pod_state.breakers"

    def test_reset_handlers_point_at_pod_state_breakers(
        self, network_path: Path,
    ) -> None:
        # Trip first
        action_breakers._trip_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost", reason="x", confirm=True,
        )
        r = action_breakers._reset_bot_handler(
            network_path=network_path, bot_id="team_bot_a",
            breaker_type="cost",
        )
        assert r.get("verify_via", {}).get("tool") == "pod_state.breakers"
