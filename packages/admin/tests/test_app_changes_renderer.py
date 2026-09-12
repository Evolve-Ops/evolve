"""evo app-changes renderer tests — PR 13."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.app_changes_renderer import (  # noqa: E402
    AppChangeSummary,
    render_app_not_found,
    render_approve_result,
    render_coherence_result,
    render_flag_result,
    render_list_changes,
    render_not_for_you,
    render_promote_result,
    render_scan_started,
    render_show_changes,
)


# ── render_list_changes ─────────────────────────────────────────────

def test_empty_list_explains_no_apps() -> None:
    out = render_list_changes([])
    assert "No applications" in out
    assert "evo install" in out


def test_all_quiet_apps_says_no_changes() -> None:
    summaries = [
        AppChangeSummary(app_id="a"),
        AppChangeSummary(app_id="b"),
    ]
    out = render_list_changes(summaries)
    assert "quiet" in out.lower() or "no changes" in out.lower()


def test_list_shows_quiet_failures() -> None:
    summaries = [
        AppChangeSummary(app_id="journal", quiet_failures=1),
    ]
    out = render_list_changes(summaries)
    assert "journal" in out
    assert "quiet failure" in out


def test_list_shows_authored_drift() -> None:
    summaries = [
        AppChangeSummary(app_id="briefing", authored_drift=2),
    ]
    out = render_list_changes(summaries)
    assert "briefing" in out
    assert "drifted" in out


def test_list_shows_file_counts() -> None:
    summaries = [
        AppChangeSummary(app_id="x", additions=3, removals=1),
    ]
    out = render_list_changes(summaries)
    assert "+3 files" in out
    assert "-1 file" in out


def test_list_overflow_shows_more_line() -> None:
    summaries = [
        AppChangeSummary(app_id=f"a{i}", additions=1)
        for i in range(12)
    ]
    out = render_list_changes(summaries)
    assert "more" in out


def test_list_only_lists_apps_with_changes() -> None:
    summaries = [
        AppChangeSummary(app_id="active", quiet_failures=1),
        AppChangeSummary(app_id="quiet1"),
        AppChangeSummary(app_id="quiet2"),
    ]
    out = render_list_changes(summaries)
    assert "active" in out
    # Quiet apps don't appear in the bullet list.
    assert "quiet1" not in out


# ── render_show_changes ───────────────────────────────────────────

def test_show_quiet_app_says_no_changes() -> None:
    out = render_show_changes(AppChangeSummary(app_id="journal"))
    assert "journal" in out
    assert "no changes" in out


def test_show_quiet_failures_first() -> None:
    out = render_show_changes(
        AppChangeSummary(app_id="x", quiet_failures=1),
        quiet_failures=[{"summary": "6pm cron missing"}],
    )
    assert "quiet failure" in out
    assert "6pm cron missing" in out


def test_show_authored_drift() -> None:
    out = render_show_changes(
        AppChangeSummary(app_id="x", drifted=1, authored_drift=1),
        drifted=[{
            "field": "description", "before": "old", "after": "new",
            "authored": True,
        }],
    )
    assert "Authored fields" in out
    assert "description" in out


def test_show_additions_with_layer() -> None:
    out = render_show_changes(
        AppChangeSummary(app_id="x", additions=1),
        additions=[{"path": "scripts/new.py", "layer": "code"}],
    )
    assert "scripts/new.py" in out
    assert "code" in out


def test_show_removals() -> None:
    out = render_show_changes(
        AppChangeSummary(app_id="x", removals=1),
        removals=[{"path": "scripts/old.py"}],
    )
    assert "scripts/old.py" in out


def test_show_includes_reply_options() -> None:
    out = render_show_changes(
        AppChangeSummary(app_id="x", additions=1),
        additions=[{"path": "a.py", "layer": "code"}],
    )
    assert "approve" in out
    assert "promote" in out
    assert "flag" in out


def test_show_overflow_more_line() -> None:
    out = render_show_changes(
        AppChangeSummary(app_id="x", additions=5),
        additions=[{"path": f"a{i}.py", "layer": "code"} for i in range(5)],
    )
    assert "2 more" in out


# ── render_approve_result ─────────────────────────────────────────

def test_approve_nothing_explains() -> None:
    out = render_approve_result(app_id="x", approved_count=0)
    assert "nothing to approve" in out


def test_approve_one_change() -> None:
    out = render_approve_result(app_id="x", approved_count=1)
    assert "approved 1 change" in out
    assert "changes" not in out   # singular


def test_approve_multiple_uses_plural() -> None:
    out = render_approve_result(app_id="x", approved_count=5)
    assert "approved 5 changes" in out


# ── render_promote_result ────────────────────────────────────────

def test_promote_empty() -> None:
    out = render_promote_result(app_id="x", promoted_fields=[])
    assert "nothing to promote" in out


def test_promote_some_fields() -> None:
    out = render_promote_result(
        app_id="x", promoted_fields=["description", "files"],
    )
    assert "2 fields" in out
    assert "drift" in out.lower()


def test_promote_single_field_singular() -> None:
    out = render_promote_result(
        app_id="x", promoted_fields=["description"],
    )
    # Singular count text uses "1 field" (no s).
    assert "1 field " in out or "1 field." in out


# ── render_flag_result ───────────────────────────────────────────

def test_flag_simple() -> None:
    out = render_flag_result(
        app_id="x", description="6pm cron missing",
    )
    assert "6pm cron missing" in out
    assert "operator" in out


def test_flag_with_proposal_id() -> None:
    out = render_flag_result(
        app_id="x", description="bad", proposal_id="prop-123",
    )
    assert "prop-123" in out


# ── render_coherence_result ──────────────────────────────────────

def test_coherence_rate_limited() -> None:
    out = render_coherence_result(app_id="x", rate_limited=True)
    assert "already ran" in out


def test_coherence_pass_a_ok() -> None:
    out = render_coherence_result(app_id="x", pass_a_status="ok")
    assert "Pass A" in out
    assert "pass" in out


def test_coherence_pass_a_with_findings() -> None:
    out = render_coherence_result(
        app_id="x", pass_a_status="warnings",
        pass_a_findings=[{"message": "missing volatile_path"}],
    )
    assert "1 finding" in out
    assert "missing volatile_path" in out


def test_coherence_c3_feasible() -> None:
    out = render_coherence_result(
        app_id="x", capability_severity="feasible",
        capability_rationale="hangs together",
    )
    assert "feasible" in out
    assert "hangs together" in out


def test_coherence_c3_incoherent() -> None:
    out = render_coherence_result(
        app_id="x", capability_severity="incoherent",
        capability_rationale="contradicts itself",
    )
    assert "incoherent" in out


def test_coherence_both_passes() -> None:
    out = render_coherence_result(
        app_id="x", pass_a_status="ok",
        capability_severity="feasible", capability_rationale="ok",
    )
    assert "Pass A" in out
    assert "Pass C3" in out


def test_coherence_no_results_shown() -> None:
    out = render_coherence_result(app_id="x")
    assert "no result" in out.lower() or "neither pass ran" in out


# ── render_scan_started ──────────────────────────────────────────

def test_scan_message() -> None:
    out = render_scan_started(bot_id="personal-bot")
    assert "personal-bot" in out
    assert "Scan" in out


# ── Auth / not-found ────────────────────────────────────────────

def test_not_for_you_offers_flag() -> None:
    out = render_not_for_you()
    assert "flag" in out
    assert "operator" in out


def test_app_not_found_lists_apps_hint() -> None:
    out = render_app_not_found("missing")
    assert "missing" in out
    assert "evo apps" in out
