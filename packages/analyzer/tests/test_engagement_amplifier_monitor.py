"""tests/test_engagement_amplifier_monitor.py — pin the monitor that
detects high-engagement low-frustration patterns worth amplifying.

Spec: docs/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2".

This is the second Phase 2 producer (capability_gap_monitor was the
first). It walks per-bot ObservationTuples, clusters by (noun, verb),
applies engagement + mood + recurrence gates, and runs an objective
categorization (confirmed vs emergent) against the bot's AGENTS.md
before emitting ``engagement_amplification_opportunity`` Signals.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import engagement_amplifier_monitor as m  # noqa: E402
from observations.tuples import write_tuples  # noqa: E402
from schema.observation import ObservationTuple  # noqa: E402
from signals import store as signals_store  # noqa: E402


BOT_ID = "team-bot-a"
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def _write_tuples(
    shared_dir: Path,
    bot_id: str,
    *,
    noun: str,
    verb: str = "tracking",
    n: int = 8,
    days_span: int = 8,
    engagement_each: int = 4,
    mood: str | None = "enthusiastic",
) -> None:
    """Write N tuples spread across `days_span` days, each in a unique
    session. Default values clear the monitor's recurrence + absolute
    engagement gates by a comfortable margin."""
    for i in range(n):
        day = NOW - timedelta(days=(i % days_span))
        t = ObservationTuple(
            id=f"obs-{noun}-{verb}-{i}",
            bot_id=bot_id,
            session_id=f"sess-{noun}-{verb}-{i}",
            segment_id=f"seg-{i}",
            noun=noun,
            verb=verb,
            mood=mood,
            engagement=engagement_each,
            timestamp_start=day.isoformat(),
            timestamp_end=(day + timedelta(minutes=5)).isoformat(),
            source_hash=f"hash-{noun}-{verb}-{i}",
        )
        write_tuples([t], shared_dir=shared_dir, bot_id=bot_id, day=day)


def _stub_agents_md(tmp_path, bot_id, *, content="", monkeypatch):
    """Drop a fake AGENTS.md and patch the monitor's reader to find it."""
    p = tmp_path / "agents" / f"{bot_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        m, "_bot_workspace_agents_md",
        lambda bid: p if bid == bot_id else Path("/nope"),
    )
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Percentile math
# ─────────────────────────────────────────────────────────────────────────────


def test_percentile_handles_empty_list():
    assert m._percentile([], 0.75) == 0.0


def test_percentile_75th_of_four_elements_is_third():
    """Nearest-rank — the 75th percentile of [1, 2, 3, 4] is 3 (the
    3rd element, 1-indexed at ceil(0.75 * 4) = 3)."""
    assert m._percentile([1.0, 2.0, 3.0, 4.0], 0.75) == 3.0


def test_percentile_handles_single_element():
    assert m._percentile([5.0], 0.75) == 5.0


# ─────────────────────────────────────────────────────────────────────────────
# Objective alignment
# ─────────────────────────────────────────────────────────────────────────────


def test_objective_alignment_confirmed_when_domain_in_purpose():
    """A noun mapping to domain:fitness with 'fitness' in AGENTS.md
    purpose → 'confirmed' alignment."""
    kw = m._domain_keywords()
    assert m._objective_alignment(
        "This bot helps with fitness coaching.", "workout", kw
    ) == "confirmed"


def test_objective_alignment_emergent_when_domain_not_in_purpose():
    """A noun whose domain isn't in the bot's stated purpose →
    'emergent' alignment (users converged organically)."""
    kw = m._domain_keywords()
    assert m._objective_alignment(
        "This bot manages a sailing schedule.", "workout", kw
    ) == "emergent"


def test_objective_alignment_emergent_for_unknown_noun():
    """A noun the keyword vocabulary doesn't recognize at all defaults
    to 'emergent' — the value is "users keep coming back to this"
    regardless of whether we can name the domain."""
    kw = m._domain_keywords()
    assert m._objective_alignment(
        "This bot does anything.", "squidlogging", kw
    ) == "emergent"


