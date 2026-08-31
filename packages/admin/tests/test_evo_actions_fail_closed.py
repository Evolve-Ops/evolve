"""tests/test_evo_actions_fail_closed.py — 7.1 C2 negative-path proof.

DESIGN PREMISE (operator decision 2026-08-25, diligence ledger entry
``7.1C-fallback-policy``): evo's proposal/signal action tools are FAIL
CLOSED. When the admin daemon is unreachable they REFUSE with an
operator-legible error and perform NO write of their own — no
direct-write fallback exists, because a fallback that activates on
"daemon unreachable" is a bypass an attacker can induce by killing the
socket.

This module is the negative-path proof the decision record requires.
The daemon transport is taken down FOR REAL — ``DEFAULT_SOCKET_PATH``
is pointed at a directory with no socket bound, so every call performs
an actual AF_UNIX ``connect()`` that fails (no mocking above the
transport) — and every write tool must then:

  (a) return the refusal (``ok=False`` + the exact
      ``DAEMON_REQUIRED_REFUSAL`` string the bot relays), never a
      raised exception and never a silent no-op; and
  (b) leave ``proposals/`` and ``signals/`` (and the dispatch-envelope
      queue) BYTE-IDENTICAL — the write must be provably absent, not
      just the success path gone.

A second block proves the complement: when the daemon answers, the
tools call the daemon and STILL write nothing locally — the daemon is
the store's only writer on the tool path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_DIR = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin.evo import admin_client  # noqa: E402
from evolve_admin.evo.admin_client import DAEMON_REQUIRED_REFUSAL  # noqa: E402
from evolve_admin.evo.tools import (  # noqa: E402
    action_dispatch,
    action_proposal,
    action_proposal_apply,
    action_proposal_refine,
    action_signal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — a seeded store + a genuinely dead daemon socket
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def dead_daemon(monkeypatch, tmp_path: Path) -> Path:
    """Point the admin client at a socket path with nothing bound.

    ``_request_json`` resolves ``DEFAULT_SOCKET_PATH`` at call time, so
    this makes every daemon call a REAL failed AF_UNIX connect — the
    exact transport state of a stopped/crashed admin daemon.
    """
    sock = tmp_path / "no-daemon" / "admin-daemon.sock"
    sock.parent.mkdir(parents=True)
    monkeypatch.setattr(admin_client, "DEFAULT_SOCKET_PATH", sock)
    return sock


def _seed_pending_proposal(shared_dir: Path, proposal_id: str = "p-pending-1"):
    from testing.harness import make_investigation_proposal
    from arbiter.state_machine import transition
    from arbiter.store import write_proposal

    p = make_investigation_proposal(
        audience="pod_operator", bot_id="team_bot_a", problem="pending seed",
    )
    p.id = proposal_id
    transition(p, "pending", actor="test", reason="seed")
    write_proposal(p, shared_dir, subdir="pending")
    return p


def _seed_applied_manual_proposal(shared_dir: Path, proposal_id: str = "p-applied-1"):
    """An ``applied`` Investigation proposal — the mark_complete target."""
    from testing.harness import make_investigation_proposal
    from arbiter.state_machine import transition
    from arbiter.store import write_proposal

    p = make_investigation_proposal(
        audience="pod_operator", bot_id="team_bot_a", problem="applied seed",
    )
    p.id = proposal_id
    transition(p, "pending", actor="test", reason="seed")
    transition(p, "approved_human", actor="test", reason="seed")
    transition(p, "applied", actor="test", reason="seed")
    write_proposal(p, shared_dir, subdir="applied")
    return p


def _seed_dispatched_proposal(shared_dir: Path, proposal_id: str = "p-dispatched-1"):
    from schema.proposal import DispatchState
    from testing.harness import make_investigation_proposal
    from arbiter.state_machine import transition
    from arbiter.store import write_proposal

    p = make_investigation_proposal(
        audience="pod_operator", bot_id="team_bot_a", problem="dispatched seed",
    )
    p.id = proposal_id
    p.dispatch_state = DispatchState(
        target="evo",
        dispatched_at="2026-08-25T12:00:00+00:00",
        message="fix it",
        session_id="s-test",
    )
    transition(p, "pending", actor="test", reason="seed")
    transition(p, "dispatched", actor="test", reason="seed")
    write_proposal(p, shared_dir, subdir="pending")

    env_dir = shared_dir / "dispatches" / "evo"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / f"{p.id}.json").write_text(json.dumps({
        "proposal_id": p.id,
        "target": "evo",
        "dispatched_at": "2026-08-25T12:00:00+00:00",
        "message": "fix it",
        "proposal": p.to_dict(),
    }))
    return p


def _seed_firing_signal(shared_dir: Path) -> str:
    from signals import store as signals_store

    sig = signals_store.observe(
        shared_dir,
        signature="fail-closed-test",
        producer="test_producer",
        type="test_condition",
        flavor="security",
        severity="warning",
        scope="bot",
        bot_id="team_bot_a",
        title="fail-closed test signal",
        body="seeded for the 7.1 C2 negative-path proof",
    )
    return sig.id


def _tree_snapshot(*roots: Path) -> dict[str, bytes]:
    """relative-path → content map across the given roots. Byte-level so
    a moved, rewritten, or newly created file all show up as a diff."""
    snap: dict[str, bytes] = {}
    for root in roots:
        if not root.exists():
            continue
        for f in sorted(root.rglob("*")):
            if f.is_file():
                snap[f"{root.name}/{f.relative_to(root)}"] = f.read_bytes()
    return snap


@pytest.fixture()
def seeded_store(tmp_path: Path):
    """A shared_dir with one of everything the write tools target."""
    shared = tmp_path / "shared"
    shared.mkdir()
    pending = _seed_pending_proposal(shared)
    applied = _seed_applied_manual_proposal(shared)
    dispatched = _seed_dispatched_proposal(shared)
    signal_id = _seed_firing_signal(shared)
    return {
        "shared": shared,
        "pending": pending,
        "applied": applied,
        "dispatched": dispatched,
        "signal_id": signal_id,
    }


def _assert_refusal(result: dict) -> None:
    assert isinstance(result, dict), "tool must return a dict, never raise"
    assert result.get("ok") is False
    assert result.get("error") == DAEMON_REQUIRED_REFUSAL
    assert result.get("daemon_unreachable") is True


# ─────────────────────────────────────────────────────────────────────────────
# The proof: daemon down → refusal + provably-absent write, per tool
# ─────────────────────────────────────────────────────────────────────────────


def _stores_snapshot(shared: Path) -> dict[str, bytes]:
    return _tree_snapshot(
        shared / "proposals", shared / "signals", shared / "dispatches",
    )


def test_proposal_snooze_fails_closed(dead_daemon, seeded_store):
    shared = seeded_store["shared"]
    before = _stores_snapshot(shared)
    result = action_proposal._snooze_handler(
        shared, seeded_store["pending"].id, duration="1w", reason="r",
    )
    _assert_refusal(result)
    assert _stores_snapshot(shared) == before


def test_proposal_reject_fails_closed(dead_daemon, seeded_store):
    shared = seeded_store["shared"]
    before = _stores_snapshot(shared)
    result = action_proposal._reject_handler(
        shared, seeded_store["pending"].id, reason="r",
    )
    _assert_refusal(result)
    assert _stores_snapshot(shared) == before


def test_proposal_mark_complete_fails_closed(dead_daemon, seeded_store):
    shared = seeded_store["shared"]
    before = _stores_snapshot(shared)
    result = action_proposal._mark_complete_handler(
        shared, seeded_store["applied"].id, reason="r",
    )
    _assert_refusal(result)
    assert _stores_snapshot(shared) == before


def test_proposal_apply_fails_closed(dead_daemon, seeded_store):
    shared = seeded_store["shared"]
    before = _stores_snapshot(shared)
    result = action_proposal_apply._apply_handler(
        shared, seeded_store["pending"].id, reason="r",
    )
    _assert_refusal(result)
    assert _stores_snapshot(shared) == before


def test_proposal_refine_fails_closed(dead_daemon, seeded_store, tmp_path):
    shared = seeded_store["shared"]
    before = _stores_snapshot(shared)
    result = action_proposal_refine._refine_handler(
        shared, tmp_path / "network.json",
        seeded_store["pending"].id, "reword this",
    )
    _assert_refusal(result)
    assert _stores_snapshot(shared) == before


def test_dispatch_acknowledge_fails_closed(dead_daemon, seeded_store):
    """Refusal must also leave the envelope in place — the unlink only
    happens after a successful daemon write."""
    shared = seeded_store["shared"]
    before = _stores_snapshot(shared)
    result = action_dispatch._acknowledge_handler(
        shared, seeded_store["dispatched"].id, "applied", message="done",
    )
    _assert_refusal(result)
    assert _stores_snapshot(shared) == before
    env = shared / "dispatches" / "evo" / f"{seeded_store['dispatched'].id}.json"
    assert env.exists(), "envelope must survive a refused acknowledge"


def test_signal_snooze_fails_closed(dead_daemon, seeded_store):
    shared = seeded_store["shared"]
    before = _stores_snapshot(shared)
    result = action_signal._snooze_handler(
        shared, seeded_store["signal_id"], duration="24h", reason="r",
    )
    _assert_refusal(result)
    assert _stores_snapshot(shared) == before


def test_signal_dismiss_fails_closed(dead_daemon, seeded_store):
    """Dismiss-with-verdict is the sharpest case: the old direct path
    wrote BOTH the archived signal AND feedback.jsonl. Neither may
    appear."""
    shared = seeded_store["shared"]
    before = _stores_snapshot(shared)
    result = action_signal._dismiss_handler(
        shared, seeded_store["signal_id"],
        verdict="false_positive", reason="r",
    )
    _assert_refusal(result)
    assert _stores_snapshot(shared) == before
    assert not (shared / "signals" / "feedback.jsonl").exists()


def test_signal_resolve_fails_closed(dead_daemon, seeded_store):
    shared = seeded_store["shared"]
    before = _stores_snapshot(shared)
    result = action_signal._resolve_handler(
        shared, seeded_store["signal_id"], reason="r",
    )
    _assert_refusal(result)
    assert _stores_snapshot(shared) == before


def test_refusal_message_is_operator_legible():
    """The refusal the bot relays must say what is disabled and until
    when — not a stack trace, not an errno."""
    assert "admin daemon unreachable" in DAEMON_REQUIRED_REFUSAL
    assert "disabled until it returns" in DAEMON_REQUIRED_REFUSAL


# ─────────────────────────────────────────────────────────────────────────────
# Complement: daemon UP → the tool calls the daemon and still writes
# nothing locally (the daemon is the only writer on the tool path)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def recording_daemon(monkeypatch):
    """Stub require_daemon_call with a recorder that answers 200."""
    calls: list[tuple[str, str, dict]] = []

    def _fake(method, path, body=None, **kwargs):
        calls.append((method, path, body or {}))
        return True, 200, {
            "ok": True,
            "new_status": "snoozed",
            "new_state": "snoozed",
            "signal": {"id": "sig-x", "title": "t", "state": "snoozed"},
            "revision_count": 1,
        }

    monkeypatch.setattr(admin_client, "require_daemon_call", _fake)
    return calls


def test_daemon_success_writes_nothing_locally(recording_daemon, seeded_store):
    """Success path: every write tool defers the store mutation to the
    daemon — the local trees stay byte-identical even on HTTP 200.
    (The envelope unlink in acknowledge is the one sanctioned local
    side effect, and it is outside proposals/ and signals/.)"""
    shared = seeded_store["shared"]
    before = _tree_snapshot(shared / "proposals", shared / "signals")

    action_proposal._snooze_handler(shared, seeded_store["pending"].id)
    action_proposal._reject_handler(shared, seeded_store["pending"].id)
    action_proposal._mark_complete_handler(shared, seeded_store["applied"].id)
    action_proposal_apply._apply_handler(shared, seeded_store["pending"].id)
    action_signal._snooze_handler(shared, seeded_store["signal_id"])
    action_signal._dismiss_handler(
        shared, seeded_store["signal_id"], verdict="false_positive")
    action_signal._resolve_handler(shared, seeded_store["signal_id"])
    action_dispatch._acknowledge_handler(
        shared, seeded_store["dispatched"].id, "applied")

    assert _tree_snapshot(shared / "proposals", shared / "signals") == before
    assert len(recording_daemon) == 8, "every tool must have called the daemon"


def test_daemon_calls_hit_the_right_endpoints(recording_daemon, seeded_store):
    """Pin the endpoint + attribution contract: each tool posts its
    daemon route with actor=evo (dispatch/result attributes via the
    proposal's dispatch target daemon-side)."""
    shared = seeded_store["shared"]
    pid = seeded_store["pending"].id
    sid = seeded_store["signal_id"]

    action_proposal._snooze_handler(shared, pid, duration="1w")
    action_proposal._reject_handler(shared, pid, reason="wrong")
    action_proposal._mark_complete_handler(shared, seeded_store["applied"].id)
    action_proposal_apply._apply_handler(shared, pid)
    action_signal._snooze_handler(shared, sid)
    action_signal._dismiss_handler(shared, sid, verdict="false_positive")
    action_signal._resolve_handler(shared, sid)
    action_dispatch._acknowledge_handler(
        shared, seeded_store["dispatched"].id, "failed", message="nope")

    paths = [(m, p) for m, p, _b in recording_daemon]
    assert paths == [
        ("POST", f"/api/arbiter/proposals/{pid}/snooze"),
        ("POST", f"/api/arbiter/proposals/{pid}/dismiss"),
        ("POST", f"/api/arbiter/proposals/{seeded_store['applied'].id}/complete"),
        ("POST", f"/api/arbiter/proposals/{pid}/act"),
        ("POST", f"/api/signals/{sid}/snooze"),
        ("POST", f"/api/signals/{sid}/dismiss"),
        ("POST", f"/api/signals/{sid}/resolve"),
        (
            "POST",
            f"/api/arbiter/proposals/{seeded_store['dispatched'].id}"
            "/dispatch/result",
        ),
    ]
    bodies = [b for _m, _p, b in recording_daemon]
    # dispatch/result carries no actor field — the daemon attributes the
    # transition to the proposal's dispatch target ("evo") itself.
    for body in bodies[:-1]:
        assert body.get("actor") == "evo"


def test_refine_success_path_contract(monkeypatch, seeded_store, tmp_path):
    """Refine's daemon-up contract (independent review, finding 4):
    /refine with the feedback + evo attribution, an LLM-scale timeout,
    and the endpoint response mapped onto the tool's shape."""
    calls: list[tuple[str, str, dict, dict]] = []

    def _fake(method, path, body=None, **kwargs):
        calls.append((method, path, body or {}, kwargs))
        return True, 200, {
            "ok": True,
            "revision_count": 2,
            "new_problem": "reworded",
            "new_admin_surface_summary": "reworded summary",
        }

    monkeypatch.setattr(admin_client, "require_daemon_call", _fake)
    result = action_proposal_refine._refine_handler(
        seeded_store["shared"], tmp_path / "network.json",
        seeded_store["pending"].id, "reword this",
    )
    assert result["ok"] is True
    assert result["revision_count"] == 2
    assert result["new_problem"] == "reworded"

    method, path, body, kwargs = calls[0]
    assert (method, path) == (
        "POST",
        f"/api/arbiter/proposals/{seeded_store['pending'].id}/refine",
    )
    assert body == {"feedback": "reword this", "actor": "evo"}
    # LLM call runs daemon-side — the default 10s POST timeout would
    # chop legitimate refines.
    assert kwargs.get("timeout") == 120.0
