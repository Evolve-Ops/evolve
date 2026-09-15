"""tests/test_sweep_stale_dispatches.py — Phase 3.5 stale-dispatch sweep tool.

Spec: internal/spec-take-this-on-evo-dispatch-2026-06-04.md §"Safety +
invariants" item 3.

Covers:
  - Fresh dispatches (under threshold) are left alone.
  - Stale dispatches (past threshold) plan a transition.
  - Dry-run (default) does NOT write — status stays ``dispatched``.
  - ``--apply`` transitions to ``failed_flagged`` and moves the file
    from ``pending/`` to ``archived/``.
  - Threshold is configurable via ``--threshold-hours``.
  - Reason string mentions ``"timed out"`` so it's grep-able in
    history.
  - Proposals without a dispatch_state are skipped (defensive).
  - Proposals with non-``dispatched`` status in pending/ are ignored.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, str(path))

from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import (  # noqa: E402
    find_proposal,
    proposal_path,
    write_proposal,
)
from schema.proposal import DispatchState  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402


def _load_sweep_module():
    """``packages/analyzer/tools/`` has no __init__.py — load the script
    directly so the test stays robust whether or not it gets one later."""
    script = _ANALYZER_DIR / "tools" / "sweep_stale_dispatches.py"
    spec = importlib.util.spec_from_file_location(
        "sweep_stale_dispatches", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sweep_stale_dispatches = _load_sweep_module()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


_NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)


def _seed_dispatched(
    shared_dir: Path,
    *,
    dispatched_ago: timedelta,
    target: str = "evo",
    message: str = "fix this",
):
    """Seed a pending → dispatched proposal with a chosen dispatched_at age."""
    p = make_investigation_proposal(
        audience="pod_operator", bot_id="team_bot_a", problem="x",
    )
    p.dispatch_target = target
    p.dispatch_message = message
    transition(p, "pending", actor="test", reason="seed")
    transition(p, "dispatched", actor="test", reason="seed-dispatched")
    p.dispatch_state = DispatchState(
        target=target,
        dispatched_at=(_NOW - dispatched_ago).isoformat(timespec="seconds"),
        message=message,
    )
    write_proposal(p, shared_dir)
    return p


def _run(shared_dir: Path, *extra: str) -> int:
    argv = ["--shared-dir", str(shared_dir), "--now", _NOW.isoformat()]
    argv.extend(extra)
    return sweep_stale_dispatches.main(argv)


def _status(shared_dir: Path, pid: str) -> tuple[str, str] | None:
    """Return (status, subdir) for the proposal or None if missing."""
    located = find_proposal(shared_dir, pid)
    if located is None:
        return None
    proposal, _, subdir = located
    return proposal.status, subdir


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_fresh_dispatch_is_left_alone(tmp_path, capsys):
    shared_dir = tmp_path / "evolve"
    (shared_dir / "proposals" / "pending").mkdir(parents=True)
    p = _seed_dispatched(shared_dir, dispatched_ago=timedelta(hours=1))

    rc = _run(shared_dir, "--apply")
    assert rc == 0

    captured = capsys.readouterr().out
    assert "No stale dispatches" in captured

    assert _status(shared_dir, p.id) == ("dispatched", "pending")


def test_stale_dispatch_plans_transition_dry_run(tmp_path, capsys):
    shared_dir = tmp_path / "evolve"
    (shared_dir / "proposals" / "pending").mkdir(parents=True)
    p = _seed_dispatched(shared_dir, dispatched_ago=timedelta(hours=25))

    # Default is dry-run — no --apply.
    rc = _run(shared_dir)
    assert rc == 0

    out = capsys.readouterr().out
    assert "Planned timeout of 1" in out
    assert "failed_flagged" in out
    assert "--apply not set" in out

    # Crucially, no write happened.
    assert _status(shared_dir, p.id) == ("dispatched", "pending")


def test_stale_dispatch_apply_transitions_and_moves_file(tmp_path):
    shared_dir = tmp_path / "evolve"
    (shared_dir / "proposals" / "pending").mkdir(parents=True)
    p = _seed_dispatched(shared_dir, dispatched_ago=timedelta(hours=25))

    rc = _run(shared_dir, "--apply")
    assert rc == 0

    located = find_proposal(shared_dir, p.id)
    assert located is not None
    proposal, _, subdir = located
    assert proposal.status == "failed_flagged"
    assert subdir == "archived"

    # File physically moved.
    assert not proposal_path(shared_dir, p.id, subdir="pending").exists()
    assert proposal_path(shared_dir, p.id, subdir="archived").exists()

    # History records the sweep with the actor we use.
    last = proposal.history[-1]
    assert last.to_status == "failed_flagged"
    assert last.actor == "stale_dispatch_sweep"
    assert "timed out" in last.reason


def test_threshold_hours_is_configurable(tmp_path):
    """A 2h-old dispatch is fresh at the default 24h but stale at 1h."""
    shared_dir = tmp_path / "evolve"
    (shared_dir / "proposals" / "pending").mkdir(parents=True)
    p = _seed_dispatched(shared_dir, dispatched_ago=timedelta(hours=2))

    rc = _run(shared_dir, "--apply")
    assert rc == 0
    assert _status(shared_dir, p.id) == ("dispatched", "pending")

    rc = _run(shared_dir, "--threshold-hours", "1", "--apply")
    assert rc == 0
    located = find_proposal(shared_dir, p.id)
    assert located is not None
    proposal, _, subdir = located
    assert proposal.status == "failed_flagged"
    assert subdir == "archived"


def test_reason_is_searchable_in_history(tmp_path):
    """The 'timed out' phrase must appear so log queries can find it."""
    shared_dir = tmp_path / "evolve"
    (shared_dir / "proposals" / "pending").mkdir(parents=True)
    p = _seed_dispatched(
        shared_dir, dispatched_ago=timedelta(hours=30), target="evo"
    )

    _run(shared_dir, "--apply")

    located = find_proposal(shared_dir, p.id)
    assert located is not None
    proposal, _, _ = located
    last = proposal.history[-1]
    assert "timed out" in last.reason
    assert "evo" in last.reason  # target named in the reason


def test_dispatched_proposal_without_state_is_skipped(tmp_path, capsys):
    """Defensive: status=dispatched but dispatch_state=None is skipped."""
    shared_dir = tmp_path / "evolve"
    (shared_dir / "proposals" / "pending").mkdir(parents=True)
    p = make_investigation_proposal(
        audience="pod_operator", bot_id="team_bot_a", problem="x",
    )
    p.dispatch_target = "evo"
    transition(p, "pending", actor="test", reason="seed")
    transition(p, "dispatched", actor="test", reason="seed-dispatched")
    # dispatch_state left as None — should not crash, should not act.
    write_proposal(p, shared_dir)

    rc = _run(shared_dir, "--apply")
    assert rc == 0
    assert _status(shared_dir, p.id) == ("dispatched", "pending")


def test_non_dispatched_pending_proposal_is_ignored(tmp_path):
    """A plain pending proposal (no dispatched status) is left alone."""
    shared_dir = tmp_path / "evolve"
    (shared_dir / "proposals" / "pending").mkdir(parents=True)
    p = make_investigation_proposal(
        audience="pod_operator", bot_id="team_bot_a", problem="x",
    )
    transition(p, "pending", actor="test", reason="seed")
    write_proposal(p, shared_dir)

    rc = _run(shared_dir, "--apply")
    assert rc == 0
    assert _status(shared_dir, p.id) == ("pending", "pending")


def test_mixed_pool_only_stale_dispatches_are_swept(tmp_path):
    """Across fresh + stale + non-dispatched, only the stale one transitions."""
    shared_dir = tmp_path / "evolve"
    (shared_dir / "proposals" / "pending").mkdir(parents=True)

    fresh = _seed_dispatched(shared_dir, dispatched_ago=timedelta(hours=1))
    stale = _seed_dispatched(shared_dir, dispatched_ago=timedelta(hours=48))
    plain = make_investigation_proposal(
        audience="pod_operator", bot_id="team_bot_a", problem="y",
    )
    transition(plain, "pending", actor="test", reason="seed")
    write_proposal(plain, shared_dir)

    rc = _run(shared_dir, "--apply")
    assert rc == 0

    assert _status(shared_dir, fresh.id) == ("dispatched", "pending")
    assert _status(shared_dir, plain.id) == ("pending", "pending")
    assert _status(shared_dir, stale.id) == ("failed_flagged", "archived")


def test_unparseable_now_returns_nonzero(tmp_path, capsys):
    shared_dir = tmp_path / "evolve"
    (shared_dir / "proposals" / "pending").mkdir(parents=True)
    rc = sweep_stale_dispatches.main(
        ["--shared-dir", str(shared_dir), "--now", "not-a-date"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not parseable" in err
