"""Tests for the runner's investigation request path (Workstream C).

Covers:
  - run_investigation_request writes per-run JSON, trail entry, and outbox
    record with kind=investigation_diagnosis.
  - process_inbox dispatches kind=investigation to the new runner.
  - _prune_investigations drops files older than the retention window.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import app_audit_runner as runner  # noqa: E402
import app_audit_investigation as inv_mod  # noqa: E402


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    for sub in (
        "manifests",
        "evolve",
        "evolve/audits",
        "evolve/audit_outbox",
        "evolve/audit_inbox",
        "evolve/investigations",
    ):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    return ws


def _stub_run_investigation(workspace, status="ok"):
    """Return a fake run_investigation that builds a fixed output."""

    def _fake(*, investigation_id, bot_id, workspace, shared_dir,
             user_description, requesting_user, requested_at):
        return inv_mod.InvestigationOutput(
            investigation_id=investigation_id,
            bot_id=bot_id,
            user_description=user_description,
            requesting_user=requesting_user,
            requested_at=requested_at,
            started_at="2026-05-17T09:00:00Z",
            completed_at="2026-05-17T09:01:00Z",
            triage=inv_mod.TriageResult(
                candidates=[
                    inv_mod.TriageCandidate(
                        element_type="app", element_id="x",
                        confidence="high", justification="y",
                    )
                ],
                top_candidate=inv_mod.TriageCandidate(
                    element_type="app", element_id="x",
                    confidence="high", justification="y",
                ),
                tokens_used=42,
            ),
            diagnosis=inv_mod.Diagnosis(
                diagnosis="root cause" if status == "ok" else None,
                suggested_fix="do thing",
                confidence="high" if status == "ok" else "low",
                evidence=["scripts/x.py:10"],
                what_i_checked=["a", "b"],
                tokens_used=58,
            ),
            chosen_candidate=inv_mod.TriageCandidate(
                element_type="app", element_id="x",
                confidence="high", justification="y",
            ),
            related_signal_ids=["sig-1"],
            status=status,
        )

    return _fake


def test_run_investigation_request_writes_all_artifacts(
    tmp_path: Path, monkeypatch,
) -> None:
    ws = _make_workspace(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()

    monkeypatch.setattr(
        inv_mod, "run_investigation", _stub_run_investigation(ws),
    )

    request = {
        "investigation_id": "inv-abc",
        "kind": "investigation",
        "user_description": "morning briefing didn't arrive",
        "requesting_user": "pod:pod_admin_user",
        "requested_at": "2026-05-17T08:00:00Z",
    }
    result = runner.run_investigation_request(
        ws, bot_id="team_bot_a", shared_dir=shared, request=request,
    )
    assert result["status"] == "ok"
    assert result["investigation_id"] == "inv-abc"

    # Per-run JSON.
    per_run = ws / "evolve" / "investigations" / "inv-abc.json"
    assert per_run.exists()
    body = json.loads(per_run.read_text())
    assert body["investigation_id"] == "inv-abc"
    assert body["diagnosis"]["diagnosis"] == "root cause"

    # Trail entry.
    trail = ws / "evolve" / "investigations" / "trail.jsonl"
    assert trail.exists()
    lines = trail.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["kind"] == "investigation"
    assert entry["investigation_id"] == "inv-abc"
    assert entry["requesting_user"] == "pod:pod_admin_user"

    # Outbox record.
    outbox_files = list((ws / "evolve" / "audit_outbox").glob("*.json"))
    assert len(outbox_files) == 1
    rec = json.loads(outbox_files[0].read_text())
    assert rec["kind"] == "investigation_diagnosis"
    assert rec["investigation_id"] == "inv-abc"
    assert rec["requesting_user"] == "pod:pod_admin_user"
    assert rec["status"] == "ok"


def test_run_investigation_request_no_diagnosis_status_propagates(
    tmp_path: Path, monkeypatch,
) -> None:
    ws = _make_workspace(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()

    monkeypatch.setattr(
        inv_mod, "run_investigation",
        _stub_run_investigation(ws, status="no_diagnosis"),
    )

    request = {
        "investigation_id": "inv-nope",
        "kind": "investigation",
        "user_description": "things feel off",
        "requesting_user": "pod:pod_admin_user",
        "requested_at": "2026-05-17T08:00:00Z",
    }
    runner.run_investigation_request(
        ws, bot_id="team_bot_a", shared_dir=shared, request=request,
    )
    outbox_files = list((ws / "evolve" / "audit_outbox").glob("*.json"))
    assert len(outbox_files) == 1
    rec = json.loads(outbox_files[0].read_text())
    assert rec["status"] == "no_diagnosis"
    assert rec["diagnosis"] is None


def test_process_inbox_routes_investigation_kind(
    tmp_path: Path, monkeypatch,
) -> None:
    """A request file with kind=investigation reaches the investigation runner."""
    ws = _make_workspace(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()

    inbox = ws / "evolve" / "audit_inbox"
    req_id = "inv-xyz"
    request_body = {
        "investigation_id": req_id,
        "kind": "investigation",
        "user_description": "x failed",
        "requesting_user": "pod:pod_admin_user",
        "requested_at": "2026-05-17T08:00:00Z",
    }
    (inbox / f"investigation-{req_id}.json").write_text(
        json.dumps(request_body)
    )

    monkeypatch.setattr(
        inv_mod, "run_investigation", _stub_run_investigation(ws),
    )

    out = runner.process_inbox(
        ws, bot_id="team_bot_a", shared_dir=shared, request_id=f"investigation-{req_id}",
    )
    assert out["processed"] == 1
    assert out["errors"] == []
    per_run = ws / "evolve" / "investigations" / f"{req_id}.json"
    assert per_run.exists()


def test_prune_investigations_drops_old_files(tmp_path: Path) -> None:
    inv_dir = tmp_path / "investigations"
    inv_dir.mkdir()
    # Old file.
    old_path = inv_dir / "inv-old.json"
    old_path.write_text("{}")
    old_mtime = (
        datetime.now(timezone.utc) - timedelta(days=45)
    ).timestamp()
    import os
    os.utime(str(old_path), (old_mtime, old_mtime))

    # Recent file.
    recent_path = inv_dir / "inv-recent.json"
    recent_path.write_text("{}")

    pruned = runner._prune_investigations(inv_dir, retain_days=30)
    assert pruned == 1
    assert recent_path.exists()
    assert not old_path.exists()
