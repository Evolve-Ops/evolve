"""Tests for the ``evo revise`` handler (Phase 2 of Issue Inbox).

Strategy: stub the LLM reviser; verify the handler's contract for each
of the documented paths:

  - Happy path: existing intake + good instruction → body rewritten,
    revision_history grows, response includes the new draft and the
    promote command
  - Empty args / missing instruction → usage hint, no mutation
  - Unknown intake id → friendly not-found, no mutation
  - Filed intake → rejected with GitHub-replies-are-source-of-truth message
  - Closed intake → rejected with capture-a-fresh-intake nudge
  - Low-confidence reviser verdict → ask-to-clarify, no mutation
  - Empty new body from reviser → reject + don't pollute revision_history
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.evo.handlers.improve import (  # noqa: E402
    _parse_revise_args,
    _split_title_body,
    _join_title_body,
    render_revise,
)
from evolve_admin.intake import classifier as cls  # noqa: E402
from evolve_admin.intake import store as _store  # noqa: E402
from evolve_admin.intake.envelope import Intake  # noqa: E402


@pytest.fixture(autouse=True)
def reset_seams():
    yield
    cls.set_reviser(None)


def _network(tmp_path: Path) -> dict:
    return {"sharedDir": str(tmp_path), "primary": "evo"}


def _stub_reviser(*, new_title="[bug] Revised", new_body="Revised body.",
                  reasoning="ok", confidence=0.9):
    cls.set_reviser(lambda t, b, instr, ctx: cls.ReviseVerdict(
        new_title=new_title, new_body=new_body,
        reasoning=reasoning, confidence=confidence,
    ))


def _seed_open_intake(tmp_path: Path, *, id_="intake-revise-test",
                     body="[bug] Original title\n\nOriginal body content."):
    ix = Intake(id=id_, kind="bug", body=body)
    _store.write_intake(ix, tmp_path)
    return ix


# ─── Argument parsing ─────────────────────────────────────────────────────
#
# Updated for Phase 2b.1: _parse_revise_args returns a 3-tuple
# ``(intake_id, instruction, undo_flag)``; --undo specifics are covered
# in test_evo_revise_undo.py. These tests pin the non-undo paths.


def test_parse_revise_args_id_and_instruction():
    assert _parse_revise_args("intake-1 make it concise") == (
        "intake-1", "make it concise", False,
    )


def test_parse_revise_args_id_only():
    assert _parse_revise_args("intake-1") == ("intake-1", "", False)


def test_parse_revise_args_empty():
    assert _parse_revise_args("") == ("", "", False)
    assert _parse_revise_args("   ") == ("", "", False)


def test_parse_revise_args_instruction_with_multiple_words():
    assert _parse_revise_args("intake-1 add a reproduction steps section") == (
        "intake-1", "add a reproduction steps section", False,
    )


# ─── Title/body splitting ────────────────────────────────────────────────


def test_split_title_body_typical():
    title, body = _split_title_body("[bug] Title\n\nLine 1\nLine 2")
    assert title == "[bug] Title"
    assert body == "Line 1\nLine 2"


def test_split_title_body_single_line():
    """Intakes captured by `evo bug "X"` with a short message have no
    body — just the title line."""
    title, body = _split_title_body("team_bot_a broken")
    assert title == "team_bot_a broken"
    assert body == ""


def test_split_title_body_leading_blank_lines():
    """Defensive: malformed body with leading blank lines should still
    pick the first non-empty line as the title."""
    title, body = _split_title_body("\n\n[bug] Title\n\nbody")
    assert title == "[bug] Title"
    assert body == "body"


def test_join_title_body_round_trip():
    """A title + body should join with double-newline separator, the
    inverse of _split_title_body."""
    joined = _join_title_body("[bug] T", "B1\nB2")
    title, body = _split_title_body(joined)
    assert title == "[bug] T"
    assert body == "B1\nB2"


def test_join_title_body_empty_body():
    assert _join_title_body("title", "") == "title"


def test_join_title_body_empty_title():
    assert _join_title_body("", "body") == "body"


def test_join_title_body_both_empty():
    assert _join_title_body("", "") == ""


# ─── Empty / usage paths ──────────────────────────────────────────────────


def test_empty_args_shows_usage(tmp_path):
    r = render_revise(
        role="primary", bot_id="evo", args="",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "Usage" in body
    assert "evo revise" in body


def test_missing_instruction_prompts_for_one(tmp_path):
    """`evo revise intake-X` (no instruction) → tell me how to change it,
    don't blank-revise."""
    _seed_open_intake(tmp_path, id_="intake-x")
    r = render_revise(
        role="primary", bot_id="evo", args="intake-x",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "Tell me how to change" in body or "make it" in body.lower()


# ─── Not-found ────────────────────────────────────────────────────────────


def test_unknown_intake_id_friendly(tmp_path):
    """No intake with that id → friendly error, no exception, no mutation."""
    _stub_reviser()  # would mutate if it ran; we expect it NOT to run
    r = render_revise(
        role="primary", bot_id="evo",
        args="intake-does-not-exist do something",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "No intake" in body
    assert "intake-does-not-exist" in body


# ─── State guards ─────────────────────────────────────────────────────────


def test_filed_intake_rejected(tmp_path):
    """Once an intake is filed, the GitHub thread is the source of truth.
    Don't let the operator silently overwrite the local draft (which
    nobody will see again)."""
    ix = _seed_open_intake(tmp_path, id_="intake-filed")
    # Promote it manually (skipping the gh call) by writing transition.
    ix.promotion.github_issue_url = "https://github.com/x/y/issues/1"
    ix.promotion.github_issue_number = 1
    _store.transition(ix, to="filed", shared_dir=tmp_path)

    _stub_reviser()
    r = render_revise(
        role="primary", bot_id="evo",
        args="intake-filed make it shorter",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "already filed" in body
    assert "https://github.com/x/y/issues/1" in body
    # Confirm body NOT mutated.
    reloaded, _, _ = _store.find_intake(tmp_path, "intake-filed")
    assert "Original" in reloaded.body
    assert reloaded.revision_history == []


def test_closed_intake_rejected(tmp_path):
    ix = _seed_open_intake(tmp_path, id_="intake-closed")
    _store.transition(ix, to="closed", shared_dir=tmp_path)
    _stub_reviser()
    r = render_revise(
        role="primary", bot_id="evo",
        args="intake-closed do something",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "closed" in body.lower()
    assert "evo improve" in body  # nudge to capture fresh


# ─── Happy path ───────────────────────────────────────────────────────────


def test_happy_path_rewrites_body_and_records_history(tmp_path):
    _stub_reviser(
        new_title="[bug] Concise title",
        new_body="Tight body.",
        reasoning="condensed",
        confidence=0.9,
    )
    _seed_open_intake(tmp_path)
    r = render_revise(
        role="primary", bot_id="evo",
        args="intake-revise-test make it concise",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "Revised" in body
    assert "[bug] Concise title" in body
    assert "Tight body." in body
    assert "condensed" in body
    # Persisted to disk
    reloaded, _, _ = _store.find_intake(tmp_path, "intake-revise-test")
    assert "[bug] Concise title" in reloaded.body
    assert "Tight body." in reloaded.body
    assert "Original" not in reloaded.body
    # Revision history captured the prior version
    assert len(reloaded.revision_history) == 1
    h = reloaded.revision_history[0]
    assert h["instruction"] == "make it concise"
    assert h["prior_title"] == "[bug] Original title"
    assert "Original body content" in h["prior_body"]
    assert h["reasoning"] == "condensed"


def test_multiple_revisions_accumulate(tmp_path):
    """Each `evo revise` call adds one entry to revision_history."""
    _seed_open_intake(tmp_path)
    for i in range(3):
        _stub_reviser(
            new_title=f"[bug] Round {i+1}",
            new_body=f"Body v{i+1}",
            confidence=0.9,
        )
        render_revise(
            role="primary", bot_id="evo",
            args=f"intake-revise-test round {i+1}",
            network=_network(tmp_path),
        )
    reloaded, _, _ = _store.find_intake(tmp_path, "intake-revise-test")
    assert len(reloaded.revision_history) == 3
    # Each prior_body is the body BEFORE that round's edit.
    assert "Original" in reloaded.revision_history[0]["prior_body"]
    assert reloaded.revision_history[1]["prior_body"] == "Body v1"
    assert reloaded.revision_history[2]["prior_body"] == "Body v2"
    # And the current body is the latest revision.
    assert "Body v3" in reloaded.body


# ─── Low-confidence / empty-output guards ─────────────────────────────────


def test_low_confidence_asks_to_clarify_no_mutation(tmp_path):
    """A confidence-below-0.5 reviser response means the model
    couldn't execute the instruction confidently. Don't silently
    overwrite the draft — ask for a clearer instruction."""
    _stub_reviser(
        new_title="[bug] something",
        new_body="something",
        reasoning="ambiguous",
        confidence=0.3,
    )
    _seed_open_intake(tmp_path)
    r = render_revise(
        role="primary", bot_id="evo",
        args="intake-revise-test do the thing",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "not sure" in body.lower() or "clearer instruction" in body.lower()
    # No mutation.
    reloaded, _, _ = _store.find_intake(tmp_path, "intake-revise-test")
    assert "Original" in reloaded.body
    assert reloaded.revision_history == []


def test_empty_new_body_rejected_and_history_clean(tmp_path):
    """A reviser that returns both empty new_title AND empty new_body
    should be rejected. The revision_history append we did optimistically
    must be reverted so we don't leave a phantom entry."""
    _stub_reviser(new_title="", new_body="", confidence=0.9)
    _seed_open_intake(tmp_path)
    r = render_revise(
        role="primary", bot_id="evo",
        args="intake-revise-test clear everything",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "empty draft" in body.lower()
    # No mutation.
    reloaded, _, _ = _store.find_intake(tmp_path, "intake-revise-test")
    assert "Original" in reloaded.body
    assert reloaded.revision_history == []


# ─── Title preservation ──────────────────────────────────────────────────


def test_title_preserved_when_new_title_empty(tmp_path):
    """If the reviser returns empty new_title (i.e. "I didn't change the
    title"), the original title must stick. The classifier's coercion
    layer handles this, but pin the behavior at the handler level too."""
    _stub_reviser(new_title="", new_body="New body only.", confidence=0.9)
    _seed_open_intake(tmp_path)
    r = render_revise(
        role="primary", bot_id="evo",
        args="intake-revise-test rewrite the body only",
        network=_network(tmp_path),
    )
    reloaded, _, _ = _store.find_intake(tmp_path, "intake-revise-test")
    # The classifier coercion preserves current_title when new is empty.
    # The handler then joins it with new_body.
    assert "[bug] Original title" in reloaded.body or "Original" in reloaded.body
    assert "New body only" in reloaded.body
