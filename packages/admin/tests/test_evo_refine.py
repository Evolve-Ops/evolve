"""tests/test_evo_refine.py — Refine in chat (Step 4 of evo unify plan).

The user gives substantive feedback on a pitched proposal ("less
aggressive about the threshold", "include the cron context") instead
of yes / no / snooze. The bot's LLM rewrites the proposal's prose
fields in place; the wizard re-pitches the revised version so the user
can accept / reject / iterate again.

Three concerns:

1. **Bridge: refine_proposal**
   Loads the proposal, calls ``arbiter.refine``, persists the updated
   proposal with a ProposalRevision audit entry, returns the refreshed
   Recommendation dict for re-pitching.

2. **Intent: refine action**
   Stage 2 LLM classifies refine when ``allow_refine=True``.
   ``allow_refine`` is False on in-process surface (refining an item
   the user already accepted feels wrong) and on non-proposal recs
   (nothing to refine).

3. **Engine: refine routing**
   Substantive-feedback reply triggers the bridge call. State is
   refreshed with the revised rec; subsequent turn pitches the new
   version.
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
# Bridge: refine_proposal
# ──────────────────────────────────────────────────────────────────────────


def _persist_pending_investigation(tmp_path: Path, *, bot_id: str = "team_bot_a") -> str:
    from arbiter import state_machine as _sm
    from arbiter import store as _store
    from testing.harness import make_investigation_proposal

    p = make_investigation_proposal(bot_id=bot_id)
    _sm.transition(p, "pending", actor="test")
    _store.write_proposal(p, tmp_path, subdir="pending")
    return p.id


def test_bridge_refine_rejects_empty_feedback(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import refine_proposal

    pid = _persist_pending_investigation(tmp_path)
    result = refine_proposal(tmp_path, pid, "   ", network={})
    assert not result.ok
    assert "empty" in result.message


def test_bridge_refine_rejects_missing_proposal(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import refine_proposal

    result = refine_proposal(tmp_path, "no-such-id", "less aggressive", network={})
    assert not result.ok
    assert "not found" in result.message


def test_bridge_refine_rejects_pod_wide_proposal(tmp_path: Path):
    """A pod-wide proposal (bot_id == "<pod>" sentinel) can't be refined —
    there's no bot account to bill the LLM call against. Bridge returns
    a pod-wide-shaped error rather than attempting the LLM call."""
    from unittest.mock import MagicMock
    from evolve_admin.evo.arbiter_bridge import refine_proposal

    # Pod-wide proposals are persisted with bot_id="<pod>" (truthy
    # sentinel); see proposal_synthesizer/synthesizer.py and
    # generators/evolve_watchdog/observe.py. Mock the lookup so we don't
    # have to construct one end-to-end.
    fake_proposal = MagicMock()
    fake_proposal.bot_id = "<pod>"
    fake_proposal.status = "pending"

    with patch(
        "arbiter.store.find_proposal",
        return_value=(fake_proposal, tmp_path / "fake.json", "pending"),
    ):
        result = refine_proposal(
            tmp_path, "p_pod_wide", "less aggressive", network={}
        )
    assert not result.ok
    assert "pod-wide" in result.message


def test_bridge_refine_calls_arbiter_and_persists_revision(tmp_path: Path):
    """End-to-end happy path with the LLM mocked. Verifies the bridge
    persists the refined proposal and returns the refreshed rec dict."""
    from arbiter import store as _store
    from evolve_admin.evo.arbiter_bridge import refine_proposal

    pid = _persist_pending_investigation(tmp_path)

    # Stub out the bot key reader so we don't need an auth-profiles.json
    # under tmp_path. Stub the LLM caller too.
    import evolve_admin.evo.arbiter_bridge as _bridge_mod

    fake_llm_response = (
        '{"problem": "Refined problem", '
        '"admin_surface_summary": "refined summary", '
        '"action_context": "Refined investigation context"}'
    )

    with patch(
        "arbiter.refine.read_bot_anthropic_key", return_value="sk-test-key"
    ), patch(
        "arbiter.refine.make_anthropic_caller",
        return_value=lambda _msg: fake_llm_response,
    ):
        result = refine_proposal(
            tmp_path, pid, "make it less aggressive", network={}
        )

    assert result.ok
    assert result.revision_count == 1
    assert result.rec_dict is not None
    # Refined content shows up in the returned rec — proposal_reader
    # maps proposal.admin_surface_summary → rec.title and
    # proposal.problem → rec.detail.
    assert result.rec_dict.get("title") == "refined summary"
    assert result.rec_dict.get("detail") == "Refined problem"

    # Persisted on disk: load the proposal back and verify it has a revision
    located = _store.find_proposal(tmp_path, pid)
    assert located is not None
    proposal = located[0]
    assert len(proposal.revisions) == 1
    assert proposal.revisions[0].feedback == "make it less aggressive"
    assert proposal.problem == "Refined problem"


# ──────────────────────────────────────────────────────────────────────────
# Intent: refine action
# ──────────────────────────────────────────────────────────────────────────


def test_intent_refine_only_available_when_allow_refine_true():
    """``parse_intent`` with ``allow_refine=False`` (default) doesn't
    classify any reply as refine. With ``allow_refine=True`` and a
    Stage 2 LLM result, refine is reachable."""
    from evolve_admin.evo.wizard import intent as _intent

    # Substitute a fake Stage 2 parser that always returns refine.
    def _always_refine(user_message, pitch_summary, allow_context, **kwargs):
        return _intent.IntentResult(
            action="refine",
            confidence=0.95,
            stage="stage2",
            rationale="test stub",
        )

    _intent.set_intent_parser(_always_refine)
    try:
        # allow_refine=False → Stage 1 misses, Stage 2 ineligible (no
        # body) → unknown. Force a long-ish reply that's Stage 2 eligible.
        # We need the user_message to bypass Stage 1 (no keywords)
        # AND be Stage 2 eligible.
        msg = "actually I'd like a different framing for this please"
        r_off = _intent.parse_intent(msg, allow_refine=False)
        # The refine action shouldn't surface — even if our stub returns
        # it, the gate should validate. (Coerce_intent rejects "refine"
        # when allow_refine=False, downgrading to "unknown".) Note: our
        # stub bypasses _coerce_intent so we can't assert here; we
        # instead test the prompt-builder and coerce paths separately.

        # allow_refine=True → stub fires, refine reaches the caller
        r_on = _intent.parse_intent(msg, allow_refine=True)
        assert r_on.action == "refine"
        assert r_on.stage == "stage2"
    finally:
        _intent.set_intent_parser(None)


def test_intent_coerce_rejects_refine_when_not_allowed():
    from evolve_admin.evo.wizard.intent import _coerce_intent

    parsed = {"action": "refine", "confidence": 0.9}
    # allow_refine=False (default) → refine isn't in valid_actions, falls
    # to unknown
    result = _coerce_intent(parsed, allow_context=True)
    assert result.action == "unknown"

    # allow_refine=True → refine is honored
    result = _coerce_intent(parsed, allow_context=True, allow_refine=True)
    assert result.action == "refine"


def test_intent_prompt_only_lists_refine_when_allowed():
    from evolve_admin.evo.wizard.intent import _build_intent_system_prompt

    p_off = _build_intent_system_prompt(
        "test pitch", allow_context=True, allow_refine=False
    )
    p_on = _build_intent_system_prompt(
        "test pitch", allow_context=True, allow_refine=True
    )
    assert "refine" not in p_off.lower() or '"refine"' not in p_off
    assert '"refine"' in p_on
    assert "substantive feedback" in p_on.lower()


# ──────────────────────────────────────────────────────────────────────────
# Engine routing: refine fires the bridge and re-pitches
# ──────────────────────────────────────────────────────────────────────────


def _make_proposal_rec(rec_id: str, proposal_id: str) -> dict:
    """Build a Recommendation-shaped dict that looks proposal-derived."""
    return {
        "id": rec_id,
        "title": "Original title",
        "detail": "Original detail",
        "context": "",
        "tags": [],
        "source": "generator:budget_hawk",
        "source_ref": {"proposal_id": proposal_id, "bot_id": "team_bot_a"},
        "action_kind": "Investigation",
    }


def _seed_inbox_session(tmp_path: Path, rec: dict):
    """Initialize a rec_pending wizard session pointing at ``rec``,
    inbox surface. Mirrors what ``start_rec_pending`` would do."""
    from evolve_admin.evo.wizard import phases as _phases
    from evolve_admin.evo.wizard import state as _state

    st = _state.initialize(
        tmp_path,
        bot_id="team_bot_a",
        user_key="ext:telegram:123",
        audience="approver",
        initial_phase=_phases.PHASE_REC_PENDING,
    )
    st.extracted["_pending_rec"] = dict(rec)
    st.extracted["_initial_surface"] = "member_bot"
    st.extracted["_initial_scope_id"] = "team_bot_a"
    st.extracted["_pending_kind"] = "inbox"
    _state.write_state(tmp_path, st)
    return st


def test_engine_routes_refine_to_bridge_and_repitches(tmp_path: Path):
    """End-to-end: substantive feedback in chat → bridge.refine_proposal
    → state's _pending_rec gets the refreshed content → next pitch
    shows the new version."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import intent as _intent
    from evolve_admin.evo.wizard import state as _state
    from evolve_admin.evo.arbiter_bridge import RefineBridgeResult

    rec = _make_proposal_rec("rec_inbox_1", "p_001")
    _seed_inbox_session(tmp_path, rec)

    # Stage 2 stub: classify any non-keyword reply as refine.
    def _stub_intent(user_message, pitch_summary, allow_context, **kwargs):
        if kwargs.get("allow_refine"):
            return _intent.IntentResult(
                action="refine",
                confidence=0.95,
                stage="stage2",
                rationale="test stub",
            )
        return _intent.IntentResult(
            action="unknown", confidence=0.0, stage="stage2"
        )

    _intent.set_intent_parser(_stub_intent)

    # Bridge stub: refine returns refreshed rec dict
    refreshed_rec = dict(rec)
    refreshed_rec["title"] = "Revised title"
    refreshed_rec["detail"] = "Revised detail"

    try:
        with patch(
            "evolve_admin.evo.arbiter_bridge.refine_proposal",
            return_value=RefineBridgeResult(
                ok=True, rec_dict=refreshed_rec, revision_count=1
            ),
        ) as mock_refine:
            result = _engine.process_turn(
                tmp_path,
                bot_id="team_bot_a",
                user_key="ext:telegram:123",
                user_message="actually, can you make it less aggressive about the threshold",
                network={},
            )
    finally:
        _intent.set_intent_parser(None)

    assert result is not None
    assert not result.completed  # session continues — re-pitch coming
    mock_refine.assert_called_once()
    args, kwargs = mock_refine.call_args
    # The user's message is passed as feedback
    assert "less aggressive" in (args[2] if len(args) > 2 else kwargs.get("feedback", ""))

    # State now carries the refreshed rec
    st = _state.read_state(tmp_path, "team_bot_a", "ext:telegram:123")
    assert st is not None
    assert st.extracted["_pending_rec"]["title"] == "Revised title"


