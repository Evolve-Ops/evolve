"""Tests for the Apps-page coherence + drift action helpers.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10–§11.
These cover the pure mutations the UI routes call into. The HTTP layer
is tested via the web-server fixture below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.coherence_actions import (  # noqa: E402
    approve_changes,
    coherence_summary,
    flag_for_operator,
    mute_finding,
    promote_to_authored,
    snooze_finding,
    unmute_finding,
)


# ── approve_changes ─────────────────────────────────────────────────

def test_approve_clears_drift_and_records_decision() -> None:
    manifest = {
        "reconciliation": {
            "added_files": [{"path": "a.py"}],
            "removed_files": [{"path": "b.py"}],
            "drifted_fields": [{"field": "schedule"}],
        },
    }
    result = approve_changes(manifest, by="ui:operator")
    assert result["approved_count"] == 3
    rec = manifest["reconciliation"]
    assert rec["added_files"] == []
    assert rec["removed_files"] == []
    assert rec["drifted_fields"] == []
    decisions = rec["operator_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["kind"] == "approve"
    assert decisions[0]["cleared"]["added_files"] == 1


def test_approve_is_idempotent_on_clean_manifest() -> None:
    manifest = {"reconciliation": {}}
    result = approve_changes(manifest)
    assert result["approved_count"] == 0
    # A clean manifest still records the operator decision — the no-op
    # is itself an operator action and we want the trail.
    assert len(manifest["reconciliation"]["operator_decisions"]) == 1


# ── promote_to_authored ─────────────────────────────────────────────

def test_promote_flips_observational_entries_to_bot_authored() -> None:
    manifest = {
        "provenance": {
            "field_origins": {
                "name":           {"source": "bot_authored"},
                "scheduled_actions": {"source": "observational"},
                "files":          {"source": "observational"},
                "description":    {"source": "user_authored"},
            },
        },
    }
    result = promote_to_authored(manifest, by="ui:operator")
    assert sorted(result["promoted_fields"]) == ["files", "scheduled_actions"]
    origins = manifest["provenance"]["field_origins"]
    assert origins["scheduled_actions"]["source"] == "bot_authored"
    assert origins["files"]["source"] == "bot_authored"
    assert origins["scheduled_actions"]["promoted_by"] == "ui:operator"
    # Untouched fields are not re-stamped.
    assert origins["name"] == {"source": "bot_authored"}
    assert origins["description"]["source"] == "user_authored"


def test_promote_is_noop_when_nothing_observational() -> None:
    manifest = {"provenance": {"field_origins": {"name": {"source": "bot_authored"}}}}
    result = promote_to_authored(manifest)
    assert result["promoted_fields"] == []


# ── flag_for_operator ─────────────────────────────────────────────

def test_flag_appends_to_coherence_flags() -> None:
    manifest: dict = {}
    result = flag_for_operator(manifest, description="cron silent since Tuesday")
    assert "flag_id" in result
    flags = manifest["coherence"]["flags"]
    assert len(flags) == 1
    assert flags[0]["description"] == "cron silent since Tuesday"
    assert flags[0]["status"] == "open"
    assert flags[0]["id"] == result["flag_id"]


def test_flag_rejects_empty_description() -> None:
    manifest: dict = {}
    with pytest.raises(ValueError):
        flag_for_operator(manifest, description="   ")


# ── mute_finding / unmute_finding ─────────────────────────────────

def test_mute_appends_signature_with_rationale() -> None:
    manifest: dict = {}
    result = mute_finding(manifest, signature="sig-abc", rationale="false positive")
    assert result["accepted_count"] == 1
    accepted = manifest["coherence"]["coherence_accepted"]
    assert accepted[0]["signature"] == "sig-abc"
    assert accepted[0]["rationale"] == "false positive"
    assert accepted[0]["accepted_by"] == "ui:operator"


def test_mute_is_idempotent_on_same_signature() -> None:
    manifest: dict = {}
    mute_finding(manifest, signature="sig-x", rationale="first")
    result = mute_finding(manifest, signature="sig-x", rationale="refined")
    assert result["accepted_count"] == 1
    assert manifest["coherence"]["coherence_accepted"][0]["rationale"] == "refined"


def test_unmute_removes_signature() -> None:
    manifest: dict = {}
    mute_finding(manifest, signature="sig-y")
    result = unmute_finding(manifest, signature="sig-y")
    assert result["removed"] == 1
    assert result["accepted_count"] == 0


def test_unmute_unknown_signature_is_noop() -> None:
    manifest: dict = {}
    result = unmute_finding(manifest, signature="sig-z")
    assert result["removed"] == 0
    assert result["accepted_count"] == 0


# ── snooze_finding ────────────────────────────────────────────────

def test_snooze_stamps_until_on_matching_finding() -> None:
    manifest = {
        "coherence": {
            "findings": [
                {"id": "C-A1", "signature": "sig-1", "severity": "minor"},
            ],
        },
    }
    result = snooze_finding(
        manifest, signature="sig-1", until_iso="2099-01-01T00:00:00Z",
    )
    assert result["snoozed_signature"] == "sig-1"
    f = manifest["coherence"]["findings"][0]
    assert f["snooze"]["until"] == "2099-01-01T00:00:00Z"
    assert f["snooze"]["by"] == "ui:operator"


def test_snooze_unknown_signature_raises() -> None:
    manifest = {"coherence": {"findings": []}}
    with pytest.raises(KeyError):
        snooze_finding(manifest, signature="nope", until_iso="2099-01-01T00:00:00Z")


# ── coherence_summary ─────────────────────────────────────────────

def test_summary_clean_manifest_is_ok() -> None:
    s = coherence_summary({})
    assert s["coherence_status"] == "ok"
    assert s["coherence_findings_count"] == 0
    assert s["reconciliation_status"] == "ok"
    assert s["reconciliation_is_orphan"] is False
    assert s["coherence_override_key"] == ""


def test_summary_critical_finding_is_incoherent() -> None:
    manifest = {
        "coherence": {
            "findings": [
                {"id": "C-A1", "signature": "s1", "severity": "critical"},
                {"id": "C-A2", "signature": "s2", "severity": "minor"},
            ],
        },
    }
    s = coherence_summary(manifest)
    assert s["coherence_status"] == "incoherent"
    assert s["coherence_critical_count"] == 1
    assert s["coherence_findings_count"] == 2
    # Override key is a 16-char hex digest.
    assert len(s["coherence_override_key"]) == 16


def test_summary_only_warnings() -> None:
    manifest = {
        "coherence": {
            "findings": [
                {"id": "C-A3", "signature": "s3", "severity": "minor"},
            ],
        },
    }
    assert coherence_summary(manifest)["coherence_status"] == "warnings"


def test_summary_muted_signatures_drop_out() -> None:
    manifest = {
        "coherence": {
            "findings": [
                {"id": "C-A1", "signature": "s-crit", "severity": "critical"},
            ],
            "coherence_accepted": [{"signature": "s-crit", "rationale": "fine"}],
        },
    }
    s = coherence_summary(manifest)
    assert s["coherence_status"] == "ok"
    assert s["coherence_findings_count"] == 0


def test_summary_snoozed_future_drops_out() -> None:
    manifest = {
        "coherence": {
            "findings": [
                {
                    "id": "C-A1", "signature": "s-snz", "severity": "critical",
                    "snooze": {"until": "2099-01-01T00:00:00Z"},
                },
            ],
        },
    }
    s = coherence_summary(manifest)
    assert s["coherence_status"] == "ok"


def test_summary_snoozed_past_still_counts() -> None:
    manifest = {
        "coherence": {
            "findings": [
                {
                    "id": "C-A1", "signature": "s-old", "severity": "critical",
                    "snooze": {"until": "2000-01-01T00:00:00Z"},
                },
            ],
        },
    }
    assert coherence_summary(manifest)["coherence_status"] == "incoherent"


def test_summary_orphan_takes_precedence_over_drift() -> None:
    manifest = {
        "reconciliation": {
            "status": "orphan",
            "drifted_fields": [{"field": "x"}],
        },
    }
    s = coherence_summary(manifest)
    assert s["reconciliation_status"] == "orphan"
    assert s["reconciliation_is_orphan"] is True


def test_summary_drift_when_added_files_present() -> None:
    manifest = {
        "reconciliation": {
            "added_files": [{"path": "a.py"}, {"path": "b.py"}],
            "removed_files": [],
        },
    }
    s = coherence_summary(manifest)
    assert s["reconciliation_status"] == "drift"
    assert s["reconciliation_added_count"] == 2
    assert s["reconciliation_is_orphan"] is False
