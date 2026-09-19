"""tests/test_evo_in_process_followup.py — In-process surfacing + complete action.

Step 3 of the evo unify plan. Three concerns:

1. **Bridge: complete_proposal + list_in_process_for_bot**
   The arbiter bridge can mark a manual-completion proposal as done
   (applied → succeeded), and the dispatcher can ask "what's currently
   in this user's In Process queue?" without going through the
   BetterEngine adapter.

2. **Intent: complete action**
   The Stage 1 keyword classifier recognizes done / finished / handled
   it. Gated on ``allow_complete=True`` so accidental "I'm done!" on an
   inbox pitch doesn't fire a wrong-shaped transition.

3. **Dispatch + engine: in-process surfacing**
   Bare ``evo`` with in-process work checks in on those FIRST
   (inprocess_followup variant) before pulling from the inbox.
   ``yes/done`` on an in-process surface routes to bridge.complete_proposal.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


def _persist_pending_investigation(tmp_path: Path, *, bot_id: str = "team_bot_a") -> str:
    from arbiter import state_machine as _sm
    from arbiter import store as _store
    from testing.harness import make_investigation_proposal

    p = make_investigation_proposal(bot_id=bot_id)
    _sm.transition(p, "pending", actor="test")
    _store.write_proposal(p, tmp_path, subdir="pending")
    return p.id


def _persist_applied_investigation(
    tmp_path: Path, *, bot_id: str = "team_bot_a"
) -> str:
    """Manual-completion (Investigation) sitting in applied/ — i.e. an
    item the user already accepted via the inbox flow and that's now in
    their In Process queue."""
    from arbiter import state_machine as _sm
    from arbiter import store as _store
    from testing.harness import make_investigation_proposal

    p = make_investigation_proposal(bot_id=bot_id)
    _sm.transition(p, "pending", actor="test")
    _sm.transition(p, "approved_human", actor="test")
    _sm.transition(p, "applied", actor="test")
    _store.write_proposal(p, tmp_path, subdir="applied")
    return p.id


# ──────────────────────────────────────────────────────────────────────────
# Bridge: complete_proposal
# ──────────────────────────────────────────────────────────────────────────


def test_complete_proposal_transitions_applied_to_succeeded(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import complete_proposal

    pid = _persist_applied_investigation(tmp_path)
    result = complete_proposal(tmp_path, pid)

    assert result.ok
    assert result.new_status == "succeeded"
    # Lands in archived/ (the destination subdir for terminal states).
    assert (tmp_path / "proposals" / "archived" / f"{pid}.json").exists()
    assert not (tmp_path / "proposals" / "applied" / f"{pid}.json").exists()


def test_complete_proposal_rejects_pending_status(tmp_path: Path):
    """A proposal still in pending hasn't been accepted yet — complete
    isn't a valid transition for it. Bridge returns clean error."""
    from evolve_admin.evo.arbiter_bridge import complete_proposal

    pid = _persist_pending_investigation(tmp_path)
    result = complete_proposal(tmp_path, pid)

    assert not result.ok
    assert "applied" in result.message  # "expected 'applied'"


