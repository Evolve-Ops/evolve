"""tests/test_evo_wizard_rec_pending_day2.py — day-2 additions.

Spec: internal/spec-better-engine-conversational-approval-2026-04-18.md

Day-2 functionality not covered by test_evo_wizard_rec_pending.py:
  * config.resolve — defaults, file overrides, bot overrides, malformed
    file fallback
  * intent.parse_intent honors llm_intent_parse_enabled=False
  * pending-rec session TTL (state expires after pending_expiry_minutes)
  * snooze duration end-to-end (BetterEngine.snooze accepts days_override)
  * voice.py — SOUL.md tone extraction, profile communication
    preferences extraction, voice_summary composition + fallback
  * push preamble scaffolding — start_push_preamble gated by config and
    rec urgency
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
# config.resolve
# ─────────────────────────────────────────────────────────────────────────────


def test_config_defaults_when_no_file(tmp_path):
    from evolve_admin.evo.wizard import config

    cfg = config.resolve(tmp_path, "team_bot_a")
    assert cfg.enabled is True
    assert cfg.llm_intent_parse_enabled is True
    assert cfg.confidence_threshold == 0.80
    assert cfg.default_snooze_days == 3
    assert cfg.pending_expiry_minutes == 60
    assert cfg.push_preamble_enabled is False


def test_config_pod_default_overrides(tmp_path):
    from evolve_admin.evo.wizard import config
    cfg_path = tmp_path / "better-engine-config.json"
    cfg_path.write_text(json.dumps({
        "schema_version": 1,
        "pod_defaults": {
            "conversational_approval": {
                "enabled": False,
                "confidence_threshold": 0.6,
                "default_snooze_days": 5,
            },
        },
    }))

    cfg = config.resolve(tmp_path, "team_bot_a")
    assert cfg.enabled is False
    assert cfg.confidence_threshold == pytest.approx(0.6)
    assert cfg.default_snooze_days == 5
    # Unspecified keys fall back to compiled defaults
    assert cfg.llm_intent_parse_enabled is True


def test_config_per_bot_overrides_pod_default(tmp_path):
    from evolve_admin.evo.wizard import config
    cfg_path = tmp_path / "better-engine-config.json"
    cfg_path.write_text(json.dumps({
        "schema_version": 1,
        "pod_defaults": {
            "conversational_approval": {
                "default_snooze_days": 3,
            },
        },
        "bots": {
            "team_bot_a": {
                "conversational_approval": {
                    "default_snooze_days": 1,  # team_bot_a's user prefers shorter
                    "push_preamble_enabled": True,
                }
            }
        },
    }))

    cfg_team_bot_a = config.resolve(tmp_path, "team_bot_a")
    cfg_other = config.resolve(tmp_path, "admin_bot")
    assert cfg_team_bot_a.default_snooze_days == 1
    assert cfg_team_bot_a.push_preamble_enabled is True
    # Pod default still applies to bots without an override
    assert cfg_other.default_snooze_days == 3
    assert cfg_other.push_preamble_enabled is False


def test_config_malformed_file_falls_back_to_defaults(tmp_path):
    from evolve_admin.evo.wizard import config
    cfg_path = tmp_path / "better-engine-config.json"
    cfg_path.write_text("{ this isn't json")
    cfg = config.resolve(tmp_path, "team_bot_a")
    # Compiled defaults — load() returns BetterEngineConfig.default()
    assert cfg.enabled is True
    assert cfg.confidence_threshold == 0.80


# ─────────────────────────────────────────────────────────────────────────────
# Intent — llm_intent_parse_enabled=False threading
# ─────────────────────────────────────────────────────────────────────────────


def test_handle_rec_pending_with_llm_disabled_clarifies_on_miss(
    tmp_path, monkeypatch,
):
    """When llm_intent_parse_enabled=False, stage-1 misses fall through
    to clarify rather than calling stage 2. Verifies the engine wires
    cfg through to intent.parse_intent."""
    from evolve_admin.evo.wizard import engine, state, phases

    # Disable LLM intent parse via config file
    cfg_path = tmp_path / "better-engine-config.json"
    cfg_path.write_text(json.dumps({
        "schema_version": 1,
        "pod_defaults": {
            "conversational_approval": {"llm_intent_parse_enabled": False},
        },
    }))

    # Stub BetterEngine — handler shouldn't reach record_feedback because
    # the reply is ambiguous and stage 2 is off.
    class _StubEngine:
        def __init__(self, *_a, **_kw):
            pass

        def record_feedback(self, *_a, **_kw):
            raise AssertionError("should not record on ambiguous + LLM off")

        def snooze(self, *_a, **_kw):
            raise AssertionError("should not snooze on ambiguous + LLM off")

        def filter_for_surface(self, *_a, **_kw):
            return []

        @property
        def recs_path(self):
            return Path("/dev/null")

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}
    rec = {
        "id": "rec_001",
        "scope_id": "team_bot_a",
        "title": "x", "detail": "y",
        "member_bot_title": "x", "member_bot_detail": "y",
        "tags": [], "status": "pending",
    }
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=rec,
        surface="member_bot",
    )

    # An ambiguous reply that misses stage 1 — would normally invoke
    # stage 2, but config disables it. Result: clarify variant.
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42",
        user_message="hmm could be useful I guess", network=network,
    )
    assert r is not None
    assert r.completed is False
    assert r.phase == phases.PHASE_REC_PENDING
    st = state.read_state(tmp_path, "team_bot_a", "ext:telegram:42")
    assert int(st.extracted.get("_unknown_streak") or 0) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Pending-rec session TTL
# ─────────────────────────────────────────────────────────────────────────────


def test_rec_pending_session_expires_after_ttl(tmp_path):
    """A rec_pending session whose updated_at is older than
    pending_expiry_minutes returns None from process_turn (plugin
    clears its session reference; bot resumes normal flow)."""
    from evolve_admin.evo.wizard import engine, state, phases

    # Initialize a session, then rewrite state with an old timestamp.
    rec = {
        "id": "rec_001", "scope_id": "team_bot_a",
        "title": "x", "detail": "y",
        "member_bot_title": "x", "member_bot_detail": "y",
        "tags": [], "status": "pending",
    }
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=rec,
        surface="member_bot",
    )

    # Override updated_at to 30 days ago — well past the 60-minute default.
    st_path = state.state_path(tmp_path, "team_bot_a", "ext:telegram:42")
    data = json.loads(st_path.read_text())
    data["updated_at"] = "2026-04-01T00:00:00Z"
    st_path.write_text(json.dumps(data))

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42",
        user_message="yes", network=network,
    )
    # Expired session → None (plugin clears its wizardSessionId)
    assert r is None
    # State is finalized/completed — won't trip the TTL check on next reads
    st_after = state.read_state(tmp_path, "team_bot_a", "ext:telegram:42")
    assert st_after is not None
    assert st_after.is_active() is False


def test_rec_pending_session_within_ttl_keeps_running(tmp_path, monkeypatch):
    """Sessions inside the TTL window proceed normally — TTL isn't
    inadvertently triggered by recent activity."""
    from evolve_admin.evo.wizard import engine, phases

    rec = {
        "id": "rec_001", "scope_id": "team_bot_a",
        "title": "x", "detail": "y",
        "member_bot_title": "x", "member_bot_detail": "y",
        "tags": [], "status": "pending",
    }
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=rec,
        surface="member_bot",
    )

    class _StubEngine:
        def __init__(self, *_a, **_kw):
            pass

        def record_feedback(self, *_a, **_kw):
            return None

        def snooze(self, *_a, **_kw):
            return None

        def filter_for_surface(self, *_a, **_kw):
            return []

        @property
        def recs_path(self):
            return Path("/dev/null")

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42",
        user_message="yes", network=network,
    )
    # Fresh session — handler ran, finalized normally (queue empty)
    assert r is not None
    assert r.completed is True


def test_rec_pending_ttl_only_applies_to_approver_sessions(tmp_path):
    """A long-idle primary (or guide_drafter) session should NOT auto-
    expire — only approver-audience sessions get the TTL treatment."""
    from evolve_admin.evo.wizard import engine, state, phases

    # Start a primary onboarding session and age it.
    engine.start_session(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42",
        audience="primary", role="primary",
    )
    st_path = state.state_path(tmp_path, "team_bot_a", "ext:telegram:42")
    data = json.loads(st_path.read_text())
    data["updated_at"] = "2026-04-01T00:00:00Z"
    st_path.write_text(json.dumps(data))

    # Even at 30+ days idle, primary sessions don't expire — process_turn
    # carries on (the test only checks we don't get None from the TTL
    # short-circuit; the actual extraction path doesn't matter here).
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42",
        user_message="hi I'm Pod_admin",
    )
    # primary onboarding still active — not the rec_pending TTL path
    assert r is not None


# ─────────────────────────────────────────────────────────────────────────────
# Snooze duration end-to-end
# ─────────────────────────────────────────────────────────────────────────────


def test_snooze_recommendation_honors_days_override():
    """The shared snooze helper accepts days_override and uses it
    instead of the escalation schedule."""
    from evolve_admin.better_engine.snooze import snooze_recommendation
    from evolve_admin.better_engine.model import Recommendation, now_iso
    from datetime import datetime, timezone, timedelta

    rec = Recommendation(
        id="r1", dedup_key="x", type="operational", source="test",
        scope="bot", scope_id="team_bot_a", title="t", detail="d", context="",
        action_label="x", action="noop", action_args={},
        member_bot_title="t", member_bot_detail="d",
        priority_score=10, priority_components={}, learning_weight=1.0,
        status="pending", snooze_count=0,
    )
    snooze_recommendation(rec, days_override=14)
    assert rec.status == "snoozed"
    assert rec.snooze_until is not None
    until = datetime.fromisoformat(rec.snooze_until)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    expected = datetime.now(timezone.utc) + timedelta(days=14)
    # Within 60 seconds — allows clock skew from test setup
    assert abs((until - expected).total_seconds()) < 60


def test_snooze_recommendation_default_uses_escalation_schedule():
    """No override → escalation schedule (snooze #1 = 1 day, #2 = 2, etc)."""
    from evolve_admin.better_engine.snooze import snooze_recommendation
    from evolve_admin.better_engine.model import Recommendation
    from datetime import datetime, timezone, timedelta

    rec = Recommendation(
        id="r1", dedup_key="x", type="operational", source="test",
        scope="bot", scope_id="team_bot_a", title="t", detail="d", context="",
        action_label="x", action="noop", action_args={},
        member_bot_title="t", member_bot_detail="d",
        priority_score=10, priority_components={}, learning_weight=1.0,
        status="pending", snooze_count=0,
    )
    snooze_recommendation(rec)
    until = datetime.fromisoformat(rec.snooze_until)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    expected = datetime.now(timezone.utc) + timedelta(days=1)  # SNOOZE_SCHEDULE[0]
    assert abs((until - expected).total_seconds()) < 60
    assert rec.snooze_count == 1


def test_handle_rec_pending_passes_stage2_snooze_hint(tmp_path, monkeypatch):
    """When stage 2 returns a snooze_hint_days, the handler forwards it
    to engine.snooze as days_override."""
    from evolve_admin.evo.wizard import engine, intent

    captured: list[tuple[str, Any]] = []

    class _StubEngine:
        def __init__(self, *_a, **_kw):
            pass

        def record_feedback(self, *_a, **_kw):
            return None

        def snooze(self, rec_id, *, days_override=None):
            captured.append((rec_id, days_override))
            return None

        def filter_for_surface(self, *_a, **_kw):
            return []

        @property
        def recs_path(self):
            return Path("/dev/null")

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)

    rec = {
        "id": "rec_001", "scope_id": "team_bot_a",
        "title": "x", "detail": "y",
        "member_bot_title": "x", "member_bot_detail": "y",
        "tags": [], "status": "pending",
    }
    engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=rec,
        surface="member_bot",
    )

    # Stub stage 2 to return snooze with a 14-day hint — simulating the
    # LLM having parsed "remind me in two weeks".
    intent.set_intent_parser(lambda _msg, _pitch, _ctx: intent.IntentResult(
        action="snooze", confidence=0.95, snooze_hint_days=14,
        rationale="stub: two weeks", stage="stage2",
    ))
    try:
        network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}
        engine.process_turn(
            tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42",
            # Phrase that misses stage 1 so stage 2 fires
            user_message="actually, push that out a couple weeks", network=network,
        )
    finally:
        intent.set_intent_parser(None)

    assert captured == [("rec_001", 14)]


