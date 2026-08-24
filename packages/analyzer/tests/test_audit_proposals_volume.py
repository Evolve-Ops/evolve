"""Tests for audit.audit_proposals volume detector.

Pins the fix in [internal/alert-root-cause-audit-2026-06-09.md]: the volume
counter now reads each proposal JSON's ``created_at`` field instead of
trusting the filename prefix (UUIDs never matched) or mtime (state-history
appends bump mtime on days-old proposals, inflating "today's" count).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def _write_proposal(path: Path, created_at_iso: str, mtime: float | None = None) -> None:
    path.write_text(json.dumps({
        "id": path.stem,
        "schema_version": 1,
        "created_at": created_at_iso,
        "status": "pending",
    }))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_counts_today_created_proposals(tmp_path: Path):
    pending = tmp_path / "proposals" / "pending"
    pending.mkdir(parents=True)
    today = datetime.now(timezone.utc).date().isoformat()
    for i in range(3):
        _write_proposal(pending / f"prop-{i}.json", f"{today}T12:00:00+00:00")

    # Threshold below count → expect a warn finding.
    findings = audit.audit_proposals(tmp_path, {"thresholds": {"maxProposalsPerDay": 2}})
    warns = [f for f in findings if f.level == "warn" and "pending" in f.message]
    assert len(warns) == 1
    assert "3 proposals today exceeds limit 2" in warns[0].message
    assert "misfiring" not in warns[0].message


def test_yesterday_proposal_with_today_mtime_is_not_counted(tmp_path: Path):
    """A proposal created yesterday whose state-history was appended today
    bumps mtime to now. The old detector counted it as "new today"; the
    fixed detector reads created_at and must not.
    """
    pending = tmp_path / "proposals" / "pending"
    pending.mkdir(parents=True)
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    today_epoch = datetime.now(timezone.utc).timestamp()

    # 5 proposals with created_at=yesterday but mtime=now.
    for i in range(5):
        _write_proposal(
            pending / f"old-{i}.json",
            f"{yesterday}T12:00:00+00:00",
            mtime=today_epoch,
        )

    # Threshold of 1 — if the old mtime path were used, 5>1 → warn.
    findings = audit.audit_proposals(tmp_path, {"thresholds": {"maxProposalsPerDay": 1}})
    warns = [f for f in findings if f.level == "warn" and "pending" in f.message]
    assert warns == [], (
        "yesterday-created proposals must not be counted as today's, "
        f"got: {[f.message for f in warns]}"
    )


def test_malformed_proposal_is_skipped(tmp_path: Path):
    """A single bad JSON file shouldn't crash the auditor or be counted."""
    pending = tmp_path / "proposals" / "pending"
    pending.mkdir(parents=True)
    today = datetime.now(timezone.utc).date().isoformat()
    _write_proposal(pending / "good.json", f"{today}T08:00:00+00:00")
    (pending / "bad.json").write_text("{not valid json")

    findings = audit.audit_proposals(tmp_path, {"thresholds": {"maxProposalsPerDay": 0}})
    warns = [f for f in findings if f.level == "warn" and "pending" in f.message]
    assert len(warns) == 1
    assert "1 proposals today" in warns[0].message
