"""tests/test_checkpoint_hold_evidence.py — the "cap trip held the user
channel" health control (D-CS13; internal/decision-cost-single-turn-2026-09-18.md).

Pins the tri-state pass condition:

  * an interactive (user-source, cost > 0) turn landed after the trip and no
    holds.jsonl marker exists for that trip_id -> WARN "unknown" (never a
    silent PASS, never a FAIL nobody can act on — see the control's own
    docstring for why FAIL is never reachable).
  * same, but a marker exists -> PASS "ok".
  * no interactive turns after the trip at all -> PASS "not exercised".

From the 2026-09-17 incident: a trip's checkpoint read `pending` while
$4.15 was spent on the operator's DM in the next seven minutes, and nothing
on disk said whether the plugin live at that minute carried the hold.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ADMIN = Path(__file__).resolve().parents[1]
_ANALYZER = Path(__file__).resolve().parents[2] / "analyzer"
for _p in (_ADMIN, _ANALYZER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve_admin import health  # noqa: E402
from breakers import store  # noqa: E402

FIXED_NOW = datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)
TRIPPED_AT = datetime(2026, 9, 17, 19, 52, 47, tzinfo=timezone.utc)


def _arm_checkpoint_trip(shared_dir: Path, bot_id: str = "team_bot_a") -> store.BreakerRecord:
    return store.trip(
        shared_dir=shared_dir, scope=bot_id, breaker_type="cost",
        duration=timedelta(hours=24), initiated_by="auto:spend_alert",
        reason="per-bot daily cap exceeded", checkpoint="pending",
        now=TRIPPED_AT,
    )


def _interactive_turn(minutes_after_trip: int, cost: float = 0.83) -> dict:
    ts = TRIPPED_AT + timedelta(minutes=minutes_after_trip)
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "source": "user", "channel": "slack", "cost": cost,
        "model": "anthropic/claude-sonnet-4-5",
    }


def _run(shared_dir: Path, turns: list[dict]) -> list[health.CheckResult]:
    report = health.HealthReport()
    health._check_cost_checkpoint_hold_evidence(
        report, shared_dir, now=FIXED_NOW,
        read_turns_fn=lambda *a, **kw: turns,
    )
    return [c for c in report.checks if c.category == "cost_checkpoint_hold"]


def test_interactive_spend_with_no_marker_is_unknown(tmp_path: Path) -> None:
    _arm_checkpoint_trip(tmp_path)
    checks = _run(tmp_path, [_interactive_turn(7)])
    assert len(checks) == 1
    # WARN is this control's "unknown" — never a silent PASS, never a FAIL
    # nobody can act on (see the control's own docstring for why).
    assert checks[0].status == health.WARN
    assert "unknown" in checks[0].detail.lower()


def test_interactive_spend_with_a_marker_is_ok(tmp_path: Path) -> None:
    rec = _arm_checkpoint_trip(tmp_path)
    p = store.holds_log_path(tmp_path, "team_bot_a")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "trip_id": rec.trip_id,
        "ts": (TRIPPED_AT + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
        "session": "slack:C123", "plugin_version": "0.1.0",
    }) + "\n")
    checks = _run(tmp_path, [_interactive_turn(7)])
    assert len(checks) == 1
    assert checks[0].status == health.PASS

def test_no_interactive_turns_is_ok_not_exercised(tmp_path: Path) -> None:
    _arm_checkpoint_trip(tmp_path)
    checks = _run(tmp_path, [])
    assert len(checks) == 1
    assert checks[0].status == health.PASS
    assert "not exercised" in checks[0].detail.lower()


def test_never_fails_even_with_spend_and_no_marker(tmp_path: Path) -> None:
    _arm_checkpoint_trip(tmp_path)
    checks = _run(tmp_path, [_interactive_turn(1), _interactive_turn(3)])
    assert all(c.status != health.FAIL for c in checks)


def test_names_the_bot_and_the_trip_id(tmp_path: Path) -> None:
    rec = _arm_checkpoint_trip(tmp_path)
    checks = _run(tmp_path, [_interactive_turn(2)])
    assert checks[0].name == f"team_bot_a:{rec.trip_id[:8]}"


def test_trip_without_a_checkpoint_is_not_evaluated(tmp_path: Path) -> None:
    # spendCapAction != "checkpoint" (or a manual `breaker trip`) — no hold
    # to evidence, so this control has nothing to say about it.
    store.trip(
        shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
        duration=timedelta(hours=24), initiated_by="auto:spend_alert",
        reason="downgrade-mode trip", now=TRIPPED_AT,
    )
    checks = _run(tmp_path, [_interactive_turn(2)])
    assert checks == []


def test_pod_wide_trip_is_not_evaluated(tmp_path: Path) -> None:
    store.trip(
        shared_dir=tmp_path, scope=store.POD_SCOPE, breaker_type="cost",
        duration=timedelta(hours=24), initiated_by="admin:pod_admin",
        reason="pod-wide pause", checkpoint="pending", now=TRIPPED_AT,
    )
    checks = _run(tmp_path, [_interactive_turn(2)])
    assert checks == []


def test_a_cleared_trip_still_scores_from_the_audit_log(tmp_path: Path) -> None:
    """The trip's cost.json is gone by the time the operator resets it —
    the audit log is the only remaining record for the health control to
    read, so it must still be able to score it."""
    rec = _arm_checkpoint_trip(tmp_path)
    store.reset(
        shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
        initiated_by="admin:pod_admin", now=TRIPPED_AT + timedelta(minutes=30),
    )
    assert store.read_trip(tmp_path, "team_bot_a", "cost") is None  # gone
    checks = _run(tmp_path, [_interactive_turn(7)])
    assert len(checks) == 1
    assert checks[0].name == f"team_bot_a:{rec.trip_id[:8]}"
    assert checks[0].status == health.WARN


def test_a_trip_older_than_the_lookback_window_is_skipped(tmp_path: Path) -> None:
    _arm_checkpoint_trip(tmp_path)  # tripped 2026-09-17
    far_future = FIXED_NOW + timedelta(days=30)
    checks = health.HealthReport()
    health._check_cost_checkpoint_hold_evidence(
        checks, tmp_path, now=far_future,
        read_turns_fn=lambda *a, **kw: [_interactive_turn(7)],
    )
    assert [c for c in checks.checks if c.category == "cost_checkpoint_hold"] == []


# ── Control-registry fixtures (tools/control-registry: every enforced control
# carries a known_good / known_bad pair on ONE axis, replayed by a real test).
_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "controls" / "health__check_cost_checkpoint_hold_evidence"


def _replay_fixture(tmp_path: Path, name: str) -> health.CheckResult:
    fx = json.loads((_FIXTURES / name).read_text())
    rec = _arm_checkpoint_trip(tmp_path, fx["bot_id"])
    if fx["hold_marker"]:
        p = store.holds_log_path(tmp_path, fx["bot_id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "trip_id": rec.trip_id,
            "ts": (TRIPPED_AT + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            "session": "slack:C123", "plugin_version": "0.1.0",
        }) + "\n")
    turns = [_interactive_turn(m) for m in fx["interactive_turns_minutes_after_trip"]]
    checks = _run(tmp_path, turns)
    assert len(checks) == 1
    assert checks[0].category == fx["expect"]["category"]
    return checks[0]


def test_known_good_fixture_passes(tmp_path: Path) -> None:
    c = _replay_fixture(tmp_path, "known_good.json")
    assert c.status == health.PASS


def test_known_bad_fixture_warns_unknown(tmp_path: Path) -> None:
    c = _replay_fixture(tmp_path, "known_bad.json")
    assert c.status == health.WARN
    assert "unknown" in c.detail.lower()