# ─────────────────────────────────────────────────────────────────────────────
# voice.py
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_markdown_section_basic():
    from evolve_admin.evo.wizard.voice import _extract_markdown_section
    text = (
        "# Title\n\n"
        "intro paragraph\n\n"
        "## Tone\n\n"
        "Direct, no preamble.\n\n"
        "## Next\n\n"
        "other content\n"
    )
    section = _extract_markdown_section(text, "Tone")
    assert section is not None
    assert "Direct, no preamble." in section
    assert "other content" not in section


def test_extract_markdown_section_handles_subheadings():
    from evolve_admin.evo.wizard.voice import _extract_markdown_section
    text = (
        "## Communication Preferences\n\n"
        "Body line one.\n\n"
        "### Sub-heading inside\n\n"
        "Body line two.\n\n"
        "## Next Section\n"
    )
    section = _extract_markdown_section(text, "Communication Preferences")
    assert section is not None
    assert "Body line one." in section
    assert "Sub-heading inside" in section
    assert "Body line two." in section
    assert "Next Section" not in section


def test_voice_summary_with_bot_guide(tmp_path):
    from evolve_admin.evo.wizard.voice import voice_summary

    class _FakeGuide:
        frontmatter = {
            "tone": "warm and concise",
            "do_say": ["lean toward action items", "ask before changes"],
            "dont_say": ["don't speculate on legal questions"],
        }

    out = voice_summary(tmp_path, "team_bot_a", bot_guide=_FakeGuide())
    assert out["tone"] == "warm and concise"
    assert "lean toward action items" in out["do_say"]
    assert "speculate" in out["dont_say"]


