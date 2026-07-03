"""Tests for the inbound-issue triage classifier (Phase 4 of Issue Inbox).

Covers:
  - TriageVerdict default values
  - _coerce_triage_verdict defends against unknown category / merit /
    urgency / recommendation / effort strings (all collapse to "unknown")
  - confidence clamped to [0, 1]
  - duplicate_of + draft_labels: non-list / non-string entries dropped
  - triage_inbound uses the active triager (test seam)
  - triage_inbound swallows exceptions → degrade to confidence=0
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.intake import classifier as cls  # noqa: E402


@pytest.fixture(autouse=True)
def reset_triager():
    yield
    cls.set_triager(None)


# ─── TriageVerdict defaults ───────────────────────────────────────────────


def test_triage_verdict_defaults():
    v = cls.TriageVerdict()
    assert v.category == "unknown"
    assert v.merit == "unknown"
    assert v.urgency == "unknown"
    assert v.recommendation == "unknown"
    assert v.confidence == 0.0
    assert v.duplicate_of == []
    assert v.draft_labels == []


# ─── Coercion ─────────────────────────────────────────────────────────────


def test_coerce_unknown_category_collapses_to_unknown():
    """Unknown category from the model must collapse to 'unknown' so
    no automated action paths fire on a guess."""
    v = cls._coerce_triage_verdict({"category": "totally_fake"})
    assert v.category == "unknown"


def test_coerce_known_categories_preserved():
    for cat in ("bug", "feature_request", "question", "duplicate",
                "spam", "docs", "unknown"):
        v = cls._coerce_triage_verdict({"category": cat})
        assert v.category == cat


def test_coerce_case_insensitive_category():
    """Models might emit 'BUG' or 'Bug'; normalize to lowercase."""
    v = cls._coerce_triage_verdict({"category": "BUG"})
    assert v.category == "bug"


def test_coerce_unknown_merit_collapses():
    v = cls._coerce_triage_verdict({"merit": "questionable"})
    assert v.merit == "unknown"


def test_coerce_unknown_urgency_collapses():
    v = cls._coerce_triage_verdict({"urgency": "panic"})
    assert v.urgency == "unknown"


def test_coerce_unknown_recommendation_collapses():
    """Critical: the recommendation field drives Phase 5 auto-actions.
    Unknown strings must NOT trigger anything; collapsing to unknown
    is the safe path."""
    v = cls._coerce_triage_verdict({"recommendation": "auto_delete_everything"})
    assert v.recommendation == "unknown"


def test_coerce_known_recommendations_preserved():
    for rec in ("auto_close_duplicate", "auto_reply_clarifying",
                "route_to_admin", "needs_investigation"):
        v = cls._coerce_triage_verdict({"recommendation": rec})
        assert v.recommendation == rec


def test_coerce_unknown_effort_collapses():
    v = cls._coerce_triage_verdict({"estimated_effort": "epic"})
    assert v.estimated_effort == "unknown"


def test_coerce_confidence_clamped():
    assert cls._coerce_triage_verdict({"confidence": 5.0}).confidence == 1.0
    assert cls._coerce_triage_verdict({"confidence": -1.0}).confidence == 0.0


def test_coerce_confidence_non_numeric_defaults_to_zero():
    assert cls._coerce_triage_verdict({"confidence": "high"}).confidence == 0.0


def test_coerce_duplicate_of_filters_non_strings():
    v = cls._coerce_triage_verdict({
        "duplicate_of": ["good/repo#1", 42, None, "another/repo#2"],
    })
    assert v.duplicate_of == ["good/repo#1", "another/repo#2"]


def test_coerce_duplicate_of_non_list_dropped():
    v = cls._coerce_triage_verdict({"duplicate_of": "not a list"})
    assert v.duplicate_of == []


def test_coerce_draft_labels_filters_non_strings():
    v = cls._coerce_triage_verdict({
        "draft_labels": ["bug", 42, "p1", None],
    })
    assert v.draft_labels == ["bug", "p1"]


def test_coerce_string_fields_stripped():
    v = cls._coerce_triage_verdict({
        "draft_reply": "  hi  ",
        "reasoning": "  because  ",
    })
    assert v.draft_reply == "hi"
    assert v.reasoning == "because"


def test_coerce_empty_dict_yields_unknowns():
    """A blank response from the model shouldn't crash or silently
    default to a real category."""
    v = cls._coerce_triage_verdict({})
    assert v.category == "unknown"
    assert v.merit == "unknown"
    assert v.urgency == "unknown"
    assert v.recommendation == "unknown"
    assert v.confidence == 0.0


# ─── User-message formatting ──────────────────────────────────────────────


def test_format_triage_message_includes_issue_fields():
    msg = cls._format_triage_user_message(
        title="X broken",
        body="reproducer here",
        repo="evolve-ops/evolve",
        author="someone",
        ctx=cls.ClassificationContext(),
    )
    assert "evolve-ops/evolve" in msg
    assert "X broken" in msg
    assert "reproducer here" in msg
    assert "@someone" in msg


def test_format_triage_message_handles_empty_fields():
    """No title, no body, no context — should still be a parseable prompt."""
    msg = cls._format_triage_user_message(
        title="", body="", repo="o/r", author="",
        ctx=cls.ClassificationContext(),
    )
    assert "o/r" in msg
    # Sentinel: empty body shouldn't blank the section.
    assert "(empty)" in msg or "(no title)" in msg


def test_format_triage_message_includes_evidence():
    """Reuses the diagnostic-evidence renderer when present."""
    from evolve_admin.intake import diagnostics as diag
    ev = diag.DiagnosticEvidence(matching_issues=[
        diag.MatchingIssue(repo="o/r", number=1, title="t",
                           state="open", url="u"),
    ])
    msg = cls._format_triage_user_message(
        title="t", body="b", repo="o/r", author="a",
        ctx=cls.ClassificationContext(diagnostic_evidence=ev),
    )
    assert "Evidence gathered" in msg
    assert "o/r#1" in msg


# ─── Public entry point ───────────────────────────────────────────────────


def test_triage_inbound_uses_active_triager():
    sentinel = cls.TriageVerdict(category="bug", confidence=0.9)
    cls.set_triager(lambda t, b, r, a, c: sentinel)
    out = cls.triage_inbound(title="t", body="b", repo="o/r", author="a")
    assert out is sentinel


def test_triage_inbound_swallows_exceptions():
    """A buggy triager must NOT crash the watcher."""
    def boom(t, b, r, a, c):
        raise RuntimeError("simulated triager bug")
    cls.set_triager(boom)
    out = cls.triage_inbound(title="t", body="b", repo="o/r", author="a")
    assert out.confidence == 0.0
    assert "RuntimeError" in out.reasoning


def test_triage_inbound_default_context_when_none():
    captured = {}
    def spy(t, b, r, a, c):
        captured["ctx"] = c
        return cls.TriageVerdict(category="bug", confidence=0.5)
    cls.set_triager(spy)
    cls.triage_inbound(title="t", body="b", repo="o/r", author="a")
    assert isinstance(captured["ctx"], cls.ClassificationContext)


def test_set_triager_none_restores_default():
    cls.set_triager(lambda t, b, r, a, c: cls.TriageVerdict(category="bug"))
    cls.set_triager(None)
    # Without an Anthropic API key, the default triager returns
    # confidence=0 with the "disabled" reason rather than calling out.
    out = cls.triage_inbound(title="t", body="b", repo="o/r", author="a")
    assert out.confidence == 0.0
    assert out.category == "unknown"
