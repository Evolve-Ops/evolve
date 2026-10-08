"""Tests for the extended forge cancel endpoint allowlist.

api_forge_job_cancel previously only allowed cancelling jobs in
'queued' or 'running' state. This test file verifies that the
extended allowlist also accepts 'awaiting_approval' and 'awaiting_oauth',
while terminal states are still rejected.

Covers:
- Cancel succeeds for queued (regression guard)
- Cancel succeeds for awaiting_approval (new)
- Cancel succeeds for awaiting_oauth (new)
- Cancel is rejected for complete (terminal — still blocked)
- Cancel is rejected for rejected (terminal — still blocked)
- Manifest status is reverted on cancel (for awaiting_approval)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.applications.forge_jobs import (  # noqa: E402
    ForgeJob,
    new_job_id,
    run_id_for,
    now_iso,
    save_job,
    load_job,
    _improvement_steps,
    _install_steps,
)
from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    load_manifest,
    save_manifest,
)
from evolve_admin.web.gallery_routes import register_gallery_routes  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _route_manifests_to_tmp(tmp_path, monkeypatch):
    """Re-point ``applications_dir`` at a tmp tree so save_manifest doesn't
    try to write to ``/Users/bot_test/.openclaw/workspace/manifests/`` —
    that path doesn't exist on the test host post-PR #1176. The test
    bot_id is a synthetic value that has no real /Users/ entry.
    """
    def _appdir(shared_dir, bot_id):
        out = tmp_path / "workspaces" / bot_id / "manifests"
        out.mkdir(parents=True, exist_ok=True)
        return out
    from evolve_admin.applications import manifest as _mf
    monkeypatch.setattr(_mf, "applications_dir", _appdir)


@pytest.fixture
def app(tmp_path: Path):
    """Minimal Flask app with gallery routes; shared_dir = tmp_path."""
    # gallery_routes needs a network.json to resolve shared_dir at startup
    network = tmp_path / "network.json"
    network.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "members": [],
    }))
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    register_gallery_routes(flask_app, network, tmp_path)
    return flask_app


def _make_job(tmp_path: Path, *, status: str, job_type: str = "improvement") -> ForgeJob:
    """Persist a ForgeJob in the given status and return it."""
    job = ForgeJob(
        job_id=new_job_id(),
        run_id=run_id_for(1),
        job_type=job_type,
        pkg_id="p-test1234",
        app_id="test-app",
        bot_id="bot_test",
        pkg_version_before=None,
        gallery_version=None,
        steps=_improvement_steps() if job_type == "improvement" else _install_steps(),
        created_at=now_iso(),
        last_updated=now_iso(),
        status=status,
    )
    save_job(job, tmp_path)
    return job


def _make_manifest(tmp_path: Path, *, status: str, job_id: str | None = None) -> ApplicationManifest:
    m = ApplicationManifest(
        id="test-app",
        name="Test App",
        bot_id="bot_test",
        status=status,
        install_job={"job_id": job_id} if job_id else None,
    )
    save_manifest(m, tmp_path)
    return m


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestForgeCancelAllowlist:

    def test_cancel_queued_succeeds(self, app, tmp_path):
        """Regression guard: queued jobs can still be cancelled."""
        job = _make_job(tmp_path, status="queued")
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/cancel")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("ok") is True

        reloaded = load_job(job.job_id, tmp_path)
        assert reloaded is not None
        assert reloaded.status == "failed"

    def test_cancel_awaiting_approval_succeeds(self, app, tmp_path):
        """awaiting_approval jobs can now be cancelled — job → failed."""
        job = _make_job(tmp_path, status="awaiting_approval")
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/cancel")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("ok") is True

        reloaded = load_job(job.job_id, tmp_path)
        assert reloaded is not None
        assert reloaded.status == "failed"

    def test_cancel_awaiting_oauth_succeeds(self, app, tmp_path):
        """awaiting_oauth jobs can now be cancelled — job → failed.

        awaiting_oauth is not yet produced by the forge engine (as of v1.1)
        but is in the allowed state set so stray jobs don't become impossible
        to clear via the API. The sweeper naturally stops polling once the job
        is no longer in awaiting_oauth status.
        """
        job = _make_job(tmp_path, status="awaiting_oauth")
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/cancel")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("ok") is True

        reloaded = load_job(job.job_id, tmp_path)
        assert reloaded is not None
        assert reloaded.status == "failed"

    def test_cancel_complete_rejected(self, app, tmp_path):
        """Terminal 'complete' jobs cannot be cancelled — returns 400."""
        job = _make_job(tmp_path, status="complete")
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/cancel")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "cannot be cancelled" in data["error"]

    def test_cancel_rejected_status_blocked(self, app, tmp_path):
        """Terminal 'rejected' jobs cannot be cancelled — returns 400."""
        job = _make_job(tmp_path, status="rejected")
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/cancel")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "cannot be cancelled" in data["error"]

    def test_cancel_awaiting_approval_reverts_manifest(self, app, tmp_path):
        """Cancelling an awaiting_approval job reverts manifest to draft/installed.

        For an install job the manifest was created by the wizard but never
        completed; cancelling reverts it to draft.
        """
        job = _make_job(tmp_path, status="awaiting_approval", job_type="install")
        _make_manifest(tmp_path, status="updating", job_id=job.job_id)

        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/cancel")
        assert resp.status_code == 200

        manifest = load_manifest("test-app", "bot_test", tmp_path)
        assert manifest is not None
        assert manifest.status == "draft"
        assert manifest.install_job is None

    def test_cancel_nonexistent_job_returns_404(self, app, tmp_path):
        """Attempting to cancel a job that doesn't exist returns 404."""
        with app.test_client() as c:
            resp = c.post("/api/forge/jobs/j-doesnotexist/cancel")
        assert resp.status_code == 404
