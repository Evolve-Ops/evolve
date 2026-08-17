"""Tests for the substrate audit chip endpoints (audit-extensions follow-up).

Exercises the deferred surfaces from PR #1218 that power the per-row
audit pill, "Run audit now" button, and cadence dropdown on the Skills
and OAuth provider pages:

  - GET /api/skills/<bot>/audit-status        — aggregated chip state
  - GET /api/providers/<bot>/audit-status     — same for providers
  - GET /api/skills/<bot>/audit-cadence       — read per-bot override
  - PUT /api/skills/<bot>/audit-cadence       — write per-bot override
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Build a test client with a fake bot whose audit trails live in tmp."""
    from evolve_admin.web.server import create_app

    fake_user_home = tmp_path / "Users" / "team_bot_a"
    fake_user_home.mkdir(parents=True)
    (fake_user_home / ".openclaw" / "workspace" / "evolve" / "skill_audits"
     / "gmail").mkdir(parents=True)
    (fake_user_home / ".openclaw" / "workspace" / "evolve" / "provider_audits"
     / "google_workspace").mkdir(parents=True)

    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "skill_audit": {"default_cadence": "weekly", "bot_cadence": {}},
        "provider_audit": {"default_cadence": "weekly", "bot_cadence": {}},
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))

    # Force the substrate-audit endpoint's path construction (which hardcodes
    # /Users/<bot>) to look under our tmp dir.
    import evolve_admin.web.server as server_mod
    orig_path = server_mod.Path

    class _ScopedPath(type(orig_path("/"))):
        def __new__(cls, *args, **kwargs):
            if args and isinstance(args[0], str) and args[0].startswith("/Users/"):
                return orig_path(str(tmp_path) + args[0])
            return orig_path(*args, **kwargs)

    monkeypatch.setattr(server_mod, "Path", _ScopedPath)

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, fake_user_home, net_file


def _write_trail(home: Path, parent: str, element: str, entries: list[dict]) -> None:
    trail = (
        home / ".openclaw" / "workspace" / "evolve" / parent / element
        / "trail.jsonl"
    )
    trail.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


# ── audit-status ────────────────────────────────────────────────────────────


def test_skill_audit_status_returns_never_when_no_trail(client) -> None:
    c, _, _ = client
    resp = c.get("/api/skills/team_bot_a/audit-status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    # We pre-created the gmail dir but didn't write a trail; status="never".
    assert data["elements"]["gmail"]["status"] == "never"
    assert data["elements"]["gmail"]["raised_count"] == 0


def test_skill_audit_status_reports_healthy(client) -> None:
    c, home, _ = client
    _write_trail(home, "skill_audits", "gmail", [
        {
            "ts": "2026-05-17T12:00:00Z",
            "kind": "audit_run",
            "status": "ok",
            "findings_count": 0,
            "outcomes": {"dismiss": 1, "auto_fix": 0, "propose": 0},
        },
    ])
    resp = c.get("/api/skills/team_bot_a/audit-status")
    data = resp.get_json()
    assert data["elements"]["gmail"]["status"] == "healthy"
    assert data["elements"]["gmail"]["raised_count"] == 0


def test_skill_audit_status_reports_findings(client) -> None:
    c, home, _ = client
    _write_trail(home, "skill_audits", "gmail", [
        {
            "ts": "2026-05-17T12:00:00Z",
            "kind": "audit_run",
            "status": "with_findings",
            "findings_count": 3,
            "outcomes": {"dismiss": 0, "auto_fix": 0, "propose": 3},
        },
    ])
    resp = c.get("/api/skills/team_bot_a/audit-status")
    data = resp.get_json()
    assert data["elements"]["gmail"]["status"] == "findings"
    assert data["elements"]["gmail"]["raised_count"] == 3


def test_skill_audit_status_reports_failed(client) -> None:
    c, home, _ = client
    _write_trail(home, "skill_audits", "gmail", [
        {
            "ts": "2026-05-17T12:00:00Z",
            "kind": "audit_run",
            "status": "failed",
            "findings_count": 0,
            "outcomes": {},
            "error": "Stage 3a timeout",
        },
    ])
    resp = c.get("/api/skills/team_bot_a/audit-status")
    data = resp.get_json()
    assert data["elements"]["gmail"]["status"] == "failed"
    assert data["elements"]["gmail"]["error"] == "Stage 3a timeout"


def test_provider_audit_status_endpoint(client) -> None:
    c, home, _ = client
    _write_trail(home, "provider_audits", "google_workspace", [
        {
            "ts": "2026-05-17T11:00:00Z",
            "kind": "audit_run",
            "status": "with_findings",
            "findings_count": 1,
            "outcomes": {"propose": 1},
        },
    ])
    resp = c.get("/api/providers/team_bot_a/audit-status")
    data = resp.get_json()
    assert data["ok"] is True
    assert data["element_type"] == "provider"
    assert data["elements"]["google_workspace"]["status"] == "findings"


def test_audit_status_unknown_bot_returns_404(client) -> None:
    c, _, _ = client
    resp = c.get("/api/skills/notabot/audit-status")
    assert resp.status_code == 404


# ── audit-cadence ───────────────────────────────────────────────────────────


def test_get_audit_cadence_default(client) -> None:
    c, _, _ = client
    resp = c.get("/api/skills/team_bot_a/audit-cadence")
    data = resp.get_json()
    assert data["ok"] is True
    assert data["pod_default"] == "weekly"
    assert data["bot_override"] is None
    assert data["effective"] == "weekly"


def test_put_audit_cadence_writes_override(client) -> None:
    c, _, net_file = client
    resp = c.put("/api/skills/team_bot_a/audit-cadence", json={"cadence": "daily"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["bot_override"] == "daily"
    # Persistence
    cfg = json.loads(net_file.read_text())
    assert cfg["skill_audit"]["bot_cadence"]["team_bot_a"] == "daily"


def test_put_audit_cadence_inherit_deletes_override(client) -> None:
    c, _, net_file = client
    # Set first
    c.put("/api/skills/team_bot_a/audit-cadence", json={"cadence": "daily"})
    # Now clear it
    resp = c.put("/api/skills/team_bot_a/audit-cadence", json={"cadence": "inherit"})
    assert resp.status_code == 200
    cfg = json.loads(net_file.read_text())
    assert "team_bot_a" not in cfg["skill_audit"].get("bot_cadence", {})


def test_put_audit_cadence_rejects_bad_value(client) -> None:
    c, _, _ = client
    resp = c.put("/api/skills/team_bot_a/audit-cadence", json={"cadence": "hourly"})
    assert resp.status_code == 400


def test_put_provider_audit_cadence_writes_to_correct_block(client) -> None:
    c, _, net_file = client
    resp = c.put(
        "/api/providers/team_bot_a/audit-cadence", json={"cadence": "monthly"},
    )
    assert resp.status_code == 200
    cfg = json.loads(net_file.read_text())
    assert cfg["provider_audit"]["bot_cadence"]["team_bot_a"] == "monthly"
    # Skill cadence untouched
    assert "team_bot_a" not in cfg.get("skill_audit", {}).get("bot_cadence", {})
