"""tests/test_capability_gap_monitor.py — pin the v1 capability-gap
monitor that closes the producer side of app_suggester's
``app_suggester_gap`` Signal contract.

Spec: internal/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2".

The monitor walks per-bot ObservationTuples, clusters by noun, maps
nouns to ``domain:*`` tags via the same vocabulary app_suggester uses
for coverage, and emits ``app_suggester_gap`` Signals for catalog
categories the bot doesn't cover.

These tests use the on-disk ObservationTuple writer so we exercise
the real read path (no mocked clusters). The Signal store + AGENTS.md
read are exercised through tmp_path / monkeypatch so the tests are
hermetic.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import capability_gap_monitor as m  # noqa: E402
from observations.tuples import write_tuples  # noqa: E402
from schema.observation import ObservationTuple  # noqa: E402
from signals import store as signals_store  # noqa: E402


BOT_ID = "team-bot-a"
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def _write_observation_tuples(
    shared_dir: Path,
    bot_id: str,
    *,
    noun: str,
    verb: str = "tracking",
    n: int = 6,
    days_span: int = 6,
    engagement_each: int = 3,
) -> None:
    """Write N synthetic ObservationTuples spread across `days_span`
    days, each in its own session. Default values clear the default
    strength thresholds (≥3 sessions, ≥10 engagement, ≥3 days)."""
    for i in range(n):
        day = NOW - timedelta(days=(i % days_span))
        t = ObservationTuple(
            id=f"obs-{noun}-{i}",
            bot_id=bot_id,
            session_id=f"sess-{i}",  # unique session per tuple
            segment_id=f"seg-{i}",
            noun=noun,
            verb=verb,
            mood=None,
            engagement=engagement_each,
            timestamp_start=day.isoformat(),
            timestamp_end=(day + timedelta(minutes=5)).isoformat(),
            source_hash=f"hash-{noun}-{i}",
        )
        write_tuples([t], shared_dir=shared_dir, bot_id=bot_id, day=day)


def _stub_workspace_agents_md(
    tmp_path: Path,
    bot_id: str,
    *,
    content: str = "",
    monkeypatch=None,
) -> Path:
    """Write a fake AGENTS.md and patch the read helper to look at it."""
    p = tmp_path / "agents" / f"{bot_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        m, "_bot_workspace_agents_md", lambda bid: p if bid == bot_id else Path("/nope")
    )
    return p


def _stub_no_manifests(shared_dir: Path, bot_id: str) -> None:
    """Ensure manifest dir exists but is empty so coverage is empty."""
    (shared_dir / "applications" / bot_id).mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Noun → domain mapping
# ─────────────────────────────────────────────────────────────────────────────


def test_noun_to_domains_maps_known_keyword():
    """A noun containing a known domain keyword surfaces that domain.
    'workout' and 'fitness' both → domain:fitness, etc."""
    kw = m._domain_keywords()
    assert "domain:fitness" in m._noun_to_domains("workout log", kw)
    assert "domain:health" in m._noun_to_domains("health stuff", kw)


def test_noun_to_domains_unknown_returns_empty():
    """Nouns with no domain keyword in them must return empty —
    we silently ignore vocabulary we don't recognize rather than
    fabricating a domain."""
    kw = m._domain_keywords()
    assert m._noun_to_domains("squid", kw) == set()
    assert m._noun_to_domains("", kw) == set()


def test_noun_to_domains_multi_domain():
    """A noun matching multiple keywords surfaces every implied
    domain. 'fitness journal' → fitness + productivity."""
    kw = m._domain_keywords()
    out = m._noun_to_domains("fitness journal", kw)
    assert "domain:fitness" in out
    assert "domain:productivity" in out


# ─────────────────────────────────────────────────────────────────────────────
# Objective fit
# ─────────────────────────────────────────────────────────────────────────────


def test_objective_fit_confirmed_when_keyword_in_purpose():
    """If the bot's AGENTS.md mentions a domain keyword, objective_fit
    is ``confirmed`` — emit at default strength threshold."""
    kw = m._domain_keywords()
    purpose = "This bot is a fitness coach and tracks workouts."
    assert m._objective_fit(purpose, "domain:fitness", kw) == "confirmed"


def test_objective_fit_neutral_when_no_match():
    """No keyword for this domain in the purpose text → ``neutral``
    (still emit if pattern is overwhelming)."""
    kw = m._domain_keywords()
    purpose = "This bot helps with sailing."
    assert m._objective_fit(purpose, "domain:fitness", kw) == "neutral"


def test_objective_fit_skip_when_no_purpose():
    """No AGENTS.md readable → ``skip`` — don't emit a Signal without
    knowing the bot's role. Conservative fail-closed."""
    kw = m._domain_keywords()
    assert m._objective_fit(None, "domain:fitness", kw) == "skip"
    assert m._objective_fit("", "domain:fitness", kw) == "skip"


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end detection
# ─────────────────────────────────────────────────────────────────────────────