def test_engine_refine_without_proposal_id_falls_to_clarify(tmp_path: Path):
    """A non-proposal rec (onboarding, scoreboard, etc.) carries no
    proposal_id. Refine isn't a valid action for these — the gate
    should keep ``allow_refine=False`` so the LLM can't even classify
    refine. We verify this by checking that the bridge is never called."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import intent as _intent

    # Onboarding-shaped rec (no source_ref → no proposal_id)
    rec = {
        "id": "rec_onboarding_1",
        "title": "Welcome",
        "detail": "...",
        "source": "onboarding",
        "tags": [],
    }
    _seed_inbox_session(tmp_path, rec)

    # Even if a stub classifier tries to return refine, _coerce_intent
    # would downgrade it because allow_refine=False (no proposal_id).
    # Easier check: stub returns unknown, verify bridge.refine_proposal
    # is never called.
    def _stub_intent(user_message, pitch_summary, allow_context, **kwargs):
        # Verify the engine passed allow_refine=False (the gate worked)
        assert kwargs.get("allow_refine") is False, (
            f"allow_refine should be False for non-proposal rec; "
            f"got kwargs={kwargs}"
        )
        return _intent.IntentResult(
            action="unknown", confidence=0.0, stage="stage2"
        )

    _intent.set_intent_parser(_stub_intent)

    try:
        with patch(
            "evolve_admin.evo.arbiter_bridge.refine_proposal"
        ) as mock_refine:
            _engine.process_turn(
                tmp_path,
                bot_id="team_bot_a",
                user_key="ext:telegram:123",
                user_message="actually, can you make it different",
                network={},
            )
    finally:
        _intent.set_intent_parser(None)

    mock_refine.assert_not_called()


def test_engine_refine_in_process_blocked(tmp_path: Path):
    """In-process surface (Investigation already accepted) shouldn't
    allow refine — the user should mark complete or dismiss, not
    iterate. Verify allow_refine=False is passed to the intent parser."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import intent as _intent
    from evolve_admin.evo.wizard import state as _state
    from evolve_admin.evo.wizard import phases as _phases

    rec = _make_proposal_rec("rec_inproc_1", "p_inproc_001")
    st = _state.initialize(
        tmp_path,
        bot_id="team_bot_a",
        user_key="ext:telegram:123",
        audience="approver",
        initial_phase=_phases.PHASE_REC_PENDING,
    )
    st.extracted["_pending_rec"] = dict(rec)
    st.extracted["_initial_surface"] = "member_bot"
    st.extracted["_initial_scope_id"] = "team_bot_a"
    st.extracted["_pending_kind"] = "inprocess"  # ← in-process surface
    _state.write_state(tmp_path, st)

    def _stub_intent(user_message, pitch_summary, allow_context, **kwargs):
        # Verify the engine refused refine for in-process surface
        assert kwargs.get("allow_refine") is False, (
            f"allow_refine should be False for in-process surface; "
            f"got kwargs={kwargs}"
        )
        # Verify allow_complete IS True (in-process)
        assert kwargs.get("allow_complete") is True
        return _intent.IntentResult(
            action="unknown", confidence=0.0, stage="stage2"
        )

    _intent.set_intent_parser(_stub_intent)

    try:
        _engine.process_turn(
            tmp_path,
            bot_id="team_bot_a",
            user_key="ext:telegram:123",
            user_message="actually can you make this different",
            network={},
        )
    finally:
        _intent.set_intent_parser(None)


def test_engine_refine_failure_falls_to_clarify(tmp_path: Path):
    """If the bridge refine call fails (LLM error, API key missing,
    etc.), the wizard re-pitches a clarify variant rather than
    crashing or eating the user's turn."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import intent as _intent
    from evolve_admin.evo.arbiter_bridge import RefineBridgeResult

    rec = _make_proposal_rec("rec_fail", "p_fail")
    _seed_inbox_session(tmp_path, rec)

    def _stub_intent(user_message, pitch_summary, allow_context, **kwargs):
        return _intent.IntentResult(
            action="refine",
            confidence=0.95,
            stage="stage2",
            rationale="test stub",
        )

    _intent.set_intent_parser(_stub_intent)

    try:
        with patch(
            "evolve_admin.evo.arbiter_bridge.refine_proposal",
            return_value=RefineBridgeResult(
                ok=False, message="simulated LLM failure"
            ),
        ):
            result = _engine.process_turn(
                tmp_path,
                bot_id="team_bot_a",
                user_key="ext:telegram:123",
                user_message="please rewrite",
                network={},
            )
    finally:
        _intent.set_intent_parser(None)

    assert result is not None
    assert not result.completed  # session continues; user can try again
