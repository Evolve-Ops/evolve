"""Tests for the LLM-side revise pieces in ``intake.classifier``.

Covers:
  - ReviseVerdict default values + coercion of malformed model output
  - revise_draft uses the active reviser (test seam)
  - revise_draft swallows reviser exceptions → degrade to confidence=0
  - User-message formatter includes the current draft + instruction
  - Evidence section piggybacks the classifier's formatter when present
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
def reset_reviser():
    yield
    cls.set_reviser(None)


# ─── Coercion ────────────────────────────────────────────────────────────


def test_coerce_revise_verdict_preserves_original_title_when_new_missing():
    """Reviser returning empty new_title shouldn't blank the title — the
    classifier coerces it back to the operator's original."""
    parsed = {"new_body": "body", "confidence": 0.8}
    v = cls._coerce_revise_verdict(parsed, current_title="[bug] Original")
    assert v.new_title == "[bug] Original"
    assert v.new_body == "body"


def test_coerce_revise_verdict_clamps_confidence():
    v_high = cls._coerce_revise_verdict({"confidence": 9.0}, "t")
    assert v_high.confidence == 1.0
    v_low = cls._coerce_revise_verdict({"confidence": -1.0}, "t")
    assert v_low.confidence == 0.0


def test_coerce_revise_verdict_handles_non_numeric_confidence():
    v = cls._coerce_revise_verdict({"confidence": "high"}, "t")
    assert v.confidence == 0.0


def test_coerce_revise_verdict_strips_strings():
    v = cls._coerce_revise_verdict(
        {"new_title": "  t  ", "new_body": "  b  ", "reasoning": "  r  "},
        current_title="orig",
    )
    assert v.new_title == "t"
    assert v.new_body == "b"
    assert v.reasoning == "r"


def test_coerce_revise_verdict_empty_response_uses_original_title():
    v = cls._coerce_revise_verdict({}, current_title="orig")
    assert v.new_title == "orig"
    assert v.new_body == ""


# ─── User-message formatting ────────────────────────────────────────────


def test_revise_user_message_includes_current_draft():
    msg = cls._format_revise_user_message(
        current_title="[bug] T",
        current_body="Body",
        instruction="shorter",
        ctx=cls.ClassificationContext(),
    )
    assert "[bug] T" in msg
    assert "Body" in msg
    assert "shorter" in msg


def test_revise_user_message_includes_context_bullets():
    msg = cls._format_revise_user_message(
        current_title="T", current_body="B", instruction="i",
        ctx=cls.ClassificationContext(
            reported_from="/alerts", evolve_version="0.3.0",
        ),
    )
    assert "/alerts" in msg
    assert "0.3.0" in msg


def test_revise_user_message_includes_evidence_section_when_present():
    """When the original classifier verdict was produced with evidence,
    pass that same evidence to the reviser — keeps the two passes
    interpretively aligned."""
    from evolve_admin.intake import diagnostics as diag
    ev = diag.DiagnosticEvidence(matching_issues=[
        diag.MatchingIssue(repo="o/r", number=1, title="t",
                          state="open", url="u"),
    ])
    msg = cls._format_revise_user_message(
        current_title="T", current_body="B", instruction="i",
        ctx=cls.ClassificationContext(diagnostic_evidence=ev),
    )
    assert "Evidence gathered" in msg
    assert "o/r#1" in msg


def test_revise_user_message_handles_empty_context():
    """No context fields populated should still produce a parseable prompt."""
    msg = cls._format_revise_user_message(
        current_title="T", current_body="B", instruction="i",
        ctx=cls.ClassificationContext(),
    )
    assert "T" in msg and "B" in msg and "i" in msg
    assert "no extra context" in msg


# ─── Public entry point ────────────────────────────────────────────────


def test_revise_draft_uses_active_reviser():
    sentinel = cls.ReviseVerdict(new_title="X", new_body="Y", confidence=0.7)
    cls.set_reviser(lambda t, b, i, c: sentinel)
    out = cls.revise_draft(
        current_title="orig-t", current_body="orig-b", instruction="x",
    )
    assert out is sentinel


def test_revise_draft_swallows_reviser_exceptions():
    """A buggy reviser must NOT crash the calling evo turn."""
    def boom(t, b, i, c):
        raise RuntimeError("simulated reviser bug")
    cls.set_reviser(boom)
    out = cls.revise_draft(
        current_title="orig-t", current_body="orig-b", instruction="x",
    )
    # On failure, preserve the originals + confidence=0 so caller asks
    # for clarification rather than mutating the draft.
    assert out.new_title == "orig-t"
    assert out.new_body == "orig-b"
    assert out.confidence == 0.0
    assert "RuntimeError" in out.reasoning


def test_revise_draft_default_context_when_none():
    captured = {}
    def spy(t, b, i, c):
        captured["ctx"] = c
        return cls.ReviseVerdict(new_title=t, new_body=b, confidence=0.9)
    cls.set_reviser(spy)
    cls.revise_draft(current_title="t", current_body="b", instruction="x")
    assert isinstance(captured["ctx"], cls.ClassificationContext)


def test_set_reviser_none_restores_default():
    cls.set_reviser(lambda t, b, i, c: cls.ReviseVerdict(
        new_title="custom", new_body="", confidence=0.9,
    ))
    cls.set_reviser(None)
    # The default reviser needs the Anthropic API key — without one,
    # it returns a degraded verdict preserving the originals.
    out = cls.revise_draft(
        current_title="t", current_body="b", instruction="x",
    )
    assert out.new_title == "t"
    assert out.new_body == "b"
    assert out.confidence == 0.0