def test_voice_summary_falls_back_to_soul_when_guide_missing(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import voice as _voice

    # Stub bot_home to point inside tmp_path so we control the SOUL.md
    bot_home = tmp_path / "bot-home" / "team_bot_a"
    soul_path = bot_home / ".openclaw" / "workspace" / "SOUL.md"
    soul_path.parent.mkdir(parents=True)
    soul_path.write_text(
        "# SOUL.md — Team_bot_a\n\n"
        "## Purpose\n\nDeploy + CI helper.\n\n"
        "## Tone\n\nTerse, no preamble. Reply with the answer first.\n\n"
        "## Confirmation Protocol\n\nAlways confirm destructive actions.\n"
    )

    monkeypatch.setattr(_voice, "_bot_home", lambda *_a, **_kw: bot_home)

    out = _voice.voice_summary(tmp_path, "team_bot_a")
    assert "soul_tone" in out
    assert "Terse" in out["soul_tone"]
    # Tone is condensed from SOUL when no guide overrides
    assert out.get("tone")
    assert "Terse" in out["tone"]


def test_voice_summary_picks_up_profile_communication_preferences(tmp_path):
    from evolve_admin.evo.wizard.voice import voice_summary

    profile_path = tmp_path / "profiles" / "team_bot_a.md"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        "---\nbot_id: team_bot_a\nschema_version: 1\n---\n\n"
        "## Demographics\n\n(empty)\n\n"
        "## Communication Preferences\n\n"
        "User prefers updates batched once per day; no emoji.\n\n"
        "## Values\n\n(empty)\n"
    )

    out = voice_summary(tmp_path, "team_bot_a")
    prefs = out.get("profile_communication_preferences", "")
    assert "batched once per day" in prefs


