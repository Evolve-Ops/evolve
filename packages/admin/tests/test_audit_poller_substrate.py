"""Tests for substrate ingestion in audit_poller (Workstream B-skills).

We exercise the record-kind dispatch table extensions: skill_finding,
provider_finding, skill_run_summary, provider_run_summary. Arbiter
write is mocked because the schema layer pulls in heavy dependencies
unrelated to dispatch correctness.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


def _write_outbox_record(bot_user_dir: Path, record: dict) -> Path:
    """Plant a record in the bot's outbox dir."""
    outbox = bot_user_dir / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    rec_id = record.get("record_id", "rec-1")
    p = outbox / f"{rec_id}.json"
    p.write_text(json.dumps(record))
    return p


def _patch_audit_outbox_dir(monkeypatch, bot_user_dir: Path) -> None:
    """Redirect audit_poller's path resolution into tmp."""
    from evolve_admin.applications import audit_poller
    monkeypatch.setattr(
        audit_poller, "_audit_outbox_dir",
        lambda bot_user: bot_user_dir / ".openclaw" / "workspace" / "evolve" / "audit_outbox",
    )
    monkeypatch.setattr(
        audit_poller, "_audit_outbox_ingested",
        lambda bot_user: bot_user_dir / ".openclaw" / "workspace" / "evolve" / "audit_outbox" / "_ingested",
    )


def test_poll_bot_counts_skill_finding(tmp_path: Path, monkeypatch) -> None:
    bot_dir = tmp_path / "team_bot_a"
    bot_dir.mkdir()
    _patch_audit_outbox_dir(monkeypatch, bot_dir)
    _write_outbox_record(bot_dir, {
        "record_id": "rec-skill-1",
        "kind": "skill_finding",
        "bot_id": "team_bot_a",
        "skill_id": "gmail",
        "signature": "sig-skill-1",
        "outcome": "propose",
        "severity": "major",
        "category": "credential_state",
        "description": "Token expired.",
    })
    from evolve_admin.applications.audit_poller import poll_bot
    with patch(
        "evolve_admin.applications.audit_poller._ingest_skill_finding",
        return_value=True,
    ):
        result = poll_bot("team_bot_a", "team_bot_a", tmp_path / "shared")
    assert result.skill_findings_ingested == 1
    assert result.skill_proposals_raised == 1
    assert result.files_processed == 1


def test_poll_bot_counts_provider_finding(tmp_path: Path, monkeypatch) -> None:
    bot_dir = tmp_path / "team_bot_a"
    bot_dir.mkdir()
    _patch_audit_outbox_dir(monkeypatch, bot_dir)
    _write_outbox_record(bot_dir, {
        "record_id": "rec-provider-1",
        "kind": "provider_finding",
        "bot_id": "team_bot_a",
        "provider_id": "google_workspace",
        "signature": "sig-prov-1",
        "outcome": "propose",
        "severity": "critical",
        "category": "token_state",
        "description": "Token expires soon.",
    })
    from evolve_admin.applications.audit_poller import poll_bot
    with patch(
        "evolve_admin.applications.audit_poller._ingest_provider_finding",
        return_value=True,
    ):
        result = poll_bot("team_bot_a", "team_bot_a", tmp_path / "shared")
    assert result.provider_findings_ingested == 1
    assert result.provider_proposals_raised == 1


def test_poll_bot_auto_fix_outcome_archives_trail_only(tmp_path: Path, monkeypatch) -> None:
    """auto_fix outcomes shouldn't raise Proposals (trail-only)."""
    bot_dir = tmp_path / "team_bot_a"
    bot_dir.mkdir()
    _patch_audit_outbox_dir(monkeypatch, bot_dir)
    _write_outbox_record(bot_dir, {
        "record_id": "rec-skill-af",
        "kind": "skill_finding",
        "bot_id": "team_bot_a",
        "skill_id": "gmail",
        "signature": "sig",
        "outcome": "auto_fix",
        "severity": "minor",
        "category": "manifest_thin",
        "description": "x",
    })
    from evolve_admin.applications.audit_poller import poll_bot
    result = poll_bot("team_bot_a", "team_bot_a", tmp_path / "shared")
    assert result.skill_findings_ingested == 0
    assert result.skill_proposals_raised == 0
    assert result.files_processed == 1


