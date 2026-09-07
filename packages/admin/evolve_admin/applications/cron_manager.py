"""
cron_manager.py — Pause and resume app cron entries for Evolve gallery apps.

Cron entries installed via forge are merged into:
    /Users/<bot_id>/.openclaw/cron/jobs.json

Each entry looks like:
    {"name": "...", "schedule": "...", "script": "...", "disabled": bool}

We disable entries by setting "disabled": true and undo that on resume.
The bot's OpenClaw scheduler skips any entry where disabled=true.

This module is also the pod's one READER of OpenClaw's cron store — see
``read_oc_cron_jobs`` at the bottom of the file, which the application scanner
uses to collect schedule evidence.

Match strategy (in order):
  1. script basename matches a cron_dict["script"] basename from the manifest
  2. job name/label matches a cron_dict["label"] or cron_dict["name"]

Both checks are case-sensitive and basename-only to avoid false path matches.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from ..config import bot_home as _bot_home


# ── I/O helpers ───────────────────────────────────────────────────────────────


def _cron_jobs_path(bot_id: str) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "cron" / "jobs.json"


def _cron_state_path(bot_id: str) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "cron" / "jobs-state.json"


def read_jobs_state(bot_id: str) -> dict:
    """Read jobs-state.json and return the inner ``jobs`` dict (keyed by job UUID).

    Each value has ``state.lastRunAtMs`` (epoch ms), ``state.lastRunStatus``
    ("ok" / "error" / …), ``state.consecutiveErrors``, ``state.nextRunAtMs``.
    Returns an empty dict if the file is missing or unreadable.
    """
    path = _cron_state_path(bot_id)

    def _parse(text: str) -> dict:
        data = json.loads(text)
        return data.get("jobs") or {} if isinstance(data, dict) else {}

    try:
        if path.exists():
            return _parse(path.read_text())
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout:
            return _parse(r.stdout)
    except Exception:
        pass
    return {}


def _read_cron_jobs(bot_id: str) -> list[dict] | None:
    """Read jobs.json.  Returns None if the file doesn't exist or can't be read."""
    path = _cron_jobs_path(bot_id)
    # Direct read first (ACL grants the evolve user read access)
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    # Fallback: sudo /bin/cat (covers bots not yet re-deployed with ACL)
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def _write_cron_jobs(bot_id: str, jobs: list[dict]) -> bool:
    """Write jobs back via /tmp staging + sudo /bin/cp (bot user owns the file)."""
    dest = _cron_jobs_path(bot_id)
    try:
        fd, tmp = tempfile.mkstemp(
            dir="/tmp", prefix=f"evolve-cron-{bot_id}-", suffix=".json"
        )
        with os.fdopen(fd, "w") as f:
            json.dump(jobs, f, indent=2)
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(dest)],
            capture_output=True, timeout=5,
        )
        os.unlink(tmp)
        return r.returncode == 0
    except Exception:
        return False


# ── Match helpers ──────────────────────────────────────────────────────────────


def _match_keys(cron_dicts: list[dict]) -> tuple[set[str], set[str]]:
    """Return (script_basenames, label_names) from a manifest's cron_dicts."""
    scripts: set[str] = set()
    labels: set[str] = set()
    for c in cron_dicts:
        if c.get("script"):
            scripts.add(Path(c["script"]).name)
        for key in ("label", "name"):
            if c.get(key):
                labels.add(c[key])
    return scripts, labels


def _job_matches(job: dict, scripts: set[str], labels: set[str]) -> bool:
    if job.get("script"):
        if Path(job["script"]).name in scripts:
            return True
    for key in ("name", "label"):
        if job.get(key) and job[key] in labels:
            return True
    return False


# ── Public API ─────────────────────────────────────────────────────────────────


def disable_app_crons(bot_id: str, manifest: object) -> dict:
    """
    Disable all cron entries that belong to *manifest* on *bot_id*.

    Returns::
        {
          "ok":      bool,
          "disabled": int,   # entries newly disabled
          "skipped":  int,   # entries not found in jobs.json
          "note":    str | None,
        }
    """
    cron_dicts: list[dict] = (
        manifest.cron_dicts() if callable(getattr(manifest, "cron_dicts", None)) else []
    )
    if not cron_dicts:
        return {"ok": True, "disabled": 0, "skipped": 0, "note": None}

    jobs = _read_cron_jobs(bot_id)
    if jobs is None:
        return {
            "ok": True,
            "disabled": 0,
            "skipped": len(cron_dicts),
            "note": "cron/jobs.json not found — crons may not be installed yet",
        }

    scripts, labels = _match_keys(cron_dicts)
    count = 0
    for job in jobs:
        if not job.get("disabled") and _job_matches(job, scripts, labels):
            job["disabled"] = True
            count += 1

    if count:
        _write_cron_jobs(bot_id, jobs)

    return {
        "ok": True,
        "disabled": count,
        "skipped": max(0, len(cron_dicts) - count),
        "note": None,
    }