def test_complete_proposal_returns_clean_for_missing_id(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import complete_proposal

    result = complete_proposal(tmp_path, "no-such-proposal")
    assert not result.ok


# ──────────────────────────────────────────────────────────────────────────
# Bridge: list_in_process_for_bot
# ──────────────────────────────────────────────────────────────────────────


def test_list_in_process_returns_only_manual_completion_kinds(
    tmp_path: Path,
):
    """Investigation + WorkflowInstruction in applied/ count.
    ConfigPatch in applied/ (auto-applied, awaiting verify) does NOT
    count — that's autonomous, not awaiting operator action."""
    from arbiter import state_machine as _sm
    from arbiter import store as _store
    from evolve_admin.evo.arbiter_bridge import list_in_process_for_bot
    from testing.harness import (
        make_config_patch_proposal,
        make_investigation_proposal,
    )

    target = tmp_path / "cfg.json"
    target.write_text("{}")

    # Investigation in applied/ → counts
    p_inv = make_investigation_proposal(bot_id="team_bot_a")
    _sm.transition(p_inv, "pending", actor="t")
    _sm.transition(p_inv, "approved_human", actor="t")
    _sm.transition(p_inv, "applied", actor="t")
    _store.write_proposal(p_inv, tmp_path, subdir="applied")

    # ConfigPatch in applied/ → does NOT count (autonomous)
    p_cfg = make_config_patch_proposal(
        target_path=f"{target}::k", value="v", bot_id="team_bot_a"
    )
    _sm.transition(p_cfg, "pending", actor="t")
    _sm.transition(p_cfg, "approved_auto", actor="t")
    _sm.transition(p_cfg, "applied", actor="t")
    _store.write_proposal(p_cfg, tmp_path, subdir="applied")

    in_process = list_in_process_for_bot(tmp_path, "team_bot_a")
    ids = [p.id for p in in_process]
    assert p_inv.id in ids
    assert p_cfg.id not in ids


def test_list_in_process_filters_by_bot(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import list_in_process_for_bot

    pid_team_bot_a = _persist_applied_investigation(tmp_path, bot_id="team_bot_a")
    pid_admin_bot = _persist_applied_investigation(tmp_path, bot_id="admin_bot")

    team_bot_a_only = list_in_process_for_bot(tmp_path, "team_bot_a")
    assert [p.id for p in team_bot_a_only] == [pid_team_bot_a]

    admin_bot_only = list_in_process_for_bot(tmp_path, "admin_bot")
    assert [p.id for p in admin_bot_only] == [pid_admin_bot]


def test_list_in_process_empty_when_nothing_applied(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import list_in_process_for_bot

    assert list_in_process_for_bot(tmp_path, "team_bot_a") == []


# ──────────────────────────────────────────────────────────────────────────
# Intent: complete action
# ──────────────────────────────────────────────────────────────────────────


def test_intent_recognizes_done_when_complete_allowed():
    from evolve_admin.evo.wizard.intent import parse_intent

    for reply in ("done", "I'm done", "took care of it", "all done", "finished it"):
        result = parse_intent(reply, allow_complete=True)
        assert result.action == "complete", (reply, result)


def test_intent_falls_through_when_complete_not_allowed():
    """An inbox pitch (allow_complete=False) shouldn't accept "done"
    as a transition — it's ambiguous on a fresh pitch. Falls to
    unknown so the bot asks for clarification."""
    from evolve_admin.evo.wizard.intent import parse_intent

    result = parse_intent("I'm done", allow_complete=False)
    assert result.action == "unknown"


def test_intent_complete_priority_over_accept():
    """'I'm done' tokens overlap with 'accept' (both convey
    affirmation). The classifier must pick complete first when the
    user is in an in-process surface."""
    from evolve_admin.evo.wizard.intent import parse_intent

    result = parse_intent("yes I'm done", allow_complete=True)
    assert result.action == "complete"


# ──────────────────────────────────────────────────────────────────────────
# Engine: complete action routes to bridge.complete_proposal
# ──────────────────────────────────────────────────────────────────────────


def _proposal_rec(proposal_id: str, *, rec_id: str = "rec_p1") -> dict:
    return {
        "id": rec_id,
        "source": "generator:budget_hawk",
        "source_ref": {"proposal_id": proposal_id, "bot_id": "team_bot_a"},
    }


def test_record_rec_action_complete_routes_to_complete_proposal(
    tmp_path: Path,
):
    from evolve_admin.evo.wizard.engine import _record_rec_action

    rec = _proposal_rec("p_in_process_001")

    with patch(
        "evolve_admin.evo.arbiter_bridge.complete_proposal"
    ) as mock_complete, patch(
        "evolve_admin.better_engine.engine.BetterEngine"
    ) as MockEngine:
        engine_instance = MagicMock()
        MockEngine.return_value = engine_instance

        _record_rec_action(
            tmp_path,
            rec=rec,
            action="complete",
            snooze_days_hint=None,
            network={},
        )

        mock_complete.assert_called_once_with(tmp_path, "p_in_process_001")
        # Complete is recorded as a positive learning signal (= accepted)
        # so the BetterEngine model picks up that the user welcomed this
        # type of proposal.
        engine_instance.record_feedback.assert_called_once_with(
            "rec_p1", "accepted"
        )


# ──────────────────────────────────────────────────────────────────────────
# Dispatch: in-process surfacing
# ──────────────────────────────────────────────────────────────────────────


def test_dispatch_surfaces_in_process_before_inbox(tmp_path: Path, monkeypatch):
    """When the user has an in-process item, ``evo better`` shows that
    one instead of pulling from the inbox. The wizard returns a
    session_id (so the plugin routes follow-up turns) and the notes
    record kind=inprocess."""
    from evolve_admin.evo import dispatch

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}

    # Drop an applied Investigation in /proposals/applied/ for team_bot_a.
    _persist_applied_investigation(tmp_path, bot_id="team_bot_a")

    # Stub BetterEngine so we'd notice if the dispatcher reached for it
    # (when in-process is non-empty, it shouldn't).
    inbox_called = {"hit": False}

    class _StubEngine:
        def __init__(self, *a, **kw):
            pass

        def get_top(self, surface="admin", scope_id=None):
            inbox_called["hit"] = True
            return None

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)

    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="123", raw_text="evo better",
    )

    assert r.mode == "speak"
    assert r.wizard_session_id  # non-empty
    # In-process surfaced; inbox not consulted
    assert not inbox_called["hit"]
    assert any("kind=inprocess" in note for note in r.notes), r.notes


def test_dispatch_falls_back_to_inbox_when_no_in_process(tmp_path: Path, monkeypatch):
    """When the In Process queue is empty for this bot, ``evo better``
    proceeds to the inbox flow as before."""
    from evolve_admin.evo import dispatch

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}
    # No in-process items.

    inbox_called = {"hit": False}

    class _StubEngine:
        def __init__(self, *a, **kw):
            pass

        def get_top(self, surface="admin", scope_id=None):
            inbox_called["hit"] = True
            return None  # empty queue

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)

    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="123", raw_text="evo better",
    )

    assert r.mode == "speak"
    assert inbox_called["hit"]  # inbox WAS consulted
    assert any("kind=inbox" in note for note in r.notes), r.notes
