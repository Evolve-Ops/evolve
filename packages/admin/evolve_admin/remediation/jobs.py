"""Persistent job records for Remediation execution.

Each job is a JSON file at ``{shared_dir}/remediation/jobs/{job_id}.json``.
Atomic writes via ``tempfile + os.replace`` — same pattern the signal
store uses. Owned by the ``evolve`` user (whichever user the admin-ui
daemon runs as) so writes are direct, no sudo staging.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from evolve_util import now_iso_offset as _utc_now_iso

JobStatus = Literal["queued", "running", "succeeded", "failed"]


def jobs_dir(shared_dir: Path) -> Path:
    """Return the dir holding job JSONs. Creates it lazily on write."""
    return shared_dir / "remediation" / "jobs"


@dataclass
class Job:
    """A queued/running/finished remediation action."""

    id: str
    kind: str  # mirrors Remediation.kind (the handler-registry key)
    params: dict[str, Any]
    status: JobStatus = "queued"
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    # Output is whatever the handler returned (handler-specific dict).
    output: dict[str, Any] | None = None
    # Error is the exception message + traceback when status="failed".
    error: str | None = None
    # signal_id ties the job back to the alert that motivated it — UI
    # uses this to surface "running" / "result" inline on the alert card.
    signal_id: str | None = None
    # actor records who triggered the action (user, daemon, etc.).
    actor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        return cls(
            id=data["id"],
            kind=data["kind"],
            params=dict(data.get("params") or {}),
            status=data.get("status", "queued"),
            created_at=data.get("created_at", _utc_now_iso()),
            updated_at=data.get("updated_at", _utc_now_iso()),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            output=data.get("output"),
            error=data.get("error"),
            signal_id=data.get("signal_id"),
            actor=data.get("actor"),
        )


def _job_path(shared_dir: Path, job_id: str) -> Path:
    return jobs_dir(shared_dir) / f"{job_id}.json"


def save_job(job: Job, shared_dir: Path) -> Path:
    """Atomically persist a Job to disk. Updates ``updated_at``."""
    job.updated_at = _utc_now_iso()
    target = _job_path(shared_dir, job.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{job.id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(job.to_dict(), f, indent=2)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def load_job(shared_dir: Path, job_id: str) -> Job | None:
    """Read a Job by id, or None if missing/unparseable."""
    p = _job_path(shared_dir, job_id)
    if not p.exists():
        return None
    try:
        return Job.from_dict(json.loads(p.read_text()))
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def create_job(
    shared_dir: Path,
    kind: str,
    params: dict[str, Any] | None = None,
    *,
    signal_id: str | None = None,
    actor: str | None = None,
) -> Job:
    """Create a fresh ``queued`` job and persist it."""
    job = Job(
        id=str(uuid4()),
        kind=kind,
        params=dict(params or {}),
        status="queued",
        signal_id=signal_id,
        actor=actor,
    )
    save_job(job, shared_dir)
    return job


def iter_recent_jobs(shared_dir: Path, limit: int = 50) -> list[Job]:
    """Return the most recent jobs by updated_at, newest first.

    Returns at most ``limit``. Skips files that fail to parse — callers
    don't care to differentiate "no jobs" from "one corrupt JSON".
    """
    d = jobs_dir(shared_dir)
    if not d.exists():
        return []
    jobs: list[Job] = []
    for p in d.glob("*.json"):
        try:
            jobs.append(Job.from_dict(json.loads(p.read_text())))
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    jobs.sort(key=lambda j: j.updated_at, reverse=True)
    return jobs[:limit]