def enable_app_crons(bot_id: str, manifest: object) -> dict:
    """
    Re-enable cron entries that belong to *manifest* on *bot_id*.

    Returns::
        {
          "ok":     bool,
          "enabled": int,
          "note":   str | None,
        }
    """
    cron_dicts: list[dict] = (
        manifest.cron_dicts() if callable(getattr(manifest, "cron_dicts", None)) else []
    )
    if not cron_dicts:
        return {"ok": True, "enabled": 0, "note": None}

    jobs = _read_cron_jobs(bot_id)
    if jobs is None:
        return {"ok": True, "enabled": 0, "note": "cron/jobs.json not found"}

    scripts, labels = _match_keys(cron_dicts)
    count = 0
    for job in jobs:
        if job.get("disabled") and _job_matches(job, scripts, labels):
            job.pop("disabled", None)
            count += 1

    if count:
        _write_cron_jobs(bot_id, jobs)

    return {"ok": True, "enabled": count, "note": None}


# ── Reading the whole OpenClaw cron store ──────────────────────────────────────
#
# OpenClaw ≥2026.7 moved its cron jobs into SQLite (``state/openclaw.sqlite``,
# table ``cron_jobs``) and turned ``cron/jobs.json`` into an import-once seed:
# the gateway reads it at startup, merges it into the table, then renames it
# ``jobs.json.migrated``.  So neither backend alone sees a whole fleet — a
# pre-2026.7 pod has only the file, a migrated one has only the table, and a
# pod mid-migration has a file the gateway has not ingested yet.
#
# The two shapes reconcile cleanly because each SQLite row keeps the ENTIRE
# job object in its ``job_json`` column, identical to a ``jobs.json`` entry
# (verified against a live 2026.7 pod, 2026-08-26).  One normalizer serves both.


def _oc_sqlite_path(bot_id: str) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "state" / "openclaw.sqlite"


def _jobs_from_payload(data: object) -> list[dict]:
    """Normalize a ``jobs.json`` payload to a list of job dicts.

    The file ships either as a bare list (what forge's merge writes) or as
    ``{"jobs": [...]}`` (what OpenClaw itself writes).  Accept both; anything
    else is no jobs at all.
    """
    if isinstance(data, dict):
        data = data.get("jobs")
    if not isinstance(data, list):
        return []
    return [j for j in data if isinstance(j, dict)]


_OC_CRON_JOB_QUERY = "SELECT job_json FROM cron_jobs ORDER BY sort_order ASC, job_id ASC"


def _query_oc_cron_jobs(uri: str) -> list[dict] | None:
    """Run the job query against one sqlite URI.  ``None`` on any error."""
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (sqlite3.Error, OSError):
        return None
    try:
        rows = conn.execute(_OC_CRON_JOB_QUERY).fetchall()
    except (sqlite3.Error, OSError):
        return None
    finally:
        conn.close()
    jobs: list[dict] = []
    for row in rows:
        raw = row[0]
        if not isinstance(raw, str):
            continue
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(job, dict):
            jobs.append(job)
    return jobs


def _read_cron_jobs_sqlite(bot_id: str) -> list[dict] | None:
    """Read every job from the bot's ``cron_jobs`` table.  ``None`` if unreadable.

    WAL-aware first, ``immutable=1`` as the fallback — the same ordering
    ``safe_upgrade._read_installs_from_sqlite`` uses and for the same reason:
    OpenClaw runs this DB in WAL mode, so a job it just wrote lives in the
    ``-wal`` sidecar until a checkpoint, and ``immutable=1`` reads the main
    file alone.  The honest read is tried first; the stale-but-possible one
    covers a reader that holds ACL read on the DB file but cannot create the
    ``-shm`` in its directory.
    """
    path = _oc_sqlite_path(bot_id)
    jobs = _query_oc_cron_jobs(f"file:{path}?mode=ro")
    if jobs is None:
        jobs = _query_oc_cron_jobs(f"file:{path}?mode=ro&immutable=1")
    return jobs


def read_oc_cron_jobs(bot_id: str) -> list[dict] | None:
    """Every job in *bot_id*'s OpenClaw cron store, whichever backend holds it.

    ``cron/jobs.json`` is read first: it is the only backend on a pre-2026.7
    pod, and on a pod mid-migration it holds jobs the table does not have yet.
    The ``cron_jobs`` SQLite table is the fallback, and on a migrated pod it is
    the only source — the seed has been renamed ``jobs.json.migrated`` by then.

    Returns ``None`` when NEITHER backend could be read — absent, unreadable,
    or corrupt.  That is deliberately distinct from ``[]`` ("read it; the bot
    has no schedules"), so a caller can decline to draw a conclusion from a
    surface it could not see.
    """
    raw = _read_cron_jobs(bot_id)
    if raw is not None:
        jobs = _jobs_from_payload(raw)
        if jobs:
            return jobs
    from_db = _read_cron_jobs_sqlite(bot_id)
    if from_db is not None:
        return from_db
    return [] if raw is not None else None
