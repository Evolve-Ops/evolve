"""tests/test_signals_unsnooze_endpoint.py — POST /api/signals/<id>/unsnooze.

Surfaces the Reports → Alerts → Snoozed sub-tab's "Unsnooze" row
action. The endpoint transitions a snoozed Signal back to firing via
the same ``signals.state_machine`` edge the snooze-wake daemon uses on
TTL expiry; this is the operator-driven path for "I want to address
it now."

See internal/spec-alerts-count-normalization-2026-06-06.md.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _seed_firing_signal(shared_dir: Path, sig_id: str) -> str:
    from schema.signal import Signal, StateTransition

    sig = Signal(
        id=sig_id,
        signature=f"plugin_monitor:test:bot_a::{sig_id}",
        producer="plugin_monitor",
        type="test_finding",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="bot_a",
        title=f"test snoozable signal {sig_id}",
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
    app.config["_test_shared"] = str(shared)
    return app


def test_unsnooze_returns_signal_to_firing(app):
    shared = Path(app.config["_test_shared"])
    sig_id = _seed_firing_signal(shared, "sig_unsnooze_1")

    until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
    with app.test_client() as c:
        snooze_resp = c.post(
            f"/api/signals/{sig_id}/snooze",
            json={"until": until},
        )
        assert snooze_resp.status_code == 200, snooze_resp.get_json()
        assert snooze_resp.get_json()["new_state"] == "snoozed"

        unsnooze_resp = c.post(f"/api/signals/{sig_id}/unsnooze")
        assert unsnooze_resp.status_code == 200, unsnooze_resp.get_json()
        body = unsnooze_resp.get_json()
        assert body["ok"] is True
        assert body["new_state"] == "firing"

    assert (shared / "signals" / "firing" / f"{sig_id}.json").exists()
    assert not (shared / "signals" / "snoozed" / f"{sig_id}.json").exists()


def test_unsnooze_404_on_missing_signal(app):
    with app.test_client() as c:
        resp = c.post("/api/signals/does_not_exist/unsnooze")
    assert resp.status_code == 404


def test_unsnooze_409_on_already_firing(app):
    """A signal already in firing can't transition firing → firing.
    The state machine refuses; the endpoint should surface a 409, not
    a silent no-op."""
    shared = Path(app.config["_test_shared"])
    sig_id = _seed_firing_signal(shared, "sig_already_firing")

    with app.test_client() as c:
        resp = c.post(f"/api/signals/{sig_id}/unsnooze")
    assert resp.status_code == 409
