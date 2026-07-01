"""
cron_manager.py — Pause and resume app cron entries for Evolve gallery apps.

Cron entries installed via forge are merged into:
    /Users/<bot_id>/.openclaw/cron/jobs.json

Each entry looks like:
    {"name": "...", "schedule": "...", "script": "...", "disabled": bool}

We disable entries by setting "disabled": true and undo that on resume.
The bot's OpenClaw scheduler skips any entry where disabled=true.

Match strategy (in order):
  1. script basename matches a cron_dict["script"] basename from the manifest
  2. job name/label matches a cron_dict["label"] or cron_dict["name"]

Both checks are case-sensitive and basename-only to avoid false path matches.
"""

from __future__ import annotations

import json
import os
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
