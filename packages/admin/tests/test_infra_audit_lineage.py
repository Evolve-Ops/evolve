"""Lineage-aware ingest of infra_finding records.

When the same fingerprint has been dismissed N times in the past 30 days,
re-proposing the same suggested_fix is operator-spam. These tests verify
that `_lineage_for_signature` correctly counts past dismissals and that
the context builder pivots the proposal framing to "investigate why this
keeps coming back" at the threshold (≥2 dismissals).

The 5d-ago dismissal lineage entry on the live mcp-bridge proposal is the
canonical case. Pre-PR, the operator dismissed it; post-audit it came right
back with the same unworkable bootstrap fix. With this PR, the proposal
body now leads with "this has been dismissed 1 time" (note-only at count 1)
or "Repeatedly dismissed" (lead-with framing at count ≥2).
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import audit_poller  # noqa: E402


def _write_archived_proposal(
    shared_dir: Path,
    *,
    proposal_id: str,
    signature: str,
    status: str = "dismissed",
    updated_at: str | None = None,
) -> Path:
    """Write an archived Proposal via the schema so from_dict can load it.

    We can't take the shortcut of writing raw JSON because Proposal.from_dict
    requires nested Provenance / action / risk_tag — missing any of them
    makes load_proposal_file return None and the lineage probe sees zero
    history.
    """
    from schema.proposal import (
        Investigation, Proposal, Provenance, RiskTag, new_proposal_id,
    )
    from arbiter import store as arbiter_store

    from schema.proposal import StatusTransition

    transition = StatusTransition(
        from_status="pending",
        to_status=status,
        at=updated_at or "2026-05-25T00:00:00Z",
        actor="user",
        reason="test fixture",
    )
    prop = Proposal(
        id=proposal_id,
        bot_id="pod",
        generator_id="infra_audit",
        dimension="reliability",
        trigger_observations=[f"infra_audit:{signature}"],
        provenance=Provenance(technique="infra_audit.v1"),
        problem="test",
        action=Investigation(context="test context"),
        risk_tag=RiskTag(blast_radius="pod", reversibility="manual"),
        status=status,
        history=[transition],
    )
    archived = arbiter_store.proposals_root(shared_dir) / "archived"
    archived.mkdir(parents=True, exist_ok=True)
    path = archived / f"{proposal_id}.json"
    path.write_text(json.dumps(prop.to_dict()))
    return path


def test_no_history_returns_zero_counts(tmp_path: Path) -> None:
    """Clean slate: empty proposal store → counts are zero."""
    lineage = audit_poller._lineage_for_signature(
        shared_dir=tmp_path, signature="any:fingerprint", window_days=30,
    )
    assert lineage == {
        "dismissal_count": 0,
        "rejection_count": 0,
        "last_dismissed_iso": "",
    }


def test_dismissals_inside_window_counted(tmp_path: Path) -> None:
    """Two dismissed proposals with the same fingerprint, both recent → count 2."""
    sig = "infra_audit:daemons:daemon_not_loaded:abc123"
    now = _dt.datetime.now(_dt.timezone.utc)
    recent_iso = now.isoformat().replace("+00:00", "Z")
    older_iso = (now - _dt.timedelta(days=5)).isoformat().replace("+00:00", "Z")
    _write_archived_proposal(
        tmp_path, proposal_id="p1", signature=sig,
        status="dismissed", updated_at=recent_iso,
    )
    _write_archived_proposal(
        tmp_path, proposal_id="p2", signature=sig,
        status="dismissed", updated_at=older_iso,
    )

    lineage = audit_poller._lineage_for_signature(
        shared_dir=tmp_path, signature=sig, window_days=30,
    )
    assert lineage["dismissal_count"] == 2
    assert lineage["rejection_count"] == 0
    # last_dismissed_iso tracks the most recent
    assert lineage["last_dismissed_iso"] == recent_iso


def test_dismissals_outside_window_ignored(tmp_path: Path) -> None:
    """A dismissal from 60 days ago doesn't count when window=30."""
    sig = "infra_audit:daemons:daemon_not_loaded:abc123"
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=60))
    old_iso = old.isoformat().replace("+00:00", "Z")
    _write_archived_proposal(
        tmp_path, proposal_id="p1", signature=sig,
        status="dismissed", updated_at=old_iso,
    )

    lineage = audit_poller._lineage_for_signature(
        shared_dir=tmp_path, signature=sig, window_days=30,
    )
    assert lineage["dismissal_count"] == 0


