"""Tier-2 structural assertions for scheduled-action contracts (schema v13).

Six new assertions added in Workstream A of the audit-extensions sprint
(docs/spec-audit-extensions-2026-05-17.md §3.3). Each assertion gets a
passing-state and failing-state test; the file ends with the
protein-reminder regression fixture (spec §1.1) which exercises the
end-to-end scanner extraction → audit catch.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_structural import (  # noqa: E402
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    check_cron_labels_loaded,
    check_heartbeat_anchors,
    check_scheduled_action_anchors,
    check_scheduled_action_evidence_paths,
    check_scheduled_action_inputs,
    check_section_drift,
    _anchor_present,
    _extract_section,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _heartbeat_with_protein_section() -> str:
    """The fixture's gold-standard heartbeat — used by several tests."""
    return (
        "# Heartbeat\n"
        "\n"
        "Standing routines for admin_bot.\n"
        "\n"
        "## Daily routines\n"
        "\n"
        "Every evening at 6 PM, read `journal/protein.md` and post a tally\n"
        "to the user's messaging channel.\n"
        "\n"
        "## Weekly review\n"
        "\n"
        "Each Sunday at 9 AM, summarize the week's protein intake.\n"
    )


# ── check_scheduled_action_evidence_paths ────────────────────────────────────


