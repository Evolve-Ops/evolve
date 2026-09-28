"""Tests for the `evo audit infra` handler (Workstream B-infra)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))


def test_evo_audit_infra_routes_through_audit_handler(monkeypatch, tmp_path) -> None:
    """`evo audit infra` → infra_audit handler, not the security viewer."""
    from evolve_admin.evo.handlers import audit as audit_handler
    from evolve_admin.evo.handlers import infra_audit as infra_handler

    called = {}

    def fake_render(*, role, bot_id, args, network):
        called["args"] = args
        called["bot_id"] = bot_id
        return {"speak": "stub"}

    monkeypatch.setattr(infra_handler, "render", fake_render)

    audit_handler.render(
        role="primary", bot_id="team_bot_a",
        args="infra status",
        network={"sharedDir": str(tmp_path)},
    )
    assert called.get("args") == "status"
    assert called.get("bot_id") == "team_bot_a"


def test_evo_audit_infra_kick_calls_request(monkeypatch, tmp_path) -> None:
    """Bare `evo audit infra` invokes request_infra_audit()."""
    from evolve_admin.evo.handlers import infra_audit as infra_handler

    called = {}
    def fake_request(*, requested_by, elements):
        called["requested_by"] = requested_by
        called["elements"] = elements
        return {"ok": True, "request_id": "test-1", "started": True}

    monkeypatch.setattr(
        "evolve_admin.applications.infra_audit.request_infra_audit",
        fake_request,
    )

    result = infra_handler.render(
        role="primary", bot_id="team_bot_a", args="",
        network={"sharedDir": str(tmp_path)},
    )
    assert called["elements"] is None
    body = result.get("body") if isinstance(result, dict) else str(result)
    # speak() returns a DispatchResult — extract the spoken text whichever
    # field carries it.
    text = json.dumps(result, default=str)
    assert "Started auditing pod infrastructure" in text


def test_evo_audit_infra_element_filter(monkeypatch, tmp_path) -> None:
    """`evo audit infra daemons` restricts to one element."""
    from evolve_admin.evo.handlers import infra_audit as infra_handler

    called = {}
    def fake_request(*, requested_by, elements):
        called["elements"] = elements
        return {"ok": True, "request_id": "test-2", "started": True}
    monkeypatch.setattr(
        "evolve_admin.applications.infra_audit.request_infra_audit",
        fake_request,
    )

    result = infra_handler.render(
        role="primary", bot_id="team_bot_a", args="daemons",
        network={"sharedDir": str(tmp_path)},
    )
    assert called["elements"] == ["daemons"]
    text = json.dumps(result, default=str)
    assert "daemons" in text


def test_evo_audit_infra_unknown_element_rejects(monkeypatch, tmp_path) -> None:
    """Unknown element name → helpful error message, no request fires."""
    from evolve_admin.evo.handlers import infra_audit as infra_handler

    called = {}
    def fake_request(**_):
        called["fired"] = True
        return {"ok": True}
    monkeypatch.setattr(
        "evolve_admin.applications.infra_audit.request_infra_audit",
        fake_request,
    )

    result = infra_handler.render(
        role="primary", bot_id="team_bot_a", args="garbage",
        network={"sharedDir": str(tmp_path)},
    )
    assert "fired" not in called
    text = json.dumps(result, default=str)
    assert "Unknown element" in text