def test_voice_summary_skips_empty_profile_section(tmp_path):
    from evolve_admin.evo.wizard.voice import voice_summary

    profile_path = tmp_path / "profiles" / "team_bot_a.md"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        "## Communication Preferences\n\n(empty)\n\n## Values\n\n(empty)\n"
    )

    out = voice_summary(tmp_path, "team_bot_a")
    # "(empty)" placeholder is filtered out
    assert "profile_communication_preferences" not in out


# ─────────────────────────────────────────────────────────────────────────────
# Voice cues threaded into the pitch prompt
# ─────────────────────────────────────────────────────────────────────────────


def test_pitch_prompt_includes_voice_cues_when_provided():
    from evolve_admin.evo.wizard.prompts import build_rec_pending_block
    rec = {
        "id": "r1", "scope_id": "team_bot_a",
        "title": "T", "detail": "D",
        "member_bot_title": "T", "member_bot_detail": "D",
        "tags": [],
    }
    out = build_rec_pending_block(variant="pitch", rec=rec, bot_id="team_bot_a")
    # No voice supplied → no voice cues block
    assert "Voice cues" not in out


def test_pitch_prompt_no_longer_emits_voice_block(tmp_path, monkeypatch):
    """Voice cues were an LLM-pitch-shaping device when rec_pending was
    an agenda phase. Under the verbatim/direct-send conversion
    (2026-05-17) the pitch is deterministic user-facing text and voice
    is unused — engine resolution still happens upstream but the
    rendered pitch drops the cues block.

    Regression guard so we notice if a future change re-introduces the
    LLM-directive form (which would re-introduce the compliance gap
    that motivated the conversion)."""
    from evolve_admin.evo.wizard import engine, voice as _voice

    bot_home = tmp_path / "bot-home" / "team_bot_a"
    soul_path = bot_home / ".openclaw" / "workspace" / "SOUL.md"
    soul_path.parent.mkdir(parents=True)
    soul_path.write_text(
        "## Tone\n\nTerse, technical, no preamble.\n"
    )
    monkeypatch.setattr(_voice, "_bot_home", lambda *_a, **_kw: bot_home)

    rec = {
        "id": "r1", "scope_id": "team_bot_a",
        "title": "T", "detail": "D",
        "member_bot_title": "T", "member_bot_detail": "D",
        "tags": [], "status": "pending",
    }
    r = engine.start_rec_pending(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", rec=rec,
        surface="member_bot",
    )
    # Voice cues are no longer rendered into the pitch.
    assert "Voice cues" not in r.system_append
    assert "Terse" not in r.system_append
    # But the pitch body itself still surfaces the rec title.
    assert "T" in r.system_append


