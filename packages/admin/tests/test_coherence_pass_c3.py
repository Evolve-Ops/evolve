"""Coherence Pass C3 tests — PR 16.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.5.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.coherence_pass_c3 import (  # noqa: E402
    C3_SYSTEM_PROMPT,
    CHARTER_FIELDS,
    DAILY_RUN_CAP,
    SEVERITY_FEASIBLE,
    SEVERITY_INCOHERENT,
    SEVERITY_UNCLEAR,
    CapabilityCheck,
    build_user_prompt,
    detect_charter_change,
    is_rate_limited,
    parse_llm_response,
    should_run_c3,
    write_capability_check,
)


# ── Prompt construction ──────────────────────────────────────────────

def test_system_prompt_has_three_severities() -> None:
    for sev in (SEVERITY_INCOHERENT, SEVERITY_FEASIBLE, SEVERITY_UNCLEAR):
        assert sev in C3_SYSTEM_PROMPT


def test_system_prompt_emphasizes_conservatism() -> None:
    """Conservative on 'incoherent' (spec §6.5)."""
    assert "conservative" in C3_SYSTEM_PROMPT.lower()


def test_build_user_prompt_includes_manifest() -> None:
    manifest = {"id": "myapp", "description": "does X"}
    out = build_user_prompt(manifest)
    assert "myapp" in out
    assert "does X" in out


def test_build_user_prompt_trims_noise_fields() -> None:
    """Files / volatile_paths / coherence / reconciliation / provenance
    are not useful for design-level judgement and get trimmed."""
    manifest = {
        "id": "x",
        "description": "describes y",
        "files": [{"path": "scripts/main.py", "sha256": "abc"}],
        "volatile_paths": [{"glob": "data/*.md"}],
        "coherence": {"findings": [{"check_id": "A-1"}]},
        "reconciliation": {"status": "ok"},
        "provenance": {"field_origins": {}},
        "observation_log": [{"ts": "t", "noun": "x"}],
    }
    out = build_user_prompt(manifest)
    assert "scripts/main.py" not in out
    assert "data/*.md" not in out
    assert "A-1" not in out
    assert "observation_log" not in out
    assert "describes y" in out   # preserved


# ── Rate limit ───────────────────────────────────────────────────────

def test_no_prior_check_is_not_rate_limited() -> None:
    assert is_rate_limited({}) is False
    assert is_rate_limited({"coherence": {}}) is False


def test_recent_check_is_rate_limited() -> None:
    now = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)
    manifest = {"coherence": {"last_capability_check": {
        "checked_at": (now - timedelta(hours=2)).isoformat(),
        "severity": "feasible", "rationale": "x",
    }}}
    assert is_rate_limited(manifest, now=now) is True


def test_24h_old_check_no_longer_rate_limited() -> None:
    now = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)
    manifest = {"coherence": {"last_capability_check": {
        "checked_at": (now - timedelta(hours=24, minutes=1)).isoformat(),
        "severity": "feasible", "rationale": "x",
    }}}
    assert is_rate_limited(manifest, now=now) is False


def test_unparseable_checked_at_treated_as_not_limited() -> None:
    manifest = {"coherence": {"last_capability_check": {
        "checked_at": "not a date",
    }}}
    assert is_rate_limited(manifest) is False


def test_daily_run_cap_constant() -> None:
    """Spec §6.5: 1 run per app per day."""
    assert DAILY_RUN_CAP == 1


# ── parse_llm_response ──────────────────────────────────────────────

def test_parse_plain_json() -> None:
    out = parse_llm_response('{"severity": "feasible", "rationale": "looks good"}')
    assert out is not None
    assert out.severity == SEVERITY_FEASIBLE
    assert out.rationale == "looks good"


def test_parse_json_in_code_fence() -> None:
    text = '```json\n{"severity": "incoherent", "rationale": "broken"}\n```'
    out = parse_llm_response(text)
    assert out is not None
    assert out.severity == SEVERITY_INCOHERENT


def test_parse_json_in_plain_fence() -> None:
    text = '```\n{"severity": "unclear", "rationale": "needs code"}\n```'
    out = parse_llm_response(text)
    assert out is not None
    assert out.severity == SEVERITY_UNCLEAR


def test_parse_invalid_severity_returns_none() -> None:
    out = parse_llm_response('{"severity": "broken", "rationale": "x"}')
    assert out is None


def test_parse_missing_rationale_returns_none() -> None:
    out = parse_llm_response('{"severity": "feasible"}')
    assert out is None


def test_parse_garbage_returns_none() -> None:
    assert parse_llm_response("hello there") is None
    assert parse_llm_response("") is None


def test_parse_severity_case_insensitive() -> None:
    out = parse_llm_response('{"severity": "FEASIBLE", "rationale": "x"}')
    assert out is not None
    assert out.severity == SEVERITY_FEASIBLE


# ── write_capability_check ──────────────────────────────────────────

def test_write_creates_coherence_section() -> None:
    manifest: dict = {}
    check = CapabilityCheck(
        severity=SEVERITY_FEASIBLE, rationale="x",
        checked_at="2026-06-06T00:00:00Z",
    )
    write_capability_check(manifest, check)
    assert manifest["coherence"]["last_capability_check"]["severity"] == "feasible"


def test_write_replaces_prior_check() -> None:
    manifest = {"coherence": {"last_capability_check": {
        "severity": "incoherent", "rationale": "old",
    }}}
    check = CapabilityCheck(
        severity=SEVERITY_FEASIBLE, rationale="now ok",
    )
    write_capability_check(manifest, check)
    assert manifest["coherence"]["last_capability_check"]["severity"] == "feasible"
    assert manifest["coherence"]["last_capability_check"]["rationale"] == "now ok"


def test_write_preserves_other_coherence_fields() -> None:
    manifest = {"coherence": {"findings": [{"check_id": "A-1"}]}}
    check = CapabilityCheck(severity=SEVERITY_FEASIBLE, rationale="x")
    write_capability_check(manifest, check)
    assert manifest["coherence"]["findings"] == [{"check_id": "A-1"}]


# ── Charter-change detection ────────────────────────────────────────

def test_charter_change_detects_description() -> None:
    before = {"description": "old"}
    after = {"description": "new"}
    assert "description" in detect_charter_change(before, after)


def test_charter_change_detects_nested_field() -> None:
    before = {"usage": {"how_to_use": "a"}}
    after = {"usage": {"how_to_use": "b"}}
    assert "usage.how_to_use" in detect_charter_change(before, after)


def test_charter_change_detects_observable_outcomes() -> None:
    before = {"success_criteria": {"observable_outcomes": ["x"]}}
    after = {"success_criteria": {"observable_outcomes": ["y"]}}
    assert "success_criteria.observable_outcomes" in detect_charter_change(
        before, after,
    )


def test_no_change_returns_empty_set() -> None:
    before = {"description": "x", "usage": {"how_to_use": "y"}}
    after = {"description": "x", "usage": {"how_to_use": "y"}}
    assert detect_charter_change(before, after) == set()


def test_non_charter_change_ignored() -> None:
    before = {"description": "x", "files": [{"path": "a"}]}
    after = {"description": "x", "files": [{"path": "b"}]}
    assert detect_charter_change(before, after) == set()


def test_charter_fields_constant() -> None:
    """Spec §6.5 #1 enumerates these three fields exactly."""
    assert CHARTER_FIELDS == frozenset({
        "description",
        "usage.how_to_use",
        "success_criteria.observable_outcomes",
    })


