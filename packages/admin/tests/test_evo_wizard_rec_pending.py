"""tests/test_evo_wizard_rec_pending.py — conversational approval (slice 5b8).

Spec: internal/spec-better-engine-conversational-approval-2026-04-18.md

Exercises:
  * intent.parse_stage1 — phrase + word matching, single-letter shortcuts,
    priority order, tokenization edge cases ("not sure" vs "no")
  * intent.parse_intent — stage 1 wins on hit, stage 2 fallback on miss,
    stage 2 disabled / ineligible inputs degrade to unknown
  * REC_PENDING_PHASE definition + presence in phase registry
  * engine.start_rec_pending — pitch path with rec, all-caught-up path
    when queue is empty, scratch fields stay underscore-prefixed
  * engine._handle_rec_pending — accept/reject/snooze/next routes through
    BetterEngine, chains to next rec or finalizes; context re-renders
    without state transition; unknown reply re-renders with clarify;
    second consecutive unknown finalizes
  * dispatch.dispatch — bare ``evo`` + ``evo better`` start a wizard
    session via start_rec_pending; engine-unavailable falls through to
    the empty-queue path
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# intent.parse_stage1 — deterministic classifier
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    # Accept — phrases + words
    ("yes", "accept"),
    ("yeah", "accept"),
    ("ok", "accept"),
    ("sure", "accept"),
    ("yes please", "accept"),
    ("go ahead", "accept"),
    ("looks good", "accept"),
    ("sounds good", "accept"),
    ("let's try it", "accept"),
    # Reject — phrases + words
    ("no", "reject"),
    ("nope", "reject"),
    ("skip", "reject"),
    ("pass", "reject"),
    ("not interested", "reject"),
    ("not for me", "reject"),
    ("no thanks", "reject"),
    # Snooze — phrases + words
    ("snooze", "snooze"),
    ("later", "snooze"),
    ("not now", "snooze"),
    ("remind me", "snooze"),
    ("ask me tomorrow", "snooze"),
    # Next
    ("next", "next"),
    ("more", "next"),
    ("show me another", "next"),
    ("anything else", "next"),
    # Context
    ("why", "context"),
    ("tell me more", "context"),
    ("more info", "context"),
    ("what does that mean", "context"),
])
def test_parse_stage1_keyword_hits(text, expected):
    from evolve_admin.evo.wizard.intent import parse_stage1

    r = parse_stage1(text, pending_rec_exists=True)
    assert r is not None, f"expected stage1 to hit on {text!r}"
    assert r.action == expected
    assert r.confidence == 1.0
    assert r.stage == "stage1"


def test_parse_stage1_misses_on_ambiguous():
    """Replies that don't carry any keyword cue should fall through to
    None so the caller can choose between stage 2 and clarification."""
    from evolve_admin.evo.wizard.intent import parse_stage1

    for text in ("hmm", "I don't know", "what's on your mind", ""):
        assert parse_stage1(text, pending_rec_exists=True) is None


def test_parse_stage1_no_substring_false_positives():
    """The phrase/word split protects against single-word ``no`` matching
    inside ``not sure``. Same invariant the wizard's GUIDE_CONFIRM gate
    enforces; we test it for the proposal-reply classifier too."""
    from evolve_admin.evo.wizard.intent import parse_stage1

    # "not sure" — neither tokens "no"/"nope" nor any reject phrase.
    # The token "not" isn't in the reject set.
    assert parse_stage1("not sure", pending_rec_exists=True) is None
    # "noisy" should NOT trigger reject on the substring "no".
    assert parse_stage1("noisy", pending_rec_exists=True) is None


def test_parse_stage1_single_letter_shortcuts_only_when_pending():
    """a/s/n only count as accept/snooze/next when there's a pending rec.
    Without that flag they're noise."""
    from evolve_admin.evo.wizard.intent import parse_stage1

    assert parse_stage1("a", pending_rec_exists=True).action == "accept"
    assert parse_stage1("s", pending_rec_exists=True).action == "snooze"
    assert parse_stage1("n", pending_rec_exists=True).action == "next"
    # Without pending flag → no match
    assert parse_stage1("a", pending_rec_exists=False) is None


