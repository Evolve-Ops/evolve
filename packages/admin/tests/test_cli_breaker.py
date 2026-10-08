"""Tests for the ``evolve-admin breaker`` CLI subcommand group.

The CLI is a thin wrapper around breakers.store; this suite exercises:
  - argument parsing (scope/type validation, duration parsing)
  - happy paths (trip → status → reset)
  - JSON output shape
  - exit codes on rejection
  - audit log integration

It does not re-test the store's internal semantics — those live in
packages/analyzer/breakers/tests/test_store.py. The CLI tests stub
``_breakers_shared_dir`` to point at a tmp_path so we don't touch
/Users/Shared/evolve.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner


# Add packages/analyzer to sys.path so `breakers.store` imports work.
_ANALYZER = Path(__file__).resolve().parent.parent.parent / "analyzer"
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

from evolve_admin import breakers_cli, breakers_enforce, cli  # noqa: E402
from breakers import store as breakers_store  # noqa: E402


@pytest.fixture
def shared_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect shared-dir, network, and enforce to test-safe stubs.

    The CLI now calls into breakers_enforce after writing state, which
    in turn calls real recovery.launchctl primitives. These tests
    cover CLI argument parsing and state-store integration only; they
    stub enforce to return success without touching launchctl. The
    enforce module's own behavior is exercised in
    test_breakers_enforce.py.
    """
    # The breaker group lives in breakers_cli (moved out of the capped
    # cli.py by the §5.2 arming PR) — patch that namespace.
    monkeypatch.setattr(breakers_cli, "_breakers_shared_dir", lambda ctx: tmp_path)
    monkeypatch.setattr(
        breakers_cli, "_shared_dir_from_network", lambda network: tmp_path,
    )
    # Stub network loading — return a minimal valid network dict.
    monkeypatch.setattr(breakers_cli, "load_network", lambda *_args, **_kw: {
        "primary": None, "members": [], "bots": {},
    })

    def _ok_enforce(*, scope, breaker_type, network, dry_run=False, **_kwargs):
        return breakers_enforce.EnforceResult(
            action="trip", scope=scope, breaker_type=breaker_type,
            ok=True, no_op=(breaker_type == "cost"),
            no_op_reason="stubbed in CLI test",
            dry_run=dry_run, elapsed_ms=0,
        )

    def _ok_reset(*, scope, breaker_type, network, dry_run=False, **_kwargs):
        return breakers_enforce.EnforceResult(
            action="reset", scope=scope, breaker_type=breaker_type,
            ok=True, no_op=(breaker_type == "cost"),
            no_op_reason="stubbed in CLI test",
            dry_run=dry_run, elapsed_ms=0,
        )

    monkeypatch.setattr(breakers_enforce, "enforce_trip", _ok_enforce)
    monkeypatch.setattr(breakers_enforce, "enforce_reset", _ok_reset)
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _invoke(runner: CliRunner, *args: str):
    """Invoke `evolve-admin <args>` and return the result."""
    return runner.invoke(cli.main, list(args))


# ─────────────────────────────────────────────────────────────────────────────
# trip
# ─────────────────────────────────────────────────────────────────────────────


