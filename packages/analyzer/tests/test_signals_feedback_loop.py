"""tests/test_signals_feedback_loop.py — Phase 4 RSI link end-to-end.

Spec: docs/spec-alerts-signal-store-2026-05-07.md §9.

The credibility test for the architecture: when a proposal is dismissed
with a signal-feedback verdict, the originating Signal is flagged in
``signals/feedback.jsonl`` so producers can tune their detection.

Covers:
  - Proposal carries motivating_signals[] through round-trip
  - Dismiss with verdict writes feedback.jsonl + detaches link
  - Dismiss without verdict is a no-op for the feedback log
  - A toy "tuner" reads feedback.jsonl and adjusts a threshold
    (concrete proof the loop closes)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import pytest  # noqa: E402

from arbiter import store as arbiter_store  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from schema.signal import Signal  # noqa: E402
from signals import store as signals_store  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Schema: Proposal.motivating_signals round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_proposal_motivating_signals_round_trip(tmp_path):
    p = make_investigation_proposal(bot_id="admin_bot", problem="cost spike noticed")
    p.motivating_signals = ["sig-abc", "sig-def"]
    transition(p, "pending", actor="test")
    arbiter_store.write_proposal(p, tmp_path)

    located = arbiter_store.find_proposal(tmp_path, p.id)
    assert located is not None
    revived, _, _ = located
    assert revived.motivating_signals == ["sig-abc", "sig-def"]


def test_proposal_motivating_signals_defaults_to_empty(tmp_path):
    p = make_investigation_proposal(bot_id="admin_bot")
    assert p.motivating_signals == []
    transition(p, "pending", actor="test")
    arbiter_store.write_proposal(p, tmp_path)

    located = arbiter_store.find_proposal(tmp_path, p.id)
    assert located is not None
    revived, _, _ = located
    assert revived.motivating_signals == []


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — minimal Flask client for the dismiss endpoint
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def admin_client(tmp_path):
    """Build a Flask test client wired to a tmp shared_dir + network.json."""
    shared = tmp_path / "shared"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(
        json.dumps({"sharedDir": str(shared), "bots": []}),
        encoding="utf-8",
    )

    from evolve_admin.web.server import create_app

    app = create_app(network)
    return app.test_client(), shared


# ─────────────────────────────────────────────────────────────────────────────
# Dismiss with verdict writes feedback.jsonl + detaches signal link
# ─────────────────────────────────────────────────────────────────────────────


def _seed_signal_and_proposal(shared_dir: Path) -> tuple[Signal, str]:
    """Create one Signal and one Proposal that links back to it."""
    sig = signals_store.observe(
        shared_dir,
        signature="pod_report:cost_spike:admin_bot",
        producer="pod_report",
        type="cost_spike",
        flavor="activity",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="Cost spike on admin_bot",
        details={"current": 4.2, "baseline": 1.05},
    )

    p = make_investigation_proposal(
        bot_id="admin_bot", problem="investigate cost spike"
    )
    p.motivating_signals = [sig.id]
    transition(p, "pending", actor="test")
    arbiter_store.write_proposal(p, shared_dir)
    # Mirror the link onto the Signal (denormalized; spec §9)
    signals_store.attach_proposal(sig, shared_dir, proposal_id=p.id)

    return sig, p.id


def test_dismiss_with_false_positive_writes_feedback(admin_client):
    client, shared = admin_client
    sig, proposal_id = _seed_signal_and_proposal(shared)

    resp = client.post(
        f"/api/arbiter/proposals/{proposal_id}/dismiss",
        json={
            "verdict": "false_positive",
            "note": "Black Friday — expected traffic",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["new_status"] == "dismissed"

    # Feedback log
    log = signals_store.feedback_log_path(shared)
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["signal_id"] == sig.id
    assert rec["signal_signature"] == sig.signature
    assert rec["proposal_id"] == proposal_id
    assert rec["verdict"] == "false_positive"
    assert rec["note"] == "Black Friday — expected traffic"

    # Signal's motivated_proposals mirror should be detached
    located = signals_store.find_signal(shared, sig.id)
    assert located is not None
    sig_after, _, _ = located
    assert proposal_id not in sig_after.motivated_proposals


def test_dismiss_without_verdict_does_not_write_feedback(admin_client):
    client, shared = admin_client
    sig, proposal_id = _seed_signal_and_proposal(shared)

    resp = client.post(
        f"/api/arbiter/proposals/{proposal_id}/dismiss",
        json={},
    )
    assert resp.status_code == 200

    log = signals_store.feedback_log_path(shared)
    # Either no log, or empty
    if log.exists():
        assert log.read_text(encoding="utf-8").strip() == ""

    # Mirror should still hold the link — no feedback means no detach
    located = signals_store.find_signal(shared, sig.id)
    assert located is not None
    sig_after, _, _ = located
    assert proposal_id in sig_after.motivated_proposals


def test_dismiss_with_unknown_verdict_is_silent_noop(admin_client):
    """Unknown verdict tokens don't trigger feedback writes — defensive
    against UI sending arbitrary strings.
    """
    client, shared = admin_client
    sig, proposal_id = _seed_signal_and_proposal(shared)

    resp = client.post(
        f"/api/arbiter/proposals/{proposal_id}/dismiss",
        json={"verdict": "i_changed_my_mind"},
    )
    assert resp.status_code == 200

    log = signals_store.feedback_log_path(shared)
    if log.exists():
        assert log.read_text(encoding="utf-8").strip() == ""


def test_dismiss_with_no_motivating_signals_is_silent_noop(admin_client):
    """Proposal without motivating_signals can still be dismissed with
    a verdict — it's just a no-op for the feedback log.
    """
    client, shared = admin_client
    p = make_investigation_proposal(bot_id="admin_bot", problem="manual review")
    transition(p, "pending", actor="test")
    arbiter_store.write_proposal(p, shared)

    resp = client.post(
        f"/api/arbiter/proposals/{p.id}/dismiss",
        json={"verdict": "false_positive"},
    )
    assert resp.status_code == 200

    log = signals_store.feedback_log_path(shared)
    if log.exists():
        assert log.read_text(encoding="utf-8").strip() == ""


# ─────────────────────────────────────────────────────────────────────────────
# Bidirectional read: Signal → Proposals
# ─────────────────────────────────────────────────────────────────────────────


def test_get_signal_proposals_returns_motivated(admin_client):
    client, shared = admin_client
    sig, proposal_id = _seed_signal_and_proposal(shared)

    resp = client.get(f"/api/signals/{sig.id}/proposals")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 1
    assert data["proposals"][0]["id"] == proposal_id


def test_get_signal_proposals_empty_when_unmotivated(admin_client):
    client, shared = admin_client
    sig = signals_store.observe(
        shared,
        signature="x:y:admin_bot",
        producer="x",
        type="y",
        flavor="activity",
        severity="info",
        scope="bot",
        bot_id="admin_bot",
    )

    resp = client.get(f"/api/signals/{sig.id}/proposals")
    assert resp.status_code == 200
    assert resp.get_json()["count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Toy tuner — concrete proof the loop closes
# ─────────────────────────────────────────────────────────────────────────────


def _toy_tuner_read_feedback(shared_dir: Path) -> dict[str, int]:
    """Minimal generator-side feedback consumer.

    Reads ``signals/feedback.jsonl`` and returns a count of
    false_positive verdicts per signal_signature. A real tuner would
    use this to raise its detection threshold for noisy signatures.
    """
    log = signals_store.feedback_log_path(shared_dir)
    counts: dict[str, int] = {}
    if not log.exists():
        return counts
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("verdict") != "false_positive":
            continue
        sig_signature = rec.get("signal_signature", "unknown")
        counts[sig_signature] = counts.get(sig_signature, 0) + 1
    return counts


def test_tuner_can_read_feedback_and_count_false_positives(admin_client):
    client, shared = admin_client

    # Seed two signals, both with proposals, both dismissed false_positive
    sig_a, prop_a = _seed_signal_and_proposal(shared)
    # Different bot for distinct signature
    sig_b = signals_store.observe(
        shared,
        signature="pod_report:cost_spike:team_bot_b",
        producer="pod_report",
        type="cost_spike",
        flavor="activity",
        severity="warn",
        scope="bot",
        bot_id="team_bot_b",
    )
    p_b = make_investigation_proposal(bot_id="team_bot_b", problem="cost spike team_bot_b")
    p_b.motivating_signals = [sig_b.id]
    transition(p_b, "pending", actor="test")
    arbiter_store.write_proposal(p_b, shared)
    signals_store.attach_proposal(sig_b, shared, proposal_id=p_b.id)

    # Dismiss both with false_positive
    client.post(
        f"/api/arbiter/proposals/{prop_a}/dismiss",
        json={"verdict": "false_positive"},
    )
    client.post(
        f"/api/arbiter/proposals/{p_b.id}/dismiss",
        json={"verdict": "false_positive"},
    )

    counts = _toy_tuner_read_feedback(shared)
    # Both signatures appear once — the tuner can now decide to raise
    # the cost_spike threshold on the noisy bots.
    assert counts == {
        "pod_report:cost_spike:admin_bot": 1,
        "pod_report:cost_spike:team_bot_b": 1,
    }