def test_parse_stage1_priority_reject_before_accept():
    """When a reply contains both reject and accept cues, reject wins so
    'no thanks' doesn't get hijacked by an embedded 'thanks' ever
    drifting into ACCEPT_WORDS."""
    from evolve_admin.evo.wizard.intent import parse_stage1

    # The word "no" should win over any embedded "ok"-like token.
    # ("no it's ok") — depending on phrase order, but "no" alone wins.
    assert parse_stage1("no", pending_rec_exists=True).action == "reject"


# ─────────────────────────────────────────────────────────────────────────────
# intent.parse_intent — pipeline
# ─────────────────────────────────────────────────────────────────────────────


def _stage2_stub_factory(action="accept", confidence=0.95, snooze_hint_days=None):
    from evolve_admin.evo.wizard.intent import IntentResult

    def _stub(user_message, pitch_summary, allow_context):
        return IntentResult(
            action=action,
            confidence=confidence,
            snooze_hint_days=snooze_hint_days,
            rationale=f"stub: {action}",
            stage="stage2",
        )
    return _stub


def test_parse_intent_stage1_wins_over_stage2():
    """Stage 1 hits never reach stage 2; the stub should not fire."""
    from evolve_admin.evo.wizard import intent

    called = {"n": 0}

    def _stub(user_message, pitch_summary, allow_context):
        called["n"] += 1
        return intent.IntentResult(
            action="reject", confidence=0.99, stage="stage2",
        )

    intent.set_intent_parser(_stub)
    try:
        r = intent.parse_intent("yes", pitch_summary="x")
    finally:
        intent.set_intent_parser(None)

    assert r.action == "accept"
    assert r.stage == "stage1"
    assert called["n"] == 0


def test_parse_intent_stage2_fires_on_stage1_miss():
    """When stage 1 returns None, parse_intent calls the stage-2 parser."""
    from evolve_admin.evo.wizard import intent

    intent.set_intent_parser(_stage2_stub_factory(action="accept", confidence=0.92))
    try:
        r = intent.parse_intent("yeah let's give it a shot", pitch_summary="x")
    finally:
        intent.set_intent_parser(None)

    # "yeah" is in _ACCEPT_WORDS so stage 1 actually hits — pick a phrase
    # that won't hit stage 1.
    intent.set_intent_parser(_stage2_stub_factory(action="accept", confidence=0.92))
    try:
        r = intent.parse_intent("hmm I think that's worth trying actually", pitch_summary="x")
    finally:
        intent.set_intent_parser(None)
    assert r.action == "accept"
    assert r.stage == "stage2"
    assert r.confidence == pytest.approx(0.92)


def test_parse_intent_stage2_disabled_returns_unknown():
    from evolve_admin.evo.wizard import intent

    r = intent.parse_intent(
        "hmm could go either way", pitch_summary="x",
        llm_enabled=False,
    )
    assert r.action == "unknown"
    assert r.confidence == 0.0
    assert r.stage == "fallback"


def test_parse_intent_stage2_skipped_for_long_messages():
    from evolve_admin.evo.wizard import intent

    long_msg = "x " * 200  # > STAGE2_MAX_LENGTH chars
    r = intent.parse_intent(long_msg, pitch_summary="pitch")
    # No stage 1 keyword in the noise → skip to fallback because length
    # disqualifies stage 2.
    assert r.action == "unknown"
    assert r.stage == "fallback"


def test_parse_intent_stage2_skipped_for_code_blocks():
    from evolve_admin.evo.wizard import intent

    r = intent.parse_intent("```python\nprint(1)\n```", pitch_summary="x")
    assert r.action == "unknown"
    assert r.stage == "fallback"


