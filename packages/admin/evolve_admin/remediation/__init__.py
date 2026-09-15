"""Remediation — server-side execution of Signal-advertised fix actions.

A Signal can carry a structured ``Remediation`` (kind + params) advertising
the action that would resolve it. The admin UI posts {kind, params} to the
remediation endpoint, which:

  1. creates a persistent Job record under ``{shared_dir}/remediation/jobs/``
  2. dispatches the work to a background thread
  3. exposes a polling endpoint for the UI to track progress

Why persistent (rather than in-memory)? install_infra_jobs takes ~30s and
the admin daemon may restart mid-run. An in-memory job that disappears
on restart is worse UX than the alert it's trying to fix.

Public surface:

  - ``jobs.Job`` — the persisted record
  - ``jobs.create_job`` / ``jobs.load_job`` / ``jobs.save_job``
  - ``handlers.HANDLERS`` — registry mapping kind → callable
  - ``dispatch.dispatch_async`` — kick off the background runner
  - ``routes.register_routes`` — Flask blueprint registration
"""

from __future__ import annotations

from .jobs import Job, create_job, load_job, save_job, jobs_dir
from .handlers import HANDLERS, get_handler
from .dispatch import dispatch_async

__all__ = [
    "Job",
    "create_job",
    "load_job",
    "save_job",
    "jobs_dir",
    "HANDLERS",
    "get_handler",
    "dispatch_async",
]
