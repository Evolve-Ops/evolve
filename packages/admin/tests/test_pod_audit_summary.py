"""Tests for /api/applications/pod-summary (audit-extensions follow-up).

Powers the Apps page header strip. Verifies the aggregation buckets
(healthy, with_findings, failed, never_audited) reflect the per-bot
manifest state and that the infra-findings segment is sourced from
infra_audit.latest_run_summary.
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
    from evolve_admin.web.server import create_app

    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}, "admin_bot": {"user": "admin_bot"}},
        "sharedDir": str(tmp_path / "shared"),
    }
    (tmp_path / "shared").mkdir()
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))

    # Stub out _list_manifests_as_bot so the test controls the manifest set.
    manifests = {
        "team_bot_a":   [tmp_path / "team_bot_a"   / "m1.json", tmp_path / "team_bot_a"   / "m2.json"],
        "admin_bot": [tmp_path / "admin_bot" / "m1.json"],
    }
    for bot, paths in manifests.items():
        (tmp_path / bot).mkdir()

    def _write(bot, app_id, last_audit):
        p = tmp_path / bot / f"{app_id}.json"
        p.write_text(json.dumps({"id": app_id, "last_audit": last_audit}))
        return p

    # Two healthy on team_bot_a, one with-findings on admin_bot.
    paths = []
    paths.append(_write("team_bot_a", "m1", {
        "verified_at": "2026-05-15T12:00:00Z",
        "status": "ok",
        "outcomes": {"propose": 0, "dismiss": 2},
    }))
    paths.append(_write("team_bot_a", "m2", {
        "verified_at": "2026-05-16T12:00:00Z",
        "status": "with_findings",
        "outcomes": {"propose": 2, "dismiss": 1},
    }))
    paths.append(_write("admin_bot", "m1", {
        "verified_at": "2026-05-17T08:00:00Z",
        "status": "failed",
        "outcomes": {},
        "error": "tier3 ran out of budget",
    }))

    by_bot = {
        "team_bot_a":   [str(tmp_path / "team_bot_a"   / "m1.json"),
                  str(tmp_path / "team_bot_a"   / "m2.json")],
        "admin_bot": [str(tmp_path / "admin_bot" / "m1.json")],
    }

    import evolve_admin.web.server as server_mod
    monkeypatch.setattr(
        server_mod, "_list_manifests_as_bot",
        lambda bot, user=None: by_bot.get(bot, []),
    )

    # Stub infra_audit.latest_run_summary
    class _StubInfra:
        @staticmethod
        def latest_run_summary(shared_dir=None):
            return {
                "kind": "infra_run_summary",
                "completed_at": "2026-05-17T12:00:00Z",
                "findings_count": 4,
            }

    monkeypatch.setattr(
        "evolve_admin.applications.infra_audit.latest_run_summary",
        _StubInfra.latest_run_summary,
    )

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_pod_summary_buckets(client) -> None:
    resp = client.get("/api/applications/pod-summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    apps = data["apps"]
    assert apps["total"] == 3
    assert apps["healthy"] == 1            # team_bot_a/m1
    assert apps["with_findings"] == 1      # team_bot_a/m2
    assert apps["failed"] == 1             # admin_bot/m1
    assert apps["never_audited"] == 0
    # last_sweep is the max verified_at
    assert apps["last_sweep"] == "2026-05-17T08:00:00Z"


def test_pod_summary_includes_infra_findings(client) -> None:
    resp = client.get("/api/applications/pod-summary")
    data = resp.get_json()
    assert data["infra"]["total_infra_findings"] == 4
    assert data["infra"]["last_run"] == "2026-05-17T12:00:00Z"


def test_pod_summary_empty_network(tmp_path, monkeypatch) -> None:
    """Empty network → all buckets zero; endpoint still 200."""
    from evolve_admin.web.server import create_app
    network = {"bots": {}, "sharedDir": str(tmp_path / "shared")}
    (tmp_path / "shared").mkdir()
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))
    monkeypatch.setattr(
        "evolve_admin.applications.infra_audit.latest_run_summary",
        lambda shared_dir=None: None,
    )
    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.get("/api/applications/pod-summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["apps"]["total"] == 0
    assert data["infra"]["total_infra_findings"] == 0


def test_apps_header_strip_renders_infra_findings_segment() -> None:
    """index.html source contains the loadPageSummary_apps function with
    the infra-findings branch + the right copy."""
    src = (Path(__file__).resolve().parents[1].parent / "admin"
           / "evolve_admin" / "web" / "index.html").read_text()
    assert "async function loadPageSummary_apps()" in src
    # Spec wording (from the user task)
    assert "apps healthy" in src
    assert "with audit findings" in src
    assert "audit failed" in src
    assert "last audit sweep" in src
    assert "infra finding" in src
    # And the dispatch is wired in
    assert "if (page === 'apps')              loadPageSummary_apps();" in src
