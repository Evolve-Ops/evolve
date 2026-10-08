"""Tests for v23 cron_evidence label population in the scanner.

Companion to the v23 producer-surface audit check in
packages/analyzer/app_audit_structural.py. Before this PR the scanner
wrote ``cron_evidence: {}`` unconditionally; check_cron_labels_loaded
had nothing to verify, and the v23 producer-surface check would never
see cron_evidence as a present surface for launchd-driven apps. This
file pins the new ``_collect_cron_evidence_labels`` helper and its
contract — same attribution match as ``scheduled_actions[]``, labels
de-duplicated, order-preserving.

Pure functions over fixtures. No filesystem, no LLM, no subprocess.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_app(
    app_id: str, evidence: list[str] | None = None,
) -> _scanner.DetectedApplication:
    return _scanner.DetectedApplication(
        id=app_id,
        name=app_id.replace("-", " ").title(),
        confidence=0.9,
        evidence_files=evidence if evidence is not None else [f"scripts/{app_id}.py"],
        evidence_summary=f"Discovered {app_id}.",
        suggested_goals=[],
        suggested_tests=[],
        suggested_privacy=[],
    )


def _entry(label: str, program_args: list[str] | None = None) -> dict:
    return {
        "label": label,
        "plist_path": f"/Users/x/Library/LaunchAgents/{label}.plist",
        "program_args": program_args or [],
        "start_interval": 3600,
        "start_calendar_interval": None,
        "run_at_load": False,
        "raw": {},
    }


# ── _collect_cron_evidence_labels ───────────────────────────────────────────


def test_empty_launchd_entries_returns_empty_list():
    app = _make_app("task-manager")
    assert _scanner._collect_cron_evidence_labels([], app, "personal-bot") == []


def test_none_launchd_entries_returns_empty_list():
    """Pre-deploy bots may have inventory.launchd_entries = None."""
    app = _make_app("task-manager")
    assert _scanner._collect_cron_evidence_labels(None, app, "personal-bot") == []


def test_collects_label_from_namespace_match():
    """com.{bot}.{app}.* label is the canonical forge install convention."""
    app = _make_app("task-manager")
    entries = [_entry("com.personal-bot.task-manager.check")]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels == ["com.personal-bot.task-manager.check"]


def test_collects_label_from_program_args_match():
    """Hand-installed launchd jobs reference app evidence files in their
    ProgramArguments. Same attribution path → same Label captured."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    entries = [_entry(
        "org.someteam.scheduled.something",
        program_args=["/usr/bin/python3", "/Users/personal-bot/.openclaw/workspace/scripts/tasks.py"],
    )]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels == ["org.someteam.scheduled.something"]


def test_skips_unattributed_entries():
    """A launchd job belonging to a different app must not leak into
    this app's cron_evidence."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    entries = [
        _entry("com.personal-bot.task-manager.check"),
        _entry("com.personal-bot.morning-briefing.send"),   # different app
        _entry("com.unrelated.thing"),
    ]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels == ["com.personal-bot.task-manager.check"]


def test_dedupes_repeated_labels():
    """Same label appearing in two inventory entries (e.g. label match
    AND program-args match for the same plist) must dedupe."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    entries = [
        _entry("com.personal-bot.task-manager.check",
               program_args=["python3", "scripts/tasks.py"]),
        _entry("com.personal-bot.task-manager.check"),  # same label again
    ]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels == ["com.personal-bot.task-manager.check"]


def test_preserves_match_order():
    """Multiple distinct launchd jobs for one app land in inventory order."""
    app = _make_app("task-manager")
    entries = [
        _entry("com.personal-bot.task-manager.check"),
        _entry("com.personal-bot.task-manager.morning"),
        _entry("com.personal-bot.task-manager.evening"),
    ]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels == [
        "com.personal-bot.task-manager.check",
        "com.personal-bot.task-manager.morning",
        "com.personal-bot.task-manager.evening",
    ]


def test_skips_entry_with_empty_label():
    """Inventory occasionally surfaces a plist with no Label (malformed).
    Attribution may still match by program-args, but with no label there
    is nothing to write into cron_evidence."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    entries = [_entry("",
                      program_args=["/usr/bin/python3", "/path/scripts/tasks.py"])]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels == []


def test_skips_entry_with_whitespace_only_label():
    """Defensive: whitespace-only label is treated as missing."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    entries = [_entry("   ",
                      program_args=["/path/scripts/tasks.py"])]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels == []


def test_skips_non_dict_entries_gracefully():
    """A truncated/garbled inventory shouldn't crash the scanner."""
    app = _make_app("task-manager")
    entries = [
        "not-a-dict",  # type: ignore[list-item]
        None,          # type: ignore[list-item]
        _entry("com.personal-bot.task-manager.check"),
    ]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels == ["com.personal-bot.task-manager.check"]


def test_strips_label_whitespace():
    """Labels with surrounding whitespace are normalized."""
    app = _make_app("task-manager")
    entries = [_entry("  com.personal-bot.task-manager.check  ")]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels == ["com.personal-bot.task-manager.check"]


# ── Integration with the v23 producer-surface audit ─────────────────────────
#
# These tests cross packages — they import the audit check and confirm
# that a Layer-B-populated cron_evidence makes Layer A's check pass.


def test_layer_b_output_satisfies_layer_a_check():
    """End-to-end: cron_evidence produced by Layer B is recognized by
    Layer A's check_app_has_producer_surface as a valid surface."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analyzer"))
    from app_audit_structural import check_app_has_producer_surface

    app = _make_app("task-manager")
    entries = [_entry("com.personal-bot.task-manager.check")]
    labels = _scanner._collect_cron_evidence_labels(entries, app, "personal-bot")
    assert labels  # sanity

    manifest = {
        "id": "task-manager",
        "name": "Task Manager",
        "scheduled_actions": [],
        "heartbeat_evidence": {},
        "cron_evidence": {"labels": labels},
        "crons": [],
        "event_triggers": [],
        "interface_contract": {},
        "usage": {},
    }
    findings = check_app_has_producer_surface(manifest, {})
    assert findings == []
