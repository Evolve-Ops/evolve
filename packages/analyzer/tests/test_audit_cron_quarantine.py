"""tests/test_audit_cron_quarantine.py — quarantined cron jobs must be VISIBLE.

OpenClaw ≥2026.7 imports ~/.openclaw/cron/jobs.json once into its SQLite store
(renaming it jobs.json.migrated) and quarantines any entry that fails
validation into ~/.openclaw/cron/jobs-quarantine.json — silently: no
lastRunStatus update, no gateway error, no alert. The primary bot's
``security:cve-scan-discover`` cron sat quarantined this way from 2026-07-28
(reason "missing-payload") with no CVE scans running and nothing firing.

These tests pin the audit-side backstop: audit_cron_health surfaces every
quarantined job as a finding — critical for security/healthcheck jobs — and
fires even when jobs.json itself is absent (the usual post-migration state).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


# The live shape captured from the mini's primary bot on 2026-08-16.
_QUARANTINE = {
    "version": 1,
    "jobs": [
        {
            "quarantinedAtMs": 1785283287955,
            "sourceIndex": 1,
            "reason": "missing-payload",
            "job": {
                "name": "security:cve-scan-discover",
                "schedule": {"kind": "cron", "expr": "cron 0 9 * * * @ America/Los_Angeles"},
                "sessionTarget": "isolated",
                "task": "Run the CVE candidate discovery procedure.",
                "state": {},
                "id": "cron-e36c4c4a-b2db-4b9d-af25-560956a1f8f1",
                "enabled": True,
                "wakeMode": "now",
            },
        }
    ],
}


def _run(fake_home: Path, tmp_path: Path, bot_id: str = "evolve"):
    def _no_sudo(*_a, **_k):
        raise AssertionError("sudo fallback must not fire in this test")

    with patch.object(audit, "_bot_home", lambda *_a, **_k: fake_home), \
         patch.object(audit.subprocess, "run", _no_sudo):
        return audit.audit_cron_health(bot_id, tmp_path)


def test_quarantined_security_job_is_critical_even_without_jobs_json(tmp_path):
    """The post-migration state: jobs.json renamed away, quarantine present.
    The old early-return on missing jobs.json must not swallow the finding."""
    fake_home = tmp_path / "evolve"
    cron_dir = fake_home / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs-quarantine.json").write_text(json.dumps(_QUARANTINE))
    # No jobs.json on disk — mirrors the live mini after the SQLite import.

    findings = _run(fake_home, tmp_path)
    quarantine = [f for f in findings if "quarantined" in f.message]
    assert quarantine, [f.message for f in findings]
    (f,) = quarantine
    assert f.level == "critical"
    assert "security:cve-scan-discover" in f.message
    assert "missing-payload" in f.message
    assert f.fix_steps and "jobs-quarantine.json" in f.fix_steps


def test_non_security_quarantined_job_is_warn(tmp_path):
    fake_home = tmp_path / "evolve"
    cron_dir = fake_home / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs-quarantine.json").write_text(json.dumps({
        "version": 1,
        "jobs": [{"reason": "invalid-schedule", "job": {"name": "daily:digest"}}],
    }))

    findings = _run(fake_home, tmp_path)
    quarantine = [f for f in findings if "quarantined" in f.message]
    (f,) = quarantine
    assert f.level == "warn"
    assert "daily:digest" in f.message and "invalid-schedule" in f.message


def test_missing_quarantine_file_is_silent_and_does_not_sudo(tmp_path):
    """The benign common case (no quarantine file, no jobs.json) must add no
    quarantine finding and never trip the sudo-cat fallback."""
    fake_home = tmp_path / "evolve"
    fake_home.mkdir()

    findings = _run(fake_home, tmp_path)
    assert not any("quarantine" in f.message for f in findings)
    # Still the benign no-cron-jobs finding from the jobs.json side.
    assert findings and findings[0].level == "ok"


def test_empty_quarantine_is_silent(tmp_path):
    """A drained quarantine file (operator cleaned it up) clears the findings —
    the sweep-resolve path needs the finding to genuinely disappear."""
    fake_home = tmp_path / "evolve"
    cron_dir = fake_home / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs-quarantine.json").write_text(json.dumps({"version": 1, "jobs": []}))

    findings = _run(fake_home, tmp_path)
    assert not any("quarantined" in f.message for f in findings)


def test_unreadable_quarantine_is_skipped_not_crash(tmp_path):
    """EACCES + failed sudo fallback is a capability gap, not an anomaly."""
    fake_home = tmp_path / "evolve"
    cron_dir = fake_home / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True)
    q = cron_dir / "jobs-quarantine.json"
    q.write_text("{}")
    q.chmod(0)

    class _R:
        returncode = 1
        stdout = ""
        stderr = ""

    try:
        with patch.object(audit, "_bot_home", lambda *_a, **_k: fake_home), \
             patch.object(audit.subprocess, "run", return_value=_R()):
            findings = audit.audit_cron_health("evolve", tmp_path)
    finally:
        q.chmod(0o644)
    skipped = [f for f in findings if f.level == "skipped"]
    assert any("jobs-quarantine" in f.message for f in skipped), [f.message for f in findings]
