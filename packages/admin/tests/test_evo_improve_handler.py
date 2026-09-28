"""Tests for the ``evo improve`` handler.

Strategy: stub the classifier (we trust its own tests cover the LLM
side) and verify the handler's behavior per category:

  - local_env → in-chat help, no intake captured
  - evolve_code / upstream / mixed → intake captured, draft preview
    shown, ``evo intake promote <id> [--to <name>]`` hint in the body
  - low confidence → ask-to-clarify, no intake captured
  - empty args → prompt the user to say what's wrong
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.evo.handlers.improve import render_improve  # noqa: E402
from evolve_admin.intake import classifier as cls  # noqa: E402
from evolve_admin.intake import diagnostics as diag  # noqa: E402
from evolve_admin.intake import store as _store  # noqa: E402


@pytest.fixture(autouse=True)
def reset_classifier():
    yield
    cls.set_classifier(None)
    diag.set_gatherer(None)


@pytest.fixture(autouse=True)
def stub_diagnostics():
    """Default to an empty-evidence gatherer so the handler tests don't
    spawn gh/git subprocesses. Individual tests can override via
    diag.set_gatherer(...)."""
    diag.set_gatherer(lambda msg, ctx: diag.DiagnosticEvidence())
    yield


def _network(tmp_path: Path) -> dict:
    return {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "intake": {
            "github": {
                "default": "evolve",
                "targets": {
                    "evolve": {"owner": "evolve-ops", "repo": "evolve"},
                    "openclaw": {"owner": "openclaw", "repo": "openclaw"},
                },
            }
        },
    }


def _stub(verdict: cls.Verdict):
    """Install a classifier stub that returns the given verdict."""
    cls.set_classifier(lambda msg, ctx: verdict)


def _intakes_in_open(shared_dir: Path) -> list:
    return list(_store.iter_intakes(shared_dir, subdirs=("open",)))


# ─── No description ─────────────────────────────────────────────────────────


def test_empty_args_prompts_for_description(tmp_path):
    """Bare `evo improve` should explain how to use it and give
    examples, not crash."""
    r = render_improve(
        role="primary", bot_id="evo", args="",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "Make Evolve better" in body
    assert "Tell me" in body
    # No intake captured.
    assert _intakes_in_open(tmp_path) == []


# ─── local_env path ────────────────────────────────────────────────────────


def test_local_env_returns_in_chat_help_no_intake(tmp_path):
    """A local-env verdict should show the help text and NOT capture
    an intake — the whole point of this category is that no issue
    needs filing."""
    _stub(cls.Verdict(
        category="local_env",
        in_chat_help="Check your Slack token — it expired 3 days ago.",
        confidence=0.9,
        reasoning="token-expired symptom",
    ))
    r = render_improve(
        role="primary", bot_id="evo", args="team_bot_a slack stopped sending",
        network=_network(tmp_path), reported_from="/integrations",
    )
    body = r.direct_send_message or ""
    assert "Slack token" in body
    assert "token-expired symptom" in body
    # Escape hatch: tell the user how to escalate if we got it wrong.
    assert "I'll draft an issue" in body
    assert _intakes_in_open(tmp_path) == []


def test_local_env_with_empty_help_uses_fallback(tmp_path):
    """If the classifier doesn't populate in_chat_help, the handler
    should still ask for more detail rather than fall through silently."""
    _stub(cls.Verdict(category="local_env", confidence=0.9))
    r = render_improve(
        role="primary", bot_id="evo", args="something feels slow",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "Tell me more" in body
    assert _intakes_in_open(tmp_path) == []


# ─── evolve_code path ─────────────────────────────────────────────────────


def test_evolve_code_captures_intake_and_shows_draft(tmp_path):
    _stub(cls.Verdict(
        category="evolve_code",
        target_name="evolve",
        draft_title="[bug] Alerts page shows resolved as firing",
        draft_body="The Alerts page is showing yesterday's incidents as firing.",
        confidence=0.85,
        reasoning="UI bug — state mismatch",
    ))
    r = render_improve(
        role="primary", bot_id="evo",
        args="alerts page shows yesterday's incidents as firing",
        network=_network(tmp_path), reported_from="/alerts",
    )
    body = r.direct_send_message or ""

    # An intake should now be in `open/`.
    intakes = _intakes_in_open(tmp_path)
    assert len(intakes) == 1
    intake = intakes[0]
    assert intake.kind == "bug"
    assert "yesterday's incidents" in intake.body

    # The preview must point the user at the right promote command.
    assert intake.id in body
    assert "Evolve codebase" in body
    assert f"evo intake promote {intake.id}" in body
    assert "--to evolve" in body


def test_upstream_routes_to_openclaw_target(tmp_path):
    _stub(cls.Verdict(
        category="upstream",
        target_name="openclaw",
        draft_title="[bug] gateway FileHandle leak",
        draft_body="Gateway crashes every ~2h with ERR_INVALID_STATE on Node 25.",
        confidence=0.9,
        reasoning="signature matches openclaw FileHandle issue",
    ))
    r = render_improve(
        role="primary", bot_id="evo",
        args="team_bot_a gateway crashes every couple of hours",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    intakes = _intakes_in_open(tmp_path)
    assert len(intakes) == 1
    assert "upstream" in body.lower()
    assert "--to openclaw" in body


def test_mixed_captures_single_intake_for_now(tmp_path):
    """Phase 0b ships a single intake even for `mixed`; auto-splitting
    into two linked intakes lands later. The classifier's body should
    explain both sides so the operator can split manually if they want."""
    _stub(cls.Verdict(
        category="mixed",
        target_name="evolve",
        draft_title="[bug] Alerts gap on upstream-crash pattern",
        draft_body=(
            "Two things going on:\n"
            "1. Evolve didn't detect repeated heartbeat-session crashes.\n"
            "2. Root cause is openclaw#84820 (upstream)."
        ),
        confidence=0.85,
        reasoning="detection gap + upstream root cause",
    ))
    r = render_improve(
        role="primary", bot_id="evo",
        args="security_bot burned $20 yesterday and we didn't catch it",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    intakes = _intakes_in_open(tmp_path)
    assert len(intakes) == 1
    assert "mixed" in body.lower()


# ─── Low-confidence path ──────────────────────────────────────────────────


def test_low_confidence_does_not_capture_intake(tmp_path):
    """When the classifier isn't sure, asking for clarification beats
    silently filing noise."""
    _stub(cls.Verdict(
        category="evolve_code",
        confidence=0.3,
        reasoning="message too short",
    ))
    r = render_improve(
        role="primary", bot_id="evo", args="broken",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "not sure" in body.lower() or "more detail" in body.lower()
    assert _intakes_in_open(tmp_path) == []


# ─── Context plumbing ─────────────────────────────────────────────────────


def test_reported_from_passed_to_classifier(tmp_path):
    """The drawer's originating page must reach the classifier as
    ``ClassificationContext.reported_from`` — it's a load-bearing
    routing signal."""
    seen: dict = {}
    def spy(msg, ctx):
        seen["reported_from"] = ctx.reported_from
        return cls.Verdict(category="local_env", confidence=0.9,
                           in_chat_help="ok")
    cls.set_classifier(spy)
    render_improve(
        role="primary", bot_id="evo", args="X",
        network=_network(tmp_path), reported_from="/alerts",
    )
    assert seen["reported_from"] == "/alerts"


def test_available_targets_passed_to_classifier(tmp_path):
    """The classifier should see the operator's configured target list
    so it can constrain its target_name pick."""
    seen: dict = {}
    def spy(msg, ctx):
        seen["targets"] = ctx.available_targets
        return cls.Verdict(category="local_env", confidence=0.9,
                           in_chat_help="ok")
    cls.set_classifier(spy)
    render_improve(
        role="primary", bot_id="evo", args="X",
        network=_network(tmp_path),
    )
    assert set(seen["targets"]) == {"evolve", "openclaw"}


# ─── Kind inference (bug vs feature) ──────────────────────────────────────


def test_feature_marker_words_set_intake_kind_to_feature(tmp_path):
    """When the operator's language reads as a wish/feature, capture
    as 'feature', not 'bug'."""
    _stub(cls.Verdict(
        category="evolve_code",
        target_name="evolve",
        draft_title="[feature] filter cost view by bot",
        draft_body="I'd like to filter the cost page by individual bot.",
        confidence=0.85,
        reasoning="explicit wish",
    ))
    r = render_improve(
        role="primary", bot_id="evo",
        args="I wish I could filter the cost view by bot",
        network=_network(tmp_path),
    )
    intakes = _intakes_in_open(tmp_path)
    assert len(intakes) == 1
    assert intakes[0].kind == "feature"


def test_no_feature_marker_words_default_to_bug(tmp_path):
    _stub(cls.Verdict(
        category="evolve_code",
        target_name="evolve",
        draft_title="[bug] X is broken",
        draft_body="X stopped working.",
        confidence=0.85,
        reasoning="failure described",
    ))
    r = render_improve(
        role="primary", bot_id="evo",
        args="alerts page is broken",
        network=_network(tmp_path),
    )
    intakes = _intakes_in_open(tmp_path)
    assert intakes[0].kind == "bug"


# ─── Diagnostic evidence integration (Phase 0c) ───────────────────────────


def test_diagnostics_run_before_classifier(tmp_path):
    """The handler should call gather_diagnostics BEFORE classify_issue,
    so evidence is available to the model when it makes its verdict."""
    call_order: list[str] = []

    def fake_diag(msg, ctx):
        call_order.append("diag")
        return diag.DiagnosticEvidence(notes=["from gatherer"])

    def spy_cls(msg, ctx):
        call_order.append("classify")
        # The evidence the gatherer produced should be on the context
        # by the time the classifier sees it.
        assert ctx.diagnostic_evidence is not None
        assert "from gatherer" in (ctx.diagnostic_evidence.notes or [])
        return cls.Verdict(category="local_env", confidence=0.9,
                           in_chat_help="ok")

    diag.set_gatherer(fake_diag)
    cls.set_classifier(spy_cls)

    render_improve(
        role="primary", bot_id="evo", args="team_bot_a is broken",
        network=_network(tmp_path),
    )
    assert call_order == ["diag", "classify"]


def test_diagnostics_threads_configured_repos(tmp_path):
    """gather_diagnostics must receive the full ``owner/repo`` strings
    for every configured intake target — that's how the gh-search
    knows where to look."""
    captured: dict = {}

    def spy_diag(msg, ctx):
        captured["repos_to_search"] = ctx.repos_to_search
        captured["reported_from"] = ctx.reported_from
        return diag.DiagnosticEvidence()

    diag.set_gatherer(spy_diag)
    cls.set_classifier(lambda m, c: cls.Verdict(
        category="local_env", confidence=0.9, in_chat_help="ok",
    ))

    render_improve(
        role="primary", bot_id="evo", args="X",
        network=_network(tmp_path), reported_from="/alerts",
    )
    # _network(tmp_path) configures evolve + openclaw as v2 targets.
    assert set(captured["repos_to_search"]) == {
        "evolve-ops/evolve", "openclaw/openclaw",
    }
    assert captured["reported_from"] == "/alerts"


def test_diagnostics_evidence_reaches_classifier_context(tmp_path):
    """End-to-end: the gatherer's evidence is on the
    ClassificationContext the classifier sees, AND it appears in the
    formatted prompt produced by _format_user_message."""
    matching = diag.MatchingIssue(
        repo="openclaw/openclaw", number=84820,
        title="FileHandle leak", state="open",
        url="https://github.com/openclaw/openclaw/issues/84820",
    )
    diag.set_gatherer(
        lambda msg, ctx: diag.DiagnosticEvidence(matching_issues=[matching]),
    )

    captured: dict = {}
    def spy_cls(msg, ctx):
        # Format what the live classifier would see.
        captured["prompt"] = cls._format_user_message(msg, ctx)
        captured["evidence_matches"] = ctx.diagnostic_evidence.matching_issues
        return cls.Verdict(category="upstream", target_name="openclaw",
                           draft_title="t", draft_body="b",
                           confidence=0.9, reasoning="found existing thread")
    cls.set_classifier(spy_cls)

    render_improve(
        role="primary", bot_id="evo",
        args="team_bot_a gateway crashes every couple hours",
        network=_network(tmp_path),
    )
    assert len(captured["evidence_matches"]) == 1
    assert captured["evidence_matches"][0].number == 84820
    # The prompt must surface the issue — that's how the model uses it.
    assert "openclaw/openclaw#84820" in captured["prompt"]
    assert "FileHandle leak" in captured["prompt"]
