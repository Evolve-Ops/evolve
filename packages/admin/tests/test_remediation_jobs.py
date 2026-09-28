"""Tests for the persistent Job record + IO."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.remediation.jobs import (  # noqa: E402
    Job,
    create_job,
    iter_recent_jobs,
    jobs_dir,
    load_job,
    save_job,
)


def test_create_job_persists_and_returns_queued(tmp_path: Path):
    job = create_job(tmp_path, "install_infra_jobs", {})
    assert job.status == "queued"
    assert job.id and len(job.id) > 8
    # File exists
    p = jobs_dir(tmp_path) / f"{job.id}.json"
    assert p.exists()
    # Round-trips through load_job
    loaded = load_job(tmp_path, job.id)
    assert loaded is not None
    assert loaded.id == job.id
    assert loaded.kind == "install_infra_jobs"


def test_load_job_missing_returns_none(tmp_path: Path):
    assert load_job(tmp_path, "does-not-exist") is None


def test_save_job_atomic_no_partial_files_on_failure(tmp_path: Path, monkeypatch):
    """If os.replace raises, the temp file is cleaned up."""
    job = create_job(tmp_path, "install_infra_jobs", {})
    # Surgically make os.replace raise
    import os
    real_replace = os.replace
    def failing_replace(*_a, **_kw):
        raise OSError("simulated")
    monkeypatch.setattr(os, "replace", failing_replace)
    try:
        save_job(job, tmp_path)
    except OSError:
        pass
    # No leftover .tmp files in the jobs dir
    leftovers = [p for p in jobs_dir(tmp_path).iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_job_overwrites_existing(tmp_path: Path):
    job = create_job(tmp_path, "install_infra_jobs", {})
    job.status = "running"
    job.output = {"hello": "world"}
    save_job(job, tmp_path)
    loaded = load_job(tmp_path, job.id)
    assert loaded is not None
    assert loaded.status == "running"
    assert loaded.output == {"hello": "world"}


@pytest.mark.real_sleep  # sleeps 1.05s for a real iso8601 second-resolution tick
def test_save_job_updates_updated_at(tmp_path: Path):
    import time
    job = create_job(tmp_path, "install_infra_jobs", {})
    first_ts = job.updated_at
    time.sleep(1.05)  # iso8601 second-resolution; force a tick
    save_job(job, tmp_path)
    assert job.updated_at > first_ts


@pytest.mark.real_sleep  # sleeps between creates for real second-resolution ordering
def test_iter_recent_jobs_returns_newest_first(tmp_path: Path):
    import time
    j1 = create_job(tmp_path, "install_infra_jobs", {})
    time.sleep(1.05)
    j2 = create_job(tmp_path, "reset_baseline",
                    {"bot_id": "security_bot", "kind": "scripts"})
    time.sleep(1.05)
    j3 = create_job(tmp_path, "flip_cron_session_target",
                    {"bot_id": "security_bot", "cron_name": "x"})
    out = iter_recent_jobs(tmp_path)
    assert [j.id for j in out] == [j3.id, j2.id, j1.id]


def test_iter_recent_jobs_skips_corrupt_files(tmp_path: Path):
    """A corrupt JSON shouldn't block iteration of the rest."""
    j = create_job(tmp_path, "install_infra_jobs", {})
    (jobs_dir(tmp_path) / "corrupt.json").write_text("not json {[")
    out = iter_recent_jobs(tmp_path)
    assert len(out) == 1
    assert out[0].id == j.id


def test_iter_recent_jobs_caps_at_limit(tmp_path: Path):
    for _ in range(12):
        create_job(tmp_path, "install_infra_jobs", {})
    out = iter_recent_jobs(tmp_path, limit=5)
    assert len(out) == 5


def test_iter_recent_jobs_empty_dir(tmp_path: Path):
    assert iter_recent_jobs(tmp_path) == []


def test_job_roundtrips_through_dict():
    job = Job(
        id="abc-123",
        kind="reset_baseline",
        params={"bot_id": "security_bot", "kind": "scripts"},
        status="succeeded",
        output={"cleared": True},
        signal_id="sig-xyz",
        actor="user:pod_admin",
    )
    j2 = Job.from_dict(job.to_dict())
    assert j2.id == job.id
    assert j2.kind == job.kind
    assert j2.params == job.params
    assert j2.status == "succeeded"
    assert j2.output == {"cleared": True}
    assert j2.signal_id == "sig-xyz"
    assert j2.actor == "user:pod_admin"
