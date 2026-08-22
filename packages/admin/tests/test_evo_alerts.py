"""tests/test_evo_alerts.py — `evo alerts` handler."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for path in (str(_ADMIN_DIR),):
    if path not in sys.path:
        sys.path.insert(0, path)


def _write_signal(shared: Path, sig_id: str, **fields) -> None:
    d = shared / "signals" / "firing"
    d.mkdir(parents=True, exist_ok=True)
    # Fill the fields Signal.from_dict requires so the record loads through
    # the sanctioned signals.store read path (Phase B). Test callers only
    # set the fields they assert on.
    producer = fields.pop("producer", "test_producer")
    sig_type = fields.pop("type", "test_type")
    scope = fields.get("scope", "pod")
    body = {
        "id": sig_id,
        "state": "firing",
        "signature": f"{producer}:{sig_type}:{sig_id}",
        "producer": producer,
        "type": sig_type,
        "flavor": fields.pop("flavor", "maintenance"),
        "severity": fields.pop("severity", "warn"),
        "scope": scope,
        **fields,
    }
    (d / f"{sig_id}.json").write_text(json.dumps(body))


def _call(tmp_path: Path, bot_id: str, members: list[str] | None = None):
    from evolve_admin.evo.handlers.alerts import render
    network = {"sharedDir": str(tmp_path),
               "members": members or [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


def test_bot_with_no_signals(tmp_path):
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "Nothing firing" in body


def test_bot_shows_only_relevant_signals(tmp_path):
    _write_signal(tmp_path, "s1", scope="bot", bot_id="admin_bot",
                  severity="alert", title="Admin_bot token expired", producer="security_warden")
    _write_signal(tmp_path, "s2", scope="bot", bot_id="team_bot_a",
                  severity="alert", title="Team_bot_a thing", producer="x")
    _write_signal(tmp_path, "s3", scope="pod",
                  severity="warn", title="Pod backup overdue", producer="pod_health")
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "Admin_bot token expired" in body
    assert "Pod backup overdue" in body   # pod scope visible to bots
    assert "Team_bot_a thing" not in body        # other bot's signal hidden


def test_bot_orders_by_severity(tmp_path):
    _write_signal(tmp_path, "warn1", scope="pod", severity="warn",
                  title="Warn item", producer="x")
    _write_signal(tmp_path, "alert1", scope="pod", severity="alert",
                  title="Alert item", producer="y")
    _write_signal(tmp_path, "info1", scope="pod", severity="info",
                  title="Info item", producer="z")
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert body.index("Alert item") < body.index("Warn item") < body.index("Info item")


def test_pod_groups_by_producer(tmp_path):
    _write_signal(tmp_path, "p1", scope="pod", severity="alert",
                  title="A", producer="pod_health")
    _write_signal(tmp_path, "p2", scope="pod", severity="warn",
                  title="B", producer="pod_health")
    _write_signal(tmp_path, "p3", scope="bot", bot_id="admin_bot",
                  severity="warn", title="C", producer="security_warden")
    r = _call(tmp_path, "evolve",
              members=["admin_bot", "team_bot_a", "evolve"])
    body = r.direct_send_message or ""
    assert "**Alerts — pod**" in body
    assert "pod_health: 2" in body
    assert "security_warden: 1" in body
    assert "3 firing across 2 producer" in body


def test_pod_with_no_signals(tmp_path):
    r = _call(tmp_path, "evolve", members=["admin_bot", "evolve"])
    body = r.direct_send_message or ""
    assert "Nothing firing across the pod" in body


def test_dispatch_envelope(tmp_path):
    r = _call(tmp_path, "admin_bot")
    assert r.subcommand == "alerts"
    assert r.mode == "speak"