class TestTrip:
    def test_trip_writes_state(self, runner: CliRunner, shared_dir: Path) -> None:
        result = _invoke(runner, "breaker", "trip", "team_bot_a", "cost",
                         "--duration", "24h", "--reason", "testing")
        assert result.exit_code == 0, result.output
        # State file exists.
        rec = breakers_store.read_trip(shared_dir, "team_bot_a", "cost")
        assert rec is not None
        assert rec.bot_id == "team_bot_a"
        assert rec.type == "cost"
        assert rec.reason == "testing"

    def test_trip_default_duration_24h(self, runner: CliRunner, shared_dir: Path) -> None:
        result = _invoke(runner, "breaker", "trip", "team_bot_a", "cost")
        assert result.exit_code == 0
        rec = breakers_store.read_trip(shared_dir, "team_bot_a", "cost")
        assert rec.expires_at is not None  # 24h, not indefinite

    def test_trip_indefinite(self, runner: CliRunner, shared_dir: Path) -> None:
        result = _invoke(runner, "breaker", "trip", "security_bot", "full",
                         "--duration", "indefinite")
        assert result.exit_code == 0, result.output
        rec = breakers_store.read_trip(shared_dir, "security_bot", "full")
        assert rec.expires_at is None

    def test_trip_rejects_unknown_type(self, runner: CliRunner, shared_dir: Path) -> None:
        result = _invoke(runner, "breaker", "trip", "team_bot_a", "security")
        # click's Choice validation rejects this before our code runs.
        assert result.exit_code != 0
        assert "security" in result.output or "Invalid value" in result.output

    def test_trip_rejects_bad_duration(self, runner: CliRunner, shared_dir: Path) -> None:
        result = _invoke(runner, "breaker", "trip", "team_bot_a", "cost",
                         "--duration", "garbage")
        assert result.exit_code == 2
        assert "Bad --duration" in result.output

    def test_trip_rejects_invalid_scope_chars(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        result = _invoke(runner, "breaker", "trip", "team_bot_a/cost", "cost")
        assert result.exit_code == 2
        assert "Trip rejected" in result.output

    def test_trip_json_output(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        result = _invoke(runner, "breaker", "trip", "team_bot_a", "cost",
                         "--duration", "1h", "--json")
        assert result.exit_code == 0
        # JSON shape: {"trip": <record>, "enforce": <result>}.
        data = json.loads(result.output)
        assert "trip" in data and "enforce" in data
        assert data["trip"]["bot_id"] == "team_bot_a"
        assert data["trip"]["type"] == "cost"
        assert data["trip"]["state"] == "tripped"
        # L1 trips are no-ops in Phase 3a.
        assert data["enforce"]["no_op"] is True
        assert data["enforce"]["ok"] is True

    def test_trip_with_motivating_signals(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        result = _invoke(runner, "breaker", "trip", "team_bot_a", "cost",
                         "--motivating-signal", "sig-1",
                         "--motivating-signal", "sig-2",
                         "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["trip"]["motivating_signals"] == ["sig-1", "sig-2"]

    def test_trip_records_initiated_by(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        result = _invoke(runner, "breaker", "trip", "team_bot_a", "cost",
                         "--initiated-by", "admin:pod_admin")
        assert result.exit_code == 0
        rec = breakers_store.read_trip(shared_dir, "team_bot_a", "cost")
        assert rec.initiated_by == "admin:pod_admin"


# ─────────────────────────────────────────────────────────────────────────────
# reset
# ─────────────────────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_after_trip(self, runner: CliRunner, shared_dir: Path) -> None:
        _invoke(runner, "breaker", "trip", "team_bot_a", "cost", "--duration", "24h")
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is not None

        result = _invoke(runner, "breaker", "reset", "team_bot_a", "cost")
        assert result.exit_code == 0
        assert breakers_store.read_trip(shared_dir, "team_bot_a", "cost") is None

    def test_reset_noop_message_when_clear(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        result = _invoke(runner, "breaker", "reset", "team_bot_a", "cost")
        assert result.exit_code == 0
        assert "nothing to reset" in result.output

    def test_reset_json_shape(self, runner: CliRunner, shared_dir: Path) -> None:
        _invoke(runner, "breaker", "trip", "team_bot_a", "cost")
        result = _invoke(runner, "breaker", "reset", "team_bot_a", "cost", "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "reset" in data
        assert data["reset"]["bot_id"] == "team_bot_a"
        # Reset of an L1 breaker triggers a no-op enforce.
        assert data["enforce"] is not None
        assert data["enforce"]["no_op"] is True

    def test_reset_cost_runs_enforce_even_when_not_tripped(
        self, runner: CliRunner, shared_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 2026-07-31 incident class: the spend-cap enforcement flag can
        # be active with no breaker file (spend_alert writes the flag
        # directly), and enforce_reset is what clears it. The CLI must
        # therefore run cost enforcement even when store.reset found
        # nothing to reset.
        calls: list[tuple[str, str]] = []

        def _capture_reset(*, scope, breaker_type, network, dry_run=False, **_kw):
            calls.append((scope, breaker_type))
            return breakers_enforce.EnforceResult(
                action="reset", scope=scope, breaker_type=breaker_type,
                ok=True, no_op=False, dry_run=dry_run, elapsed_ms=0,
            )

        monkeypatch.setattr(breakers_enforce, "enforce_reset", _capture_reset)
        result = _invoke(runner, "breaker", "reset", "team_bot_a", "cost")
        assert result.exit_code == 0, result.output
        assert calls == [("team_bot_a", "cost")]
        assert "nothing to reset" in result.output

    def test_reset_full_skips_enforce_when_not_tripped(
        self, runner: CliRunner, shared_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # L2 keeps the old gating — bootstrapping a gateway that was
        # never bootout'd is not implied by "nothing to reset".
        calls: list[tuple[str, str]] = []

        def _capture_reset(*, scope, breaker_type, network, dry_run=False, **_kw):
            calls.append((scope, breaker_type))
            return breakers_enforce.EnforceResult(
                action="reset", scope=scope, breaker_type=breaker_type,
                ok=True, no_op=False, dry_run=dry_run, elapsed_ms=0,
            )

        monkeypatch.setattr(breakers_enforce, "enforce_reset", _capture_reset)
        result = _invoke(runner, "breaker", "reset", "team_bot_a", "full")
        assert result.exit_code == 0, result.output
        assert calls == []
        assert "nothing to reset" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# extend
# ─────────────────────────────────────────────────────────────────────────────


class TestExtend:
    def test_extend_after_trip(self, runner: CliRunner, shared_dir: Path) -> None:
        _invoke(runner, "breaker", "trip", "team_bot_a", "cost", "--duration", "1h")
        before = breakers_store.read_trip(shared_dir, "team_bot_a", "cost")

        result = _invoke(runner, "breaker", "extend", "team_bot_a", "cost", "--by", "2h")
        assert result.exit_code == 0
        after = breakers_store.read_trip(shared_dir, "team_bot_a", "cost")
        # New expiry is later than old expiry.
        assert after.expires_at > before.expires_at
        # trip_id preserved.
        assert after.trip_id == before.trip_id

    def test_extend_rejects_when_no_trip(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        result = _invoke(runner, "breaker", "extend", "team_bot_a", "cost", "--by", "1h")
        assert result.exit_code == 1
        assert "No active trip" in result.output

    def test_extend_rejects_indefinite_by(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        result = _invoke(runner, "breaker", "extend", "team_bot_a", "cost",
                         "--by", "indefinite")
        assert result.exit_code == 2
        assert "cannot be 'indefinite'" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# status
# ─────────────────────────────────────────────────────────────────────────────


class TestStatus:
    def test_status_empty(self, runner: CliRunner, shared_dir: Path) -> None:
        result = _invoke(runner, "breaker", "status")
        assert result.exit_code == 0
        assert "No active circuit breakers" in result.output

    def test_status_lists_active_trips(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        _invoke(runner, "breaker", "trip", "team_bot_a", "cost", "--duration", "24h",
                "--reason", "alpha")
        _invoke(runner, "breaker", "trip", "security_bot", "full", "--duration", "1h",
                "--reason", "bravo")

        result = _invoke(runner, "breaker", "status")
        assert result.exit_code == 0
        assert "team_bot_a/cost" in result.output
        assert "security_bot/full" in result.output
        assert "alpha" in result.output
        assert "bravo" in result.output

    def test_status_json_includes_trips_and_audit(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        _invoke(runner, "breaker", "trip", "team_bot_a", "cost", "--duration", "1h")
        result = _invoke(runner, "breaker", "status", "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "trips" in data
        assert "audit" in data
        assert len(data["trips"]) == 1
        # Audit log has the trip entry.
        assert any(e.get("action") == "trip" for e in data["audit"])

    def test_status_audit_days_zero_skips_audit(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        _invoke(runner, "breaker", "trip", "team_bot_a", "cost", "--duration", "1h")
        result = _invoke(runner, "breaker", "status", "--audit-days", "0", "--json")
        data = json.loads(result.output)
        assert data["audit"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Cross-command flow — trip → status → reset → status
# ─────────────────────────────────────────────────────────────────────────────


class TestRoundtrip:
    def test_trip_status_reset_status(
        self, runner: CliRunner, shared_dir: Path,
    ) -> None:
        # 1. trip
        r1 = _invoke(runner, "breaker", "trip", "team_bot_a", "cost", "--duration", "24h",
                     "--reason", "spike")
        assert r1.exit_code == 0

        # 2. status shows it
        r2 = _invoke(runner, "breaker", "status")
        assert "team_bot_a/cost" in r2.output
        assert "spike" in r2.output

        # 3. reset
        r3 = _invoke(runner, "breaker", "reset", "team_bot_a", "cost")
        assert r3.exit_code == 0

        # 4. status is clean (no active trip; audit history persists)
        r4 = _invoke(runner, "breaker", "status")
        assert "No active circuit breakers" in r4.output
        # Audit shows the trip + reset history.
        assert "trip" in r4.output and "reset" in r4.output

    def test_pod_scope_trip(self, runner: CliRunner, shared_dir: Path) -> None:
        result = _invoke(runner, "breaker", "trip", "pod", "full",
                         "--duration", "1h", "--reason", "panic")
        assert result.exit_code == 0
        rec = breakers_store.read_trip(shared_dir, "pod", "full")
        assert rec is not None
        assert rec.bot_id == "pod"
