"""Tests for the /api/breakers Flask routes (Phase 4b).

Exercises the three new endpoints via Flask's test_client:
  - GET  /api/breakers              — list + audit
  - POST /api/breakers/trip         — trip
  - POST /api/breakers/reset        — reset

Plus the /api/status augmentation that surfaces per-bot
``active_breakers`` and a top-level ``pod_breakers`` list (the data
the dashboard renders the pill + modal from).

``breakers_enforce`` is monkeypatched so tests never actually invoke
launchctl. Mirrors the mock pattern from test_cli_breaker.py.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest


_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))


from evolve_admin import breakers_enforce  # noqa: E402
from evolve_admin.web.server import create_app  # noqa: E402
from breakers import store as breakers_store  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    """Write a tiny network.json with two bots into tmp_path."""
    path = tmp_path / "network.json"
    path.write_text(json.dumps({
        "primary": "team_bot_a",
        "members": ["team_bot_a", "security_bot"],
        "bots": {
            "team_bot_a": {"user": "team_bot_a"},
            "security_bot": {"user": "security_bot"},
        },
        "sharedDir": str(tmp_path / "shared"),
    }))
    (tmp_path / "shared").mkdir()
    return path


@pytest.fixture
def shared_dir(network_path: Path) -> Path:
    return network_path.parent / "shared"


@pytest.fixture(autouse=True)
def stub_enforce(monkeypatch: pytest.MonkeyPatch):
    """Stub breakers_enforce so we never actually call launchctl."""

    def _ok(*, scope, breaker_type, network, dry_run=False, **_kwargs):
        from evolve_admin.recovery import PerBotResult
        bots = (network or {}).get("bots") or {}
        per_bot = []
        if breaker_type == "full":
            target_ids = list(bots.keys()) if scope == "pod" else [scope]
            per_bot = [
                PerBotResult(
                    bot_id=b, label=f"ai.openclaw.{b}-gateway",
                    ok=True, rc=0, stdout="", stderr="", elapsed_ms=1,
                ) for b in target_ids
            ]
        return breakers_enforce.EnforceResult(
            action="trip", scope=scope, breaker_type=breaker_type,
            ok=True, no_op=(breaker_type == "cost"),
            no_op_reason="stubbed for tests",
            per_bot=per_bot, dry_run=dry_run, elapsed_ms=0,
        )

    monkeypatch.setattr(breakers_enforce, "enforce_trip", _ok)
    monkeypatch.setattr(breakers_enforce, "enforce_reset", _ok)


@pytest.fixture
def client(network_path: Path):
    app = create_app(network_path)
    app.testing = True
    return app.test_client()


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/breakers
# ─────────────────────────────────────────────────────────────────────────────


class TestList:
    def test_empty_returns_ok_with_no_trips(self, client) -> None:
        r = client.get("/api/breakers")
        assert r.status_code == 200
        data = r.get_json()
        assert data == {
            "ok": True, "active_count": 0, "trips": [], "audit": [],
        }

    def test_lists_active_trips(self, client, shared_dir: Path) -> None:
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="test", reason="alpha",
        )
        breakers_store.trip(
            shared_dir=shared_dir, scope="pod", breaker_type="full",
            duration=None, initiated_by="test", reason="bravo",
        )
        r = client.get("/api/breakers")
        data = r.get_json()
        assert data["ok"] is True
        assert data["active_count"] == 2
        scopes = sorted(t["scope"] for t in data["trips"])
        assert scopes == ["pod", "team_bot_a"]
        # Audit log records both trips.
        actions = [e.get("action") for e in data["audit"]]
        assert actions.count("trip") == 2

    def test_include_expired_query_flag(self, client, shared_dir: Path) -> None:
        # Pre-trip an already-expired entry.
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(seconds=-1), initiated_by="test", reason="x",
        )
        # Default: filters expired.
        d = client.get("/api/breakers").get_json()
        assert d["active_count"] == 0
        assert d["trips"] == []
        # With flag: includes.
        d2 = client.get("/api/breakers?include_expired=1").get_json()
        assert len(d2["trips"]) == 1
        assert d2["trips"][0]["expired"] is True
        assert d2["active_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/breakers/trip
# ─────────────────────────────────────────────────────────────────────────────


class TestTrip:
    def test_trip_requires_scope_and_type(self, client) -> None:
        r = client.post("/api/breakers/trip", json={})
        assert r.status_code == 400
        assert "required" in r.get_json()["error"]

    def test_trip_cost_writes_state(self, client, shared_dir: Path) -> None:
        r = client.post("/api/breakers/trip", json={
            "scope": "team_bot_a", "type": "cost",
            "duration": "1h", "reason": "spike",
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["trip"]["bot_id"] == "team_bot_a"
        # Cost trip is a no-op at the enforce layer until Phase 3b's plugin runs.
        assert data["enforce"]["no_op"] is True
        # State persisted.
        rec = breakers_store.read_trip(shared_dir, "team_bot_a", "cost")
        assert rec is not None

    def test_trip_full_per_bot_returns_per_bot_results(
        self, client, shared_dir: Path,
    ) -> None:
        r = client.post("/api/breakers/trip", json={
            "scope": "security_bot", "type": "full",
            "duration": "24h", "reason": "halt",
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["enforce"]["no_op"] is False
        per_bot = data["enforce"]["per_bot"]
        assert len(per_bot) == 1 and per_bot[0]["bot_id"] == "security_bot"

    def test_trip_full_pod_affects_every_bot(
        self, client, shared_dir: Path,
    ) -> None:
        r = client.post("/api/breakers/trip", json={
            "scope": "pod", "type": "full",
            "duration": "1h", "reason": "panic",
        })
        assert r.status_code == 200
        data = r.get_json()
        per_bot_ids = sorted(p["bot_id"] for p in data["enforce"]["per_bot"])
        assert per_bot_ids == ["security_bot", "team_bot_a"]

    def test_trip_indefinite_duration(self, client, shared_dir: Path) -> None:
        r = client.post("/api/breakers/trip", json={
            "scope": "team_bot_a", "type": "cost",
            "duration": "indefinite", "reason": "x",
        })
        assert r.status_code == 200
        rec = breakers_store.read_trip(shared_dir, "team_bot_a", "cost")
        assert rec.expires_at is None

    def test_trip_bad_duration_rejected(self, client) -> None:
        r = client.post("/api/breakers/trip", json={
            "scope": "team_bot_a", "type": "cost",
            "duration": "garbage", "reason": "x",
        })
        assert r.status_code == 400
        assert "duration" in r.get_json()["error"].lower()

    def test_trip_invalid_type_rejected(self, client) -> None:
        r = client.post("/api/breakers/trip", json={
            "scope": "team_bot_a", "type": "security", "reason": "x",
        })
        assert r.status_code == 400

    def test_trip_invalid_scope_chars_rejected(self, client) -> None:
        r = client.post("/api/breakers/trip", json={
            "scope": "team_bot_a/cost", "type": "cost", "reason": "x",
        })
        assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/breakers/reset
# ─────────────────────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_when_no_trip_is_ok_no_op(
        self, client,
    ) -> None:
        r = client.post("/api/breakers/reset", json={
            "scope": "team_bot_a", "type": "cost",
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["was_tripped"] is False
        assert data["reset"] is None

    def test_reset_clears_existing_trip(
        self, client, shared_dir: Path,
    ) -> None:
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="test", reason="x",
        )
        r = client.post("/api/breakers/reset", json={
            "scope": "team_bot_a", "type": "cost",
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data["was_tripped"] is True
        assert data["reset"]["bot_id"] == "team_bot_a"
        # State removed.
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is None

    def test_reset_requires_scope_and_type(self, client) -> None:
        r = client.post("/api/breakers/reset", json={})
        assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# /api/status augmentation (the dashboard data source)
# ─────────────────────────────────────────────────────────────────────────────


class TestPartialSuccess:
    """When state persists but enforce fails, server must return HTTP 207
    with the ``trip``/``reset`` record in the body so the JS handler can
    differentiate ``state-written-but-launchctl-fell-over`` from
    ``nothing-happened``. Without this shape the dashboard would tell the
    operator "Trip failed" while the breaker is in fact persisted on disk
    — a confusing dead end during incident response.

    The fix lives in submitBreakerTrip / submitBreakerReset (index.html).
    These tests pin the API contract those handlers consume.
    """

    @pytest.fixture
    def failing_enforce(self, monkeypatch: pytest.MonkeyPatch):
        """Override the autouse stub: enforce returns ok=False with detail."""
        def _fail(*, scope, breaker_type, network, dry_run=False, **_kwargs):
            from evolve_admin.recovery import PerBotResult
            return breakers_enforce.EnforceResult(
                action="trip", scope=scope, breaker_type=breaker_type,
                ok=False, no_op=False, no_op_reason="",
                per_bot=[PerBotResult(
                    bot_id=scope, label=f"ai.openclaw.{scope}-gateway",
                    ok=False, rc=1, stdout="", stderr="simulated launchctl failure",
                    elapsed_ms=1,
                )],
                dry_run=dry_run, elapsed_ms=0,
            )
        monkeypatch.setattr(breakers_enforce, "enforce_trip", _fail)
        monkeypatch.setattr(breakers_enforce, "enforce_reset", _fail)

    def test_trip_returns_207_with_trip_body_when_enforce_fails(
        self, client, shared_dir: Path, failing_enforce,
    ) -> None:
        r = client.post("/api/breakers/trip", json={
            "scope": "team_bot_a", "type": "full",
            "duration": "1h", "reason": "x",
        })
        assert r.status_code == 207
        data = r.get_json()
        assert data["ok"] is False
        # Critical: the trip record is in the body so the JS can detect
        # partial success and avoid showing "Trip failed" + offering retry.
        assert data["trip"] is not None
        assert data["trip"]["bot_id"] == "team_bot_a"
        # Enforce result is also present with the underlying failure detail.
        assert data["enforce"]["ok"] is False
        # State actually persisted (operator must see the breaker tile pill).
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "full") is not None

    def test_reset_returns_207_with_reset_body_when_enforce_fails(
        self, client, shared_dir: Path, failing_enforce,
    ) -> None:
        # Pre-trip directly so the reset has something to clear.
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="full",
            duration=timedelta(hours=1), initiated_by="test", reason="x",
        )
        r = client.post("/api/breakers/reset", json={
            "scope": "team_bot_a", "type": "full",
        })
        assert r.status_code == 207
        data = r.get_json()
        assert data["ok"] is False
        assert data["was_tripped"] is True
        # Reset record present → JS knows state was cleared even though
        # bootstrap (launchctl bring-up) failed.
        assert data["reset"] is not None
        assert data["enforce"]["ok"] is False
        # State actually cleared.
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "full") is None


class TestStatusOverlay:
    def test_status_includes_pod_breakers_when_empty(self, client) -> None:
        d = client.get("/api/status").get_json()
        assert "pod_breakers" in d
        assert d["pod_breakers"] == []

    def test_status_lists_pod_wide_trip(
        self, client, shared_dir: Path,
    ) -> None:
        breakers_store.trip(
            shared_dir=shared_dir, scope="pod", breaker_type="full",
            duration=timedelta(hours=1), initiated_by="test", reason="panic",
        )
        d = client.get("/api/status").get_json()
        assert len(d["pod_breakers"]) == 1
        assert d["pod_breakers"][0]["type"] == "full"
        assert d["pod_breakers"][0]["reason"] == "panic"

    def test_status_per_bot_breakers_when_bot_present(
        self, client, shared_dir: Path,
    ) -> None:
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="test", reason="x",
        )
        d = client.get("/api/status").get_json()
        for bot_id, bot in (d.get("bots") or {}).items():
            # Every rendered bot gets at least an empty list.
            assert "active_breakers" in bot, f"bot {bot_id} missing active_breakers"
        # team_bot_a specifically (if rendered) should have the trip.
        if "team_bot_a" in (d.get("bots") or {}):
            assert any(
                ab["type"] == "cost" for ab in d["bots"]["team_bot_a"]["active_breakers"]
            )

    def test_status_filters_expired_in_overlay(
        self, client, shared_dir: Path,
    ) -> None:
        breakers_store.trip(
            shared_dir=shared_dir, scope="pod", breaker_type="cost",
            duration=timedelta(seconds=-1), initiated_by="test", reason="x",
        )
        d = client.get("/api/status").get_json()
        # Expired pod trip should NOT appear in the overlay (list_active filters).
        assert d["pod_breakers"] == []
