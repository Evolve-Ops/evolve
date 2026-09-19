"""Tests for the scanner's usage.model inference.

The bot-side Tier-2 discoverability verifier in
packages/analyzer/app_audit_structural.py treats apps with
usage.model in {"user-initiated", "ambient", ""} as user-routed and
requires hint_words + example_triggers + interface_contract.cli on
each. Apps with model in {"scheduled", "event-driven"} skip those
checks.

When usage.model is unset, every scheduled app trips no_cli +
no_example_triggers findings — confirmed pod-wide 2026-06-07 with 114
firing alerts of this exact shape.

This test file pins the inference logic so the calibration sticks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.scanner import _infer_usage_model  # noqa: E402


# ── Heartbeat-driven → scheduled ───────────────────────────────────────────

def test_heartbeat_evidence_present_returns_scheduled() -> None:
    """Production validation: personal-bot-daily-logging has 10 active heartbeat
    scheduled_actions + heartbeat_evidence populated. Must classify as
    scheduled or the verifier fires no_cli + no_triggers."""
    model = _infer_usage_model(
        scheduled_actions=[],
        heartbeat_evidence={
            "file_path": "HEARTBEAT.md",
            "section_anchors": ["Checks"],
        },
    )
    assert model == "scheduled"


def test_heartbeat_trigger_kind_returns_scheduled() -> None:
    """When heartbeat_evidence isn't populated but actions have
    trigger.kind == heartbeat (canonical OC heartbeat shape), still
    scheduled."""
    model = _infer_usage_model(
        scheduled_actions=[
            {"id": "morning", "trigger": {"kind": "heartbeat",
                                            "schedule": "daily"}},
        ],
        heartbeat_evidence={},
    )
    assert model == "scheduled"


# ── Cron-driven → scheduled ────────────────────────────────────────────────

def test_cron_trigger_returns_scheduled() -> None:
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": {"kind": "cron"}}],
        heartbeat_evidence={},
    )
    assert model == "scheduled"


def test_oc_native_crons_list_returns_scheduled() -> None:
    """Production calibration: security-cve-scan uses crons[] (OC-native
    cron format) without scheduled_actions[]. Must classify as scheduled."""
    model = _infer_usage_model(
        crons=[{
            "name": "security:cve-scan-discover",
            "schedule": "cron 0 9 * * * @ America/Los_Angeles",
            "task": "Run the CVE candidate discovery procedure.",
        }],
    )
    assert model == "scheduled"


def test_cron_evidence_block_returns_scheduled() -> None:
    """When cron_evidence is populated (analogous to heartbeat_evidence),
    classify as scheduled even without scheduled_actions entries."""
    model = _infer_usage_model(
        cron_evidence={"crontab_line": "0 7 * * * scripts/morning.py"},
    )
    assert model == "scheduled"


def test_empty_crons_list_falls_through_to_user_initiated() -> None:
    """An empty crons[] doesn't claim scheduled."""
    model = _infer_usage_model(crons=[])
    assert model == "user-initiated"


def test_crons_with_no_schedule_or_task_skipped() -> None:
    """Defensive: malformed cron entries shouldn't claim scheduled."""
    model = _infer_usage_model(crons=[{"unrelated_field": "x"}])
    assert model == "user-initiated"


def test_launchd_trigger_returns_scheduled() -> None:
    """v16 install-artifact attribution uses 'launchd' as the kind."""
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": {"kind": "launchd"}}],
        heartbeat_evidence={},
    )
    assert model == "scheduled"


def test_systemd_trigger_returns_scheduled() -> None:
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": {"kind": "systemd"}}],
        heartbeat_evidence={},
    )
    assert model == "scheduled"


# ── Event-driven ──────────────────────────────────────────────────────────

def test_webhook_trigger_returns_event_driven() -> None:
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": {"kind": "webhook"}}],
        heartbeat_evidence={},
    )
    assert model == "event-driven"


def test_signal_trigger_returns_event_driven() -> None:
    """Apps that respond to signal-store events get event-driven."""
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": {"kind": "signal"}}],
        heartbeat_evidence={},
    )
    assert model == "event-driven"


# ── Fallback to user-initiated ────────────────────────────────────────────

def test_no_actions_no_evidence_returns_user_initiated() -> None:
    """Genuinely user-routed apps fall through to user-initiated; the
    verifier will then correctly check for CLI + triggers + hints."""
    model = _infer_usage_model(
        scheduled_actions=[],
        heartbeat_evidence={},
    )
    assert model == "user-initiated"


def test_unknown_trigger_kind_returns_user_initiated() -> None:
    """When the trigger kind isn't recognized, conservative default."""
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": {"kind": "made-up-thing"}}],
        heartbeat_evidence={},
    )
    assert model == "user-initiated"


