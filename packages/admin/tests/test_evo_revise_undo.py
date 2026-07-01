"""Tests for ``evo revise --undo`` (Phase 2b.1 of Issue Inbox).

Strategy: stub the LLM reviser when we need to seed a revised intake,
then exercise the undo flow. Verifies the documented contract:

  - --undo with no revision_history → friendly noop, no mutation
  - --undo with one entry → restore prior, history empty
  - --undo with multiple entries → restore the most-recent, history -1
  - --undo can appear in any position relative to the id
  - --undo on a filed intake → rejected (same guard as regular revise)
  - --undo on a closed intake → rejected (same guard)
  - --undo doesn't accept an instruction (ignored if present)
  - usage hint mentions --undo when the operator just types the id
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


def _seed_intake(tmp_path: Path, *, id_="intake-undo-test",
                 body="[bug] Original\n\nOriginal body."):
    ix = Intake(id=id_, kind="bug", body=body)
    _store.write_intake(ix, tmp_path)
    return ix


def _seed_revised_intake(tmp_path: Path):
    """Helper: capture an intake, then run one revise so revision_history
    has exactly one entry. Returns the intake id."""
    _seed_intake(tmp_path)
    cls.set_reviser(lambda t, b, i, c: cls.ReviseVerdict(
        new_title="[bug] Revised",
        new_body="Revised body.",
        confidence=0.9, reasoning="condensed",
    ))
    render_revise(
        role="primary", bot_id="evo",
        args="intake-undo-test make it concise",
        network=_network(tmp_path),
    )
    return "intake-undo-test"


# ─── Argument parsing ─────────────────────────────────────────────────────


def test_parse_undo_flag_after_id():
    assert _parse_revise_args("intake-x --undo") == ("intake-x", "", True)


def test_parse_undo_flag_before_id():
    """Operator might type `--undo` first; tolerate either order."""
    assert _parse_revise_args("--undo intake-x") == ("intake-x", "", True)


def test_parse_undo_flag_mixed_with_instruction():
    """If both --undo and an instruction are present, --undo wins —
    the handler will route to the undo path and ignore the instruction.
    Pin the parser's return shape so the handler logic stays right."""
    parsed = _parse_revise_args("intake-x --undo make it concise")
    assert parsed[0] == "intake-x"
    assert parsed[2] is True


def test_parse_no_undo_flag_returns_false():
    assert _parse_revise_args("intake-x make it concise") == (
        "intake-x", "make it concise", False,
    )


def test_parse_empty_returns_false_undo():
    assert _parse_revise_args("") == ("", "", False)


def test_parse_only_undo_flag_no_id():
    """`--undo` alone with no id should not produce a usable id."""
    assert _parse_revise_args("--undo") == ("", "", True)


# ─── Empty history ────────────────────────────────────────────────────────