def test_poll_bot_handles_skill_run_summary(tmp_path: Path, monkeypatch) -> None:
    bot_dir = tmp_path / "team_bot_a"
    bot_dir.mkdir()
    _patch_audit_outbox_dir(monkeypatch, bot_dir)
    _write_outbox_record(bot_dir, {
        "record_id": "rec-skill-sum",
        "kind": "skill_run_summary",
        "bot_id": "team_bot_a",
        "skills_audited": 3,
    })
    from evolve_admin.applications.audit_poller import poll_bot
    result = poll_bot("team_bot_a", "team_bot_a", tmp_path / "shared")
    assert result.summaries_processed == 1
    assert result.files_processed == 1


def test_poll_bot_handles_provider_run_summary(tmp_path: Path, monkeypatch) -> None:
    bot_dir = tmp_path / "team_bot_a"
    bot_dir.mkdir()
    _patch_audit_outbox_dir(monkeypatch, bot_dir)
    _write_outbox_record(bot_dir, {
        "record_id": "rec-prov-sum",
        "kind": "provider_run_summary",
        "bot_id": "team_bot_a",
        "providers_audited": 2,
    })
    from evolve_admin.applications.audit_poller import poll_bot
    result = poll_bot("team_bot_a", "team_bot_a", tmp_path / "shared")
    assert result.summaries_processed == 1


def test_tick_result_substrate_aggregates() -> None:
    from evolve_admin.applications.audit_poller import PollResult, TickResult
    tr = TickResult(bots=[
        PollResult(bot_id="a", bot_user="a", skill_findings_ingested=2, skill_proposals_raised=2),
        PollResult(bot_id="b", bot_user="b", provider_findings_ingested=1, provider_proposals_raised=1),
    ])
    assert tr.total_skill_findings == 2
    assert tr.total_skill_proposals == 2
    assert tr.total_provider_findings == 1
    assert tr.total_provider_proposals == 1


def test_emit_substrate_proposal_dedupes_existing_trigger(tmp_path: Path) -> None:
    """When a Proposal with the same trigger_observation already exists,
    re-emitting is a no-op (still returns True)."""
    from evolve_admin.applications.audit_poller import _emit_substrate_proposal

    existing = MagicMock()
    existing.trigger_observations = [
        "skill_audit:skill_audit:gmail:sig-existing"
    ]
    mock_iter = MagicMock(return_value=[existing])

    with patch.dict("sys.modules", {
        "arbiter": MagicMock(store=MagicMock(iter_proposals=mock_iter)),
        "arbiter.store": MagicMock(iter_proposals=mock_iter),
        "schema": MagicMock(),
        "schema.proposal": MagicMock(),
    }):
        ok = _emit_substrate_proposal(
            shared_dir=tmp_path,
            record={
                "signature": "sig-existing",
                "bot_id": "team_bot_a",
                "skill_id": "gmail",
                "category": "credential_state",
                "severity": "major",
                "description": "x",
            },
            element_type="skill",
            element_id="gmail",
            generator_id="skill_audit:gmail",
            dimension="reliability",
        )
    assert ok is True


def test_emit_substrate_proposal_returns_false_when_signature_missing(tmp_path: Path) -> None:
    from evolve_admin.applications.audit_poller import _emit_substrate_proposal
    ok = _emit_substrate_proposal(
        shared_dir=tmp_path,
        record={"signature": "", "bot_id": "team_bot_a"},   # missing
        element_type="skill", element_id="x",
        generator_id="skill_audit:x", dimension="reliability",
    )
    assert ok is False
