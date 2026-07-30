"""Bot-side session-surface tests — PR 12.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.session_surface_apps import (  # noqa: E402
    AUDIENCE_POD_ADMIN,
    AUDIENCE_PRIMARY_USER,
    AUDIENCE_TEAM_MEMBER,
    MAX_VISIBLE_FINDINGS,
    FindingForBlock,
    build_apps_block,
    can_dispatch_repair,
    escalation_dispatch_message,
    escalation_message_for_team_member,
)


# ── Empty case ──────────────────────────────────────────────────────────

def test_empty_findings_returns_empty_string() -> None:
    """Spec §10.9.6 rule 6: no eligible findings → block absent."""
    assert build_apps_block([]) == ""


def test_only_snoozed_findings_returns_empty() -> None:
    """Snoozed findings excluded (rule 5) — if everything's snoozed, no
    block."""
    findings = [
        FindingForBlock(app_id="x", severity="critical",
                         summary="bad", snoozed=True),
    ]
    assert build_apps_block(findings) == ""


# ── Single finding ──────────────────────────────────────────────────────

def test_single_critical_finding_renders() -> None:
    findings = [
        FindingForBlock(
            app_id="journal", severity="critical",
            summary="6 PM cron missing from crontab",
            repair_hint="Repair via `evo app-repair journal`.",
        ),
    ]
    block = build_apps_block(findings)
    assert "[Apps] 1 finding to be aware of." in block
    assert "CRITICAL:" in block
    assert "journal:" in block
    assert "6 PM cron missing" in block
    assert "evo app-repair journal" in block


def test_singular_vs_plural_header() -> None:
    one = build_apps_block([
        FindingForBlock(app_id="x", severity="major", summary="y"),
    ])
    assert "1 finding " in one

    two = build_apps_block([
        FindingForBlock(app_id="x", severity="major", summary="y"),
        FindingForBlock(app_id="z", severity="major", summary="w"),
    ])
    assert "2 findings " in two


# ── Severity grouping (CRITICAL first, then OTHER) ─────────────────────

def test_critical_grouped_above_other() -> None:
    findings = [
        FindingForBlock(app_id="z", severity="minor", summary="m"),
        FindingForBlock(app_id="a", severity="critical", summary="c"),
        FindingForBlock(app_id="b", severity="major", summary="j"),
    ]
    block = build_apps_block(findings)
    # CRITICAL appears before OTHER.
    assert block.index("CRITICAL") < block.index("OTHER")
    # The critical app appears before the major+minor ones.
    assert block.index("a: c") < block.index("b: j")
    assert block.index("a: c") < block.index("z: m")


def test_no_critical_only_other_section() -> None:
    findings = [
        FindingForBlock(app_id="x", severity="major", summary="y"),
    ]
    block = build_apps_block(findings)
    assert "OTHER" in block
    assert "CRITICAL:" not in block


def test_no_other_only_critical_section() -> None:
    findings = [
        FindingForBlock(app_id="x", severity="critical", summary="y"),
    ]
    block = build_apps_block(findings)
    assert "CRITICAL" in block
    assert "OTHER" not in block


# ── Overflow ────────────────────────────────────────────────────────────

def test_overflow_critical_shows_overflow_line() -> None:
    findings = [
        FindingForBlock(app_id=f"app{i}", severity="critical", summary=f"s{i}")
        for i in range(7)
    ]
    block = build_apps_block(findings, max_visible=3)
    # 4 overflow.
    assert "(4 more critical" in block


def test_overflow_other_section() -> None:
    findings = [
        FindingForBlock(app_id=f"app{i}", severity="major", summary=f"s{i}")
        for i in range(7)
    ]
    block = build_apps_block(findings, max_visible=3)
    assert "more" in block


# ── Snoozed exclusion ─────────────────────────────────────────────────

def test_snoozed_findings_excluded_from_count_and_lines() -> None:
    findings = [
        FindingForBlock(app_id="a", severity="critical", summary="s1"),
        FindingForBlock(app_id="b", severity="critical", summary="s2",
                         snoozed=True),
        FindingForBlock(app_id="c", severity="critical", summary="s3"),
    ]
    block = build_apps_block(findings)
    assert "1 finding" not in block   # 2 visible, not 1
    assert "2 findings" in block
    assert "a:" in block
    assert "c:" in block
    assert "b:" not in block          # snoozed; excluded


# ── Audience qualifier (team bots) ──────────────────────────────────

def test_audience_qualifier_changes_header() -> None:
    findings = [
        FindingForBlock(app_id="x", severity="critical", summary="s"),
    ]
    block = build_apps_block(findings, audience="the primary user")
    assert "visible to the primary user" in block


def test_no_audience_uses_default_header() -> None:
    findings = [
        FindingForBlock(app_id="x", severity="critical", summary="s"),
    ]
    block = build_apps_block(findings)
    assert "to be aware of" in block
    assert "visible to" not in block


# ── Constants ──────────────────────────────────────────────────────

def test_max_visible_default_is_five() -> None:
    """Spec §10.9.6 rule 4: cap at 5 visible findings."""
    assert MAX_VISIBLE_FINDINGS == 5


# ── Repair authority (spec §10.9.7) ─────────────────────────────────

def test_pod_admin_can_dispatch_repair() -> None:
    assert can_dispatch_repair(AUDIENCE_POD_ADMIN) is True


def test_primary_user_can_dispatch_repair() -> None:
    assert can_dispatch_repair(AUDIENCE_PRIMARY_USER) is True


def test_team_member_cannot_dispatch_repair() -> None:
    """Reads-open writes-escalate (spec §10.9.7)."""
    assert can_dispatch_repair(AUDIENCE_TEAM_MEMBER) is False


def test_unknown_audience_cannot_dispatch() -> None:
    assert can_dispatch_repair("random") is False


# ── Escalation messages ─────────────────────────────────────────

def test_escalation_message_for_team_member() -> None:
    msg = escalation_message_for_team_member(
        member_name="Family member", primary_user_name="Diana",
        finding_summary="cron broken",
    )
    assert "Diana" in msg


def test_escalation_dispatch_message_to_primary() -> None:
    msg = escalation_dispatch_message(
        member_name="Family member", finding_summary="cron broken",
    )
    assert "Family member" in msg
    assert "cron broken" in msg
    assert "yes" in msg.lower()   # call-to-action