def test_intent_result_clamps_confidence():
    """LLM occasionally returns 1.2 or strings; coerce shouldn't crash."""
    from evolve_admin.evo.wizard.intent import _coerce_intent

    r = _coerce_intent({"action": "accept", "confidence": 1.5}, allow_context=True)
    assert 0.0 <= r.confidence <= 1.0

    r = _coerce_intent({"action": "accept", "confidence": "0.8"}, allow_context=True)
    assert r.confidence == pytest.approx(0.8)

    r = _coerce_intent({"action": "accept", "confidence": "garbage"}, allow_context=True)
    assert r.confidence == 0.0


def test_intent_result_snooze_hint_extraction():
    from evolve_admin.evo.wizard.intent import _coerce_intent

    r = _coerce_intent(
        {"action": "snooze", "confidence": 0.9, "snooze_hint_days": 7},
        allow_context=True,
    )
    assert r.action == "snooze"
    assert r.snooze_hint_days == 7

    # Hint only honored for snooze
    r = _coerce_intent(
        {"action": "accept", "confidence": 0.9, "snooze_hint_days": 7},
        allow_context=True,
    )
    assert r.snooze_hint_days is None

    # Out-of-range hint dropped
    r = _coerce_intent(
        {"action": "snooze", "confidence": 0.9, "snooze_hint_days": 999},
        allow_context=True,
    )
    assert r.snooze_hint_days is None


def test_intent_result_unknown_action_when_invalid():
    from evolve_admin.evo.wizard.intent import _coerce_intent

    r = _coerce_intent(
        {"action": "destroy_everything", "confidence": 0.9}, allow_context=True,
    )
    assert r.action == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Phase definition
# ─────────────────────────────────────────────────────────────────────────────


def test_rec_pending_phase_registered():
    from evolve_admin.evo.wizard import phases

    p = phases.get_phase(phases.PHASE_REC_PENDING)
    assert p is not None
    assert p.name == "rec_pending"
    assert p.targets == ()  # engine special-cases this phase
    assert p.has_extractor is False
    assert p.next_phase is None  # terminal — engine routes


# ─────────────────────────────────────────────────────────────────────────────
# Engine — start_rec_pending + _handle_rec_pending
# ─────────────────────────────────────────────────────────────────────────────


def _make_rec(**overrides):
    """Minimal Recommendation.to_dict() shape — what start_rec_pending
    accepts. Mirrors fields the prompt builder reads."""
    base = {
        "id": "rec_001",
        "scope_id": "team_bot_a",
        "title": "Restart team_bot_a's stuck gateway",
        "detail": "The gateway has been wedged for 2 cycles; a kickstart should clear it.",
        "context": "Restart safe; no in-flight messages affected.",
        "member_bot_title": "Quick fix you might want",
        "member_bot_detail": "Your gateway looks stuck — want me to nudge it?",
        "accept_label": "restart",
        "tags": ["urgency:high"],
        "status": "pending",
    }
    base.update(overrides)
    return base


def _make_network(tmp_path):
    return {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "bots": {"team_bot_a": {}},
    }


def test_start_rec_pending_with_rec_initializes_session(tmp_path):
    from evolve_admin.evo.wizard import engine, state, phases

    rec = _make_rec()
    r = engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:123", rec=rec,
        surface="member_bot",
    )

    assert r.completed is False
    assert r.wizard_session_id == "ext:telegram:123"
    assert r.phase == phases.PHASE_REC_PENDING
    # rec_pending is verbatim/direct-send (2026-05-17): the user-facing
    # pitch text lives in direct_send_message; system_append carries
    # the "respond verbatim with this message" framing for the LLM
    # fallback path. The agenda-mode [EVO WIZARD] header is gone.
    assert r.direct_send_message is not None
    assert "Quick fix" in r.direct_send_message or "stuck gateway" in r.direct_send_message.lower()
    # system_append still references the pitch body (wrapped with the
    # verbatim instruction) so substring assertions stay loose.
    assert "verbatim" in r.system_append.lower()
    assert "Quick fix" in r.system_append or "stuck gateway" in r.system_append.lower()

    # State was persisted with scratch fields under underscore-prefix
    st = state.read_state(tmp_path, "team_bot_a", "ext:telegram:123")
    assert st is not None
    assert st.is_active()
    assert st.audience == "approver"
    assert st.extracted.get("_pending_rec", {}).get("id") == "rec_001"
    assert st.extracted.get("_initial_surface") == "member_bot"
    assert st.extracted.get("_initial_scope_id") == "team_bot_a"