# ── Mixed-trigger conservatism ─────────────────────────────────────────────

def test_mixed_heartbeat_and_webhook_returns_scheduled() -> None:
    """Conservatism order: scheduled > event-driven > user-initiated.
    An app with both heartbeat + webhook triggers gets 'scheduled' (the
    safer label — the bot won't bother trying to route to it)."""
    model = _infer_usage_model(
        scheduled_actions=[
            {"trigger": {"kind": "heartbeat"}},
            {"trigger": {"kind": "webhook"}},
        ],
        heartbeat_evidence={},
    )
    assert model == "scheduled"


# ── Defensive shape handling ──────────────────────────────────────────────

def test_malformed_action_entries_skipped() -> None:
    """Non-dict entries don't break the inferrer."""
    model = _infer_usage_model(
        scheduled_actions=["not a dict", None, {}],  # type: ignore[list-item]
        heartbeat_evidence={
            "file_path": "HEARTBEAT.md",
            "section_anchors": ["x"],
        },
    )
    assert model == "scheduled"


def test_action_with_no_trigger_field_skipped() -> None:
    """Action entries without trigger contribute nothing to inference."""
    model = _infer_usage_model(
        scheduled_actions=[{"id": "x", "summary": "does things"}],
        heartbeat_evidence={},
    )
    assert model == "user-initiated"


def test_action_with_non_dict_trigger_skipped() -> None:
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": "not a dict"}],  # type: ignore[dict-item]
        heartbeat_evidence={},
    )
    assert model == "user-initiated"


def test_trigger_kind_case_insensitive() -> None:
    """Case-insensitive on the kind to be tolerant of upstream shape drift."""
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": {"kind": "HEARTBEAT"}}],
        heartbeat_evidence={},
    )
    assert model == "scheduled"


# ── Library / on-demand detection ──────────────────────────────────────────

def test_app_must_call_phrase_returns_library() -> None:
    """Production calibration 2026-06-08: Biometric Integration's
    example triggers include 'App must call get_token() first,
    auto-refresh if needed'. Must classify as library, not user-routed,
    so C-A1 doesn't fire on the 'daily' mention in the description."""
    model = _infer_usage_model(
        description="OAuth 2.0 token lifecycle management for Whoop API",
        example_triggers=[
            "User: 'Get my recovery score' → App must call get_token() first",
            "System: Scheduled job to sync biometric data runs daily → Calls get_token() which auto-refreshes tokens",
        ],
    )
    assert model == "library"


def test_token_lifecycle_phrase_returns_library() -> None:
    model = _infer_usage_model(
        description="Manages the token lifecycle for the Whoop API on behalf of other apps.",
    )
    assert model == "library"


def test_available_on_demand_phrase_returns_library() -> None:
    model = _infer_usage_model(
        success_criteria={
            "observable_outcomes": [
                "Valid access_token always available on demand via get_token() without user intervention",
            ],
        },
    )
    assert model == "library"


def test_called_by_other_apps_returns_library() -> None:
    model = _infer_usage_model(
        identity={
            "scope_includes": ["Called by other apps to obtain Whoop API tokens"],
        },
    )
    assert model == "library"


def test_library_keyword_returns_library() -> None:
    """Explicit 'is a library' phrasing wins."""
    model = _infer_usage_model(
        description="This library for token management has no user-facing surface.",
    )
    assert model == "library"


# ── Conservatism: scheduled wins over library ──────────────────────────────

def test_scheduled_beats_library_when_both_signals_present() -> None:
    """An app with heartbeat AND library-style prose stays 'scheduled' —
    we don't downgrade a confirmed scheduled app to library based on
    ambiguous prose."""
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": {"kind": "heartbeat"}}],
        description="This library is called by other apps to do X.",
    )
    assert model == "scheduled"


def test_event_driven_beats_library_when_both_signals_present() -> None:
    model = _infer_usage_model(
        scheduled_actions=[{"trigger": {"kind": "webhook"}}],
        description="This library handles webhooks on behalf of other apps.",
    )
    assert model == "event-driven"


def test_random_phrase_does_not_match_library() -> None:
    """Conservative — only the documented phrases trigger library
    classification; generic descriptions don't."""
    model = _infer_usage_model(
        description="A general-purpose tool for managing journal entries.",
    )
    assert model == "user-initiated"


def test_partial_phrase_match_does_not_trigger() -> None:
    """'library' alone isn't enough; needs 'this library' or 'is a library'."""
    model = _infer_usage_model(
        description="Browse the library of available templates.",
    )
    assert model == "user-initiated"


def test_library_detection_handles_non_dict_inputs() -> None:
    """Defensive: scope_includes might be a string or other shape."""
    model = _infer_usage_model(
        identity={"scope_includes": "not a list"},  # malformed
        description="Plain description.",
    )
    assert model == "user-initiated"
