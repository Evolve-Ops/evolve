"""Tests for /api/autonomy/* — the ladder's operator-direct API.

Route-glue focus: response shapes, error codes, CAS conflict mapping,
and the promote→render→history loop. The renderer/backfill internals
have their own suites under packages/analyzer/tests/.

Seam injection per house rules: scheduler via ``set_scheduler`` (never
patch ``<module>.subprocess.run``), bot home via ``evolve_config.bot_home``
monkeypatch, and the validate-shellout via a fake
``safe_write_bot_config`` that writes the file directly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


class _FakeScheduler:
    def __init__(self):
        self.restarted: list[str] = []

    def restart(self, label: str):
        self.restarted.append(label)
        return True

    def __getattr__(self, name):
        return lambda *a, **k: True


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    home = tmp_path / "bot-homes" / "alpha"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps({
        "mcp": {"servers": {
            "google_workspace": {"command": "uvx", "args": ["workspace-mcp"]},
        }},
    }))

    network = {
        "networkId": "pod-test-1",
        "sharedDir": str(shared_dir),
        "bots": {"alpha": {"user": "alpha"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Bot-home seam: the dev machine has no "alpha" user.
    import evolve_config
    monkeypatch.setattr(
        evolve_config, "bot_home", lambda bot_id, config=None: home,
    )

    # Write seam: skip the openclaw-CLI validation shellout; write direct.
    def _fake_safe_write(bot_id, cfg, *, reason="", bot_user=None):
        (home / ".openclaw" / "openclaw.json").write_text(json.dumps(cfg, indent=2))
        return True, ""
    import evolve_admin.deploy as _deploy
    monkeypatch.setattr(_deploy, "safe_write_bot_config", _fake_safe_write)

    # Scheduler seam (feedback_subprocess_patch_fakes_break_on_module_move).
    from runtime.scheduler import set_scheduler
    sched = _FakeScheduler()
    set_scheduler(sched)

    app = create_app(network_path)
    app.config["TESTING"] = True
    try:
        yield {
            "client": app.test_client(),
            "shared_dir": shared_dir,
            "home": home,
            "scheduler": sched,
        }
    finally:
        set_scheduler(None)


def test_inventory_backfills_and_shapes_rows(app_env):
    r = app_env["client"].get("/api/autonomy/inventory")
    assert r.status_code == 200
    data = r.get_json()
    rows = data["bots"]["alpha"]["integrations"]
    assert len(rows) == 1
    row = rows[0]
    assert row["integration_id"] == "google_workspace"
    assert row["integration_label"] == "email (Google Workspace)"
    # No deny entries live → observed wider than default, unconfirmed.
    assert row["rung"] == "act_with_approval"
    assert row["rung_label"] == "Asks first"
    assert row["unconfirmed"] is True
    assert row["in_sync"] is True       # observe-only: nothing expected yet
    assert row["default_rung"] == "draft_only"
    assert row["promote"]["rung"] == "autonomous_within_rules"
    assert row["promote"]["requires_rules"] is True
    assert row["demote"]["rung"] == "draft_only"
    assert row["rung_meaning"]
    assert row["promote"]["consequence"]


def test_inventory_omits_bots_without_eligible_integrations(app_env, tmp_path):
    # Strip the MCP server → no ladder-eligible integrations → no rows,
    # and the bot key is omitted entirely (no dead affordances).
    home = app_env["home"]
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps({"mcp": {"servers": {}}}))
    r = app_env["client"].get("/api/autonomy/inventory")
    assert r.status_code == 200
    assert r.get_json()["bots"] == {}


def test_post_demote_renders_and_records_history(app_env):
    client = app_env["client"]
    client.get("/api/autonomy/inventory")  # backfill

    r = client.post(
        "/api/autonomy/alpha/google_workspace",
        json={"rung": "draft_only", "expected_current_rung": "act_with_approval"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["rung"] == "draft_only"
    assert body["render"]["written"] is True
    assert body["render"]["error"] is None

    # The mechanical surface actually changed.
    cfg = json.loads(
        (app_env["home"] / ".openclaw" / "openclaw.json").read_text()
    )
    assert "mcp__google_workspace__send_gmail_message" in cfg["tools"]["deny"]
    assert app_env["scheduler"].restarted == ["ai.openclaw.alpha-gateway"]

    # History shows actor + timestamps; posture is now deliberate.
    r2 = client.get("/api/autonomy/inventory")
    row = r2.get_json()["bots"]["alpha"]["integrations"][0]
    assert row["rung"] == "draft_only"
    assert row["unconfirmed"] is False
    assert row["in_sync"] is True
    assert row["set_by"]["actor"] == "operator_ui"
    assert row["history"][-1]["from"] == "act_with_approval"
    assert row["history"][-1]["to"] == "draft_only"
    assert row["history"][-1]["at"]
    assert {e["surface"] for e in row["enforcement"]} == {
        "mcp_tool_allowlist", "bot_guidance",
    }


def test_post_stale_cas_returns_409(app_env):
    client = app_env["client"]
    client.get("/api/autonomy/inventory")
    r = client.post(
        "/api/autonomy/alpha/google_workspace",
        json={"rung": "draft_only", "expected_current_rung": "autonomous_within_rules"},
    )
    assert r.status_code == 409
    assert r.get_json()["stale"] is True


def test_post_rung3_without_rules_400(app_env):
    client = app_env["client"]
    client.get("/api/autonomy/inventory")
    r = client.post(
        "/api/autonomy/alpha/google_workspace",
        json={"rung": "autonomous_within_rules"},
    )
    assert r.status_code == 400
    assert "rules" in r.get_json()["error"]


def test_post_unknown_bot_404(app_env):
    r = app_env["client"].post(
        "/api/autonomy/ghost/google_workspace", json={"rung": "draft_only"},
    )
    assert r.status_code == 404


# ── Phase B: actor field (evo chat front door, spec §3.1) ────────────────────


def test_post_primary_bot_actor_recorded(app_env):
    from autonomy import store as _astore

    client = app_env["client"]
    client.get("/api/autonomy/inventory")  # backfill
    r = client.post(
        "/api/autonomy/alpha/google_workspace",
        json={
            "rung": "draft_only",
            "expected_current_rung": "act_with_approval",
            "actor": "primary_bot",
            "note": "operator confirmed in chat",
        },
    )
    assert r.status_code == 200
    posture = _astore.load(
        app_env["shared_dir"], "alpha",
    ).integrations["google_workspace"]
    assert posture.set_by["actor"] == "primary_bot"
    assert posture.history[-1]["actor"] == "primary_bot"
    assert posture.history[-1]["note"] == "operator confirmed in chat"


def test_post_rejects_provenance_actors(app_env):
    # proposal:* / auto_demotion:* are written by their own code paths,
    # never via HTTP — accepting them here would let any UI caller
    # forge applier/reflex provenance.
    client = app_env["client"]
    client.get("/api/autonomy/inventory")
    for actor in ("proposal:p-1", "auto_demotion:s-1", "backfill_inferred"):
        r = client.post(
            "/api/autonomy/alpha/google_workspace",
            json={"rung": "draft_only", "actor": actor},
        )
        assert r.status_code == 400, actor
        assert "actor" in r.get_json()["error"]


# ── Phase B: restore_autonomy_posture remediation handler ────────────────────


def test_restore_remediation_handler_restores_and_renders(app_env):
    from autonomy import store as _astore
    from evolve_admin.remediation.handlers import (
        HANDLER_FIX_RISK,
        handle_restore_autonomy_posture,
    )

    shared = app_env["shared_dir"]
    client = app_env["client"]
    client.get("/api/autonomy/inventory")  # backfill
    # Simulate the reflex's demotion (rung 3 → rung 2, rules cleared).
    _astore.set_posture(
        shared, "alpha", "google_workspace",
        rung="act_with_approval",
        actor="auto_demotion:sig-1",
        expected_current_rung="act_with_approval",
    )

    out = handle_restore_autonomy_posture({
        "bot_id": "alpha",
        "integration_id": "google_workspace",
        "rung": "autonomous_within_rules",
        "rules": {"actions_per_day": 4},
        "expected_current_rung": "act_with_approval",
    }, shared)
    assert out["rung"] == "autonomous_within_rules"
    posture = _astore.load(shared, "alpha").integrations["google_workspace"]
    assert posture.rules == {"actions_per_day": 4}
    assert posture.set_by["actor"] == "operator_ui"
    # Restore is a promotion — pinned high so it never auto-fires.
    assert HANDLER_FIX_RISK["restore_autonomy_posture"] == "high"


def test_restore_remediation_handler_cas_guard(app_env):
    from autonomy import store as _astore
    from evolve_admin.remediation.handlers import handle_restore_autonomy_posture

    shared = app_env["shared_dir"]
    app_env["client"].get("/api/autonomy/inventory")
    # The operator already changed it by hand — restore must not clobber.
    _astore.set_posture(
        shared, "alpha", "google_workspace",
        rung="draft_only", actor="operator_ui",
        expected_current_rung="act_with_approval",
    )
    with pytest.raises(_astore.StalePostureError):
        handle_restore_autonomy_posture({
            "bot_id": "alpha",
            "integration_id": "google_workspace",
            "rung": "autonomous_within_rules",
            "rules": {"actions_per_day": 4},
            "expected_current_rung": "act_with_approval",
        }, shared)