def test_start_rec_pending_empty_queue_finalizes_immediately(tmp_path):
    from evolve_admin.evo.wizard import engine, state, phases

    r = engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:123", rec=None,
    )

    assert r.completed is True
    # plugin clears its session when wizard_session_id is null
    assert r.wizard_session_id is None
    assert r.phase == phases.PHASE_REC_PENDING
    assert "all caught up" in r.system_append.lower() or "good shape" in r.system_append.lower() or "queue empty" in r.system_append.lower() or "no recommendations" in r.system_append.lower() or "no recommendation" in r.system_append.lower() or "queued" in r.system_append.lower()


# ── _handle_rec_pending ──────────────────────────────────────────────────────


@pytest.fixture
def _record_calls(monkeypatch):
    """Capture every call to BetterEngine record_feedback / snooze made
    by the rec_pending handler so tests can assert routing without
    needing a real recommendations.json."""
    calls: list[tuple[str, str, str | None]] = []

    class _StubEngine:
        def __init__(self, shared_dir, network):
            self.shared_dir = shared_dir

        def record_feedback(self, rec_id, signal, reason=""):
            calls.append(("record_feedback", rec_id, signal + (f"|{reason}" if reason else "")))
            return None

        def snooze(self, rec_id, *, days_override=None):
            # days_override is None when the user said "snooze" / "later"
            # without a duration hint and the config default is honored
            # by the engine; non-None when stage-2 extracted a hint.
            tail = None if days_override is None else f"days={days_override}"
            calls.append(("snooze", rec_id, tail))
            return None

        def filter_for_surface(self, recs, surface, scope_id):
            return []  # default: empty queue → handler should finalize after action

        @property
        def recs_path(self):
            return Path("/dev/null")  # never read by handler when filter returns []

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)
    return calls


def _drive_one_turn(tmp_path, user_key, user_message, network):
    """Helper: process_turn for an active rec_pending session."""
    from evolve_admin.evo.wizard import engine
    return engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=user_key, user_message=user_message,
        network=network,
    )


def test_handle_rec_pending_accept_records_and_finalizes(
    tmp_path, _record_calls, monkeypatch,
):
    from evolve_admin.evo.wizard import engine, intent

    # Stage 2 disabled (no LLM call needed here — "yes" is stage 1).
    network = _make_network(tmp_path)
    rec = _make_rec()
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=rec,
        surface="member_bot",
    )

    # Stub the next-rec lookup to return None so we finalize cleanly.
    # (Already empty via _record_calls fixture.)

    r = _drive_one_turn(tmp_path, "ext:telegram:42", "yes", network)

    assert r is not None
    assert ("record_feedback", "rec_001", "accepted") in _record_calls
    assert r.completed is True
    assert r.wizard_session_id is None  # plugin clears its session


def test_handle_rec_pending_reject_records_rejected(
    tmp_path, _record_calls,
):
    from evolve_admin.evo.wizard import engine

    network = _make_network(tmp_path)
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=_make_rec(),
        surface="member_bot",
    )

    _drive_one_turn(tmp_path, "ext:telegram:42", "no thanks", network)
    assert ("record_feedback", "rec_001", "rejected") in _record_calls


