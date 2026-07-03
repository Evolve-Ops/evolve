"""
Tests for POST /api/bots/<bot_id>/reflect/strip-marker (+ the bulk
/reflect/strip-markers variant).

The scrub action removes an ``evolve:`` provenance marker that was mis-stamped
onto a path no application may own — platform telemetry under
``workspace/evolve/``, an OpenClaw-standard file like ``AGENTS.md``, or a marker
whose owning app is gone. The key safety property: the endpoint re-classifies
the target server-side against the reconciliation ledger and REFUSES to strip
anything the ledger calls owned / attach / missing (defense in depth).

Placeholder bot names only (no real bot identities) per the public-launch
scrub guard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.provenance import embed_marker, parse_marker


@pytest.fixture
def strip_env(tmp_path, monkeypatch):
    """create_app + a bot workspace, with bot_home patched on both the recon
    ledger (which classifies) and the sync-routes module (the traversal guard)
    so they meet on the same tmp workspace."""
    from evolve_admin.web.server import create_app
    from evolve_admin.applications import recon_ledger as rl
    from evolve_admin.applications import reflect as rfl
    from evolve_admin.web import routes_applications_sync as ras

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    bot_id = "team_bot_a"
    bot_dir = tmp_path / "bot-homes" / bot_id
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)

    network = {
        "networkId": "pod-test-1",
        "sharedDir": str(shared_dir),
        "bots": {bot_id: {"user": bot_id}},
        "members": [bot_id],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    monkeypatch.setattr(rl, "bot_home", lambda _bid, *a, **k: bot_dir)
    monkeypatch.setattr(rfl, "bot_home", lambda _bid, *a, **k: bot_dir)
    monkeypatch.setattr(ras, "bot_home", lambda _bid, *a, **k: bot_dir)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client(), {
        "bot_id": bot_id,
        "workspace": workspace,
        "manifests": workspace / "manifests",
        "scripts": workspace / "scripts",
    }


# ── Builders ──────────────────────────────────────────────────────────────────

def _stamp(p: Path, spec_id: str, file_id: str, version: str = "2026.05.20-1.0") -> None:
    embed_marker(
        p, pkg_ids=[spec_id], file_id=file_id,
        pkg_versions={spec_id: version}, file_version=version,
        keyword="spec", merge=False,
    )


def _make_instance(env, instance_id: str, spec_id: str, realized: list[dict]) -> None:
    inst = {
        "instance_id": instance_id,
        "bot_id": env["bot_id"],
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": "2026.05.20-1.0",
            "installed_at": "2026-05-20T00:00:00Z",
            "installed_by": "test",
        },
        "realized_files": realized,
        "status": "active",
    }
    (env["manifests"] / f"{instance_id}.json").write_text(json.dumps(inst))


def _rf(path: Path, file_id: str) -> dict:
    return {"logical_name": path.stem, "path": str(path.resolve()),
            "file_id": file_id, "marker_state": "OWNED"}


def _write(env, relpath: str, content: str = "x = 1\n") -> Path:
    p = env["workspace"] / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ── Happy path: strip a scrub candidate ───────────────────────────────────────

class TestStripScrubCandidate:
    def test_strips_telemetry_marker(self, strip_env):
        client, env = strip_env
        # A marker mis-stamped onto platform telemetry under evolve/ — ineligible.
        p = _write(env, "evolve/audit_outbox/_ingested/rec-1.json",
                   content=json.dumps({"finding": "x"}))
        _stamp(p, "p-cccc3333", "f-rec00001")
        assert parse_marker(p) is not None  # marker present before

        r = client.post("/api/bots/team_bot_a/reflect/strip-marker",
                        json={"path": "evolve/audit_outbox/_ingested/rec-1.json"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["stripped"] is True
        # Marker gone, file content preserved.
        assert parse_marker(p) is None
        assert json.loads(p.read_text())["finding"] == "x"

    def test_strips_agents_md_marker(self, strip_env):
        """AGENTS.md at the workspace root is an OpenClaw-standard file — scrub,
        not attach. (Dev laptop has write access, so the direct strip lands; the
        PermissionError → 403 manual_cli path can't be exercised in unit tests.)"""
        client, env = strip_env
        p = _write(env, "AGENTS.md", content="<!-- marker below -->\n# Standing instructions\n")
        _stamp(p, "p-dddd4444", "f-agents01")
        assert parse_marker(p) is not None

        r = client.post("/api/bots/team_bot_a/reflect/strip-marker",
                        json={"path": "AGENTS.md"})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["stripped"] is True
        assert parse_marker(p) is None
        assert "Standing instructions" in p.read_text()


# ── GET /reflect now carries the five-bucket recon ledger ─────────────────────

class TestReflectCarriesRecon:
    def test_get_reflect_includes_recon_buckets(self, strip_env):
        client, env = strip_env
        p = _write(env, "AGENTS.md", content="# instr\n")
        _stamp(p, "p-dddd4444", "f-agents01")

        r = client.get("/api/bots/team_bot_a/reflect")
        assert r.status_code == 200, r.get_json()
        recon = r.get_json().get("recon")
        assert recon and "buckets" in recon
        assert recon["counts"]["scrub_candidate"] == 1
        assert recon["buckets"]["scrub_candidate"][0]["path"] == "AGENTS.md"


# ── Refusal: never strip an owned / attach path ───────────────────────────────

class TestRefusesNonScrub:
    def test_refuses_owned_path(self, strip_env):
        client, env = strip_env
        p = _write(env, "scripts/keep.py")
        _stamp(p, "p-aaaa1111", "f-keep0001")
        _make_instance(env, "i-1", "p-aaaa1111", realized=[_rf(p, "f-keep0001")])

        r = client.post("/api/bots/team_bot_a/reflect/strip-marker",
                        json={"path": "scripts/keep.py"})
        assert r.status_code == 400
        assert "not a scrub candidate" in r.get_json()["error"]
        # Marker untouched.
        assert parse_marker(p) is not None

    def test_refuses_attach_candidate(self, strip_env):
        client, env = strip_env
        # A live app claims main.py; stray.py's marker resolves to it but no
        # Instance lists it → attach_candidate, NOT scrub. Must not be stripped.
        claimed = _write(env, "scripts/main.py")
        _stamp(claimed, "p-bbbb2222", "f-main0001")
        _make_instance(env, "i-1", "p-bbbb2222", realized=[_rf(claimed, "f-main0001")])
        stray = _write(env, "scripts/stray.py", content="y = 2\n")
        _stamp(stray, "p-bbbb2222", "f-stray001")

        r = client.post("/api/bots/team_bot_a/reflect/strip-marker",
                        json={"path": "scripts/stray.py"})
        assert r.status_code == 400
        assert "not a scrub candidate" in r.get_json()["error"]
        assert parse_marker(stray) is not None


# ── Validation ────────────────────────────────────────────────────────────────

class TestValidation:
    def test_missing_path_400(self, strip_env):
        client, _ = strip_env
        r = client.post("/api/bots/team_bot_a/reflect/strip-marker", json={})
        assert r.status_code == 400
        assert "path required" in r.get_json()["error"]

    def test_unknown_bot_404(self, strip_env):
        client, _ = strip_env
        r = client.post("/api/bots/ghost/reflect/strip-marker",
                        json={"path": "AGENTS.md"})
        assert r.status_code == 404
        assert "unknown bot_id" in r.get_json()["error"]

    def test_unknown_path_is_not_scrub_400(self, strip_env):
        client, _ = strip_env
        r = client.post("/api/bots/team_bot_a/reflect/strip-marker",
                        json={"path": "scripts/never-existed.py"})
        assert r.status_code == 400
        assert "not a scrub candidate" in r.get_json()["error"]


# ── Bulk strip ────────────────────────────────────────────────────────────────

class TestStripAll:
    def test_strips_all_scrub_candidates(self, strip_env):
        client, env = strip_env
        # Two scrub candidates (telemetry + AGENTS.md) plus one owned file that
        # must be left alone.
        t = _write(env, "evolve/audit_outbox/_ingested/rec-9.json",
                   content=json.dumps({"a": 1}))
        _stamp(t, "p-cccc3333", "f-rec00009")
        a = _write(env, "AGENTS.md", content="# instr\n")
        _stamp(a, "p-dddd4444", "f-agents01")
        keep = _write(env, "scripts/keep.py")
        _stamp(keep, "p-aaaa1111", "f-keep0001")
        _make_instance(env, "i-1", "p-aaaa1111", realized=[_rf(keep, "f-keep0001")])

        r = client.post("/api/bots/team_bot_a/reflect/strip-markers")
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["stripped_count"] == 2
        assert body["blocked"] == []
        # Both scrub markers gone; the owned marker survives.
        assert parse_marker(t) is None
        assert parse_marker(a) is None
        assert parse_marker(keep) is not None
