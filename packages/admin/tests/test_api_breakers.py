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


# The autouse ``stub_enforce`` fixture replaces these; grab the real ones at
# import time so a test can opt back into the genuine implementation.
_REAL_ENFORCE_RESET = breakers_enforce.enforce_reset


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


class TestResetAtomicity:
    """2026-09-04 reset incident — the reset must not be able to clear
    the breaker file without running the bring-up half.

    Two independent failures produced that state:

      * ``store.reset`` raised ``PermissionError`` from its audit append
        AFTER unlinking the breaker file, so the route's ``except
        Exception`` returned before ``enforce_reset`` ever ran;
      * the route only reached ``enforce_reset`` when ``prior is not
        None``, so a retry short-circuited on ``ok:true,
        was_tripped:false`` and never re-attempted bring-up.

    The bot was then heartbeat-less and pinned to the fast rung with no
    breaker for any monitor or the UI to show.
    """

    def test_audit_append_failure_still_enforces(
        self, client, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The exact incident: audit append raises mid-reset."""
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="test", reason="x",
        )
        calls: list[dict] = []

        def _record(**kwargs):
            calls.append(kwargs)
            return breakers_enforce.EnforceResult(
                action="reset", scope=kwargs["scope"],
                breaker_type=kwargs["breaker_type"], ok=True,
            )

        monkeypatch.setattr(breakers_enforce, "enforce_reset", _record)

        real_open = Path.open

        def _deny_audit_open(self_path, *args, **kwargs):
            if self_path.suffix == ".jsonl" and "breakers" in self_path.parts:
                raise PermissionError(13, "Permission denied", str(self_path))
            return real_open(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", _deny_audit_open)

        r = client.post("/api/breakers/reset", json={
            "scope": "team_bot_a", "type": "cost",
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["was_tripped"] is True
        # Bring-up ran — this is the assertion the incident would fail.
        assert len(calls) == 1
        assert calls[0]["scope"] == "team_bot_a"
        # ...and the state clear still happened, so the operator is not
        # left staring at a breaker that will not clear.
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is None

    def test_enforce_failure_leaves_breaker_retryable(
        self, client, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A RAISING enforce must not consume the breaker file.

        Bring-up runs first, so nothing is cleared: the UI still shows an
        active breaker, the Reactivate button stays, and a retry actually
        re-attempts bring-up instead of short-circuiting on
        ``was_tripped:false``.
        """
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="test", reason="x",
        )

        def _boom(**_kwargs):
            raise RuntimeError("gateway kickstart blew up")

        monkeypatch.setattr(breakers_enforce, "enforce_reset", _boom)

        r = client.post("/api/breakers/reset", json={
            "scope": "team_bot_a", "type": "cost",
        })
        assert r.get_json()["ok"] is False
        # Deleted-but-unenforced is exactly what must not happen.
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is not None

        # Retry, this time with a working enforce: it reaches bring-up
        # AND clears the state.
        calls: list[dict] = []

        def _ok(**kwargs):
            calls.append(kwargs)
            return breakers_enforce.EnforceResult(
                action="reset", scope=kwargs["scope"],
                breaker_type=kwargs["breaker_type"], ok=True,
            )

        monkeypatch.setattr(breakers_enforce, "enforce_reset", _ok)
        r2 = client.post("/api/breakers/reset", json={
            "scope": "team_bot_a", "type": "cost",
        })
        assert r2.status_code == 200
        assert r2.get_json()["was_tripped"] is True
        assert len(calls) == 1
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is None

    def test_cost_reset_passes_shared_dir_to_enforce(
        self, client, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without shared_dir, enforce_reset("cost") returns ok=False /
        "shared_dir required for cost enforcement" and skips every L1
        bring-up action — the heartbeat stash, the exec-approval stash
        and today's spend-cap enforcement flag all stay as the trip left
        them. Silent half-recovery by a different route."""
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="test", reason="x",
        )
        calls: list[dict] = []

        def _record(**kwargs):
            calls.append(kwargs)
            return breakers_enforce.EnforceResult(
                action="reset", scope=kwargs["scope"],
                breaker_type=kwargs["breaker_type"], ok=True,
            )

        monkeypatch.setattr(breakers_enforce, "enforce_reset", _record)
        client.post("/api/breakers/reset", json={
            "scope": "team_bot_a", "type": "cost",
        })
        assert calls and calls[0].get("shared_dir") == shared_dir

    def test_no_trip_does_not_enforce(
        self, client, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing tripped → no bring-up. enforce_reset("cost") posts a
        "background work is back" message to the bot's user channel; a
        reset that cleared nothing must not send one."""
        calls: list[dict] = []

        def _record(**kwargs):
            calls.append(kwargs)
            return breakers_enforce.EnforceResult(
                action="reset", scope=kwargs["scope"],
                breaker_type=kwargs["breaker_type"], ok=True,
            )

        monkeypatch.setattr(breakers_enforce, "enforce_reset", _record)
        r = client.post("/api/breakers/reset", json={
            "scope": "team_bot_a", "type": "cost",
        })
        assert r.status_code == 200
        assert r.get_json()["was_tripped"] is False
        assert calls == []

    def test_bad_scope_still_rejected_when_nothing_tripped(
        self, client,
    ) -> None:
        """The no-op path still validates — a typo'd bot is a 400, not a
        cheerful ``was_tripped:false``."""
        r = client.post("/api/breakers/reset", json={
            "scope": "no/such/bot", "type": "cost",
        })
        assert r.status_code == 400


class TestResetDecommissionedBot:
    """A bot removed from network.json must still be resettable.

    ``enforce_reset`` raises ValueError for a scope it cannot resolve, and
    bring-up for a bot that no longer exists is impossible — permanently,
    not transiently. If that raise also blocks the state clear, the
    breaker file survives every retry and the operator has no way to get
    rid of it: heal's TTL reaper only reaps EXPIRED trips
    (``is_expired`` is False for an indefinite one), so an indefinite L1
    trip on a decommissioned bot has this route as its only path.

    That exact residue was seen live on 2026-07-31 (bot "ledger"); see
    ``heal._clear_defunct_breaker_residue``, which clears it for the same
    reason. This route must reach the same terminal state.
    """

    def test_reset_clears_breaker_for_bot_gone_from_network(
        self, client, shared_dir: Path,
    ) -> None:
        # "ledger" is deliberately absent from the network fixture — the
        # 2026-07-31 shape: a bot removed from the pod whose breakers/
        # dir survived. Indefinite trip (duration=None), so heal's TTL
        # reaper will never touch it either.
        breakers_store.trip(
            shared_dir=shared_dir, scope="ledger", breaker_type="cost",
            duration=None, initiated_by="test", reason="x",
        )
        with pytest.MonkeyPatch.context() as mp:
            # Real enforce_reset, so the ValueError and the "is this scope
            # still resolvable?" check are both the production ones.
            mp.setattr(breakers_enforce, "enforce_reset", _REAL_ENFORCE_RESET)
            r = client.post("/api/breakers/reset", json={
                "scope": "ledger", "type": "cost",
            })

        # Partial success, not a rejection: the operator is told bring-up
        # did not run, but the breaker is GONE rather than stuck forever.
        assert r.status_code == 207
        data = r.get_json()
        assert data["ok"] is False
        assert data["was_tripped"] is True
        assert data["reset"] is not None
        assert "not present in network.json" in data["error"]
        assert breakers_store.read_trip(shared_dir, "ledger", "cost") is None

        # And it stays gone: a second reset is the ordinary no-op, not a
        # rejection, so the operator is not stuck in a retry loop.
        r2 = client.post("/api/breakers/reset", json={
            "scope": "ledger", "type": "cost",
        })
        assert r2.status_code == 200
        assert r2.get_json()["was_tripped"] is False

    def test_live_bot_enforce_valueerror_keeps_breaker_retryable(
        self, client, shared_dir: Path,
    ) -> None:
        """The clear-anyway path is scoped to a scope that is actually
        gone. A bot still in network.json whose enforcement raises
        ValueError (an unresolvable breaker type, a momentarily empty
        network read) must keep its breaker, so a retry can still work
        — clearing there would be the original incident again."""
        breakers_store.trip(
            shared_dir=shared_dir, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="test", reason="x",
        )

        def _raise(**_kwargs):
            raise ValueError("something else went wrong")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(breakers_enforce, "enforce_reset", _raise)
            r = client.post("/api/breakers/reset", json={
                "scope": "team_bot_a", "type": "cost",
            })

        assert r.get_json()["ok"] is False
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is not None


class TestTripEnforcementArgs:
    """The web trip route must hand enforce_trip a shared_dir.

    Without it, ``enforce_trip("cost")`` short-circuits to ok=False /
    "shared_dir required for cost enforcement" and performs no L1 action
    — no heartbeat stash, no exec-approval passthrough, no
    "I've paused background work" message — while the breaker file is
    written and the operator is told the trip fired. The reset side then
    has no stash to restore, so the mismatch is invisible from both ends.
    """

    def test_trip_passes_shared_dir_to_enforce(
        self, client, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[dict] = []

        def _record(**kwargs):
            calls.append(kwargs)
            return breakers_enforce.EnforceResult(
                action="trip", scope=kwargs["scope"],
                breaker_type=kwargs["breaker_type"], ok=True,
            )

        monkeypatch.setattr(breakers_enforce, "enforce_trip", _record)
        r = client.post("/api/breakers/trip", json={
            "scope": "team_bot_a", "type": "cost",
            "duration": "1h", "reason": "x",
        })
        assert r.status_code == 200
        assert calls and calls[0].get("shared_dir") == shared_dir


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