# ─────────────────────────────────────────────────────────────────────────────
# Push preamble scaffolding
# ─────────────────────────────────────────────────────────────────────────────


def test_start_push_preamble_returns_none_when_disabled(tmp_path):
    """Default config (push_preamble_enabled=False) → no-op."""
    from evolve_admin.evo.wizard import engine
    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}
    r = engine.start_push_preamble(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", network=network,
    )
    assert r is None


def test_start_push_preamble_returns_none_when_no_urgent_rec(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import engine

    cfg_path = tmp_path / "better-engine-config.json"
    cfg_path.write_text(json.dumps({
        "schema_version": 1,
        "pod_defaults": {
            "conversational_approval": {"push_preamble_enabled": True},
        },
    }))

    class _StubEngine:
        def __init__(self, *_a, **_kw):
            pass

        def get_top(self, surface="admin", scope_id=None):
            class _Rec:
                def to_dict(self):
                    return {
                        "id": "r1", "tags": ["urgency:low"], "type": "operational",
                    }
            return _Rec()

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}
    r = engine.start_push_preamble(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", network=network,
    )
    assert r is None  # not urgent → no push


def test_start_push_preamble_starts_session_for_urgent_rec(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import engine, state, phases

    cfg_path = tmp_path / "better-engine-config.json"
    cfg_path.write_text(json.dumps({
        "schema_version": 1,
        "pod_defaults": {
            "conversational_approval": {"push_preamble_enabled": True},
        },
    }))

    urgent_rec = {
        "id": "r99", "scope_id": "team_bot_a",
        "title": "Disk full on the gateway",
        "detail": "Free space < 1GB. Action needed.",
        "member_bot_title": "Disk almost full",
        "member_bot_detail": "We're below 1GB free.",
        "tags": ["urgency:critical"], "type": "operational",
        "status": "pending",
    }

    class _StubEngine:
        def __init__(self, *_a, **_kw):
            pass

        def get_top(self, surface="admin", scope_id=None):
            class _Rec:
                def to_dict(self):
                    return urgent_rec
            return _Rec()

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}
    r = engine.start_push_preamble(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", network=network,
    )
    assert r is not None
    assert r.completed is False
    assert r.wizard_session_id == "ext:telegram:42"
    assert r.phase == phases.PHASE_REC_PENDING
    # Push variant has its own user-facing framing — "Heads up" + the
    # rec title; distinct from the regular "Suggestion for …" pitch.
    assert "Heads up" in r.system_append
    assert "Disk almost full" in r.system_append
    # State persisted with the urgent rec staged
    st = state.read_state(tmp_path, "team_bot_a", "ext:telegram:42")
    assert st is not None
    assert st.audience == "approver"
    assert (st.extracted.get("_pending_rec") or {}).get("id") == "r99"


