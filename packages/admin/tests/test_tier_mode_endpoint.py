"""tests/test_tier_mode_endpoint.py — PUT /api/admin/config/<bot>/tier-mode.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 5 — the per-bot
Use-pod-defaults / Custom toggle that replaces the Phase 7 per-tier provenance
control. STRICT all-or-nothing: a bot is either Custom (its own full rungs/
roles) or Use-defaults (no per-bot rungs/roles, inherits the merged default).

Coverage:
  - mode=custom materializes a per-bot override (seeded server-side) and writes
    rungs/roles wholesale via the safe config-set path, then audits.
  - mode=default clears the bot's rungs/roles (empty rungs/roles/roleCaps).
  - Payload validation: missing / bad mode → 400, no write.
  - Unknown bot → 400 before any write.
  - Write failure → 500 with a hint.
  - The materialize-yields-nothing edge → 500 (never a silent empty write).

No real bot/user names appear; placeholders only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


_SEED = {
    "rungs": [
        {"id": "low-class", "costClass": "low", "models": ["prov_a/m-small"]},
        {"id": "mid-class", "costClass": "medium", "models": ["prov_a/m-mid"]},
    ],
    "roles": {"fast": "low-class", "standard": "mid-class"},
    "roleCaps": {"power": {"maxPerDayPerBot": 10}},
}


@pytest.fixture
def app_and_calls(tmp_path, monkeypatch):
    """Flask app with stubbed config-set, materialize, and audit."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["a_bot"],
        "sharedDir": str(shared),
        "bots": {"a_bot": {"role": "member"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    cfg_calls: list[dict] = []
    audit_calls: list[dict] = []

    def _stub_cfg_set_err(bot_id, updates, **kw):
        cfg_calls.append({"bot_id": bot_id, "updates": updates})
        return {"ok": True, "bot": bot_id}, None

    def _stub_audit(action, bot_id, details, oc_keys=None):
        audit_calls.append({"action": action, "bot_id": bot_id, "details": details})

    import oc_cli
    import primary_bot
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", _stub_cfg_set_err)
    monkeypatch.setattr(
        primary_bot, "materialize_bot_tier_override",
        lambda net, bot_id: json.loads(json.dumps(_SEED)),
    )
    monkeypatch.setattr(srv, "_audit_log_entry", _stub_audit)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, cfg_calls, audit_calls


# ── Customize (use-defaults → custom) ─────────────────────────────────────────


def test_customize_materializes_and_writes_rungs_roles(app_and_calls):
    """mode=custom writes the materialized rungs/roles wholesale, then audits.

    The seed is computed server-side; the endpoint forwards it as the
    rungs/roles/roleCaps update keys so the bot now defines its own tier set
    (flips it to Custom) without changing what the gateway routes."""
    app, cfg_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put("/api/admin/config/a_bot/tier-mode", json={"mode": "custom"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and body["mode"] == "custom"

    assert len(cfg_calls) == 1
    updates = cfg_calls[0]["updates"]
    # The seeded rungs/roles are written wholesale (becomes Custom).
    assert updates["rungs"] == _SEED["rungs"]
    assert updates["roles"] == _SEED["roles"]
    assert updates["roleCaps"] == _SEED["roleCaps"]

    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "config.tier_mode.set"
    assert audit_calls[0]["details"] == {"mode": "custom"}


def test_customize_with_empty_seed_returns_500(tmp_path, monkeypatch):
    """If materialize yields no rungs (e.g. an unresolvable catalog), the
    endpoint must 500 rather than write an empty Custom config that would leave
    the bot with no tier definitions at all."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {"sharedDir": str(shared), "bots": {"a_bot": {"role": "member"}}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    wrote: list = []
    import oc_cli
    import primary_bot
    monkeypatch.setattr(
        primary_bot, "materialize_bot_tier_override",
        lambda net, bot_id: {"rungs": [], "roles": {}},
    )
    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error",
        lambda *a, **kw: (wrote.append(a) or ({"ok": True}, None)),
    )
    monkeypatch.setattr(srv, "_audit_log_entry", lambda *a, **kw: None)

    app = create_app(network_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.put("/api/admin/config/a_bot/tier-mode", json={"mode": "custom"})
    assert resp.status_code == 500
    assert wrote == [], "no write may happen when the seed is empty"


# ── Reset (custom → use-defaults) ─────────────────────────────────────────────


def test_reset_clears_rungs_roles(app_and_calls):
    """mode=default writes empties for rungs/roles/roleCaps (and autoUpgrade —
    spec-model-auto-upgrade lifecycle rule 2); the oc_model wholesale path
    drops them so the file carries no per-bot tier definitions and the merge
    inherits the pod/code default in full, auto-upgrade posture included."""
    app, cfg_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put("/api/admin/config/a_bot/tier-mode", json={"mode": "default"})
    assert resp.status_code == 200
    assert resp.get_json()["mode"] == "default"

    assert len(cfg_calls) == 1
    updates = cfg_calls[0]["updates"]
    assert updates == {"rungs": [], "roles": {}, "roleCaps": {}, "autoUpgrade": {}}
    assert audit_calls[0]["details"] == {"mode": "default"}


def test_customize_seeds_pod_auto_upgrade_block(tmp_path, monkeypatch):
    """Lifecycle rule 1 (spec-model-auto-upgrade §Scope): flipping to Custom
    inherits the pod's CURRENT autoUpgrade block as a literal seed, so
    customizing rungs does not silently change whether the bot rides the
    latest version. No pod block → no seed (code default governs either way)."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    pod_au = {"enabled": True, "applyDay": "friday"}
    network = {
        "sharedDir": str(shared),
        "bots": {"a_bot": {"role": "member"}},
        "models": {"autoUpgrade": dict(pod_au)},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    cfg_calls: list[dict] = []
    import oc_cli
    import primary_bot
    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error",
        lambda bot_id, updates, **kw: (
            cfg_calls.append({"bot_id": bot_id, "updates": updates})
            or ({"ok": True}, None)
        ),
    )
    monkeypatch.setattr(
        primary_bot, "materialize_bot_tier_override",
        lambda net, bot_id: json.loads(json.dumps(_SEED)),
    )
    monkeypatch.setattr(srv, "_audit_log_entry", lambda *a, **kw: None)

    app = create_app(network_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.put("/api/admin/config/a_bot/tier-mode", json={"mode": "custom"})
    assert resp.status_code == 200
    assert len(cfg_calls) == 1
    assert cfg_calls[0]["updates"]["autoUpgrade"] == pod_au


def test_customize_without_pod_auto_upgrade_seeds_nothing(app_and_calls):
    """No pod autoUpgrade block → the Custom seed carries no autoUpgrade key
    (the bot resolves to the code default, exactly as the pod did)."""
    app, cfg_calls, _audit = app_and_calls
    with app.test_client() as c:
        resp = c.put("/api/admin/config/a_bot/tier-mode", json={"mode": "custom"})
    assert resp.status_code == 200
    assert "autoUpgrade" not in cfg_calls[0]["updates"]


# ── Validation + failure ──────────────────────────────────────────────────────


def test_missing_mode_returns_400(app_and_calls):
    app, cfg_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put("/api/admin/config/a_bot/tier-mode", json={})
    assert resp.status_code == 400
    assert cfg_calls == [] and audit_calls == []


def test_bad_mode_returns_400(app_and_calls):
    app, cfg_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        for bad in ("on", "use-defaults", "Custom", True, 1):
            resp = c.put("/api/admin/config/a_bot/tier-mode", json={"mode": bad})
            assert resp.status_code == 400, f"mode {bad!r} should be rejected"
    assert cfg_calls == [] and audit_calls == []


def test_unknown_bot_returns_400(app_and_calls):
    app, cfg_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put("/api/admin/config/ghost/tier-mode", json={"mode": "custom"})
    assert resp.status_code == 400
    assert cfg_calls == [] and audit_calls == []


def test_write_failure_returns_500(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {"sharedDir": str(shared), "bots": {"a_bot": {"role": "member"}}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    import oc_cli
    import primary_bot
    monkeypatch.setattr(
        primary_bot, "materialize_bot_tier_override",
        lambda net, bot_id: json.loads(json.dumps(_SEED)),
    )
    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error",
        lambda *a, **kw: (None, "disk full"),
    )
    monkeypatch.setattr(srv, "_audit_log_entry", lambda *a, **kw: None)

    app = create_app(network_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.put("/api/admin/config/a_bot/tier-mode", json={"mode": "default"})
    assert resp.status_code == 500
    assert "disk full" in resp.get_json()["error"]
