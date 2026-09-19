"""tests/test_cascade_endpoint.py — PUT /api/admin/config/<bot>/cascade.

The cascade endpoint flips the Phase-3 cutover flag for tier-cascade
routing on a per-bot basis. The plugin's ModelRouter reads
``evolve-tiers.json::cascade.enabled`` to decide whether the
CascadeController's per-session verdicts drive routing or stay
shadow-only.

Coverage:
  - PUT with {"enabled": true} writes through oc_full_config_set
    with the cascade key, then audits the change
  - PUT with {"enabled": false} flips back through the same path
  - Payload validation: missing or non-bool "enabled" returns 400
  - Write failure surfaces a clear error
  - Audit log entry has the right shape (action, bot_id, enabled)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def app_and_calls(tmp_path, monkeypatch):
    """Build a Flask app with stubbed oc_full_config_set + audit_log."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["admin_bot"],
        "sharedDir": str(shared),
        "bots": {"admin_bot": {"role": "member"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    cfg_set_calls: list[dict] = []
    audit_calls: list[dict] = []

    def _stub_cfg_set(bot_id, updates, **kw):
        cfg_set_calls.append({"bot_id": bot_id, "updates": updates})
        # Mirror the real shape — the endpoint expects `cascade` back.
        return {"cascade": updates.get("cascade", {})}

    def _stub_audit(action, bot_id, details, oc_keys=None):
        audit_calls.append({"action": action, "bot_id": bot_id, "details": details})

    import oc_cli
    monkeypatch.setattr(oc_cli, "oc_full_config_set", _stub_cfg_set)
    monkeypatch.setattr(srv, "_audit_log_entry", _stub_audit)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, cfg_set_calls, audit_calls


# ── Happy path ──────────────────────────────────────────────────────────────


def test_put_enables_cascade_for_bot(app_and_calls):
    """PUT {"enabled": true} writes cascade.enabled=true via
    oc_full_config_set and audits the change. Response echoes the
    written cascade key so the UI can update local state without a
    follow-up GET."""
    app, cfg_set_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put("/api/admin/config/admin_bot/cascade", json={"enabled": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["bot"] == "admin_bot"
    assert body["cascade"]["enabled"] is True

    # Underlying write call
    assert len(cfg_set_calls) == 1
    assert cfg_set_calls[0]["bot_id"] == "admin_bot"
    assert cfg_set_calls[0]["updates"] == {"cascade": {"enabled": True}}

    # Audit log entry
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "config.cascade.set"
    assert audit_calls[0]["bot_id"] == "admin_bot"
    assert audit_calls[0]["details"] == {"enabled": True}


def test_put_disables_cascade_for_bot(app_and_calls):
    """Same endpoint flips back. No separate disable route."""
    app, cfg_set_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put("/api/admin/config/admin_bot/cascade", json={"enabled": False})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["cascade"]["enabled"] is False
    assert cfg_set_calls[0]["updates"] == {"cascade": {"enabled": False}}
    assert audit_calls[0]["details"] == {"enabled": False}


# ── Payload validation ─────────────────────────────────────────────────────


def test_put_missing_enabled_returns_400(app_and_calls):
    app, cfg_set_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put("/api/admin/config/admin_bot/cascade", json={})
    assert resp.status_code == 400
    assert "enabled" in resp.get_json()["error"]
    # No write attempted
    assert cfg_set_calls == []
    assert audit_calls == []


def test_put_non_bool_enabled_returns_400(app_and_calls):
    """Strings like "true" / "1" / 1 must not flip the switch — the
    JSON contract is strict bool. Catches client-side coercion errors
    that would otherwise silently no-op or worse, set cascade.enabled
    to a truthy-but-wrong value the plugin can't reason about."""
    app, cfg_set_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        for bad in ("true", "false", 1, 0, None, "yes"):
            resp = c.put(
                "/api/admin/config/admin_bot/cascade", json={"enabled": bad},
            )
            assert resp.status_code == 400, f"value {bad!r} should be rejected"
    assert cfg_set_calls == []
    assert audit_calls == []


# ── Unknown bot rejection ──────────────────────────────────────────────────


def test_put_unknown_bot_id_returns_400(app_and_calls):
    """Bot IDs not present in network.json::bots must be rejected with
    400 BEFORE the writer runs. Without this guard, the URL path string
    flows through to oc_cli._resolve_user, which falls back to using
    bot_id as a unix username, and we shell out to ``sudo -u <X>`` —
    failing opaquely with "unknown user". Seen 2026-06-03 when a batch
    enabled cascade on "forge" (a feature name, not a bot)."""
    app, cfg_set_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/forge/cascade", json={"enabled": True},
        )
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert "forge" in err and "network.json" in err
    # The writer was never invoked
    assert cfg_set_calls == []
    assert audit_calls == []


# ── Failure path ────────────────────────────────────────────────────────────


def test_put_returns_500_when_underlying_write_fails(monkeypatch, tmp_path):
    """When oc_full_config_set returns falsy, surface 500 with a hint
    — operators need to know the write didn't land."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["admin_bot"],
        "sharedDir": str(shared),
        "bots": {"admin_bot": {"role": "member"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    import oc_cli
    monkeypatch.setattr(oc_cli, "oc_full_config_set", lambda *a, **kw: None)
    monkeypatch.setattr(srv, "_audit_log_entry", lambda *a, **kw: None)

    app = create_app(network_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.put("/api/admin/config/admin_bot/cascade", json={"enabled": True})
    assert resp.status_code == 500
    assert "write failed" in resp.get_json()["error"]
