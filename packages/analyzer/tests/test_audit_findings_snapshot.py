"""Tests for audit._write_findings_snapshot and dispatch_findings snapshot side-effect.

Pins the contract that audit.py writes shared_dir/audit/current-findings.json after
each successful run, so the pod report can read *current* outstanding findings
rather than reconstructing state from the audit-warns event log. A missing or
stale snapshot signals "audit not running" to the report.

Background: from 2026-05-05 to 2026-05-07 audit.py was crashing during
audit_process (IndexError on Chrome's PasswordManagerBrowserExtensionHelper
ps line) and never reaching dispatch_findings. The pod report's Security
section, which read audit-warns.jsonl with a 24h cutoff, showed "No findings"
for two days while 14 CRITICAL findings sat undelivered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def _finding(level, msg, category="machine", bot_id=None, detail=""):
    return audit.Finding(level=level, category=category, bot_id=bot_id,
                         message=msg, detail=detail,
                         # Criticals require an explicit classification (R-3).
                         finding_kind="event" if level == "critical" else "")


# ── _write_findings_snapshot: shape and atomicity ──


def test_snapshot_has_expected_shape(tmp_path: Path):
    crits = [_finding("critical", "team_bot_a: Telegram token in .env", category="config", bot_id="team_bot_a")]
    warns = [_finding("warn", "machine: pfctl rules empty"),
             _finding("warn", "admin_bot: cron baseline drift", bot_id="admin_bot")]

    audit._write_findings_snapshot(crits, warns, tmp_path)

    snap_path = tmp_path / "audit" / "current-findings.json"
    assert snap_path.exists()
    data = json.loads(snap_path.read_text())

    assert data["schema_version"] == audit.CURRENT_FINDINGS_SCHEMA
    assert data["audit_succeeded"] is True
    assert "audit_completed_at" in data and data["audit_completed_at"]
    assert len(data["critical"]) == 1
    assert len(data["warn"]) == 2

    # Each finding entry preserves all fields the reader needs
    c = data["critical"][0]
    assert c == {
        "category": "config", "bot_id": "team_bot_a",
        "message": "team_bot_a: Telegram token in .env", "detail": "",
        "finding_kind": "event",
    }


def test_snapshot_overwrites_previous(tmp_path: Path):
    """Resolved findings disappear: a second run with fewer findings replaces, not appends."""
    audit._write_findings_snapshot(
        [_finding("critical", "old crit")],
        [_finding("warn", "old warn 1"), _finding("warn", "old warn 2")],
        tmp_path,
    )
    audit._write_findings_snapshot([], [_finding("warn", "only warn left")], tmp_path)

    snap = json.loads((tmp_path / "audit" / "current-findings.json").read_text())
    assert snap["critical"] == []
    assert len(snap["warn"]) == 1
    assert snap["warn"][0]["message"] == "only warn left"


def test_snapshot_creates_audit_dir_if_missing(tmp_path: Path):
    """The audit/ subdir is created lazily — a fresh shared_dir doesn't have it."""
    assert not (tmp_path / "audit").exists()
    audit._write_findings_snapshot([], [], tmp_path)
    assert (tmp_path / "audit").is_dir()
    assert (tmp_path / "audit" / "current-findings.json").exists()


def test_snapshot_no_temp_file_left_behind(tmp_path: Path):
    """Atomic write must not leave a .tmp sibling."""
    audit._write_findings_snapshot([], [], tmp_path)
    files = list((tmp_path / "audit").iterdir())
    assert [f.name for f in files] == ["current-findings.json"]


# ── dispatch_findings: snapshot side-effect ──


def test_dispatch_findings_writes_snapshot_in_normal_mode(tmp_path: Path, monkeypatch):
    # Stub out alert delivery so the test stays hermetic
    monkeypatch.setattr(audit, "_send_security_alert", lambda *a, **k: True)
    monkeypatch.setattr(audit, "_send_warn_log", lambda *a, **k: None)

    findings = [
        _finding("critical", "leaked token"),
        _finding("warn", "minor drift"),
        _finding("ok", "all good"),
    ]
    audit.dispatch_findings(findings, tmp_path, config={}, dry_run=False)

    snap = json.loads((tmp_path / "audit" / "current-findings.json").read_text())
    assert len(snap["critical"]) == 1
    assert len(snap["warn"]) == 1
    # ok findings are NOT included — the snapshot is for actionable issues only
    assert all(f["message"] != "all good" for f in snap["critical"] + snap["warn"])


def test_dispatch_findings_skips_snapshot_in_dry_run(tmp_path: Path):
    """Dry-run runs (e.g. ad-hoc invocations) must not pollute the production snapshot."""
    findings = [_finding("critical", "should-not-be-recorded")]
    audit.dispatch_findings(findings, tmp_path, config={}, dry_run=True)
    assert not (tmp_path / "audit").exists()


def test_dispatch_writes_snapshot_even_if_alert_dispatch_raises(tmp_path: Path, monkeypatch):
    """Real-world failure mode: delivery bookkeeping raised PermissionError
    on the live pod 2026-05-07 (alerts/ dir not writable). The snapshot must
    still land — it's the report's source of truth and can't be hostage to
    alert delivery bookkeeping."""
    monkeypatch.setattr(audit, "_send_security_alert", lambda *a, **k: True)
    monkeypatch.setattr(audit, "_send_warn_log", lambda *a, **k: None)

    def boom(*a, **k):
        raise PermissionError("signals/ not writable")
    monkeypatch.setattr(audit, "_record_telegram_delivery", boom)

    findings = [_finding("critical", "leaked token"), _finding("warn", "drift")]
    with pytest.raises(PermissionError):
        audit.dispatch_findings(findings, tmp_path, config={}, dry_run=False)

    # Snapshot was written despite the downstream crash
    snap = json.loads((tmp_path / "audit" / "current-findings.json").read_text())
    assert len(snap["critical"]) == 1
    assert len(snap["warn"]) == 1
