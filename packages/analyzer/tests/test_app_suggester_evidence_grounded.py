"""tests/test_app_suggester_evidence_grounded.py — pin the
post-2026-06-05 evidence-grounded operator-facing pitch.

Spec: docs/spec-rsi-proposal-eligibility-2026-06-05.md.

After capability_gap_monitor shipped, the Signals it produces carry
``details.example_nouns / distinct_sessions / distinct_days /
engagement_total / objective_fit / window_days``. app_suggester now
quotes those in its proposal Summary and Explanation when present —
turning a generic "consider X" pitch into an evidence-grounded
recommendation that names the actual conversational pattern.

These tests pin:
  1. The Phase A operator-first content fields (summary, explanation,
     action_label, manual_path) are populated.
  2. When the grounding Signal carries the capability_gap_monitor
     shape, the summary quotes the actual nouns + sessions + days.
  3. The explanation cites engagement total and AGENTS.md objective
     fit when present.
  4. Backward-compat: a Signal with no details (pre-spec producer,
     fixture-only) still produces a valid proposal, just without
     the evidence section.
  5. End-to-end: dropping a capability_gap_monitor-shaped Signal
     and running observe() emits a Proposal whose summary names
     the noun the monitor put in details.example_nouns.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from generators.app_suggester.observe import (  # noqa: E402
    AppSuggesterContext,
    _make_proposal,
    observe,
)
from signals import store as signals_store  # noqa: E402


BOT_ID = "team-bot-a"


def _entry() -> dict:
    return {
        "category": "fitness_tracking",
        "title": "Workout log",
        "description": "Track workouts conversationally.",
        "example_apps": ["workout-log"],
        "tags": ["domain:fitness", "intent:explore"],
    }


class _StubSignal:
    """A minimal Signal-shaped object — just the attributes
    ``_make_proposal`` reads. The store would normally hand us
    full Signal dataclasses; this stub keeps the test hermetic."""

    def __init__(self, sig_id: str, **details):
        self.id = sig_id
        self.details = details


# ─────────────────────────────────────────────────────────────────────────────
# Phase A operator-first fields are populated
# ─────────────────────────────────────────────────────────────────────────────


def test_phase_a_fields_populated_with_evidence():
    """Summary, explanation, action_label, manual_path all set when
    rich grounding signals are present."""
    p = _make_proposal(
        BOT_ID,
        _entry(),
        covered_domains=set(),
        motivating_signals=["sig-1"],
        grounding_signals=[
            _StubSignal(
                "sig-1",
                category="fitness_tracking",
                example_nouns=["workout", "fitness"],
                distinct_sessions=8,
                distinct_days=12,
                engagement_total=47,
                objective_fit="confirmed",
                window_days=30,
            )
        ],
    )
    assert p is not None
    assert p.summary, "summary must be populated"
    assert p.explanation, "explanation must be populated"
    assert p.action_label == "Open Applications tab"
    assert p.manual_path == f"Applications → {BOT_ID} → Add"


def test_phase_a_fields_populated_even_without_evidence():
    """Backward compat: a Signal with no details still produces
    valid Phase A content — just without the evidence section."""
    p = _make_proposal(
        BOT_ID,
        _entry(),
        covered_domains=set(),
        motivating_signals=["sig-1"],
        grounding_signals=[_StubSignal("sig-1")],
    )
    assert p is not None
    # Summary + explanation are present; they just don't quote nouns.
    assert p.summary
    assert p.explanation
    assert p.action_label == "Open Applications tab"


# ─────────────────────────────────────────────────────────────────────────────
# Summary cites the actual evidence
# ─────────────────────────────────────────────────────────────────────────────


def test_summary_quotes_nouns_when_signals_carry_them():
    """The summary the operator sees first must name the actual nouns
    the user has been raising. That's the load-bearing piece of
    evidence-grounded RSI: 'workout' is concrete, 'fitness theme' is
    abstract."""
    p = _make_proposal(
        BOT_ID,
        _entry(),
        covered_domains=set(),
        motivating_signals=["sig-1"],
        grounding_signals=[
            _StubSignal(
                "sig-1",
                category="fitness_tracking",
                example_nouns=["workout", "fitness"],
                distinct_sessions=8,
                distinct_days=12,
                engagement_total=47,
                objective_fit="confirmed",
            )
        ],
    )
    assert p is not None
    assert "workout" in p.summary, (
        f"summary must quote example_nouns[0]; got {p.summary!r}"
    )
    assert "8" in p.summary, "summary must quote distinct_sessions count"
    assert "12" in p.summary, "summary must quote distinct_days count"


def test_summary_pluralization():
    """Single-session / single-day patterns must still read naturally —
    'in 1 session on 1 day' not 'in 1 sessions on 1 days'. Small
    surface but operator-readability matters when the queue is long."""
    p = _make_proposal(
        BOT_ID,
        _entry(),
        covered_domains=set(),
        motivating_signals=["sig-1"],
        grounding_signals=[
            _StubSignal(
                "sig-1",
                example_nouns=["workout"],
                distinct_sessions=1,
                distinct_days=1,
                engagement_total=5,
            )
        ],
    )
    assert p is not None
    assert "1 session " in p.summary
    assert "1 day;" in p.summary


# ─────────────────────────────────────────────────────────────────────────────
# Explanation cites engagement total + objective fit
# ─────────────────────────────────────────────────────────────────────────────


def test_explanation_cites_engagement_and_window_when_present():
    p = _make_proposal(
        BOT_ID,
        _entry(),
        covered_domains=set(),
        motivating_signals=["sig-1"],
        grounding_signals=[
            _StubSignal(
                "sig-1",
                example_nouns=["workout"],
                distinct_sessions=8,
                distinct_days=12,
                engagement_total=47,
                window_days=30,
                objective_fit="confirmed",
            )
        ],
    )
    assert p is not None
    assert "30 days" in p.explanation, (
        "explanation must cite the observation window"
    )
    assert "47" in p.explanation, (
        "explanation must cite engagement_total so the operator can "
        "gauge intensity, not just frequency"
    )
    assert "AGENTS.md" in p.explanation, (
        "explanation must surface the objective check — the operator "
        "needs to know the bot's role was confirmed against the domain"
    )


def test_explanation_handles_neutral_fit():
    p = _make_proposal(
        BOT_ID,
        _entry(),
        covered_domains=set(),
        motivating_signals=["sig-1"],
        grounding_signals=[
            _StubSignal(
                "sig-1",
                example_nouns=["workout"],
                distinct_sessions=5,
                distinct_days=5,
                engagement_total=30,
                window_days=30,
                objective_fit="neutral",
            )
        ],
    )
    assert p is not None
    assert "doesn't explicitly mention" in p.explanation, (
        "explanation must distinguish neutral from confirmed so the "
        "operator knows the AGENTS.md check returned uncertain"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Audit trail
# ─────────────────────────────────────────────────────────────────────────────


def test_evidence_recorded_in_provenance_for_audit():
    """Whatever we cite in the operator-facing pitch must also live
    in provenance.signals so a later reviewer can answer 'what did
    the system see when it emitted this?'. Critical for the
    arbiter's lineage scan + post-hoc 'why this proposal' explanation."""
    p = _make_proposal(
        BOT_ID,
        _entry(),
        covered_domains=set(),
        motivating_signals=["sig-1"],
        grounding_signals=[
            _StubSignal(
                "sig-1",
                example_nouns=["workout", "fitness"],
                distinct_sessions=8,
                distinct_days=12,
                engagement_total=47,
                objective_fit="confirmed",
                window_days=30,
            )
        ],
    )
    assert p is not None
    ev = p.provenance.signals.get("evidence")
    assert isinstance(ev, dict)
    assert ev.get("example_nouns") == ["workout", "fitness"]
    assert ev.get("distinct_sessions") == 8
    assert ev.get("objective_fit") == "confirmed"


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end against the Signal store
# ─────────────────────────────────────────────────────────────────────────────


