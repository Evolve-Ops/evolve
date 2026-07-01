"""Tests for the issue-reporting classifier.

Strategy: stub the LLM call. Verify Verdict coercion (unknown category
collapses to local_env, confidence clamped to [0,1], unknown target
name dropped), context formatting, and the public ``classify_issue``
entry point's exception safety.
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
def reset_classifier():
    """Restore the default classifier after each test so set_classifier
    in one test never leaks into the next."""
    yield
    cls.set_classifier(None)


# ─── Coercion ───────────────────────────────────────────────────────────────


def test_coerce_unknown_category_collapses_to_local_env():
    """An unknown category from the model must NOT silently file an
    issue — collapsing to local_env is the safe default."""
    ctx = cls.ClassificationContext()
    parsed = {"category": "totally_made_up", "draft_body": "shouldn't matter"}
    v = cls._coerce_verdict(parsed, ctx)
    assert v.category == "local_env"


def test_coerce_missing_category_collapses_to_local_env():
    v = cls._coerce_verdict({}, cls.ClassificationContext())
    assert v.category == "local_env"


def test_coerce_known_category_preserved():
    for c in cls.VALID_CATEGORIES:
        v = cls._coerce_verdict({"category": c}, cls.ClassificationContext())
        assert v.category == c


def test_coerce_unknown_target_dropped_when_targets_constrained():
    """If the model picks a target_name that isn't in the configured
    targets list, drop it — the caller falls back to the default."""
    ctx = cls.ClassificationContext(available_targets=("evolve", "openclaw"))
    v = cls._coerce_verdict(
        {"category": "upstream", "target_name": "ghost"}, ctx,
    )
    assert v.target_name is None


def test_coerce_known_target_preserved():
    ctx = cls.ClassificationContext(available_targets=("evolve", "openclaw"))
    v = cls._coerce_verdict(
        {"category": "upstream", "target_name": "openclaw"}, ctx,
    )
    assert v.target_name == "openclaw"


def test_coerce_unconstrained_targets_accepts_any_name():
    """When the caller doesn't pass available_targets, we don't gate
    the name — the caller will resolve it (and surface a clear error
    if it's unknown)."""
    ctx = cls.ClassificationContext()  # available_targets empty
    v = cls._coerce_verdict(
        {"category": "evolve_code", "target_name": "anything"}, ctx,
    )
    assert v.target_name == "anything"


def test_coerce_confidence_clamped():
    v = cls._coerce_verdict({"category": "local_env", "confidence": 5.0},
                             cls.ClassificationContext())
    assert v.confidence == 1.0
    v2 = cls._coerce_verdict({"category": "local_env", "confidence": -0.5},
                              cls.ClassificationContext())
    assert v2.confidence == 0.0


def test_coerce_confidence_non_numeric_defaults_to_zero():
    v = cls._coerce_verdict({"category": "local_env", "confidence": "high"},
                             cls.ClassificationContext())
    assert v.confidence == 0.0


def test_coerce_string_fields_stripped():
    v = cls._coerce_verdict(
        {"category": "evolve_code", "draft_title": "  hi  ",
         "draft_body": "  body\n  ", "reasoning": "  why  "},
        cls.ClassificationContext(),
    )
    assert v.draft_title == "hi"
    assert v.draft_body == "body"
    assert v.reasoning == "why"


# ─── User message formatting ────────────────────────────────────────────────


def test_format_user_message_includes_operator_message():
    ctx = cls.ClassificationContext()
    out = cls._format_user_message("team_bot_a is broken", ctx)
    assert "team_bot_a is broken" in out
    assert "# Operator said" in out


def test_format_user_message_includes_page_context():
    ctx = cls.ClassificationContext(reported_from="/alerts")
    out = cls._format_user_message("X", ctx)
    assert "/alerts" in out
    assert "Reported from" in out


def test_format_user_message_includes_available_targets():
    ctx = cls.ClassificationContext(available_targets=("evolve", "openclaw"))
    out = cls._format_user_message("X", ctx)
    assert "evolve" in out
    assert "openclaw" in out


def test_format_user_message_handles_empty_context():
    """No context at all should still produce a parseable prompt."""
    out = cls._format_user_message("X", cls.ClassificationContext())
    assert "X" in out
    # Sentinel line that confirms the function ran the no-bullets branch.
    assert "no extra context" in out


def test_format_user_message_caps_conversation_history():
    """Long conversation history should be truncated to the last 6 turns."""
    turns = tuple(
        {"role": "user" if i % 2 == 0 else "evo", "text": f"turn {i}"}
        for i in range(20)
    )
    ctx = cls.ClassificationContext(conversation_excerpt=turns)
    out = cls._format_user_message("X", ctx)
    # First turn shouldn't appear; the last few should.
    assert "turn 0" not in out
    assert "turn 19" in out


# ─── JSON parsing ───────────────────────────────────────────────────────────


def test_parse_json_object_tolerates_code_fence():
    raw = '```json\n{"category": "evolve_code"}\n```'
    parsed = cls._parse_json_object(raw)
    assert parsed == {"category": "evolve_code"}


def test_parse_json_object_tolerates_prose_around():
    raw = 'Here is the verdict:\n\n{"category": "upstream"}\n\nDone.'
    parsed = cls._parse_json_object(raw)
    assert parsed == {"category": "upstream"}


def test_parse_json_object_returns_empty_on_garbage():
    assert cls._parse_json_object("no json here") == {}
    assert cls._parse_json_object("") == {}


# ─── Public entry point ─────────────────────────────────────────────────────


def test_classify_issue_uses_active_classifier():
    """Stubs returned by set_classifier should actually drive the output."""
    sentinel = cls.Verdict(category="upstream", confidence=0.9)
    cls.set_classifier(lambda msg, ctx: sentinel)
    out = cls.classify_issue("X")
    assert out is sentinel


def test_classify_issue_swallows_classifier_exceptions():
    """A buggy classifier must NOT crash the calling evo turn."""
    def boom(msg, ctx):
        raise RuntimeError("simulated failure")
    cls.set_classifier(boom)
    out = cls.classify_issue("X")
    assert out.category == "local_env"
    assert out.confidence == 0.0
    assert "RuntimeError" in out.reasoning


def test_classify_issue_passes_context_through():
    captured: dict = {}
    def stub(msg, ctx):
        captured["msg"] = msg
        captured["ctx"] = ctx
        return cls.Verdict(category="local_env")
    cls.set_classifier(stub)
    cls.classify_issue(
        "hi",
        context=cls.ClassificationContext(
            reported_from="/alerts", available_targets=("evolve",),
        ),
    )
    assert captured["msg"] == "hi"
    assert captured["ctx"].reported_from == "/alerts"
    assert captured["ctx"].available_targets == ("evolve",)


def test_classify_issue_default_context_when_none():
    """No context argument should still call the classifier with a
    valid ClassificationContext (not None)."""
    captured: dict = {}
    def stub(msg, ctx):
        captured["ctx"] = ctx
        return cls.Verdict(category="local_env")
    cls.set_classifier(stub)
    cls.classify_issue("X")
    assert isinstance(captured["ctx"], cls.ClassificationContext)


def test_set_classifier_none_restores_default():
    custom = cls.Verdict(category="evolve_code")
    cls.set_classifier(lambda msg, ctx: custom)
    cls.set_classifier(None)
    # The default classifier requires the Anthropic API key. With no
    # env var set in tests, it returns the disabled-classifier fallback.
    out = cls.classify_issue("X")
    assert out.category == "local_env"
    assert out.confidence == 0.0


# ─── Evidence formatting (Phase 0c) ────────────────────────────────────────


def test_format_evidence_renders_matching_issues():
    """The matching-issues section must include enough for the model to
    decide whether to reference an existing thread vs. file fresh."""
    from evolve_admin.intake import diagnostics as diag
    ev = diag.DiagnosticEvidence(matching_issues=[
        diag.MatchingIssue(
            repo="openclaw/openclaw", number=84820,
            title="FileHandle leak", state="open",
            url="https://github.com/openclaw/openclaw/issues/84820",
        ),
    ])
    lines = cls._format_evidence(ev)
    text = "\n".join(lines)
    assert "openclaw/openclaw#84820" in text
    assert "FileHandle leak" in text
    assert "open" in text
    assert "https://github.com/openclaw/openclaw/issues/84820" in text


def test_format_evidence_renders_recent_signals():
    from evolve_admin.intake import diagnostics as diag
    ev = diag.DiagnosticEvidence(recent_signals=[
        diag.RecentSignal(
            producer="cost_watchdog", severity="warn",
            signature="cost_watchdog:spike:security_bot",
            signal_id="sig-1", bot_id="security_bot",
            last_observed_at="2026-05-22T19:00:00Z",
        ),
    ])
    lines = cls._format_evidence(ev)
    text = "\n".join(lines)
    assert "cost_watchdog" in text
    assert "warn" in text
    assert "bot=security_bot" in text


def test_format_evidence_renders_recent_commits():
    from evolve_admin.intake import diagnostics as diag
    ev = diag.DiagnosticEvidence(recent_commits=[
        diag.RecentCommit(
            sha="abc1234", subject="fix(alerts): producers use HTML mode",
            relative_date="2 days ago",
            path="packages/admin/evolve_admin/alerts/",
        ),
    ])
    lines = cls._format_evidence(ev)
    text = "\n".join(lines)
    assert "abc1234" in text
    assert "fix(alerts)" in text
    assert "alerts/" in text


def test_format_evidence_includes_notes():
    """Investigation notes are how the classifier tells empty results
    apart from 'tool didn't run.'"""
    from evolve_admin.intake import diagnostics as diag
    ev = diag.DiagnosticEvidence(notes=["gh-search skipped: no repos"])
    lines = cls._format_evidence(ev)
    text = "\n".join(lines)
    assert "Investigation notes" in text
    assert "no repos" in text


def test_format_evidence_empty_returns_empty_list():
    from evolve_admin.intake import diagnostics as diag
    ev = diag.DiagnosticEvidence()
    assert cls._format_evidence(ev) == []


def test_format_user_message_includes_evidence_section():
    """The prompt must surface evidence prominently — under a clear
    heading the model is instructed to weigh."""
    from evolve_admin.intake import diagnostics as diag
    ev = diag.DiagnosticEvidence(matching_issues=[
        diag.MatchingIssue(
            repo="o/r", number=1, title="t", state="open", url="u",
        ),
    ])
    ctx = cls.ClassificationContext(diagnostic_evidence=ev)
    out = cls._format_user_message("X", ctx)
    assert "# Evidence gathered" in out
    assert "o/r#1" in out


def test_format_user_message_omits_evidence_section_when_absent():
    """No evidence → no Evidence heading. Don't bloat the prompt with
    empty sections."""
    out = cls._format_user_message("X", cls.ClassificationContext())
    assert "# Evidence gathered" not in out


def test_format_user_message_tolerates_malformed_evidence():
    """A non-DiagnosticEvidence object in the field must not crash."""
    ctx = cls.ClassificationContext(diagnostic_evidence="not an evidence object")
    # The hasattr(to_dict) check skips the section cleanly.
    out = cls._format_user_message("X", ctx)
    assert "# Evidence gathered" not in out