def test_undo_with_empty_history_is_noop(tmp_path):
    """Fresh-captured intake has no revisions to walk back."""
    _seed_intake(tmp_path)
    r = render_revise(
        role="primary", bot_id="evo",
        args="intake-undo-test --undo",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "Nothing to undo" in body
    # No mutation.
    reloaded, _, _ = _store.find_intake(tmp_path, "intake-undo-test")
    assert "Original" in reloaded.body
    assert reloaded.revision_history == []


# ─── Single undo ──────────────────────────────────────────────────────────


def test_undo_restores_prior_body_and_clears_history_entry(tmp_path):
    intake_id = _seed_revised_intake(tmp_path)
    # Sanity: after revise, body is the revised version and history has 1.
    pre, _, _ = _store.find_intake(tmp_path, intake_id)
    assert "Revised" in pre.body
    assert len(pre.revision_history) == 1

    r = render_revise(
        role="primary", bot_id="evo",
        args=f"{intake_id} --undo",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "Undone" in body
    assert "[bug] Original" in body

    post, _, _ = _store.find_intake(tmp_path, intake_id)
    assert "[bug] Original" in post.body
    assert "Original body" in post.body
    assert "Revised" not in post.body
    assert post.revision_history == []


def test_undo_response_mentions_promote_command(tmp_path):
    """The undo preview should tell the operator how to file the
    restored draft — completes the chat → file loop."""
    intake_id = _seed_revised_intake(tmp_path)
    r = render_revise(
        role="primary", bot_id="evo",
        args=f"{intake_id} --undo",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert f"evo intake promote {intake_id}" in body


# ─── Multiple revisions ───────────────────────────────────────────────────


def test_undo_walks_back_one_step_at_a_time(tmp_path):
    """Chain: revise → revise → undo → undo. Each undo restores the
    immediately-prior version, not the original."""
    _seed_intake(tmp_path)

    cls.set_reviser(lambda t, b, i, c: cls.ReviseVerdict(
        new_title="[bug] Round 1", new_body="Body v1",
        confidence=0.9, reasoning="r1",
    ))
    render_revise(role="primary", bot_id="evo",
                  args="intake-undo-test round 1",
                  network=_network(tmp_path))

    cls.set_reviser(lambda t, b, i, c: cls.ReviseVerdict(
        new_title="[bug] Round 2", new_body="Body v2",
        confidence=0.9, reasoning="r2",
    ))
    render_revise(role="primary", bot_id="evo",
                  args="intake-undo-test round 2",
                  network=_network(tmp_path))

    pre, _, _ = _store.find_intake(tmp_path, "intake-undo-test")
    assert "Body v2" in pre.body
    assert len(pre.revision_history) == 2

    # First undo restores Round 1.
    render_revise(role="primary", bot_id="evo",
                  args="intake-undo-test --undo",
                  network=_network(tmp_path))
    after1, _, _ = _store.find_intake(tmp_path, "intake-undo-test")
    assert "Body v1" in after1.body
    assert "Body v2" not in after1.body
    assert len(after1.revision_history) == 1

    # Second undo restores the original capture.
    render_revise(role="primary", bot_id="evo",
                  args="intake-undo-test --undo",
                  network=_network(tmp_path))
    after2, _, _ = _store.find_intake(tmp_path, "intake-undo-test")
    assert "Original" in after2.body
    assert "Body v1" not in after2.body
    assert after2.revision_history == []


def test_undo_then_revise_again_works(tmp_path):
    """After undo, a fresh revise should layer onto the restored
    version, not the (popped) prior."""
    intake_id = _seed_revised_intake(tmp_path)
    # Undo back to original.
    render_revise(role="primary", bot_id="evo",
                  args=f"{intake_id} --undo",
                  network=_network(tmp_path))

    # New revise: starts from Original, produces a different revision.
    cls.set_reviser(lambda t, b, i, c: cls.ReviseVerdict(
        new_title="[bug] Different", new_body="Different body.",
        confidence=0.9, reasoning="layered",
    ))
    render_revise(role="primary", bot_id="evo",
                  args=f"{intake_id} reframe it",
                  network=_network(tmp_path))

    final, _, _ = _store.find_intake(tmp_path, intake_id)
    assert "Different" in final.body
    assert len(final.revision_history) == 1
    # The new revision's prior_body is the Original, not the Revised
    # (which we undid). This is the contract that makes undo "useful":
    # rewinding to the right starting point.
    assert "Original" in final.revision_history[0]["prior_body"]


# ─── State guards (mirror the revise guards) ──────────────────────────────


def test_undo_on_filed_intake_rejected(tmp_path):
    """Same as revise: a filed intake's draft is no longer the source
    of truth; the GitHub thread is. Don't let the operator silently
    blow away their post by undoing into a different version."""
    intake_id = _seed_revised_intake(tmp_path)
    ix, _, _ = _store.find_intake(tmp_path, intake_id)
    ix.promotion.github_issue_url = "https://github.com/x/y/issues/1"
    ix.promotion.github_issue_number = 1
    _store.transition(ix, to="filed", shared_dir=tmp_path)

    r = render_revise(
        role="primary", bot_id="evo",
        args=f"{intake_id} --undo",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "already filed" in body
    # No mutation.
    reloaded, _, _ = _store.find_intake(tmp_path, intake_id)
    assert len(reloaded.revision_history) == 1


def test_undo_on_closed_intake_rejected(tmp_path):
    intake_id = _seed_revised_intake(tmp_path)
    ix, _, _ = _store.find_intake(tmp_path, intake_id)
    _store.transition(ix, to="closed", shared_dir=tmp_path)

    r = render_revise(
        role="primary", bot_id="evo",
        args=f"{intake_id} --undo",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "closed" in body.lower()


# ─── Usage hint mentions --undo ───────────────────────────────────────────


def test_usage_hint_mentions_undo_flag(tmp_path):
    """Bare `evo revise` (no args) → usage. Pin that --undo is
    documented so operators discover it."""
    r = render_revise(
        role="primary", bot_id="evo", args="",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "--undo" in body


def test_missing_instruction_hint_mentions_undo(tmp_path):
    """`evo revise <id>` without an instruction should also mention undo
    in the hint — that's where an operator who realized "I want to
    rewind" is most likely to look."""
    _seed_intake(tmp_path)
    r = render_revise(
        role="primary", bot_id="evo",
        args="intake-undo-test",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "--undo" in body


# ─── Undo doesn't accept an instruction (logically) ───────────────────────


def test_undo_with_instruction_takes_undo_path(tmp_path):
    """`evo revise <id> --undo make it concise` — --undo wins. The
    instruction is silently ignored; we don't bother flagging it
    because the operator's intent (walk back) is clear and we don't
    want to be pedantic."""
    intake_id = _seed_revised_intake(tmp_path)
    r = render_revise(
        role="primary", bot_id="evo",
        args=f"{intake_id} --undo make it concise",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    # We routed to undo (not revise) — verify by the response shape.
    assert "Undone" in body
    # And the intake is back to the original, not "made concise".
    post, _, _ = _store.find_intake(tmp_path, intake_id)
    assert "Original" in post.body
    assert post.revision_history == []
