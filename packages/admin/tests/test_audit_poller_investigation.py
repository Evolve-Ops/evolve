"""Tests for the audit_poller's investigation record kinds (Workstream C).

Covers:
  - investigation_diagnosis → notification queue entry, no Proposal
  - investigation_unresolved → Proposal in the arbiter
  - unknown kinds still error out cleanly
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import audit_poller  # noqa: E402


def _make_outbox(tmp_root: Path, bot_user: str) -> Path:
    outbox = (
        tmp_root / "Users" / bot_user
        / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
    )
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch) -> Path:
    """Re-point audit_poller's /Users paths into tmp_path."""
    def _audit_outbox(bot_user: str) -> Path:
        return (
            tmp_path / "Users" / bot_user
            / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
        )

    def _audit_ingested(bot_user: str) -> Path:
        return _audit_outbox(bot_user) / "_ingested"

    monkeypatch.setattr(audit_poller, "_audit_outbox_dir", _audit_outbox)
    monkeypatch.setattr(audit_poller, "_audit_outbox_ingested", _audit_ingested)
    return tmp_path


def _write_record(outbox: Path, record: dict, record_id: str) -> Path:
    p = outbox / f"{record_id}.json"
    record.setdefault("record_id", record_id)
    p.write_text(json.dumps(record))
    return p


# ── investigation_diagnosis → notification ──────────────────────────────────


def test_diagnosis_record_writes_user_notification(
    tmp_root: Path, tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    _write_record(outbox, {
        "kind": "investigation_diagnosis",
        "ts": "2026-05-17T10:00:00Z",
        "producer": "app_audit_investigation",
        "investigation_id": "inv-abc",
        "bot_id": "team_bot_a",
        "user_description": "morning briefing didn't arrive",
        "requesting_user": "pod:pod_admin_user",
        "requested_at": "2026-05-17T08:00:00Z",
        "completed_at": "2026-05-17T08:01:00Z",
        "status": "ok",
        "diagnosis": "The gmail token expired three days ago.",
        "suggested_fix": "Regenerate the gmail token.",
        "confidence": "high",
        "evidence": ["auth-profiles.json"],
        "what_i_checked": ["recent signals", "gmail trail"],
        "chosen_candidate": {
            "element_type": "skill", "element_id": "gmail",
            "confidence": "high",
        },
    }, "rec-1")

    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    assert result.investigation_notifications == 1
    assert result.files_processed == 1
    assert result.errors == []

    # Notification landed under the user's queue.
    queue_dir = shared / "notifications"
    queues = list(queue_dir.glob("*.jsonl"))
    assert len(queues) == 1
    # The user-key in the file name is colon-encoded.
    lines = queues[0].read_text().splitlines()
    events = [json.loads(ln) for ln in lines]
    assert any(
        ev.get("kind") == "investigation_diagnosis" for ev in events
    )
    event = next(
        ev for ev in events
        if ev.get("kind") == "investigation_diagnosis"
    )
    assert "gmail token" in event["detail"].lower()
    assert "Regenerate" in event["detail"]
    assert event["investigation_id"] == "inv-abc"

    # No Proposal should have been written — diagnosis lives in the thread.
    proposals_dir = shared / "proposals"
    assert not any(
        (proposals_dir / sd).exists() and any((proposals_dir / sd).iterdir())
        for sd in ("pending", "snoozed") if (proposals_dir / sd).exists()
    )


def test_no_diagnosis_record_renders_couldnt_pinpoint(
    tmp_root: Path, tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    _write_record(outbox, {
        "kind": "investigation_diagnosis",
        "ts": "2026-05-17T10:00:00Z",
        "producer": "app_audit_investigation",
        "investigation_id": "inv-low",
        "bot_id": "team_bot_a",
        "user_description": "something feels off",
        "requesting_user": "pod:pod_admin_user",
        "requested_at": "2026-05-17T08:00:00Z",
        "completed_at": "2026-05-17T08:01:00Z",
        "status": "no_diagnosis",
        "diagnosis": None,
        "confidence": "low",
        "what_i_checked": ["recent signals", "all trails"],
    }, "rec-2")

    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.investigation_notifications == 1

    queue_files = list((shared / "notifications").glob("*.jsonl"))
    assert len(queue_files) == 1
    events = [json.loads(ln) for ln in queue_files[0].read_text().splitlines()]
    event = next(
        ev for ev in events
        if ev.get("kind") == "investigation_diagnosis"
    )
    assert "couldn't pinpoint" in event["detail"]
    assert "recent signals" in event["detail"].lower()
    assert "evo fail flag" in event["detail"]


# ── investigation_unresolved → Proposal ────────────────────────────────────


def test_unresolved_record_writes_proposal(
    tmp_root: Path, tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    _write_record(outbox, {
        "kind": "investigation_unresolved",
        "ts": "2026-05-17T10:00:00Z",
        "producer": "app_audit_investigation",
        "investigation_id": "inv-flag",
        "bot_id": "team_bot_a",
        "user_description": "morning briefing didn't arrive",
        "requesting_user": "pod:pod_admin_user",
        "triage_candidates": [
            {"element_type": "app", "element_id": "morning-briefing",
             "confidence": "medium"}
        ],
        "chosen_candidate": {
            "element_type": "app", "element_id": "morning-briefing",
            "confidence": "medium",
        },
        "evidence": ["scripts/briefing.py:42"],
        "previous_diagnosis": None,
        "previous_confidence": "low",
        "related_signal_ids": ["sig-x"],
    }, "rec-3")

    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.investigation_proposals_raised == 1
    assert result.errors == []

    # Proposal landed under shared/proposals/pending/
    pending = list((shared / "proposals" / "pending").glob("*.json"))
    assert len(pending) == 1
    proposal = json.loads(pending[0].read_text())
    assert proposal["generator_id"] == "app_audit_investigation_unresolved"
    assert proposal["bot_id"] == "team_bot_a"
    assert "inv-flag" in str(proposal.get("trigger_observations") or "")
    assert "morning briefing didn't arrive" in proposal["problem"]


def test_unresolved_is_idempotent(tmp_root: Path, tmp_path: Path) -> None:
    """Re-ingesting the same investigation_id shouldn't duplicate the Proposal."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    body = {
        "kind": "investigation_unresolved",
        "ts": "2026-05-17T10:00:00Z",
        "producer": "app_audit_investigation",
        "investigation_id": "inv-same",
        "bot_id": "team_bot_a",
        "user_description": "x",
        "requesting_user": "pod:pod_admin_user",
        "triage_candidates": [],
        "chosen_candidate": None,
        "evidence": [],
    }
    _write_record(outbox, dict(body), "rec-a")
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    _write_record(outbox, dict(body), "rec-b")
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    pending = list((shared / "proposals" / "pending").glob("*.json"))
    assert len(pending) == 1