def test_emits_signal_when_pattern_strong_and_fit_confirmed(
    tmp_path, monkeypatch
):
    """Happy path: 6 observation tuples across 6 days mentioning
    'workout', no installed fitness app, AGENTS.md mentions fitness.
    Monitor must emit a single ``app_suggester_gap`` Signal whose
    details.category is fitness_tracking."""
    _stub_workspace_agents_md(
        tmp_path, BOT_ID,
        content="This bot is a fitness coach.",
        monkeypatch=monkeypatch,
    )
    _stub_no_manifests(tmp_path, BOT_ID)
    _write_observation_tuples(tmp_path, BOT_ID, noun="workout")

    detections = m.detect_capability_gaps(BOT_ID, tmp_path, now=NOW)
    cats = [d["details"]["category"] for d in detections]
    assert "fitness_tracking" in cats, (
        f"Expected fitness_tracking signal; got {cats}"
    )
    fit_sig = next(
        d for d in detections if d["details"]["category"] == "fitness_tracking"
    )
    assert fit_sig["type"] == "app_suggester_gap"
    assert fit_sig["bot_id"] == BOT_ID
    assert (
        fit_sig["signature"]
        == f"app_suggester_gap:{BOT_ID}:fitness_tracking"
    ), "Signature shape must match app_suggester's grounding lookup"
    assert fit_sig["details"]["objective_fit"] == "confirmed"


def test_no_emission_when_pattern_below_strength(tmp_path, monkeypatch):
    """A single observation in one session must NOT emit a Signal —
    that's the session_token_outlier-shaped failure the spec calls
    out as the anti-pattern."""
    _stub_workspace_agents_md(
        tmp_path, BOT_ID,
        content="fitness coach",
        monkeypatch=monkeypatch,
    )
    _stub_no_manifests(tmp_path, BOT_ID)
    # Only 1 session, 1 day, 1 engagement — way below all thresholds.
    _write_observation_tuples(
        tmp_path, BOT_ID, noun="workout", n=1, days_span=1, engagement_each=1
    )
    assert m.detect_capability_gaps(BOT_ID, tmp_path, now=NOW) == []


def test_no_emission_when_no_agents_md(tmp_path, monkeypatch):
    """Without a readable AGENTS.md the objective_fit is ``skip`` and
    the monitor stays silent. Fail-closed."""
    # Point AGENTS.md at a nonexistent path.
    monkeypatch.setattr(
        m, "_bot_workspace_agents_md", lambda bid: tmp_path / "missing.md"
    )
    _stub_no_manifests(tmp_path, BOT_ID)
    _write_observation_tuples(tmp_path, BOT_ID, noun="workout")
    assert m.detect_capability_gaps(BOT_ID, tmp_path, now=NOW) == []


