"""tests/test_evo_audit.py — `evo audit` handler."""

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
    from evolve_admin.evo.handlers.audit import render
    network = {"sharedDir": str(tmp_path),
               "members": members or [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


def test_filters_to_security_warden(tmp_path):
    _write_signal(tmp_path, "sw1", scope="pod", severity="alert",
                  producer="security_warden", title="Stale token")
    _write_signal(tmp_path, "ph1", scope="pod", severity="alert",
                  producer="pod_health", title="Gateway wedged")  # not audit
    r = _call(tmp_path, "evolve", members=["admin_bot", "evolve"])
    body = r.direct_send_message or ""
    assert "Stale token" in body
    assert "Gateway wedged" not in body


def test_bot_with_no_findings(tmp_path):
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "No outstanding security findings" in body


def test_bot_orders_by_severity(tmp_path):
    _write_signal(tmp_path, "i1", scope="pod", severity="info",
                  producer="security_warden", title="Info finding")
    _write_signal(tmp_path, "a1", scope="pod", severity="alert",
                  producer="security_warden", title="Alert finding")
    _write_signal(tmp_path, "w1", scope="pod", severity="warn",
                  producer="security_warden", title="Warn finding")
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert body.index("Alert finding") < body.index("Warn finding") < body.index("Info finding")


def test_bot_shows_short_id_for_each(tmp_path):
    _write_signal(tmp_path, "abc123def456", scope="pod", severity="warn",
                  producer="security_warden", title="Thing")
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    # short id (first 12 chars) appears in parens for `evo mute <id>` reuse
    assert "abc123def456" in body


def test_pod_summary_counts(tmp_path):
    _write_signal(tmp_path, "a", scope="pod", severity="alert",
                  producer="security_warden", title="A")
    _write_signal(tmp_path, "b", scope="pod", severity="warn",
                  producer="security_warden", title="B")
    _write_signal(tmp_path, "c", scope="bot", bot_id="admin_bot", severity="warn",
                  producer="security_warden", title="C")
    r = _call(tmp_path, "evolve", members=["admin_bot", "evolve"])
    body = r.direct_send_message or ""
    assert "**Audit — pod** (3 findings)" in body
    assert "1 alert" in body
    assert "2 warn" in body
