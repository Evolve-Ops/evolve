"""tests/test_cascade_labeler.py — audit-layer Labeler (Phase 2).

Verifies the ground-truth signal extraction per spec § 2.3 Component 1.
Covers:
  - Signal #1: UI-chip override events → SHOULD_HAVE_ESCALATED / DEMOTED
  - Signal #3: struggle-resolution-at-next-tier → CORRECTLY_ESCALATED
  - Hijacked-holdout exclusion (round-3 #1)
  - Idempotent file writes
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from cascade.labeler import (  # noqa: E402
    Label,
    LabelSource,
    LabeledOutcome,
    SessionFeatures,
    _accumulate_session_features,
    label_session,
    label_spans,
    write_labels,
)


# ─────────────────────────────────────────────────────────────────────────────
# Span helpers
# ─────────────────────────────────────────────────────────────────────────────


def _span(*, session_id="s1", bot_id="team_bot_a", turn_index=1,
          tier_used="tier2", tier_chosen_by="classifier",
          struggle_score=None, holdout=False, end_time="2026-05-27T12:00:00Z"):
    """Build a synthetic span dict matching the cascade telemetry shape."""
    attrs = {
        "session_id": session_id,
        "turn_index": turn_index,
        "cascade.tier_used": tier_used,
        "cascade.tier_chosen_by": tier_chosen_by,
        "cascade.holdout": holdout,
        "cascade.variant": "baseline" if holdout else "A",
    }
    if struggle_score is not None:
        attrs["cascade.struggle.score"] = struggle_score
    return {
        "bot_id": bot_id,
        "end_time": end_time,
        "start_time": end_time,
        "attributes": attrs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Signal #1: UI-chip override (STRONG)
# ─────────────────────────────────────────────────────────────────────────────


def test_ui_chip_override_to_tier1_labels_should_have_escalated():
    spans = [
        _span(session_id="s1", turn_index=1, tier_used="tier2", tier_chosen_by="classifier"),
        _span(session_id="s1", turn_index=2, tier_used="tier1", tier_chosen_by="user_request"),
        _span(session_id="s1", turn_index=3, tier_used="tier1", tier_chosen_by="user_request"),
    ]
    outcomes = list(label_spans(spans))
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.session_id == "s1"
    assert o.label == Label.SHOULD_HAVE_ESCALATED
    assert o.source == LabelSource.UI_CHIP_OVERRIDE
    assert o.confidence == 0.9


def test_ui_chip_override_to_tier3_labels_should_have_demoted():
    spans = [
        _span(session_id="s1", turn_index=1, tier_used="tier2", tier_chosen_by="classifier"),
        _span(session_id="s1", turn_index=2, tier_used="tier3", tier_chosen_by="user_request"),
    ]
    outcomes = list(label_spans(spans))
    assert len(outcomes) == 1
    assert outcomes[0].label == Label.SHOULD_HAVE_DEMOTED


def test_no_ui_chip_no_label_from_signal_1():
    # All turns at tier2 with classifier — no UI chip override fires.
    spans = [
        _span(session_id="s1", turn_index=i, tier_used="tier2", tier_chosen_by="classifier")
        for i in range(1, 5)
    ]
    outcomes = list(label_spans(spans))
    # No label from signal #1; signal #3 also no match (no escalation in timeline).
    assert outcomes == []


# ─────────────────────────────────────────────────────────────────────────────
# Signal #3: struggle resolution at next tier (STRONG)
# ─────────────────────────────────────────────────────────────────────────────


def test_struggle_resolution_after_escalation_labels_correctly_escalated():
    # Turns 1-3 at tier2 with high struggle; turn 4 escalates to tier1
    # (autonomously, NOT via UI chip); turns 4-5 at tier1 with low struggle.
    spans = [
        _span(session_id="s1", turn_index=1, tier_used="tier2", tier_chosen_by="cascade", struggle_score=0.7),
        _span(session_id="s1", turn_index=2, tier_used="tier2", tier_chosen_by="cascade", struggle_score=0.8),
        _span(session_id="s1", turn_index=3, tier_used="tier2", tier_chosen_by="cascade", struggle_score=0.75),
        _span(session_id="s1", turn_index=4, tier_used="tier1", tier_chosen_by="cascade", struggle_score=0.15),
        _span(session_id="s1", turn_index=5, tier_used="tier1", tier_chosen_by="cascade", struggle_score=0.10),
    ]
    outcomes = list(label_spans(spans))
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.label == Label.CORRECTLY_ESCALATED
    assert o.source == LabelSource.STRUGGLE_RESOLUTION
    assert o.evidence["from_tier"] == "tier2"
    assert o.evidence["to_tier"] == "tier1"


def test_struggle_resolution_without_sustained_pre_struggle_no_label():
    # Only ONE high-struggle turn before escalation — needs ≥2 to fire.
    spans = [
        _span(session_id="s1", turn_index=1, tier_used="tier2", tier_chosen_by="cascade", struggle_score=0.3),
        _span(session_id="s1", turn_index=2, tier_used="tier2", tier_chosen_by="cascade", struggle_score=0.8),
        _span(session_id="s1", turn_index=3, tier_used="tier1", tier_chosen_by="cascade", struggle_score=0.1),
        _span(session_id="s1", turn_index=4, tier_used="tier1", tier_chosen_by="cascade", struggle_score=0.1),
    ]
    outcomes = list(label_spans(spans))
    assert outcomes == []


def test_struggle_resolution_without_low_post_struggle_no_label():
    # Escalation happened, but struggle stayed high at tier1 too — escalation
    # didn't help, NOT a "correctly_escalated" signal.
    spans = [
        _span(session_id="s1", turn_index=1, tier_used="tier2", tier_chosen_by="cascade", struggle_score=0.7),
        _span(session_id="s1", turn_index=2, tier_used="tier2", tier_chosen_by="cascade", struggle_score=0.8),
        _span(session_id="s1", turn_index=3, tier_used="tier1", tier_chosen_by="cascade", struggle_score=0.7),
        _span(session_id="s1", turn_index=4, tier_used="tier1", tier_chosen_by="cascade", struggle_score=0.6),
    ]
    outcomes = list(label_spans(spans))
    assert outcomes == []


# ─────────────────────────────────────────────────────────────────────────────
# Hijacked-holdout exclusion (round-3 #1)
# ─────────────────────────────────────────────────────────────────────────────


def test_hijacked_holdout_excluded_from_labeling():
    # Holdout session where user picked Power mid-session = hijacked.
    # MUST NOT be labeled (would contaminate the tuner per round-3 #1).
    spans = [
        _span(session_id="s1", turn_index=1, tier_used="tier2", tier_chosen_by="classifier", holdout=True),
        _span(session_id="s1", turn_index=2, tier_used="tier1", tier_chosen_by="user_request", holdout=True),
    ]
    outcomes = list(label_spans(spans))
    assert outcomes == []  # No label despite UI chip event — hijacked


def test_holdout_without_hijack_can_still_be_labeled():
    # A holdout session that goes through normally (no UI chip override)
    # CAN still produce labels from other signals (e.g., struggle resolution).
    # The exclusion is specifically for the hijack scenario.
    spans = [
        _span(session_id="s1", turn_index=1, tier_used="tier2", tier_chosen_by="cascade",
              holdout=True, struggle_score=0.7),
        _span(session_id="s1", turn_index=2, tier_used="tier2", tier_chosen_by="cascade",
              holdout=True, struggle_score=0.8),
        _span(session_id="s1", turn_index=3, tier_used="tier1", tier_chosen_by="cascade",
              holdout=True, struggle_score=0.1),
        _span(session_id="s1", turn_index=4, tier_used="tier1", tier_chosen_by="cascade",
              holdout=True, struggle_score=0.15),
    ]
    outcomes = list(label_spans(spans))
    # Note: holdout sessions don't actually run cascade in production, but
    # for THIS TEST the spans say they did — labeler should still produce
    # the signal because the input data shape is what matters here.
    assert len(outcomes) == 1
    assert outcomes[0].label == Label.CORRECTLY_ESCALATED


# ─────────────────────────────────────────────────────────────────────────────
# Multi-session input
# ─────────────────────────────────────────────────────────────────────────────


def test_label_spans_handles_multiple_sessions():
    spans = [
        # Session s1: UI chip override → should_have_escalated
        _span(session_id="s1", turn_index=1, tier_used="tier2", tier_chosen_by="classifier"),
        _span(session_id="s1", turn_index=2, tier_used="tier1", tier_chosen_by="user_request"),
        # Session s2: clean no-label session
        _span(session_id="s2", turn_index=1, tier_used="tier2", tier_chosen_by="classifier"),
        # Session s3: struggle resolution
        _span(session_id="s3", turn_index=1, tier_used="tier2", tier_chosen_by="cascade", struggle_score=0.7),
        _span(session_id="s3", turn_index=2, tier_used="tier2", tier_chosen_by="cascade", struggle_score=0.8),
        _span(session_id="s3", turn_index=3, tier_used="tier1", tier_chosen_by="cascade", struggle_score=0.1),
        _span(session_id="s3", turn_index=4, tier_used="tier1", tier_chosen_by="cascade", struggle_score=0.1),
    ]
    outcomes = {o.session_id: o for o in label_spans(spans)}
    assert "s1" in outcomes
    assert "s2" not in outcomes  # no signal fired
    assert "s3" in outcomes
    assert outcomes["s1"].label == Label.SHOULD_HAVE_ESCALATED
    assert outcomes["s3"].label == Label.CORRECTLY_ESCALATED


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────


def test_write_labels_creates_file(tmp_path: Path):
    outcomes = [
        LabeledOutcome(
            session_id="s1", bot_id="team_bot_a",
            label=Label.SHOULD_HAVE_ESCALATED, confidence=0.9,
            source=LabelSource.UI_CHIP_OVERRIDE, evidence={},
        ),
    ]
    report = write_labels(outcomes, tmp_path, day="2026-05-27")
    assert report["written"] == 1
    assert "2026-05-27" in report["path"]

    path = tmp_path / "cascade" / "labels" / "2026-05-27.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["session_id"] == "s1"
    assert parsed["label"] == "should_have_escalated"


def test_write_labels_appends_on_re_run(tmp_path: Path):
    o1 = LabeledOutcome(
        session_id="s1", bot_id="team_bot_a",
        label=Label.SHOULD_HAVE_ESCALATED, confidence=0.9,
        source=LabelSource.UI_CHIP_OVERRIDE,
    )
    o2 = LabeledOutcome(
        session_id="s2", bot_id="team_bot_a",
        label=Label.CORRECTLY_ESCALATED, confidence=0.75,
        source=LabelSource.STRUGGLE_RESOLUTION,
    )
    write_labels([o1], tmp_path, day="2026-05-27")
    write_labels([o2], tmp_path, day="2026-05-27")
    path = tmp_path / "cascade" / "labels" / "2026-05-27.jsonl"
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2  # appended


# ─────────────────────────────────────────────────────────────────────────────
# Feature accumulator
# ─────────────────────────────────────────────────────────────────────────────


def test_accumulate_features_sorts_by_turn_index():
    # Spans out of order; accumulator must sort them.
    spans = [
        _span(session_id="s1", turn_index=3, tier_used="tier1"),
        _span(session_id="s1", turn_index=1, tier_used="tier2"),
        _span(session_id="s1", turn_index=2, tier_used="tier2"),
    ]
    features = _accumulate_session_features(spans)
    assert features.turn_count == 3
    assert features.timeline[0]["turn_index"] == 1
    assert features.timeline[1]["turn_index"] == 2
    assert features.timeline[2]["turn_index"] == 3


def test_accumulate_features_detects_holdout_hijack():
    # Holdout session with UI-chip user_request mid-stream → hijacked.
    spans = [
        _span(session_id="s1", turn_index=1, tier_chosen_by="classifier", holdout=True),
        _span(session_id="s1", turn_index=2, tier_chosen_by="user_request", holdout=True),
    ]
    features = _accumulate_session_features(spans)
    assert features.holdout_hijacked is True
    # Note: holdout flag is FLIPPED to false to prevent double-counting.
    assert features.holdout is False


def test_empty_spans_returns_empty_session():
    features = _accumulate_session_features([])
    assert features.session_id == "<empty>"
    assert features.turn_count == 0
