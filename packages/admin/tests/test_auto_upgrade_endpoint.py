"""tests/test_auto_upgrade_endpoint.py — GET/PUT /api/models/auto-upgrade.

Spec: docs/spec-model-auto-upgrade-2026-07-30.md §Scope — the auto-upgrade
toggle is a TOP-LEVEL control on the tier-definition cards (pod defaults card
+ each Custom bot's tier card), backed by these two routes; the easy-setup
modal only mirrors the posture and never writes it.

Coverage:
  - GET pod: resolved policy (code default = disabled), the governance split
    (Use-defaults bots governed, Custom bots excluded by name), and a family
    map covering the scope's catalog models.
  - GET unknown bot → 404.
  - PUT pod: merges ``enabled`` into ``models.autoUpgrade`` preserving
    hand-set subordinate knobs AND the sibling ``models.embedding`` block.
  - PUT bot on Use-defaults → 400 (spec §Scope: it follows the pod toggle).
  - PUT bot on Custom → partial-merge ``{"autoUpgrade": {"enabled": ...}}``
    through the config-set seam.
  - Bad body (missing scope / non-bool enabled) → 400; unknown bot → 404.

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


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Flask app: two bots (one Custom, one Use-defaults), an embedding block
    that must survive pod writes, and stubbed config-set + audit seams."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    embedding = {"provider": "prov_a", "model": "prov_a/embed-1", "dimensions": 256}
    network = {
        "members": ["a_bot", "b_bot"],
        "sharedDir": str(shared),
        "bots": {
            "a_bot": {"role": "member", "user": "a_bot"},
            "b_bot": {"role": "member", "user": "b_bot"},
        },
        "models": {
            "embedding": dict(embedding),
            "autoUpgrade": {"enabled": False, "applyDay": "tuesday"},
        },
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network, indent=2))

    cfg_calls: list[dict] = []
    audit_calls: list[dict] = []

    def fake_cfg_set_err(bot_id, updates, **kw):
        cfg_calls.append({"bot_id": bot_id, "updates": updates})
        return {"ok": True, "bot": bot_id}, None

    def fake_audit(action, bot_id, details, oc_keys=None):
        audit_calls.append({"action": action, "bot_id": bot_id, "details": details})

    # a_bot is Custom (owns its toggle); b_bot follows the pod defaults.
    def fake_custom(net, bot_id):
        return bot_id == "a_bot"

    import oc_cli
    import primary_bot
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", fake_cfg_set_err)
    monkeypatch.setattr(primary_bot, "bot_has_custom_tiers", fake_custom)
    monkeypatch.setattr(srv, "_audit_log_entry", fake_audit)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return {
        "app": app,
        "network_path": network_path,
        "cfg_calls": cfg_calls,
        "audit_calls": audit_calls,
        "embedding": embedding,
    }


def _read_models(network_path: Path) -> dict:
    return json.loads(network_path.read_text()).get("models", {})


# ── GET ─────────────────────────────────────────────────────────────────────────


def test_get_pod_state_policy_and_governance(app_env):
    with app_env["app"].test_client() as c:
        resp = c.get("/api/models/auto-upgrade?scope=pod")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True and body["scope"] == "pod"
    assert body["auto_upgrade"]["enabled"] is False
    # Governance split (spec §migration-era hazard): the Custom bot is named
    # as excluded; the Use-defaults bot is governed.
    assert body["auto_upgrade_governed"] == ["b_bot"]
    assert body["auto_upgrade_excluded"] == ["a_bot"]
    # Family map covers the scope's catalog models (the real
    # DEFAULT_MODEL_CATALOG — every merged rung model gets a stem).
    fams = body["families"]
    assert isinstance(fams, dict) and fams
    import primary_bot
    view = primary_bot.pod_default_catalog_view(json.loads(app_env["network_path"].read_text()))
    for rung in view["rungs"]:
        for m in rung["models"]:
            assert m in fams and fams[m]


def test_get_defaults_to_pod_scope(app_env):
    with app_env["app"].test_client() as c:
        resp = c.get("/api/models/auto-upgrade")
    assert resp.status_code == 200
    assert resp.get_json()["scope"] == "pod"


def test_get_bot_scope_reports_ownership(app_env):
    with app_env["app"].test_client() as c:
        custom = c.get("/api/models/auto-upgrade?scope=a_bot").get_json()
        inherits = c.get("/api/models/auto-upgrade?scope=b_bot").get_json()
    assert custom["custom_tiers"] is True
    assert inherits["custom_tiers"] is False
    # A Use-defaults bot resolves to the pod posture it follows.
    assert inherits["auto_upgrade"]["enabled"] is False


def test_get_unknown_bot_404(app_env):
    with app_env["app"].test_client() as c:
        resp = c.get("/api/models/auto-upgrade?scope=nope_bot")
    assert resp.status_code == 404


# ── PUT pod ─────────────────────────────────────────────────────────────────────


def test_put_pod_merges_enabled_and_preserves_siblings(app_env):
    with app_env["app"].test_client() as c:
        resp = c.put("/api/models/auto-upgrade", json={"scope": "pod", "enabled": True})
    assert resp.status_code == 200, resp.get_json()
    models = _read_models(app_env["network_path"])
    # enabled flipped; the hand-set subordinate knob survives the merge.
    assert models["autoUpgrade"]["enabled"] is True
    assert models["autoUpgrade"]["applyDay"] == "tuesday"
    # The sibling embedding block is untouched (autoUpgrade-granular patch).
    assert models["embedding"] == app_env["embedding"]
    assert app_env["audit_calls"] == [{
        "action": "config.auto_upgrade.set", "bot_id": "pod",
        "details": {"enabled": True},
    }]


# ── PUT bot ─────────────────────────────────────────────────────────────────────


def test_put_use_defaults_bot_rejected(app_env):
    """Spec §Scope: a Use-defaults bot has no toggle of its own — it follows
    the pod. The write is refused, not silently forked into a bot doc."""
    with app_env["app"].test_client() as c:
        resp = c.put("/api/models/auto-upgrade", json={"scope": "b_bot", "enabled": True})
    assert resp.status_code == 400
    assert "pod" in resp.get_json()["error"]
    assert app_env["cfg_calls"] == []


def test_put_custom_bot_partial_merges_enabled(app_env):
    with app_env["app"].test_client() as c:
        resp = c.put("/api/models/auto-upgrade", json={"scope": "a_bot", "enabled": True})
    assert resp.status_code == 200, resp.get_json()
    assert app_env["cfg_calls"] == [{
        "bot_id": "a_bot", "updates": {"autoUpgrade": {"enabled": True}},
    }]
    # The pod block is untouched by a bot-scope write.
    assert _read_models(app_env["network_path"])["autoUpgrade"]["enabled"] is False


# ── validation ──────────────────────────────────────────────────────────────────


def test_put_bad_bodies_400(app_env):
    with app_env["app"].test_client() as c:
        assert c.put("/api/models/auto-upgrade", json={"enabled": True}).status_code == 400
        # A string "true" is not a decision to change the pod's model config.
        assert c.put("/api/models/auto-upgrade", json={"scope": "pod", "enabled": "true"}).status_code == 400
        assert c.put("/api/models/auto-upgrade", json={"scope": "pod"}).status_code == 400


def test_put_unknown_bot_404(app_env):
    with app_env["app"].test_client() as c:
        resp = c.put("/api/models/auto-upgrade", json={"scope": "nope_bot", "enabled": True})
    assert resp.status_code == 404