def test_objective_alignment_skip_without_purpose():
    """No AGENTS.md → skip. Conservative: don't fire on a bot whose
    role we can't read."""
    kw = m._domain_keywords()
    assert m._objective_alignment(None, "workout", kw) == "skip"


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end detection
# ─────────────────────────────────────────────────────────────────────────────


def test_emits_signal_when_pattern_strong_and_alignment_confirmed(
    tmp_path, monkeypatch
):
    """Happy path: 8 enthusiastic-mood tuples across 8 days, AGENTS.md
    confirms domain. Monitor must emit a signal whose details match
    the cluster shape."""
    _stub_agents_md(
        tmp_path, BOT_ID,
        content="fitness coach",
        monkeypatch=monkeypatch,
    )
    _write_tuples(tmp_path, BOT_ID, noun="workout", verb="tracking")
    detections = m.detect_amplification_opportunities(
        BOT_ID, tmp_path, now=NOW
    )
    assert len(detections) == 1
    d = detections[0]
    assert d["type"] == "engagement_amplification_opportunity"
    assert d["bot_id"] == BOT_ID
    assert d["signature"] == (
        f"engagement_amplification:{BOT_ID}:workout:tracking"
    )
    assert d["details"]["objective_alignment"] == "confirmed"
    assert d["details"]["noun"] == "workout"
    assert d["details"]["verb"] == "tracking"
    assert d["details"]["distinct_sessions"] == 8
    assert d["details"]["positive_mood_share"] > 0


def test_emits_emergent_when_alignment_not_in_purpose(
    tmp_path, monkeypatch
):
    """High-engagement workout pattern on a sailing bot fires as
    ``emergent`` — users converged on something outside the stated
    purpose, which is exactly the worth-noticing case."""
    _stub_agents_md(
        tmp_path, BOT_ID,
        content="This bot manages sailing schedules.",
        monkeypatch=monkeypatch,
    )
    _write_tuples(tmp_path, BOT_ID, noun="workout", verb="tracking")
    detections = m.detect_amplification_opportunities(
        BOT_ID, tmp_path, now=NOW
    )
    assert len(detections) == 1
    assert detections[0]["details"]["objective_alignment"] == "emergent"


def test_no_emission_when_pattern_below_engagement_floor(
    tmp_path, monkeypatch
):
    """Engagement_total < ABSOLUTE_ENGAGEMENT_FLOOR → no signal.
    Stops the trivial case of a single weak cluster."""
    _stub_agents_md(
        tmp_path, BOT_ID, content="fitness", monkeypatch=monkeypatch
    )
    # 8 sessions x engagement 1 = 8, below the floor of 15.
    _write_tuples(
        tmp_path, BOT_ID, noun="workout", verb="tracking",
        engagement_each=1,
    )
    assert m.detect_amplification_opportunities(
        BOT_ID, tmp_path, now=NOW
    ) == []


def test_no_emission_when_frustration_too_high(tmp_path, monkeypatch):
    """High engagement + frustration > MAX_FRUSTRATED_SHARE → drop.
    Persona tuner handles those; amplifier doesn't."""
    _stub_agents_md(
        tmp_path, BOT_ID, content="fitness", monkeypatch=monkeypatch
    )
    _write_tuples(
        tmp_path, BOT_ID, noun="workout", verb="tracking",
        mood="frustrated",
    )
    assert m.detect_amplification_opportunities(
        BOT_ID, tmp_path, now=NOW
    ) == []


