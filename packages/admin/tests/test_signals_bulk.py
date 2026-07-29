"""tests/test_signals_bulk.py — POST /api/signals/bulk-action.

The Reports → Alerts page's sticky bulk-action bar fans bulk
snooze/dismiss/resolve out through a single endpoint. These tests pin
the contract:

  - bulk snooze/dismiss/resolve over N valid signals transitions each
  - missing or invalid ids return a per-id error without failing the
    whole batch (operator sees a "57 dismissed, 2 missing" summary)
  - verdict validation matches the single-signal dismiss endpoint
  - duration parsing matches the single-signal snooze endpoint
  - dismiss verdict feedback is written to signals/feedback.jsonl
    (mirrors the single-signal dismiss feedback path)

Background: the 2026-05-21 audit-noise transcript landed the operator
on 87 firing signals and no in-UI path to bulk-dismiss them. The
endpoint is the structural fix; the front-end is in
test_alerts_per_row_actions.py.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _seed_signal(shared_dir: Path, sig_id: str, *,
                 producer: str = "audit_config",
                 sig_type: str = "stale_baseline",
                 bot_id: str | None = "team_bot_a",
                 severity: str = "warn") -> str:
    """Drop a Signal JSON file into shared_dir/signals/firing/.

    Returns the signal id. Uses the schema directly rather than going
    through observe() so the seeded signal has a predictable id.
    """
    from schema.signal import Signal, StateTransition

    sig = Signal(
        id=sig_id,
        signature=f"{producer}:{sig_type}:{bot_id or 'pod'}",
        producer=producer,
        type=sig_type,
        flavor="maintenance",
        severity=severity,
        scope="bot" if bot_id else "pod",
        bot_id=bot_id,
        title=f"test {sig_type} on {bot_id or 'pod'}",
        body="seed",
    )
    sig.state_history.append(
        StateTransition(
            from_state=None,
            to_state="firing",
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            actor="test",
            reason="seed",
        )
    )
    from signals import store as signals_store
    signals_store.write_signal(sig, shared_dir, subdir="firing")
    return sig.id


@pytest.fixture
def app(tmp_path):
    from evolve_admin.web.server import create_app

    shared = tmp_path / "evolve"
    for sub in ("firing", "snoozed", "archived"):
        (shared / "signals" / sub).mkdir(parents=True)

    network = {"members": [], "bots": {}, "sharedDir": str(shared)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    app.config["_test_shared"] = str(shared)  # expose to tests
    return app


# ── Happy path: bulk snooze / resolve / dismiss ─────────────────────────────


def test_bulk_snooze_transitions_all_signals(app):
    shared = Path(app.config["_test_shared"])
    ids = [_seed_signal(shared, f"sig_snz_{i}") for i in range(3)]

    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": ids,
            "action": "snooze",
            "duration": "1d",
        })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["ok"] is True
    assert data["action"] == "snooze"
    assert data["applied"] == 3
    assert data["failed"] == 0
    assert data["total"] == 3
    # Each result row reports the new state.
    for r in data["results"]:
        assert r["ok"] is True
        assert r["new_state"] == "snoozed"
    # Files moved to snoozed/.
    snoozed = list((shared / "signals" / "snoozed").glob("*.json"))
    assert len(snoozed) == 3


def test_bulk_resolve_transitions_all_signals(app):
    shared = Path(app.config["_test_shared"])
    ids = [_seed_signal(shared, f"sig_rsv_{i}") for i in range(2)]

    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": ids,
            "action": "resolve",
        })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["applied"] == 2
    # Resolved signals land in archived/.
    archived = list((shared / "signals" / "archived").glob("*.json"))
    assert len(archived) == 2


def test_bulk_dismiss_transitions_all_signals(app):
    shared = Path(app.config["_test_shared"])
    ids = [_seed_signal(shared, f"sig_dms_{i}") for i in range(4)]

    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": ids,
            "action": "dismiss",
            "verdict": "not_actionable",
            "note": "audit noise",
        })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["applied"] == 4
    # Dismissed signals land in archived/ as well.
    archived = list((shared / "signals" / "archived").glob("*.json"))
    assert len(archived) == 4


# ── Partial failure: per-id errors don't fail the batch ─────────────────────


def test_bulk_dismiss_returns_per_id_error_for_missing_id(app):
    shared = Path(app.config["_test_shared"])
    real_id = _seed_signal(shared, "sig_real")

    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": [real_id, "sig_does_not_exist"],
            "action": "dismiss",
            "verdict": "false_positive",
        })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["applied"] == 1
    assert data["failed"] == 1
    by_id = {r["signal_id"]: r for r in data["results"]}
    assert by_id[real_id]["ok"] is True
    assert by_id["sig_does_not_exist"]["ok"] is False
    assert "not found" in by_id["sig_does_not_exist"]["error"]


def test_bulk_dedupes_signal_ids(app):
    shared = Path(app.config["_test_shared"])
    sig_id = _seed_signal(shared, "sig_dedupe")

    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": [sig_id, sig_id, sig_id],
            "action": "resolve",
        })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    # All three references collapse to one transition attempt.
    assert data["total"] == 1
    assert data["applied"] == 1


# ── Validation: 400 on bad input ────────────────────────────────────────────


def test_bulk_rejects_empty_signal_ids(app):
    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": [],
            "action": "resolve",
        })
    assert resp.status_code == 400
    assert "non-empty" in resp.get_json()["error"].lower()


def test_bulk_rejects_unknown_action(app):
    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": ["whatever"],
            "action": "delete",
        })
    assert resp.status_code == 400
    assert "snooze" in resp.get_json()["error"]


def test_bulk_rejects_invalid_verdict(app):
    """Verdict validation matches the single-signal dismiss endpoint —
    only false_positive / bad_inference / not_actionable accepted."""
    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": ["whatever"],
            "action": "dismiss",
            "verdict": "i_dont_like_it",
        })
    assert resp.status_code == 400
    assert "verdict" in resp.get_json()["error"]


def test_bulk_rejects_invalid_duration(app):
    """Duration parser matches the single-signal snooze endpoint."""
    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": ["whatever"],
            "action": "snooze",
            "duration": "forever",
        })
    assert resp.status_code == 400
    assert "duration" in resp.get_json()["error"]


# ── Feedback path: dismiss verdict writes feedback.jsonl ────────────────────


def test_bulk_dismiss_with_verdict_writes_feedback(app):
    """Mirrors the single-signal dismiss endpoint: writing a verdict
    pushes a row into signals/feedback.jsonl so producers can tune
    detection later."""
    shared = Path(app.config["_test_shared"])
    sig_id = _seed_signal(shared, "sig_fbk")

    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": [sig_id],
            "action": "dismiss",
            "verdict": "false_positive",
            "note": "tested in CI",
        })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    fb_path = shared / "signals" / "feedback.jsonl"
    assert fb_path.exists(), "feedback.jsonl should be written on verdict-dismiss"
    lines = [json.loads(ln) for ln in fb_path.read_text().splitlines() if ln.strip()]
    assert len(lines) >= 1
    assert any(r.get("signal_id") == sig_id for r in lines)
    assert any(r.get("verdict") == "false_positive" for r in lines)


def test_bulk_dismiss_without_verdict_does_not_write_feedback(app):
    """No verdict → no feedback row. Matches single-signal dismiss."""
    shared = Path(app.config["_test_shared"])
    sig_id = _seed_signal(shared, "sig_nofbk")

    with app.test_client() as c:
        resp = c.post("/api/signals/bulk-action", json={
            "signal_ids": [sig_id],
            "action": "dismiss",
        })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    fb_path = shared / "signals" / "feedback.jsonl"
    # Either the file doesn't exist, or no rows match this signal_id.
    if fb_path.exists():
        lines = [json.loads(ln) for ln in fb_path.read_text().splitlines() if ln.strip()]
        assert not any(r.get("signal_id") == sig_id for r in lines)