def test_rejections_counted_separately(tmp_path: Path) -> None:
    """Rejections (peer review) and dismissals (operator) live on different counters."""
    sig = "infra_audit:daemons:daemon_not_loaded:abc123"
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    _write_archived_proposal(
        tmp_path, proposal_id="p1", signature=sig,
        status="dismissed", updated_at=now_iso,
    )
    _write_archived_proposal(
        tmp_path, proposal_id="p2", signature=sig,
        status="rejected", updated_at=now_iso,
    )

    lineage = audit_poller._lineage_for_signature(
        shared_dir=tmp_path, signature=sig, window_days=30,
    )
    assert lineage["dismissal_count"] == 1
    assert lineage["rejection_count"] == 1


def test_different_signature_not_counted(tmp_path: Path) -> None:
    """A dismissed proposal with a DIFFERENT fingerprint must not contribute."""
    me = "infra_audit:daemons:daemon_not_loaded:mine"
    other = "infra_audit:daemons:daemon_not_loaded:other"
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    _write_archived_proposal(
        tmp_path, proposal_id="p1", signature=other,
        status="dismissed", updated_at=now_iso,
    )

    lineage = audit_poller._lineage_for_signature(
        shared_dir=tmp_path, signature=me, window_days=30,
    )
    assert lineage["dismissal_count"] == 0


def test_context_pivots_at_threshold_two() -> None:
    """At ≥2 dismissals the context body leads with 'Repeatedly dismissed'."""
    record = {
        "element": "daemons",
        "category": "daemon_not_loaded",
        "severity": "critical",
        "description": "LaunchAgent foo is installed but not loaded.",
        "evidence": {"label": "foo", "path": "/tmp/x.plist"},
        "suggested_fix": "/bin/launchctl bootstrap gui/$UID /tmp/x.plist",
        "rationale": "severity=critical; auto-surface per heuristic triage",
        "audit_run_id": "run-123",
        "record_id": "rec-456",
        "_lineage": {
            "dismissal_count": 3,
            "rejection_count": 0,
            "last_dismissed_iso": "2026-05-25T00:00:00Z",
        },
    }
    body = audit_poller._render_infra_finding_context(record)
    assert "Repeatedly dismissed" in body
    assert "dismissed 3 times" in body
    assert "2026-05-25" in body
    assert "investigate why" in body.lower()
    # Original finding still in the body — operator can still apply if needed.
    assert "/bin/launchctl bootstrap" in body


def test_context_notes_single_dismissal_without_pivoting() -> None:
    """At count=1 the body shows the original framing with a small note."""
    record = {
        "element": "daemons",
        "category": "daemon_not_loaded",
        "severity": "critical",
        "description": "LaunchAgent foo is installed but not loaded.",
        "evidence": {"label": "foo"},
        "suggested_fix": "/bin/launchctl bootstrap gui/$UID /tmp/x.plist",
        "rationale": "",
        "audit_run_id": "run-123",
        "record_id": "rec-456",
        "_lineage": {
            "dismissal_count": 1,
            "rejection_count": 0,
            "last_dismissed_iso": "2026-05-25T00:00:00Z",
        },
    }
    body = audit_poller._render_infra_finding_context(record)
    # No pivot framing
    assert "Repeatedly dismissed" not in body
    # But the dismissal IS surfaced as a note
    assert "dismissed once" in body
    assert "2026-05-25" in body


def test_context_zero_dismissal_default_framing() -> None:
    """Zero lineage history → standard infra finding body, no notes."""
    record = {
        "element": "daemons",
        "category": "daemon_not_loaded",
        "severity": "critical",
        "description": "Test.",
        "evidence": {},
        "suggested_fix": "fix",
        "rationale": "",
        "audit_run_id": "run-123",
        "record_id": "rec-456",
        "_lineage": {"dismissal_count": 0, "rejection_count": 0, "last_dismissed_iso": ""},
    }
    body = audit_poller._render_infra_finding_context(record)
    assert "Repeatedly dismissed" not in body
    assert "dismissed once" not in body
    assert "## Infrastructure audit finding" in body