def _drop_capability_gap_signal(
    shared_dir: Path, bot_id: str, category: str, **details
) -> None:
    """Drop a Signal in the shape capability_gap_monitor emits."""
    signals_store.observe(
        shared_dir,
        signature=f"app_suggester_gap:{bot_id}:{category}",
        producer="capability_gap_monitor",
        type="app_suggester_gap",
        flavor="activity",
        severity="info",
        scope="bot",
        bot_id=bot_id,
        title=f"Gap: {category}",
        body="(end-to-end test)",
        details={
            "category": category,
            "bot_id": bot_id,
            **details,
        },
    )


def test_observe_emits_evidence_grounded_proposal(tmp_path):
    """Drop a capability_gap_monitor-shaped Signal; run observe();
    confirm the resulting Proposal carries the evidence in its
    summary. This is the integration test that proves the producer
    (cap-gap monitor) and consumer (app_suggester) agree on the
    Signal details contract."""
    _drop_capability_gap_signal(
        tmp_path,
        BOT_ID,
        "fitness_tracking",
        example_nouns=["workout", "fitness"],
        distinct_sessions=8,
        distinct_days=12,
        engagement_total=47,
        objective_fit="confirmed",
        window_days=30,
        domain_tag="domain:fitness",
    )
    # Manifest dir exists but empty → no domain coverage.
    (tmp_path / "applications" / BOT_ID).mkdir(parents=True)
    ctx = AppSuggesterContext(
        bot_ids=[BOT_ID],
        shared_dir=tmp_path,
        max_per_run=10,
    )
    proposals = observe(ctx)
    fit = [
        p for p in proposals if "fitness_tracking" in p.trigger_observations[0]
    ]
    assert len(fit) == 1, (
        f"Expected one fitness_tracking proposal; got {len(fit)}"
    )
    p = fit[0]
    assert "workout" in p.summary, (
        f"end-to-end: monitor's example_nouns must reach the proposal "
        f"summary unchanged; got {p.summary!r}"
    )
    assert "8 session" in p.summary
    assert "12 day" in p.summary
    assert "confirmed" not in p.summary.lower(), (
        "the 'confirmed' fit goes in the explanation, not the summary "
        "(the summary stays operator-facing without the internal label)"
    )
    assert "AGENTS.md confirms" in p.explanation