def test_handle_rec_pending_next_records_ignored(
    tmp_path, _record_calls,
):
    from evolve_admin.evo.wizard import engine

    network = _make_network(tmp_path)
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=_make_rec(),
        surface="member_bot",
    )

    _drive_one_turn(tmp_path, "ext:telegram:42", "next one", network)
    # ``next`` records as rejected with reason="ignored" so the learning
    # layer can distinguish soft-dismiss from explicit reject.
    assert ("record_feedback", "rec_001", "rejected|ignored") in _record_calls


def test_handle_rec_pending_snooze_calls_snooze(
    tmp_path, _record_calls,
):
    from evolve_admin.evo.wizard import engine

    network = _make_network(tmp_path)
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=_make_rec(),
        surface="member_bot",
    )

    _drive_one_turn(tmp_path, "ext:telegram:42", "remind me later", network)
    # Stage 1 hit "snooze" with no extracted duration → engine.snooze
    # gets days_override=None and falls through to the escalation
    # schedule. The fixture surfaces None as the third tuple element.
    assert ("snooze", "rec_001", None) in _record_calls


def test_handle_rec_pending_context_re_renders_without_action(
    tmp_path, _record_calls,
):
    from evolve_admin.evo.wizard import engine, state, phases

    network = _make_network(tmp_path)
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=_make_rec(),
        surface="member_bot",
    )

    r = _drive_one_turn(tmp_path, "ext:telegram:42", "tell me more", network)

    # Context shouldn't fire any record_feedback / snooze
    assert _record_calls == []
    # And the session is still active in REC_PENDING
    assert r is not None
    assert r.completed is False
    assert r.phase == phases.PHASE_REC_PENDING
    st = state.read_state(tmp_path, "team_bot_a", "ext:telegram:42")
    assert st is not None and st.is_active()


def test_handle_rec_pending_unknown_clarifies_then_finalizes(
    tmp_path, _record_calls,
):
    """Spec §6.3: unrelated reply re-renders with clarify variant once;
    on the second consecutive miss, finalize without action so the bot
    resumes normal flow. The pending rec stays in the queue."""
    from evolve_admin.evo.wizard import engine, state, intent, phases

    # Force stage 2 off so any non-stage-1 reply lands as fallback
    # unknown — the handler should clarify the first time.
    network = _make_network(tmp_path)
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=_make_rec(),
        surface="member_bot",
    )

    # First unknown reply → clarify, stay in phase
    intent.set_intent_parser(_stage2_stub_factory(action="unknown", confidence=0.0))
    try:
        r1 = _drive_one_turn(tmp_path, "ext:telegram:42", "what's the weather like", network)
    finally:
        intent.set_intent_parser(None)

    assert r1 is not None
    assert r1.completed is False
    assert r1.phase == phases.PHASE_REC_PENDING
    st = state.read_state(tmp_path, "team_bot_a", "ext:telegram:42")
    assert (st.extracted.get("_unknown_streak") or 0) == 1
    assert _record_calls == []

    # Second consecutive unknown → finalize without action
    intent.set_intent_parser(_stage2_stub_factory(action="unknown", confidence=0.0))
    try:
        r2 = _drive_one_turn(tmp_path, "ext:telegram:42", "anyway, about that other thing", network)
    finally:
        intent.set_intent_parser(None)

    assert r2 is not None
    assert r2.completed is True
    assert r2.wizard_session_id is None
    # No actions recorded — rec stays pending in the queue
    assert _record_calls == []


