"""Coherence Pass C2 tests — PR 16.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §6.4.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.coherence_pass_c2 import (  # noqa: E402
    C2_PROMPT_SECTION,
    CATEGORY_BEHAVIOR_MISMATCH,
    CATEGORY_MANIFEST_MISMATCH,
    annotate_finding_as_c2,
    parse_c2_findings,
)


# ── Prompt section ──────────────────────────────────────────────────

def test_prompt_section_contains_key_phrases() -> None:
    """The prompt must convey the spec §6.4 intent."""
    assert "scheduled_actions" in C2_PROMPT_SECTION
    assert "behavior_mismatch" in C2_PROMPT_SECTION
    assert "manifest_mismatch" in C2_PROMPT_SECTION
    assert "conservative" in C2_PROMPT_SECTION.lower()


def test_prompt_mentions_example() -> None:
    """The 'sends daily briefing but only logs to stdout' example from
    the spec keeps the intent grounded."""
    assert "stdout" in C2_PROMPT_SECTION or "briefing" in C2_PROMPT_SECTION


# ── parse_c2_findings ───────────────────────────────────────────────

def test_filters_c2_categories_from_mixed_list() -> None:
    findings = [
        {"category": "cost_outlier", "summary": "x"},
        {"category": CATEGORY_BEHAVIOR_MISMATCH, "summary": "y"},
        {"category": CATEGORY_MANIFEST_MISMATCH, "summary": "z"},
        {"category": "security", "summary": "w"},
    ]
    out = parse_c2_findings(findings)
    assert len(out) == 2
    assert all(f["category"] in {CATEGORY_BEHAVIOR_MISMATCH,
                                  CATEGORY_MANIFEST_MISMATCH}
               for f in out)


def test_handles_empty_list() -> None:
    assert parse_c2_findings([]) == []


def test_handles_none() -> None:
    assert parse_c2_findings(None) == []   # type: ignore[arg-type]


def test_handles_non_dict_entries() -> None:
    findings = [
        "not a dict",
        {"category": CATEGORY_BEHAVIOR_MISMATCH},
        None,
    ]
    out = parse_c2_findings(findings)   # type: ignore[arg-type]
    assert len(out) == 1


def test_category_match_is_case_insensitive() -> None:
    findings = [{"category": "BEHAVIOR_MISMATCH"}]
    assert len(parse_c2_findings(findings)) == 1


# ── annotate_finding_as_c2 ──────────────────────────────────────────

def test_annotate_stamps_origin() -> None:
    finding = {"category": CATEGORY_BEHAVIOR_MISMATCH, "summary": "x"}
    out = annotate_finding_as_c2(finding)
    assert out["coherence_pass"] == "C2"
    # Original untouched.
    assert "coherence_pass" not in finding


def test_annotate_preserves_other_fields() -> None:
    finding = {"category": "behavior_mismatch",
               "summary": "x", "action_id": "send"}
    out = annotate_finding_as_c2(finding)
    assert out["summary"] == "x"
    assert out["action_id"] == "send"