def test_evidence_path_passes_when_present(tmp_path: Path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("## Daily routines\n6 PM tally\n")
    manifest = {
        "scheduled_actions": [
            {"id": "protein-6pm-tally",
             "trigger": {"kind": "heartbeat",
                         "evidence_path": "HEARTBEAT.md",
                         "evidence_locator": "Daily routines"}}
        ]
    }
    assert check_scheduled_action_evidence_paths(manifest, {"workspace": tmp_path}) == []


def test_evidence_path_missing_is_critical(tmp_path: Path) -> None:
    manifest = {
        "scheduled_actions": [
            {"id": "protein-6pm-tally",
             "trigger": {"kind": "heartbeat",
                         "evidence_path": "HEARTBEAT.md",
                         "evidence_locator": "Daily routines"}}
        ]
    }
    findings = check_scheduled_action_evidence_paths(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_CRITICAL
    assert findings[0].assertion_id == "scheduled_action_evidence_path"
    assert findings[0].evidence["path"] == "HEARTBEAT.md"


# ── check_scheduled_action_anchors ───────────────────────────────────────────


def test_anchor_passes_when_locator_resolves(tmp_path: Path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text(_heartbeat_with_protein_section())
    manifest = {
        "scheduled_actions": [
            {"id": "protein-6pm-tally",
             "trigger": {"kind": "heartbeat",
                         "evidence_path": "HEARTBEAT.md",
                         "evidence_locator": "Daily routines"}}
        ]
    }
    assert check_scheduled_action_anchors(manifest, {"workspace": tmp_path}) == []


def test_anchor_missing_is_critical(tmp_path: Path) -> None:
    # Heartbeat is present but has been clobbered — the anchor's gone.
    (tmp_path / "HEARTBEAT.md").write_text("Short clobbered file with no sections.\n")
    manifest = {
        "scheduled_actions": [
            {"id": "protein-6pm-tally",
             "trigger": {"kind": "heartbeat",
                         "evidence_path": "HEARTBEAT.md",
                         "evidence_locator": "Daily routines"}}
        ]
    }
    findings = check_scheduled_action_anchors(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_CRITICAL
    assert findings[0].assertion_id == "scheduled_action_anchor"
    assert "Daily routines" in findings[0].evidence["locator"]


# ── check_scheduled_action_inputs ────────────────────────────────────────────


def test_input_passes_when_file_present(tmp_path: Path) -> None:
    (tmp_path / "journal").mkdir()
    (tmp_path / "journal/protein.md").write_text("# 2026-05-01\n- 90g\n")
    manifest = {
        "scheduled_actions": [
            {"id": "protein-6pm-tally",
             "trigger": {"kind": "heartbeat", "evidence_path": "HEARTBEAT.md", "evidence_locator": ""},
             "inputs": [{"path": "journal/protein.md", "kind": "data_file"}]}
        ]
    }
    assert check_scheduled_action_inputs(manifest, {"workspace": tmp_path}) == []


def test_input_missing_is_major(tmp_path: Path) -> None:
    manifest = {
        "scheduled_actions": [
            {"id": "protein-6pm-tally",
             "trigger": {"kind": "heartbeat", "evidence_path": "HEARTBEAT.md", "evidence_locator": ""},
             "inputs": [{"path": "journal/missing.md", "kind": "data_file"}]}
        ]
    }
    findings = check_scheduled_action_inputs(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR
    assert findings[0].assertion_id == "scheduled_action_input_missing"


def test_input_external_kind_skipped(tmp_path: Path) -> None:
    """`kind=external` inputs aren't FS-resolved — nothing to check."""
    manifest = {
        "scheduled_actions": [
            {"id": "x",
             "trigger": {"kind": "heartbeat", "evidence_path": "HEARTBEAT.md", "evidence_locator": ""},
             "inputs": [{"path": "https://api.example.com", "kind": "external"}]}
        ]
    }
    assert check_scheduled_action_inputs(manifest, {"workspace": tmp_path}) == []


# ── check_heartbeat_anchors ──────────────────────────────────────────────────


def test_heartbeat_anchors_pass_when_present(tmp_path: Path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text(_heartbeat_with_protein_section())
    manifest = {
        "heartbeat_evidence": {
            "file_path": "HEARTBEAT.md",
            "section_anchors": ["Daily routines", "Weekly review"],
        }
    }
    assert check_heartbeat_anchors(manifest, {"workspace": tmp_path}) == []


def test_heartbeat_anchors_missing_is_critical(tmp_path: Path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("# Heartbeat\n\nClobbered.\n")
    manifest = {
        "heartbeat_evidence": {
            "file_path": "HEARTBEAT.md",
            "section_anchors": ["Daily routines"],
        }
    }
    findings = check_heartbeat_anchors(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_CRITICAL
    assert findings[0].assertion_id == "heartbeat_anchors_present"


def test_heartbeat_file_missing_is_critical(tmp_path: Path) -> None:
    """Whole heartbeat file gone → critical (not just one anchor)."""
    manifest = {
        "heartbeat_evidence": {
            "file_path": "HEARTBEAT.md",
            "section_anchors": ["Daily routines"],
        }
    }
    findings = check_heartbeat_anchors(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_CRITICAL


# ── check_cron_labels_loaded ─────────────────────────────────────────────────


def test_cron_label_passes_when_loaded(tmp_path: Path) -> None:
    manifest = {"cron_evidence": {"labels": ["com.example.morning-briefing"]}}
    ctx = {
        "workspace": tmp_path,
        "launchctl_labels": ["com.example.morning-briefing", "com.other.thing"],
        "crontab_lines": [],
    }
    assert check_cron_labels_loaded(manifest, ctx) == []


def test_cron_label_missing_is_major(tmp_path: Path) -> None:
    manifest = {"cron_evidence": {"labels": ["com.example.morning-briefing"]}}
    ctx = {
        "workspace": tmp_path,
        "launchctl_labels": ["com.other.thing"],
        "crontab_lines": [],
    }
    findings = check_cron_labels_loaded(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR
    assert findings[0].assertion_id == "cron_labels_loaded"


# ── check_section_drift ──────────────────────────────────────────────────────


def test_section_drift_passes_when_sha_matches(tmp_path: Path) -> None:
    heartbeat = _heartbeat_with_protein_section()
    (tmp_path / "HEARTBEAT.md").write_text(heartbeat)
    section_text = _extract_section(heartbeat, "Daily routines")
    assert section_text is not None
    expected_sha = _sha(section_text)
    manifest = {
        "scheduled_actions": [
            {"id": "protein-6pm-tally",
             "trigger": {"kind": "heartbeat",
                         "evidence_path": "HEARTBEAT.md",
                         "evidence_locator": "Daily routines",
                         "section_sha256": expected_sha}}
        ]
    }
    assert check_section_drift(manifest, {"workspace": tmp_path}) == []


def test_section_drift_is_minor_when_sha_changes(tmp_path: Path) -> None:
    """A rewording of the section body emits a `minor` drift finding."""
    heartbeat = _heartbeat_with_protein_section()
    (tmp_path / "HEARTBEAT.md").write_text(heartbeat)
    # Capture sha BEFORE editing, then mutate
    section_text = _extract_section(heartbeat, "Daily routines")
    assert section_text is not None
    original_sha = _sha(section_text)
    mutated = heartbeat.replace(
        "Every evening at 6 PM, read `journal/protein.md` and post a tally",
        "Every evening at 7 PM, summarize the day's protein and post it",
    )
    (tmp_path / "HEARTBEAT.md").write_text(mutated)
    manifest = {
        "scheduled_actions": [
            {"id": "protein-6pm-tally",
             "trigger": {"kind": "heartbeat",
                         "evidence_path": "HEARTBEAT.md",
                         "evidence_locator": "Daily routines",
                         "section_sha256": original_sha}}
        ]
    }
    findings = check_section_drift(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MINOR
    assert findings[0].assertion_id == "scheduled_action_section_drift"


# ── Section / anchor helpers ─────────────────────────────────────────────────


def test_anchor_present_is_case_insensitive() -> None:
    text = "## Daily Routines\n\nDo things.\n"
    assert _anchor_present(text, "daily routines")
    assert _anchor_present(text, "DAILY routines")


def test_extract_section_returns_section_body() -> None:
    text = (
        "# H1\n"
        "preamble\n"
        "## Section A\n"
        "body A line 1\n"
        "body A line 2\n"
        "## Section B\n"
        "body B\n"
    )
    section = _extract_section(text, "Section A")
    assert section is not None
    assert "body A line 1" in section
    assert "body B" not in section


def test_extract_section_returns_none_for_missing_heading() -> None:
    text = "## Section A\nbody\n"
    assert _extract_section(text, "Nonexistent") is None


# ── Protein-reminder regression fixture (spec §1.1) ──────────────────────────
#
# This is the gold-standard end-to-end test for Workstream A. We
# reproduce the April-2026 protein-reminder failure by:
#
#   1. Setting up a synthetic bot workspace with HEARTBEAT.md describing
#      a 6 PM protein-tally behavior + a journal/protein.md data file.
#   2. Building a manifest that captures the scheduled_action contract
#      the scanner WOULD produce after Workstream A lands.
#   3. Showing the pre-fix state (a manifest WITHOUT scheduled_actions)
#      catches nothing — even with the heartbeat clobbered.
#   4. Showing the post-fix state (manifest WITH scheduled_actions)
#      passes when the heartbeat is intact, then emits exactly one
#      `critical` finding citing the missing anchor when the heartbeat
#      is clobbered.


def _build_post_fix_manifest(workspace: Path) -> dict:
    """The manifest shape the scanner produces after Workstream A.

    Captures scheduled_actions[] with evidence_locator + section sha,
    heartbeat_evidence with the section anchor, and the journal input.
    """
    heartbeat_text = (workspace / "HEARTBEAT.md").read_text()
    section_text = _extract_section(heartbeat_text, "Daily routines")
    assert section_text is not None, "fixture heartbeat must have a Daily routines section"
    section_sha = _sha(section_text)
    return {
        "id": "protein-reminder",
        "name": "Protein Reminder",
        "scheduled_actions": [
            {
                "id": "protein-6pm-tally",
                "trigger": {
                    "kind": "heartbeat",
                    "schedule": "18:00 daily",
                    "evidence_path": "HEARTBEAT.md",
                    "evidence_locator": "Daily routines",
                    "section_sha256": section_sha,
                },
                "inputs": [
                    {"path": "journal/protein.md", "kind": "data_file"}
                ],
                "outputs": [{"kind": "messaging_channel", "channel": "configured"}],
                "summary": "Reads protein journal entries for the day and posts a tally to the user's messaging channel.",
            }
        ],
        "heartbeat_evidence": {
            "file_path": "HEARTBEAT.md",
            "section_anchors": ["Daily routines"],
        },
    }


def _setup_protein_workspace(tmp_path: Path) -> Path:
    """Synthetic admin_bot-shaped workspace for the regression test."""
    (tmp_path / "HEARTBEAT.md").write_text(_heartbeat_with_protein_section())
    (tmp_path / "journal").mkdir()
    (tmp_path / "journal/protein.md").write_text(
        "# Protein journal\n## 2026-05-16\n- breakfast: 30g\n- lunch: 40g\n"
    )
    return tmp_path


def test_protein_reminder_pre_fix_misses_clobber(tmp_path: Path) -> None:
    """The current-state Health Tracking manifest catches nothing.

    Before Workstream A, the scanner grouped journal/protein.md into a
    Health Tracking app whose manifest had NO scheduled_actions[]. So
    when the heartbeat got clobbered, no audit assertion fired — the
    failure was silent. This test pins that pre-fix behavior so the
    regression case is documented in code.
    """
    workspace = _setup_protein_workspace(tmp_path)
    pre_fix_manifest = {
        "id": "health-tracking",
        "name": "Health Tracking",
        "files": [{"path": "journal/protein.md", "layer": "data"}],
        # NOTE: no scheduled_actions[], no heartbeat_evidence — the gap.
    }
    # Even after a heartbeat clobber, no Workstream-A assertion fires.
    (workspace / "HEARTBEAT.md").write_text("clobbered\n" * 5)
    ctx = {"workspace": workspace, "launchctl_labels": [], "crontab_lines": []}
    assert check_scheduled_action_evidence_paths(pre_fix_manifest, ctx) == []
    assert check_scheduled_action_anchors(pre_fix_manifest, ctx) == []
    assert check_heartbeat_anchors(pre_fix_manifest, ctx) == []


def test_protein_reminder_post_fix_passes_when_heartbeat_intact(tmp_path: Path) -> None:
    """After Workstream A, the manifest has scheduled_actions[] + the
    heartbeat is intact → zero findings. Establishes the no-noise
    baseline before the clobber half of the test exercises the catch.
    """
    workspace = _setup_protein_workspace(tmp_path)
    manifest = _build_post_fix_manifest(workspace)
    ctx = {"workspace": workspace, "launchctl_labels": [], "crontab_lines": []}
    findings = (
        check_scheduled_action_evidence_paths(manifest, ctx)
        + check_scheduled_action_anchors(manifest, ctx)
        + check_scheduled_action_inputs(manifest, ctx)
        + check_heartbeat_anchors(manifest, ctx)
        + check_section_drift(manifest, ctx)
    )
    assert findings == [], f"unexpected findings: {[f.summary for f in findings]}"


def test_protein_reminder_post_fix_catches_heartbeat_clobber(tmp_path: Path) -> None:
    """THE CATCH: heartbeat gets clobbered (anchor gone) → exactly one
    `critical` finding citing the missing heartbeat anchor.

    This is the protein-reminder regression case from spec §1.1.
    Without Workstream A, the heartbeat clobber was silent. With it,
    the audit emits a finding within 6 hours of the next Tier-2 tick.
    """
    workspace = _setup_protein_workspace(tmp_path)
    manifest = _build_post_fix_manifest(workspace)

    # Now clobber the heartbeat — truncated past the Daily routines anchor
    (workspace / "HEARTBEAT.md").write_text("# Heartbeat\n\nClobbered.\n")

    ctx = {"workspace": workspace, "launchctl_labels": [], "crontab_lines": []}

    # heartbeat_anchors fires (the load-bearing assertion). The other
    # assertions are documented here too so the test pinpoints the
    # severity + assertion_id of the catch.
    hb_findings = check_heartbeat_anchors(manifest, ctx)
    assert len(hb_findings) == 1, (
        "expected exactly one critical heartbeat-anchor finding; got "
        f"{[(f.assertion_id, f.summary) for f in hb_findings]}"
    )
    assert hb_findings[0].severity == SEVERITY_CRITICAL
    assert hb_findings[0].assertion_id == "heartbeat_anchors_present"
    assert "Daily routines" in hb_findings[0].evidence["anchor"]
    # And the scheduled_action_anchor check also catches it (defense
    # in depth — the heartbeat_evidence and per-action anchors are
    # both verified).
    sa_findings = check_scheduled_action_anchors(manifest, ctx)
    assert any(f.assertion_id == "scheduled_action_anchor" for f in sa_findings)
