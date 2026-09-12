"""tests/test_engagement_amplifier_generator.py — pin the consumer
side of the engagement_amplifier_monitor → engagement_amplifier pair.

The generator subscribes to ``engagement_amplification_opportunity``
Signals and emits Phase A operator-first Proposals on
``surface=improvement``. The narrative distinguishes ``confirmed``
("deepen what's working") from ``emergent`` ("users converged on
something outside stated scope — consider embracing it").

These tests pin:
  1. Phase A fields (summary, explanation, action_label, manual_path)
     populated.
  2. Summary cites actual evidence — bot, noun, sessions, days,
     engagement, frustration.
  3. Alignment shapes the framing: ``confirmed`` vs ``emergent``
     produce different "why this matters" prose.
  4. Explanation lists concrete ways to deepen (cron / app / AGENTS).
  5. action_kind is Investigation (no auto-apply; v1 charter).
  6. surface=improvement implied by charter, not overridden.
  7. End-to-end: monitor drops a Signal, generator emits a Proposal
     that quotes the signal's details.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import engagement_amplifier_monitor as monitor_mod  # noqa: E402
from generators.engagement_amplifier import (  # noqa: E402
    EngagementAmplifierContext,
    observe,
)
from generators.engagement_amplifier.observe import (  # noqa: E402
    _build_proposal,
    _verb_friendly,
)
from observations.tuples import write_tuples  # noqa: E402
from schema.observation import ObservationTuple  # noqa: E402
from signals import store as signals_store  # noqa: E402


BOT_ID = "team-bot-a"
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


class _StubSignal:
    """Minimal Signal-shaped object covering the attributes
    ``_build_proposal`` reads. The Signal store would normally hand us
    full Signal dataclasses; the stub keeps these tests hermetic."""

    def __init__(self, sig_id: str, bot_id: str, **details):
        self.id = sig_id
        self.bot_id = bot_id
        self.type = monitor_mod.SIGNAL_TYPE
        self.details = {"bot_id": bot_id, **details}


def _confirmed_signal(**overrides) -> _StubSignal:
    base = dict(
        noun="workout",
        verb="tracking",
        objective_alignment="confirmed",
        engagement_total=42,
        distinct_sessions=8,
        distinct_days=12,
        frustrated_share=0.05,
        positive_mood_share=0.6,
        domain_tags=["domain:fitness"],
        window_days=30,
    )
    base.update(overrides)
    return _StubSignal("sig-1", BOT_ID, **base)


# ─────────────────────────────────────────────────────────────────────────────
# Verb prettifier
# ─────────────────────────────────────────────────────────────────────────────


def test_verb_friendly_strips_ing():
    assert _verb_friendly("tracking") == "tracks"
    assert _verb_friendly("planning") == "plans"


def test_verb_friendly_passes_through_non_ing():
    assert _verb_friendly("review") == "review"


# ─────────────────────────────────────────────────────────────────────────────
# Phase A content shape
# ─────────────────────────────────────────────────────────────────────────────


def test_phase_a_fields_populated():
    """summary, explanation, action_label, manual_path must all be
    populated for the Phase A operator-first renderer."""
    p = _build_proposal(
        _confirmed_signal(),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert p.summary, "summary must be set"
    assert p.explanation, "explanation must be set"
    assert p.action_label == "Pick a way to deepen"
    assert "Applications" in (p.manual_path or "")


def test_action_kind_is_investigation():
    """Charter allowlist: only Investigation. Auto-apply isn't safe
    for "deepen a working pattern" — the operator picks the lever."""
    p = _build_proposal(
        _confirmed_signal(),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert p.action.kind == "Investigation"


def test_urgency_is_improvement():
    """RSI proposals carry urgency=improvement so the renderer
    routes them to Recommendations (not Alerts) as the default
    surface routing."""
    p = _build_proposal(
        _confirmed_signal(),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert p.urgency == "improvement"


# ─────────────────────────────────────────────────────────────────────────────
# Summary cites evidence
# ─────────────────────────────────────────────────────────────────────────────


def test_summary_cites_noun_sessions_days_engagement():
    p = _build_proposal(
        _confirmed_signal(distinct_sessions=8, distinct_days=12,
                          engagement_total=42),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert "workout" in p.summary
    assert "8" in p.summary
    assert "12" in p.summary
    assert "42" in p.summary


def test_summary_pluralization():
    """Single-session / single-day reads naturally."""
    p = _build_proposal(
        _confirmed_signal(distinct_sessions=1, distinct_days=1,
                          engagement_total=15),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert "1 session " in p.summary
    assert "1 day," in p.summary


# ─────────────────────────────────────────────────────────────────────────────
# Alignment shapes framing
# ─────────────────────────────────────────────────────────────────────────────


def test_confirmed_summary_frames_as_deepen():
    """``confirmed`` alignment must read "stated scope working well —
    consider deepening it." — that's the operator-readable framing."""
    p = _build_proposal(
        _confirmed_signal(objective_alignment="confirmed"),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert "stated scope" in p.summary
    assert "deepening" in p.summary or "deepen" in p.summary


def test_emergent_summary_frames_as_organic_convergence():
    """``emergent`` alignment frames the operator question as
    "users organically converged; embrace?" — the operator-readable
    framing for "this isn't in the bot's scope but they keep coming"."""
    p = _build_proposal(
        _confirmed_signal(objective_alignment="emergent"),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert "organically" in p.summary
    assert "AGENTS.md" in p.summary


def test_explanation_distinguishes_confirmed_vs_emergent():
    """The 'why this matters' section reads differently for each
    alignment. confirmed = invest in working pattern; emergent =
    operator-readable gap between stated scope and real use."""
    confirmed = _build_proposal(
        _confirmed_signal(objective_alignment="confirmed"),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    emergent = _build_proposal(
        _confirmed_signal(objective_alignment="emergent"),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert confirmed is not None and emergent is not None
    assert "stated scope" in confirmed.explanation
    assert "organically converged" in emergent.explanation


def test_explanation_lists_concrete_ways_to_deepen():
    """The operator-actionable section must list 3+ concrete options
    (cron / manifest / AGENTS / dismiss). 'Pick a way to deepen' is
    meaningless without options."""
    p = _build_proposal(
        _confirmed_signal(),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    e = p.explanation
    assert "Schedule a proactive surface" in e
    assert "Formalize as an app" in e
    assert "Dismiss" in e


def test_emergent_explanation_offers_agents_md_update():
    """``emergent`` cases get an explicit "update AGENTS.md" option
    — because the gap between stated scope and real use is the
    bot's purpose lagging the user's actual use, and AGENTS.md is
    where you state scope."""
    p = _build_proposal(
        _confirmed_signal(objective_alignment="emergent"),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert "Update AGENTS.md" in p.explanation


# ─────────────────────────────────────────────────────────────────────────────
# Provenance audit trail
# ─────────────────────────────────────────────────────────────────────────────


def test_provenance_records_signal_payload():
    """Everything the proposal quotes lives in provenance.signals so
    the audit trail answers "what did the system see when it emitted
    this?". Mirrors the contract from app_suggester's
    evidence-grounded change."""
    sig = _confirmed_signal()
    p = _build_proposal(
        sig,
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    ps = p.provenance.signals
    assert ps["noun"] == "workout"
    assert ps["objective_alignment"] == "confirmed"
    assert ps["engagement_total"] == 42
    assert ps["grounding_signal_ids"] == [sig.id]


# ─────────────────────────────────────────────────────────────────────────────
# Dismiss signature granularity
# ─────────────────────────────────────────────────────────────────────────────


def test_dismiss_signature_per_noun_verb():
    """Dismissing the 'tracking workouts' opportunity must NOT
    suppress 'planning workouts' — same noun, different intent."""
    p_track = _build_proposal(
        _confirmed_signal(verb="tracking"),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    p_plan = _build_proposal(
        _confirmed_signal(verb="planning"),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p_track is not None and p_plan is not None
    assert p_track.dismiss_signature != p_plan.dismiss_signature


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: monitor → store → generator
# ─────────────────────────────────────────────────────────────────────────────


def _write_strong_workout_pattern(shared_dir: Path) -> None:
    """8 enthusiastic-mood tuples across 8 days, 4 engagement each.
    Mirrors the test_engagement_amplifier_monitor happy-path fixture."""
    for i in range(8):
        day = NOW - timedelta(days=i)
        t = ObservationTuple(
            id=f"obs-{i}", bot_id=BOT_ID, session_id=f"sess-{i}",
            segment_id=f"seg-{i}", noun="workout", verb="tracking",
            mood="enthusiastic", engagement=4,
            timestamp_start=day.isoformat(),
            timestamp_end=(day + timedelta(minutes=5)).isoformat(),
            source_hash=f"h-{i}",
        )
        write_tuples([t], shared_dir=shared_dir, bot_id=BOT_ID, day=day)


def test_end_to_end_monitor_signal_to_proposal(tmp_path, monkeypatch):
    """Drop a strong workout pattern + fitness-shaped AGENTS.md, run
    the monitor (which writes a Signal), then run observe() and
    confirm the resulting Proposal quotes the monitor's evidence."""
    p_md = tmp_path / "agents-bot.md"
    p_md.write_text("This bot is a fitness coach.", encoding="utf-8")
    monkeypatch.setattr(
        monitor_mod, "_bot_workspace_agents_md", lambda _bid: p_md
    )
    _write_strong_workout_pattern(tmp_path)

    # Producer run.
    summary = monitor_mod.run_for_pod([BOT_ID], tmp_path, now=NOW)
    assert summary["signals_emitted"] == 1

    # Consumer run.
    ctx = EngagementAmplifierContext(
        bot_ids=[BOT_ID], shared_dir=tmp_path, max_per_run=10
    )
    proposals = observe(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    # The proposal must carry the actual evidence the monitor put on
    # the Signal — that's the producer/consumer contract.
    assert p.bot_id == BOT_ID
    assert "workout" in p.summary
    assert "stated scope" in p.summary  # → confirmed framing
    assert p.urgency == "improvement"
    assert p.action.kind == "Investigation"


# ─────────────────────────────────────────────────────────────────────────────
# Value-free dedup identity (2026-08-30 alerts review, root cause 3)
# ─────────────────────────────────────────────────────────────────────────────


def _ctx() -> EngagementAmplifierContext:
    return EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp"))


def test_triggers_are_value_free():
    """trigger_observations must carry only the stable cluster identity —
    no measured values (engagement running total, alignment class). A
    value-bearing token mints a fresh dedup fingerprint every run, which
    is exactly the 32-duplicate pile-up on the live mini."""
    p = _build_proposal(_confirmed_signal(), _ctx())
    assert p is not None
    assert p.trigger_observations == [
        f"engagement_amplification:{BOT_ID}:workout:tracking"
    ]


def test_fingerprint_stable_across_value_changes():
    """Two runs over the same (bot, noun, verb) cluster with different
    engagement totals AND a flipped alignment produce the SAME dedup
    fingerprint — the arbiter merges instead of minting a duplicate."""
    from arbiter.dedup import compute_fingerprint, run_dedup_hook
    from arbiter.state_machine import transition

    p1 = _build_proposal(
        _confirmed_signal(engagement_total=42), _ctx()
    )
    p2 = _build_proposal(
        _confirmed_signal(
            engagement_total=57, objective_alignment="emergent"
        ),
        _ctx(),
    )
    assert p1 is not None and p2 is not None
    assert compute_fingerprint(p1) == compute_fingerprint(p2)

    # The ingest-side dedup hook sees the collision.
    transition(p1, "pending", actor="test")
    result = run_dedup_hook(p2, open_proposals=[p1])
    assert result.collisions == [p1.id]


def test_second_run_folds_into_one_pending(tmp_path):
    """Store-level guarantee: writing the second run's proposal (same
    cluster, different measured values) does NOT create a second
    pending file — the shared value-free coalesce_key folds it into
    the existing parent."""
    from arbiter.state_machine import transition
    from arbiter.store import write_proposal

    p1 = _build_proposal(_confirmed_signal(engagement_total=42), _ctx())
    p2 = _build_proposal(_confirmed_signal(engagement_total=57), _ctx())
    assert p1 is not None and p2 is not None
    transition(p1, "pending", actor="test")
    transition(p2, "pending", actor="test")

    write_proposal(p1, tmp_path)
    write_proposal(p2, tmp_path)

    pending = list((tmp_path / "proposals" / "pending").glob("*.json"))
    assert len(pending) == 1


def test_legibility_fields_set():
    """Producer-legibility ratchet: value-free coalesce_key matching the
    dismiss signature, plus a count-agnostic human_title (no measured
    numbers baked into the title)."""
    p = _build_proposal(_confirmed_signal(), _ctx())
    assert p is not None
    assert p.coalesce_key == f"engagement_amplifier:{BOT_ID}:workout:tracking"
    assert p.coalesce_key == p.dismiss_signature
    assert p.human_title
    assert "42" not in p.human_title