def test_handle_rec_pending_chains_to_next_rec(tmp_path, monkeypatch):
    """When BetterEngine.filter_for_surface returns more recs after the
    user accepts the current one, the handler stages the next rec as
    the new pending and stays in the phase."""
    from evolve_admin.evo.wizard import engine, state, phases

    # Stub BetterEngine to record the accept and then "find" a next rec.
    next_rec = _make_rec(id="rec_002", title="next thing")

    class _StubRec:
        def __init__(self, data):
            self._data = data
            self.id = data["id"]

        def to_dict(self):
            return self._data

    class _StubEngine:
        def __init__(self, shared_dir, network):
            pass

        def record_feedback(self, rec_id, signal, reason=""):
            return None

        def snooze(self, rec_id):
            return None

        def filter_for_surface(self, recs, surface, scope_id):
            return [_StubRec(next_rec)]

        @property
        def recs_path(self):
            return Path("/dev/null")

    def _stub_load_recs(_path):
        return [_StubRec(next_rec)]

    import evolve_admin.better_engine.engine as _be
    import evolve_admin.better_engine.storage as _storage
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)
    monkeypatch.setattr(_storage, "load_recommendations", _stub_load_recs)

    network = _make_network(tmp_path)
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=_make_rec(),
        surface="member_bot",
    )

    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42",
        user_message="yes", network=network,
    )

    # We stay in the phase, with rec_002 staged
    assert r is not None
    assert r.completed is False
    assert r.phase == phases.PHASE_REC_PENDING
    st = state.read_state(tmp_path, "team_bot_a", "ext:telegram:42")
    assert st.extracted.get("_pending_rec", {}).get("id") == "rec_002"
    assert int(st.extracted.get("_recs_shown") or 0) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch — bare evo + evo better
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_bare_evo_returns_orientation(tmp_path):
    """Bare ``evo`` returns a short orientation message — it does NOT
    start a rec_pending wizard session. Rec_pending is reached via the
    explicit ``evo better`` subcommand."""
    from evolve_admin.evo import dispatch
    from evolve_admin.evo.identity import derive_user_key
    from evolve_admin.evo.wizard import state as _state

    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "bots": {"team_bot_a": {}},
    }
    user_key = derive_user_key(
        network, bot_id="team_bot_a", channel="telegram", sender_external_id="123"
    )
    _state.mark_onboarded(tmp_path, "team_bot_a", user_key, audience="primary")

    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="123", raw_text="evo",
    )
    assert r.subcommand == "evo"
    assert r.mode == "speak"
    assert r.wizard_session_id is None
    # Onboarded user → suggest `evo better`.
    body = r.direct_send_message or ""
    assert "evo better" in body
    assert "evo help" in body


def test_dispatch_evo_better_starts_rec_pending(tmp_path, monkeypatch):
    """`evo better` starts the rec_pending flow directly — no
    orientation indirection, no onboarding pre-emption."""
    from evolve_admin.evo import dispatch
    from evolve_admin.evo.identity import derive_user_key
    from evolve_admin.evo.wizard import state as _state

    class _StubEngine:
        def __init__(self, shared_dir, network):
            pass

        def get_top(self, surface="admin", scope_id=None):
            return None  # empty queue

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)

    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "bots": {"team_bot_a": {}},
    }
    user_key = derive_user_key(
        network, bot_id="team_bot_a", channel="telegram", sender_external_id="123"
    )
    _state.mark_onboarded(tmp_path, "team_bot_a", user_key, audience="primary")

    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="123", raw_text="evo better",
    )
    assert r.subcommand == "better"
    # Empty queue → start_rec_pending finalizes immediately, so
    # wizard_session_id is None (plugin clears state).
    assert r.mode == "speak"
    assert r.wizard_session_id is None
    assert r.system_append


def test_dispatch_renders_empty_queue_when_engine_unavailable(tmp_path, monkeypatch):
    """If BetterEngine import / construction fails (mid-deploy or
    missing module), the dispatcher treats the inbox as empty and the
    wizard renders the "all caught up" variant rather than dropping
    the turn. Exercised via ``evo better`` since that's the rec_pending
    entry point now."""
    from evolve_admin.evo import dispatch

    class _BrokenEngine:
        def __init__(self, *_a, **_kw):
            raise RuntimeError("engine offline for tests")

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _BrokenEngine)

    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "bots": {"team_bot_a": {}},
    }

    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="123", raw_text="evo better",
    )
    assert r.mode == "speak"
    # Empty queue → start_rec_pending finalizes immediately; no wizard
    # session continues.
    assert r.wizard_session_id is None
    assert r.system_append
    assert any("BetterEngine unavailable" in (n or "") for n in r.notes)