def test_neutral_fit_requires_stronger_pattern(tmp_path, monkeypatch):
    """When AGENTS.md doesn't mention the candidate domain, the
    monitor only emits when the pattern is overwhelming (the
    NEUTRAL_MIN_* thresholds). A pattern that would pass the
    confirmed bar must NOT pass the neutral bar."""
    _stub_workspace_agents_md(
        tmp_path, BOT_ID,
        content="This bot handles sailing logistics.",
        monkeypatch=monkeypatch,
    )
    _stub_no_manifests(tmp_path, BOT_ID)
    # Pattern that clears confirmed (3/10/3) but NOT neutral (5/25/3).
    _write_observation_tuples(
        tmp_path, BOT_ID, noun="workout", n=3, days_span=3, engagement_each=4
    )
    # objective_fit=neutral, distinct_sessions=3 < 5 → drops.
    assert m.detect_capability_gaps(BOT_ID, tmp_path, now=NOW) == []


def test_no_emission_for_covered_domain(tmp_path, monkeypatch):
    """When the bot already has an installed app covering the domain,
    the candidate must be suppressed. The app_suggester catalog
    won't surface the category in app_suggester either, so the
    Signal would be inert noise."""
    _stub_workspace_agents_md(
        tmp_path, BOT_ID,
        content="fitness coach",
        monkeypatch=monkeypatch,
    )
    # Drop a fitness-shaped manifest so coverage picks up fitness.
    manif_dir = tmp_path / "applications" / BOT_ID
    manif_dir.mkdir(parents=True)
    (manif_dir / "workout-log.json").write_text(
        '{"name": "workout-log", "description": "tracks workouts"}',
        encoding="utf-8",
    )
    _write_observation_tuples(tmp_path, BOT_ID, noun="workout")
    detections = m.detect_capability_gaps(BOT_ID, tmp_path, now=NOW)
    cats = [d["details"]["category"] for d in detections]
    assert "fitness_tracking" not in cats, (
        f"fitness_tracking emitted despite installed fitness manifest; "
        f"got {cats}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sweep-resolve
# ─────────────────────────────────────────────────────────────────────────────


def test_sweep_resolve_clears_stale_signal(tmp_path, monkeypatch):
    """When a previously firing Signal's gap closes (no detections
    this run), the sweep at the end of run_for_pod must resolve it."""
    _stub_workspace_agents_md(
        tmp_path, BOT_ID,
        content="fitness coach",
        monkeypatch=monkeypatch,
    )
    _stub_no_manifests(tmp_path, BOT_ID)
    # Manually drop a firing app_suggester_gap signal as if the
    # monitor had emitted it last week.
    sig = signals_store.observe(
        tmp_path,
        signature=f"app_suggester_gap:{BOT_ID}:fitness_tracking",
        producer=m.PRODUCER,
        type=m.SIGNAL_TYPE,
        flavor="activity",
        severity="info",
        scope="bot",
        bot_id=BOT_ID,
        title="prior gap",
        body="historical",
        details={"category": "fitness_tracking", "bot_id": BOT_ID},
    )
    assert sig.state == "firing"

    # No observations this run → no new detections → sweep resolves
    # the stale one.
    summary = m.run_for_pod([BOT_ID], tmp_path, now=NOW)
    assert summary["signals_emitted"] == 0
    assert summary["signals_resolved"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────────


def test_repeat_run_reuses_existing_signal(tmp_path, monkeypatch):
    """Running the monitor twice with the same observations must NOT
    create two Signals. signals_store.observe is find-or-create by
    signature; a re-run bumps observation_count, not creates."""
    _stub_workspace_agents_md(
        tmp_path, BOT_ID,
        content="fitness coach",
        monkeypatch=monkeypatch,
    )
    _stub_no_manifests(tmp_path, BOT_ID)
    _write_observation_tuples(tmp_path, BOT_ID, noun="workout")
    m.run_for_bot(BOT_ID, tmp_path, now=NOW)
    m.run_for_bot(BOT_ID, tmp_path, now=NOW)
    firing = list(signals_store.iter_active(tmp_path, bot_id=BOT_ID))
    sigs = [s for s in firing if s.type == m.SIGNAL_TYPE]
    assert len(sigs) == 1
    # observation_count goes up on the second visit.
    assert sigs[0].observation_count >= 2
