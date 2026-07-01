"""End-to-end tests for the remediation Flask endpoints.

Uses Flask's ``test_client`` against a tiny app that registers only the
remediation routes — keeps the test self-contained and fast. The
handler-registry is the real one; subprocess + filesystem boundaries
are stubbed via monkey-patches where needed.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.remediation.routes import register_routes  # noqa: E402
from evolve_admin.remediation.jobs import load_job  # noqa: E402


@pytest.fixture
def app(tmp_path: Path):
    """Minimal Flask app with just the remediation routes; shared_dir = tmp_path."""
    a = Flask(__name__)
    register_routes(a, lambda: tmp_path)
    a.config["TESTING"] = True
    return a


# ── POST .../execute ───────────────────────────────────────────────────────


def test_execute_missing_kind_returns_400(app):
    with app.test_client() as c:
        resp = c.post("/api/admin/remediation/execute", json={})
        assert resp.status_code == 400
        assert "kind" in resp.get_json()["error"]


def test_execute_unknown_kind_returns_400_with_available_list(app):
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/remediation/execute",
            json={"kind": "destroy_pod", "params": {}},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "destroy_pod" in data["error"]
        assert "install_infra_jobs" in data["available"]


def test_execute_params_not_object_returns_400(app):
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/remediation/execute",
            json={"kind": "install_infra_jobs", "params": "not a dict"},
        )
        assert resp.status_code == 400


def test_execute_happy_path_returns_job_id(app, tmp_path: Path):
    """Successful POST → 202, job_id, status=queued. The job's actual run
    happens in a background thread; the endpoint returns immediately."""
    fake_result = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("evolve_admin.remediation.handlers.subprocess.run",
               return_value=fake_result):
        with app.test_client() as c:
            resp = c.post(
                "/api/admin/remediation/execute",
                json={"kind": "install_infra_jobs", "params": {}},
            )
            assert resp.status_code == 202
            data = resp.get_json()
            assert data["status"] == "queued"
            job_id = data["job_id"]
            assert job_id

            # Wait briefly for the background thread to finish so we can
            # assert the persisted outcome. The handler is fast (mocked
            # subprocess).
            for _ in range(20):
                job = load_job(tmp_path, job_id)
                if job and job.status in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
            job = load_job(tmp_path, job_id)
            assert job is not None
            assert job.status == "succeeded"
            assert job.output is not None


def test_execute_accepts_signal_id_and_actor(app, tmp_path: Path):
    """Pass-through fields are persisted on the Job."""
    fake_result = MagicMock(returncode=0, stdout="", stderr="")
    with patch("evolve_admin.remediation.handlers.subprocess.run",
               return_value=fake_result):
        with app.test_client() as c:
            resp = c.post(
                "/api/admin/remediation/execute",
                json={
                    "kind": "install_infra_jobs",
                    "params": {},
                    "signal_id": "sig-abc",
                    "actor": "user:pod_admin",
                },
            )
            job_id = resp.get_json()["job_id"]
    job = load_job(tmp_path, job_id)
    assert job is not None
    assert job.signal_id == "sig-abc"
    assert job.actor == "user:pod_admin"


# ── GET .../job/<id> ───────────────────────────────────────────────────────


def test_get_job_unknown_id_returns_404(app):
    with app.test_client() as c:
        resp = c.get("/api/admin/remediation/job/no-such-job")
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"]


def test_get_job_returns_full_job_dict(app, tmp_path: Path):
    fake_result = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("evolve_admin.remediation.handlers.subprocess.run",
               return_value=fake_result):
        with app.test_client() as c:
            resp = c.post(
                "/api/admin/remediation/execute",
                json={"kind": "install_infra_jobs", "params": {}},
            )
            job_id = resp.get_json()["job_id"]
            # Wait briefly for the thread to finish.
            for _ in range(20):
                j = load_job(tmp_path, job_id)
                if j and j.status in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
            resp = c.get(f"/api/admin/remediation/job/{job_id}")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["id"] == job_id
            assert data["kind"] == "install_infra_jobs"
            assert data["status"] == "succeeded"


# ── GET .../jobs (list) ────────────────────────────────────────────────────


@pytest.mark.real_sleep  # sleeps 1.05s for real second-resolution created_at ordering
def test_list_jobs_returns_newest_first(app, tmp_path: Path):
    from evolve_admin.remediation.jobs import create_job
    j1 = create_job(tmp_path, "install_infra_jobs", {})
    time.sleep(1.05)
    j2 = create_job(tmp_path, "reset_baseline",
                    {"bot_id": "security_bot", "kind": "scripts"})
    with app.test_client() as c:
        resp = c.get("/api/admin/remediation/jobs")
        assert resp.status_code == 200
        ids = [j["id"] for j in resp.get_json()["jobs"]]
    assert ids[0] == j2.id
    assert ids[1] == j1.id


# ── Failure path ───────────────────────────────────────────────────────────


def test_execute_failed_handler_persists_error(app, tmp_path: Path):
    """Handler raise → job status=failed, error captured with traceback."""
    fake_result = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("evolve_admin.remediation.handlers.subprocess.run",
               return_value=fake_result):
        with app.test_client() as c:
            resp = c.post(
                "/api/admin/remediation/execute",
                json={"kind": "install_infra_jobs", "params": {}},
            )
            job_id = resp.get_json()["job_id"]
            for _ in range(20):
                j = load_job(tmp_path, job_id)
                if j and j.status in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
    job = load_job(tmp_path, job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.error is not None
    assert "boom" in job.error