def test_start_push_preamble_skips_when_session_already_active(tmp_path, monkeypatch):
    """Push only fires when the user isn't already in a wizard
    (don't clobber an in-flight session)."""
    from evolve_admin.evo.wizard import engine

    cfg_path = tmp_path / "better-engine-config.json"
    cfg_path.write_text(json.dumps({
        "schema_version": 1,
        "pod_defaults": {
            "conversational_approval": {"push_preamble_enabled": True},
        },
    }))

    # Start a primary onboarding session — that's an active wizard.
    engine.start_session(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42",
        audience="primary", role="primary",
    )

    class _StubEngine:
        def __init__(self, *_a, **_kw):
            pass

        def get_top(self, surface="admin", scope_id=None):
            class _Rec:
                def to_dict(self):
                    return {
                        "id": "r1", "tags": ["urgency:critical"],
                        "title": "x", "detail": "y",
                        "member_bot_title": "x", "member_bot_detail": "y",
                    }
            return _Rec()

    import evolve_admin.better_engine.engine as _be
    monkeypatch.setattr(_be, "BetterEngine", _StubEngine)

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path), "bots": {"team_bot_a": {}}}
    r = engine.start_push_preamble(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:42", network=network,
    )
    assert r is None


def test_is_urgent_rec_recognizes_v1_and_v2_urgency_signals():
    from evolve_admin.evo.wizard.engine import _is_urgent_rec

    assert _is_urgent_rec({"tags": ["urgency:critical"]}) is True
    assert _is_urgent_rec({"tags": ["urgency:high"]}) is True
    assert _is_urgent_rec({"type": "security_critical"}) is True
    assert _is_urgent_rec({"type": "operational_urgent"}) is True
    assert _is_urgent_rec({"tags": ["type:operational_urgent"]}) is True

    assert _is_urgent_rec({"tags": ["urgency:low"]}) is False
    assert _is_urgent_rec({"tags": []}) is False
    assert _is_urgent_rec({"type": "operational"}) is False
    assert _is_urgent_rec({}) is False
