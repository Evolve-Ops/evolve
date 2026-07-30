"""Tests for the Security → Host tab policy-acceptance endpoints.

Covers GET /api/security/host-policies + PUT /api/security/host-policies/<id>
— the operator-facing surface that records ``policy_acceptances`` entries
on ``network.json``. Today only ``machine.filevault_off`` is wired up;
the test pins the contract the audit pipeline already honors via
``audit.policy_acceptance()``.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


def _fdesetup_completed(state: str):
    """Build a fake CompletedProcess for ``/usr/bin/fdesetup status``."""
    import subprocess as _sp
    return _sp.CompletedProcess(
        args=["/usr/bin/fdesetup", "status"],
        returncode=0,
        stdout=f"FileVault is {state}.\n",
        stderr="",
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    shared = tmp_path / "shared"
    shared.mkdir()
    network = {
        "networkId": "test",
        "sharedDir": str(shared),
        "bots": {},
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network, indent=2))

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, net_file


def test_get_returns_filevault_on_state(client, monkeypatch):
    """When fdesetup reports 'on', the FileVault policy reads as state=on
    with no recorded acceptance."""
    c, _ = client
    import subprocess
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _fdesetup_completed("On"),
    )
    resp = c.get("/api/security/host-policies")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "policies" in data
    fv = next((p for p in data["policies"] if p["id"] == "machine.filevault_off"), None)
    assert fv is not None
    assert fv["state"] == "on"
    assert fv["acceptance"] is None
    assert fv["title"] == "FileVault disk encryption"


def test_get_returns_filevault_off_state(client, monkeypatch):
    c, _ = client
    import subprocess
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _fdesetup_completed("Off"),
    )
    resp = c.get("/api/security/host-policies")
    data = resp.get_json()
    fv = next(p for p in data["policies"] if p["id"] == "machine.filevault_off")
    assert fv["state"] == "off"
    assert fv["acceptance"] is None


def test_put_opt_out_writes_policy_acceptance(client, monkeypatch):
    """PUT with intent=opt_out persists policy_acceptances entry to
    network.json with the operator's reason + a timestamp.

    audit.py already reads this block via ``policy_acceptance()`` —
    once written, the next audit cycle demotes the FileVault finding
    from critical to ok and the firing Signal is auto-resolved.
    """
    c, net_file = client
    import subprocess
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _fdesetup_completed("Off"),
    )
    resp = c.put(
        "/api/security/host-policies/machine.filevault_off",
        json={"intent": "opt_out", "reason": "single-tenant dev mini in locked room"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["policy"]["acceptance"] is not None
    assert body["policy"]["acceptance"]["reason"] == "single-tenant dev mini in locked room"

    saved = json.loads(net_file.read_text())
    assert "policy_acceptances" in saved
    entry = saved["policy_acceptances"]["machine.filevault_off"]
    assert entry["reason"] == "single-tenant dev mini in locked room"
    assert entry["accepted_at"]  # ISO date stamped at write time
    assert entry["accepted_by"] == "admin-ui"


def test_put_use_clears_acceptance(client, monkeypatch):
    """PUT with intent=use removes any prior opt_out — the FileVault
    alert will resume firing on the next audit if FV is still off."""
    c, net_file = client
    import subprocess
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _fdesetup_completed("Off"),
    )
    # Pre-seed an acceptance entry.
    net = json.loads(net_file.read_text())
    net["policy_acceptances"] = {
        "machine.filevault_off": {
            "reason": "old reason",
            "accepted_at": "2026-01-01",
            "accepted_by": "admin-ui",
        }
    }
    net_file.write_text(json.dumps(net, indent=2))

    resp = c.put(
        "/api/security/host-policies/machine.filevault_off",
        json={"intent": "use"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["policy"]["acceptance"] is None

    saved = json.loads(net_file.read_text())
    # Block removed entirely when empty so we don't leave dead keys behind.
    assert "policy_acceptances" not in saved or not saved["policy_acceptances"]


def test_put_rejects_unknown_policy(client):
    c, _ = client
    resp = c.put(
        "/api/security/host-policies/machine.nope",
        json={"intent": "opt_out", "reason": "x"},
    )
    assert resp.status_code == 404


def test_put_rejects_bad_intent(client):
    c, _ = client
    resp = c.put(
        "/api/security/host-policies/machine.filevault_off",
        json={"intent": "maybe"},
    )
    assert resp.status_code == 400


def test_put_default_reason_when_omitted(client, monkeypatch):
    """Empty/missing reason still writes a self-describing default so the
    audit-trail entry doesn't read as 'no reason recorded'."""
    c, net_file = client
    import subprocess
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _fdesetup_completed("Off"),
    )
    resp = c.put(
        "/api/security/host-policies/machine.filevault_off",
        json={"intent": "opt_out"},
    )
    assert resp.status_code == 200
    saved = json.loads(net_file.read_text())
    reason = saved["policy_acceptances"]["machine.filevault_off"]["reason"]
    assert reason  # non-empty
    assert "opt" in reason.lower() or "out" in reason.lower()
