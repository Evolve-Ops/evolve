"""tests/test_refine.py — Operator-driven proposal iteration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.dedup import compute_fingerprint  # noqa: E402
from arbiter.refine import (  # noqa: E402
    RefineResult,
    _build_user_message,
    _parse_response,
    apply_refinement,
    refine_proposal,
)
from schema.proposal import Investigation  # noqa: E402
from testing.harness import (  # noqa: E402
    make_config_patch_proposal,
    make_investigation_proposal,
)


# ──────────────────────────────────────────────────────────────────────────
# _parse_response
# ──────────────────────────────────────────────────────────────────────────


def test_parse_response_clean_json():
    raw = json.dumps(
        {
            "problem": "Bot team_bot_c spending hit $3.50 today",
            "admin_surface_summary": "team_bot_c daily spend $3.50",
            "action_context": "Review heartbeat frequency for team_bot_c.",
        }
    )
    r = _parse_response(raw)
    assert r.ok
    assert r.new_problem == "Bot team_bot_c spending hit $3.50 today"
    assert r.new_admin_surface_summary == "team_bot_c daily spend $3.50"
    assert r.new_action_context == "Review heartbeat frequency for team_bot_c."


def test_parse_response_handles_fenced_json():
    raw = "```json\n" + json.dumps(
        {
            "problem": "P",
            "admin_surface_summary": "S",
        }
    ) + "\n```"
    r = _parse_response(raw)
    assert r.ok
    assert r.new_problem == "P"
    assert r.new_action_context is None  # omitted from response


def test_parse_response_truncates_long_summary():
    raw = json.dumps(
        {
            "problem": "P",
            "admin_surface_summary": "x" * 200,
        }
    )
    r = _parse_response(raw)
    assert r.ok
    assert len(r.new_admin_surface_summary) == 118  # 117 + ellipsis char


def test_parse_response_rejects_non_json():
    r = _parse_response("Sorry I can't do that")
    assert not r.ok
    assert "parse_failure" in r.error


def test_parse_response_rejects_empty_fields():
    raw = json.dumps({"problem": "", "admin_surface_summary": ""})
    r = _parse_response(raw)
    assert not r.ok


# ──────────────────────────────────────────────────────────────────────────
# refine_proposal — end to end with mocked LLM
# ──────────────────────────────────────────────────────────────────────────


def test_refine_proposal_calls_llm_with_feedback_and_returns_revisions():
    p = make_investigation_proposal()
    captured: list[str] = []

    def fake_llm(user_msg: str) -> str:
        captured.append(user_msg)
        return json.dumps(
            {
                "problem": "Refined problem text",
                "admin_surface_summary": "refined summary",
                "action_context": "Refined investigation context",
            }
        )

    result = refine_proposal(p, "make it less aggressive", llm_call=fake_llm)
    assert result.ok
    assert result.new_problem == "Refined problem text"
    assert "make it less aggressive" in captured[0]
    assert "ORIGINAL PROPOSAL" in captured[0]


def test_refine_proposal_handles_llm_exception():
    p = make_investigation_proposal()

    def boom(user_msg: str) -> str:
        raise RuntimeError("simulated API outage")

    result = refine_proposal(p, "feedback", llm_call=boom)
    assert not result.ok
    assert "api_error" in result.error
    assert "RuntimeError" in result.error


def test_refine_proposal_rejects_empty_feedback():
    p = make_investigation_proposal()
    result = refine_proposal(p, "   ", llm_call=lambda _msg: "")
    assert not result.ok
    assert "empty" in result.error


# ──────────────────────────────────────────────────────────────────────────
# apply_refinement — mutation + revision audit
# ──────────────────────────────────────────────────────────────────────────


def test_apply_refinement_mutates_proposal_and_records_history():
    p = make_investigation_proposal()
    original_problem = p.problem
    original_summary = p.admin_surface_summary
    original_context = p.action.context if isinstance(p.action, Investigation) else None
    original_fingerprint = compute_fingerprint(p)

    result = RefineResult(
        ok=True,
        new_problem="New problem",
        new_admin_surface_summary="new summary",
        new_action_context="New investigation context",
    )
    apply_refinement(p, result, feedback="be more concise", actor="user")

    assert p.problem == "New problem"
    assert p.admin_surface_summary == "new summary"
    if isinstance(p.action, Investigation):
        assert p.action.context == "New investigation context"

    # One revision recorded with the prior values
    assert len(p.revisions) == 1
    rev = p.revisions[0]
    assert rev.actor == "user"
    assert rev.feedback == "be more concise"
    assert rev.prior_problem == original_problem
    assert rev.prior_admin_surface_summary == original_summary
    if original_context is not None:
        assert original_context in rev.prior_action_summary

    # Fingerprint unchanged — refine doesn't touch structural fields
    assert compute_fingerprint(p) == original_fingerprint


def test_apply_refinement_skips_action_context_for_non_prose_kinds(tmp_path: Path):
    """ConfigPatch has no prose context. Refine still updates problem +
    admin_surface_summary, but the action object is untouched."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"k": "v"}))
    p = make_config_patch_proposal(
        target_path=f"{target}::k",
        value="new",
    )
    original_action_value = p.action.value

    result = RefineResult(
        ok=True,
        new_problem="Tighter problem statement",
        new_admin_surface_summary="tighter",
        new_action_context="this should be ignored for ConfigPatch",
    )
    apply_refinement(p, result, feedback="tighten", actor="user")

    assert p.problem == "Tighter problem statement"
    # Action object untouched even though new_action_context was set in
    # the result — ConfigPatch has no context field to write into.
    assert p.action.value == original_action_value


def test_apply_refinement_rejects_failed_result():
    p = make_investigation_proposal()
    result = RefineResult(ok=False, error="parse_failure")
    with pytest.raises(ValueError):
        apply_refinement(p, result, feedback="x", actor="user")


def test_multiple_refinements_accumulate():
    p = make_investigation_proposal()
    for i in range(3):
        apply_refinement(
            p,
            RefineResult(
                ok=True,
                new_problem=f"problem v{i + 1}",
                new_admin_surface_summary=f"summary v{i + 1}",
                new_action_context=f"context v{i + 1}",
            ),
            feedback=f"round {i + 1}",
            actor="user",
        )
    assert len(p.revisions) == 3
    assert p.problem == "problem v3"
    assert p.revisions[0].prior_problem != p.revisions[2].prior_problem
    # Earliest revision's prior is the original; latest's prior is v2.
    assert p.revisions[2].prior_problem == "problem v2"


# ──────────────────────────────────────────────────────────────────────────
# Schema roundtrip — revisions survive serialization
# ──────────────────────────────────────────────────────────────────────────


def test_proposal_with_revisions_roundtrips_through_dict():
    from schema.proposal import Proposal

    p = make_investigation_proposal()
    apply_refinement(
        p,
        RefineResult(
            ok=True,
            new_problem="revised",
            new_admin_surface_summary="rev",
            new_action_context="ctx",
        ),
        feedback="make it shorter",
        actor="user:pod_admin",
    )
    blob = p.to_dict()
    p2 = Proposal.from_dict(blob)
    assert len(p2.revisions) == 1
    assert p2.revisions[0].feedback == "make it shorter"
    assert p2.revisions[0].actor == "user:pod_admin"
    assert p2.problem == "revised"
