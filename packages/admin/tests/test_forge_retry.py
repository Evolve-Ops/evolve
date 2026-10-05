"""Tests for the forge job retry endpoint + reset_to_queued helper.

POST /api/forge/jobs/<job_id>/retry CLONES a failed/cancelled/rejected
job into a NEW job_id and dispatches the clone. The original is frozen
with ``superseded_by_job_id`` pointing at the clone; the clone carries
``prior_job_id`` pointing back. The retry-chain audit is preserved
across the new job_id boundary.

Background: the Apps → Forge Jobs UI had no way to retry a failed run.
Operators' only escape was deleting the JSON on disk and re-creating
the job from the Gallery, losing all step history. This PR adds the
retry path used by the new "↻ Retry" button in the Actions column.

2026-06-05 update: retry was changed from in-place ``reset_to_queued``
mutation to ``clone_for_retry`` cloning, after a real job (j-9093a7a3)
ended up with orphan-step state from multi-cycle in-place retries.
The ``reset_to_queued`` helper is still tested as a unit because the
CLI and some callers still use it.

Coverage:
- reset_to_queued() (unit): all the field invariants
- retry endpoint: succeeds for failed / cancelled / rejected
- retry endpoint: 400 for queued / running / awaiting_approval / complete
- retry endpoint: 404 for unknown job_id
- retry endpoint: dispatch=false returns ok without starting a thread
- retry endpoint: dispatch=true (default) kicks off run_forge_job with
  the new clone's job_id
- retry endpoint: response carries job_id (new) + prior_job_id (old)
- retry endpoint: prior is frozen, only the clone is dispatched
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

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
    reset_to_queued,
    _install_steps,
)
from evolve_admin.web.gallery_routes import register_gallery_routes  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _route_manifests_to_tmp(tmp_path, monkeypatch):
    """Mirrors test_forge_cancel_allowlist — manifests go to tmp."""
    def _appdir(shared_dir, bot_id):
        out = tmp_path / "workspaces" / bot_id / "manifests"
        out.mkdir(parents=True, exist_ok=True)
        return out
    from evolve_admin.applications import manifest as _mf
    monkeypatch.setattr(_mf, "applications_dir", _appdir)


@pytest.fixture
def app(tmp_path: Path):
    network = tmp_path / "network.json"
    network.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "members": [],
    }))
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    register_gallery_routes(flask_app, network, tmp_path)
    return flask_app


def _make_failed_job(
    tmp_path: Path, *,
    status: str = "failed",
    current_step: int = 4,
    populate_run_state: bool = True,
) -> ForgeJob:
    """Persist a job in a failed-ish state with run state populated.

    populate_run_state mimics a job that actually ran for a few steps
    before failing — so reset_to_queued has something to clear.
    """
    steps = _install_steps()
    if populate_run_state:
        for s in steps[:current_step - 1]:
            s.status = "done"
            s.started_at = now_iso()
            s.finished_at = now_iso()
            s.detail = "ran"
        if current_step <= len(steps):
            steps[current_step - 1].status = "failed"
            steps[current_step - 1].started_at = now_iso()
            steps[current_step - 1].finished_at = now_iso()
            steps[current_step - 1].detail = "boom"
    job = ForgeJob(
        job_id=new_job_id(),
        run_id=run_id_for(1),
        job_type="install",
        pkg_id="p-12345678",
        app_id="test-app",
        bot_id="bot_test",
        pkg_version_before=None,
        gallery_version="v1.0.0",
        steps=steps,
        current_step=current_step,
        created_at=now_iso(),
        last_updated=now_iso(),
        status=status,
        critique_rounds_done=2,
        issues_found=4,
        issues_resolved=3,
        issues_deferred=1,
        test_exit_code=1,
        test_output_summary="2 of 5 tests failed",
        reject_reason="operator changed mind" if status == "rejected" else "",
        rejected_by="operator" if status == "rejected" else "",
        rejected_at=now_iso() if status == "rejected" else "",
        context_snapshot={"important": "do not clobber"},
    )
    save_job(job, tmp_path)
    return job


# ── reset_to_queued() unit ────────────────────────────────────────────────────


class TestResetToQueued:

    def test_resets_status_to_queued(self, tmp_path):
        job = _make_failed_job(tmp_path, status="failed")
        reset_to_queued(job, tmp_path)
        reloaded = load_job(job.job_id, tmp_path)
        assert reloaded.status == "queued"

    def test_clears_current_step(self, tmp_path):
        job = _make_failed_job(tmp_path, current_step=7)
        reset_to_queued(job, tmp_path)
        reloaded = load_job(job.job_id, tmp_path)
        assert reloaded.current_step == 0

    def test_clears_run_state_counters(self, tmp_path):
        job = _make_failed_job(tmp_path)
        reset_to_queued(job, tmp_path)
        reloaded = load_job(job.job_id, tmp_path)
        assert reloaded.critique_rounds_done == 0
        assert reloaded.issues_found == 0
        assert reloaded.issues_resolved == 0
        assert reloaded.issues_deferred == 0
        assert reloaded.test_exit_code is None
        assert reloaded.test_output_summary == ""

    def test_clears_rejection_fields(self, tmp_path):
        """Rejected jobs being retried must shed their rejection metadata
        so a subsequent rejection (if any) starts clean."""
        job = _make_failed_job(tmp_path, status="rejected")
        reset_to_queued(job, tmp_path)
        reloaded = load_job(job.job_id, tmp_path)
        assert reloaded.reject_reason == ""
        assert reloaded.rejected_by == ""
        assert reloaded.rejected_at == ""

    def test_step_state_reset_to_pending(self, tmp_path):
        """All steps go back to 'pending', detail / started_at / finished_at
        cleared. The AWAIT step keeps its seed status of 'waiting' (UI
        contract — the waiting glyph there is intentional)."""
        job = _make_failed_job(tmp_path)
        reset_to_queued(job, tmp_path)
        reloaded = load_job(job.job_id, tmp_path)
        for step in reloaded.steps:
            assert step.started_at is None
            assert step.finished_at is None
            assert step.detail == ""
            if step.label.startswith("AWAIT"):
                assert step.status == "waiting"
            else:
                assert step.status == "pending"

    def test_preserves_audit_continuity(self, tmp_path):
        """job_id, run_id, created_at, pkg_id, app_id, bot_id all stay —
        retrying must not look like a fresh job to anything downstream."""
        job = _make_failed_job(tmp_path)
        original = {
            "job_id": job.job_id, "run_id": job.run_id,
            "created_at": job.created_at,
            "pkg_id": job.pkg_id, "app_id": job.app_id,
            "bot_id": job.bot_id, "gallery_version": job.gallery_version,
        }
        reset_to_queued(job, tmp_path)
        reloaded = load_job(job.job_id, tmp_path)
        for k, v in original.items():
            assert getattr(reloaded, k) == v, f"{k} was clobbered"

    def test_preserves_context_snapshot(self, tmp_path):
        """The bot's working memory survives a retry — without this,
        the bot would re-do all the analysis work from scratch."""
        job = _make_failed_job(tmp_path)
        reset_to_queued(job, tmp_path)
        reloaded = load_job(job.job_id, tmp_path)
        assert reloaded.context_snapshot == {"important": "do not clobber"}


# ── /api/forge/jobs/<id>/retry endpoint ───────────────────────────────────────


class TestRetryEndpoint:

    def test_retry_failed_returns_new_clone(self, app, tmp_path):
        """Retry clones the failed job — the response carries the new
        job_id and the prior_job_id pointer. The clone is queued; the
        prior is frozen with superseded_by_job_id pointing at the clone."""
        job = _make_failed_job(tmp_path, status="failed")
        with patch("evolve_admin.applications.forge_engine.run_forge_job") as m_run:
            with app.test_client() as c:
                resp = c.post(f"/api/forge/jobs/{job.job_id}/retry",
                              json={"dispatch": False})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["prior_job_id"] == job.job_id
        clone_id = data["job_id"]
        assert clone_id != job.job_id
        clone = load_job(clone_id, tmp_path)
        assert clone is not None
        assert clone.status == "queued"
        assert clone.prior_job_id == job.job_id
        prior = load_job(job.job_id, tmp_path)
        assert prior is not None
        assert prior.status == "failed"  # prior status is frozen
        assert prior.superseded_by_job_id == clone_id
        m_run.assert_not_called()

    def test_retry_cancelled_returns_new_clone(self, app, tmp_path):
        job = _make_failed_job(tmp_path, status="cancelled")
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/retry",
                          json={"dispatch": False})
        assert resp.status_code == 200
        data = resp.get_json()
        clone = load_job(data["job_id"], tmp_path)
        assert clone is not None
        assert clone.status == "queued"
        assert clone.prior_job_id == job.job_id

    def test_retry_rejected_returns_new_clone(self, app, tmp_path):
        job = _make_failed_job(tmp_path, status="rejected")
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/retry",
                          json={"dispatch": False})
        assert resp.status_code == 200
        clone = load_job(resp.get_json()["job_id"], tmp_path)
        assert clone is not None
        assert clone.status == "queued"
        # Rejection metadata from the prior must NOT leak into the
        # clone — a clean attempt starts with empty rejection fields.
        assert clone.reject_reason == ""
        assert clone.rejected_by == ""
        assert clone.rejected_at == ""

    @pytest.mark.parametrize("blocked_status", [
        "queued", "running", "awaiting_approval", "approved", "complete",
    ])
    def test_retry_blocks_non_terminal_failure_states(self, app, tmp_path, blocked_status):
        """Retry is the wrong tool for jobs that haven't failed yet.
        Allowing it from 'awaiting_approval' would clobber the bot's
        completed work; from 'running' it would race with the live thread."""
        job = _make_failed_job(tmp_path, status=blocked_status)
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/retry",
                          json={"dispatch": False})
        assert resp.status_code == 400
        data = resp.get_json()
        assert "cannot be retried" in data["error"]

    def test_retry_unknown_job_returns_404(self, app):
        with app.test_client() as c:
            resp = c.post("/api/forge/jobs/j-nonexistent/retry",
                          json={"dispatch": False})
        assert resp.status_code == 404

    def test_retry_with_dispatch_true_kicks_off_forge_on_clone(self, app, tmp_path):
        """dispatch=true (default) starts run_forge_job on the CLONE's
        job_id, not the prior's. Prior must not be re-dispatched."""
        import time
        job = _make_failed_job(tmp_path, status="failed")
        with patch("evolve_admin.applications.forge_engine.run_forge_job") as m_run:
            with app.test_client() as c:
                resp = c.post(f"/api/forge/jobs/{job.job_id}/retry", json={})
            # Daemon thread takes a tick to actually call the patched fn
            time.sleep(0.2)
        assert resp.status_code == 200
        clone_id = resp.get_json()["job_id"]
        m_run.assert_called_once()
        kwargs = m_run.call_args.kwargs
        assert kwargs["job_id"] == clone_id
        assert kwargs["job_id"] != job.job_id
        assert kwargs["bot_id"] == job.bot_id

    def test_retry_dispatch_false_skips_thread(self, app, tmp_path):
        """dispatch=false just clones, doesn't run. Useful for operators
        who want to inspect/edit the clone state before dispatching."""
        job = _make_failed_job(tmp_path, status="failed")
        with patch("evolve_admin.applications.forge_engine.run_forge_job") as m_run:
            with app.test_client() as c:
                resp = c.post(f"/api/forge/jobs/{job.job_id}/retry",
                              json={"dispatch": False})
        assert resp.status_code == 200
        assert "dispatch skipped" in resp.get_json()["info"]
        m_run.assert_not_called()

    def test_retry_preserves_prior_job_id_for_audit(self, app, tmp_path):
        """The prior_job_id chain is the new audit trail: operators
        navigate the chain via the UI instead of mutating one job_id."""
        job = _make_failed_job(tmp_path, status="failed")
        original_job_id = job.job_id
        original_run_id = job.run_id
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{job.job_id}/retry",
                          json={"dispatch": False})
        assert resp.status_code == 200
        clone_id = resp.get_json()["job_id"]
        # Prior is loadable by its original id
        prior = load_job(original_job_id, tmp_path)
        assert prior is not None
        assert prior.job_id == original_job_id
        assert prior.run_id == original_run_id
        # Clone's prior_job_id chain points back
        clone = load_job(clone_id, tmp_path)
        assert clone is not None
        assert clone.prior_job_id == original_job_id

    def test_retry_sets_is_retry_flag_on_clone(self, app, tmp_path):
        """The clone carries ``is_retry=True`` so Step 2 build dispatch
        (forge_engine.py) tells the bot's LLM to wipe ``apps/<app_id>/``
        before re-building. Without this, stale files from the prior
        attempt at the same (bot_id, app_id) slot cause sha256
        verification failures. The flag lives on the CLONE — the prior
        is frozen and never re-dispatched. See PR #2210."""
        prior_job = _make_failed_job(tmp_path, status="failed")
        assert prior_job.is_retry is False
        with app.test_client() as c:
            resp = c.post(f"/api/forge/jobs/{prior_job.job_id}/retry",
                          json={"dispatch": False})
        assert resp.status_code == 200
        clone_id = resp.get_json()["job_id"]
        clone = load_job(clone_id, tmp_path)
        assert clone is not None
        assert clone.is_retry is True
        # Prior stays untouched (the frozen-after-clone contract).
        reloaded_prior = load_job(prior_job.job_id, tmp_path)
        assert reloaded_prior.is_retry is False

    def test_retry_calls_workspace_cleanup_on_prior(self, app, tmp_path):
        """clean_workspace_for_retry is called against the PRIOR's
        bot_id + job_id — that's where the stale inbox/outbox files
        live. The clone has a fresh job_id and won't reuse those files
        by name, but leaving them around is still a hygiene issue."""
        prior_job = _make_failed_job(tmp_path, status="failed")
        with patch(
            "evolve_admin.applications.bot_forge.clean_workspace_for_retry"
        ) as m_clean:
            m_clean.return_value = {"inbox_removed": 2, "outbox_removed": 1}
            with app.test_client() as c:
                resp = c.post(f"/api/forge/jobs/{prior_job.job_id}/retry",
                              json={"dispatch": False})
        assert resp.status_code == 200
        m_clean.assert_called_once_with(prior_job.bot_id, prior_job.job_id)

    def test_retry_proceeds_when_cleanup_raises(self, app, tmp_path):
        """Cleanup is best-effort — a permission glitch or a missing
        forge dir must not block the retry. The bot-side ``rm -rf``
        instruction (build_prompt is_retry paragraph) is the primary
        defence; clearing inbox/outbox is belt-and-suspenders."""
        prior_job = _make_failed_job(tmp_path, status="failed")
        with patch(
            "evolve_admin.applications.bot_forge.clean_workspace_for_retry",
            side_effect=PermissionError("ACL drift"),
        ):
            with app.test_client() as c:
                resp = c.post(f"/api/forge/jobs/{prior_job.job_id}/retry",
                              json={"dispatch": False})
        assert resp.status_code == 200
        # The clone still gets created and queued.
        clone_id = resp.get_json()["job_id"]
        clone = load_job(clone_id, tmp_path)
        assert clone is not None
        assert clone.status == "queued"
        assert clone.is_retry is True