# ── should_run_c3 ──────────────────────────────────────────────────

def test_should_run_unknown_trigger() -> None:
    ok, reason = should_run_c3(after={"id": "x"}, trigger="bogus")
    assert ok is False
    assert "unknown" in reason


def test_should_run_forge_approval() -> None:
    ok, _ = should_run_c3(after={"id": "x"}, trigger="forge_approval")
    assert ok is True


def test_should_run_on_demand() -> None:
    ok, _ = should_run_c3(after={"id": "x"}, trigger="on_demand")
    assert ok is True


def test_should_run_charter_change_with_diff() -> None:
    ok, reason = should_run_c3(
        before={"description": "old"},
        after={"description": "new"},
        trigger="charter_change",
    )
    assert ok is True
    assert "description" in reason


def test_should_run_charter_change_without_diff() -> None:
    ok, reason = should_run_c3(
        before={"description": "x"},
        after={"description": "x"},
        trigger="charter_change",
    )
    assert ok is False
    assert "no charter" in reason


def test_should_run_rate_limited() -> None:
    now = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)
    after = {
        "description": "y",
        "coherence": {"last_capability_check": {
            "checked_at": (now - timedelta(minutes=10)).isoformat(),
            "severity": "feasible", "rationale": "x",
        }},
    }
    ok, reason = should_run_c3(
        before={"description": "x"},
        after=after, trigger="charter_change", now=now,
    )
    assert ok is False
    assert "rate-limited" in reason
