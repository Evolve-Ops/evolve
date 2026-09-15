"""tests/test_evo_health.py — `evo health` handler."""

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
    from evolve_admin.evo.handlers.health import render
    network = {"sharedDir": str(tmp_path),
               "members": members or [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


def test_filters_to_health_producers(tmp_path):
    _write_signal(tmp_path, "h1", scope="pod", severity="alert",
                  producer="pod_health", title="Gateway wedged")
    _write_signal(tmp_path, "a1", scope="pod", severity="alert",
                  producer="cost_watchdog", title="Cost spike")  # not health
    r = _call(tmp_path, "evolve", members=["admin_bot", "evolve"])
    body = r.direct_send_message or ""
    assert "Gateway wedged" in body
    assert "Cost spike" not in body


def test_bot_with_no_health_signals(tmp_path):
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "Everything looks healthy" in body


def test_pod_with_no_health_signals(tmp_path):
    r = _call(tmp_path, "evolve", members=["admin_bot", "evolve"])
    body = r.direct_send_message or ""
    assert "Everything looks healthy across the pod" in body


def test_pod_summary_counts_severities(tmp_path):
    _write_signal(tmp_path, "a", scope="pod", severity="alert",
                  producer="pod_health", title="A")
    _write_signal(tmp_path, "b", scope="pod", severity="warn",
                  producer="pod_health", title="B")
    _write_signal(tmp_path, "c", scope="host", severity="warn",
                  producer="host_health", title="C")
    r = _call(tmp_path, "evolve", members=["admin_bot", "evolve"])
    body = r.direct_send_message or ""
    assert "1 alert" in body
    assert "2 warn" in body


def test_bot_sees_pod_scope_health(tmp_path):
    _write_signal(tmp_path, "p1", scope="pod", severity="alert",
                  producer="pod_health", title="Backup overdue")
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "Backup overdue" in body