def test_no_emission_when_recurrence_below_threshold(
    tmp_path, monkeypatch
):
    """High engagement in only 1 day, 3 sessions → drops on the
    recurrence gate."""
    _stub_agents_md(
        tmp_path, BOT_ID, content="fitness", monkeypatch=monkeypatch
    )
    _write_tuples(
        tmp_path, BOT_ID, noun="workout", verb="tracking",
        n=3, days_span=1, engagement_each=10,
    )
    assert m.detect_amplification_opportunities(
        BOT_ID, tmp_path, now=NOW
    ) == []


def test_no_emission_without_agents_md(tmp_path, monkeypatch):
    """No readable AGENTS.md → ``skip`` → no signal. Fail-closed."""
    monkeypatch.setattr(
        m, "_bot_workspace_agents_md",
        lambda bid: tmp_path / "missing.md",
    )
    _write_tuples(tmp_path, BOT_ID, noun="workout", verb="tracking")
    assert m.detect_amplification_opportunities(
        BOT_ID, tmp_path, now=NOW
    ) == []


def test_per_bot_signal_cap_enforced(tmp_path, monkeypatch):
    """If 5 different (noun, verb) clusters all clear gates, the
    monitor must emit at most MAX_SIGNALS_PER_BOT (3), ranked by
    engagement_total descending."""
    _stub_agents_md(
        tmp_path, BOT_ID, content="fitness budget reading reflection",
        monkeypatch=monkeypatch,
    )
    # 5 strong clusters with descending engagement.
    for i, noun in enumerate(
        ["workout", "budget", "reading", "journal", "fitness"]
    ):
        eng = 10 + (5 - i)  # 15, 14, 13, 12, 11
        _write_tuples(
            tmp_path, BOT_ID, noun=noun, verb="tracking",
            engagement_each=eng,
        )
    detections = m.detect_amplification_opportunities(
        BOT_ID, tmp_path, now=NOW
    )
    assert len(detections) <= m.MAX_SIGNALS_PER_BOT
    # Strongest engagement comes first.
    eng_values = [d["details"]["engagement_total"] for d in detections]
    assert eng_values == sorted(eng_values, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Sweep-resolve + idempotency
# ─────────────────────────────────────────────────────────────────────────────


def test_sweep_resolve_clears_stale_signal(tmp_path, monkeypatch):
    """A previously firing opportunity whose pattern faded gets
    resolved by sweep at the end of run_for_pod."""
    _stub_agents_md(
        tmp_path, BOT_ID, content="fitness", monkeypatch=monkeypatch
    )
    # Drop a firing signal as if last week's monitor had emitted it.
    sig = signals_store.observe(
        tmp_path,
        signature=f"engagement_amplification:{BOT_ID}:workout:tracking",
        producer=m.PRODUCER,
        type=m.SIGNAL_TYPE,
        flavor="activity",
        severity="info",
        scope="bot",
        bot_id=BOT_ID,
        title="prior opportunity",
        body="historical",
        details={
            "bot_id": BOT_ID, "noun": "workout", "verb": "tracking"
        },
    )
    assert sig.state == "firing"

    # No observations this run → no detections → sweep resolves.
    summary = m.run_for_pod([BOT_ID], tmp_path, now=NOW)
    assert summary["signals_emitted"] == 0
    assert summary["signals_resolved"] >= 1


def test_repeat_run_reuses_existing_signal(tmp_path, monkeypatch):
    """Running the monitor twice with the same observations bumps
    observation_count, doesn't create duplicate signals."""
    _stub_agents_md(
        tmp_path, BOT_ID, content="fitness", monkeypatch=monkeypatch
    )
    _write_tuples(tmp_path, BOT_ID, noun="workout", verb="tracking")
    m.run_for_bot(BOT_ID, tmp_path, now=NOW)
    m.run_for_bot(BOT_ID, tmp_path, now=NOW)
    firing = [
        s for s in signals_store.iter_active(tmp_path, bot_id=BOT_ID)
        if s.type == m.SIGNAL_TYPE
    ]
    assert len(firing) == 1
    assert firing[0].observation_count >= 2
