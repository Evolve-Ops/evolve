"""tests/test_rsi_tuples.py — ObservationTuple schema, storage, extraction."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from observations import window  # noqa: E402
from observations.extract import (  # noqa: E402
    ExtractorVocabulary,
    Transcript,
    Turn,
    extract_tuples,
    reset_extractor,
    set_extractor,
)
from observations.tuples import (  # noqa: E402
    read_tuples_range,
    write_tuples,
)
from schema.observation import (  # noqa: E402
    ClusterSpec,
    MOOD_VOCABULARY,
    ObservationTuple,
    VERB_VOCABULARY,
    new_tuple_id,
)


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────


def _good_tuple(**overrides) -> ObservationTuple:
    base = dict(
        id=new_tuple_id(),
        bot_id="team_bot_a",
        session_id="s1",
        segment_id="seg-0",
        noun="fitness",
        verb="tracking",
        mood="enthusiastic",
        engagement=5,
        timestamp_start="2026-05-01T12:00:00+00:00",
        timestamp_end="2026-05-01T12:15:00+00:00",
        source_hash="abc123",
        extraction_confidence=0.8,
    )
    base.update(overrides)
    return ObservationTuple(**base)


def test_rejects_invalid_verb():
    with pytest.raises(ValueError):
        _good_tuple(verb="dance")


def test_rejects_invalid_mood():
    with pytest.raises(ValueError):
        _good_tuple(mood="spicy")


def test_accepts_none_mood():
    t = _good_tuple(mood=None)
    assert t.mood is None


def test_normalizes_noun_to_lowercase():
    t = _good_tuple(noun="FITNESS")
    assert t.noun == "fitness"


def test_rejects_empty_noun():
    with pytest.raises(ValueError):
        _good_tuple(noun="")


def test_rejects_negative_engagement():
    with pytest.raises(ValueError):
        _good_tuple(engagement=-1)


def test_rejects_confidence_out_of_range():
    with pytest.raises(ValueError):
        _good_tuple(extraction_confidence=1.5)


def test_roundtrip():
    t = _good_tuple()
    assert ObservationTuple.from_dict(t.to_dict()) == t


def test_verb_vocabulary_size():
    # Matches the spec §3.1 "fixed starter set of ~20"
    assert len(VERB_VOCABULARY) == 20


def test_mood_vocabulary_size():
    assert len(MOOD_VOCABULARY) == 6


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────


def test_write_and_read_tuples_roundtrip(tmp_path):
    t1 = _good_tuple(id="t1")
    t2 = _good_tuple(id="t2", noun="email", verb="drafting")
    path = write_tuples(
        [t1, t2],
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        day=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert path.exists()
    loaded = list(
        read_tuples_range(
            tmp_path,
            "team_bot_a",
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
    )
    assert {t.id for t in loaded} == {"t1", "t2"}


def test_write_tuples_is_idempotent_on_same_source_hash(tmp_path):
    # Use the default tuple's timestamp date (2026-05-01) consistently so
    # the write day and read window match up.
    day = datetime(2026, 5, 1, tzinfo=timezone.utc)
    t1 = _good_tuple(id="t1", source_hash="sh1")
    # Same id AND same source_hash → skipped on second write
    write_tuples([t1], shared_dir=tmp_path, bot_id="team_bot_a", day=day)
    write_tuples([t1], shared_dir=tmp_path, bot_id="team_bot_a", day=day)
    loaded = list(
        read_tuples_range(
            tmp_path,
            "team_bot_a",
            start=day,
            end=day + timedelta(days=1),
        )
    )
    assert len(loaded) == 1


def test_write_tuples_handles_empty_input(tmp_path):
    write_tuples([], shared_dir=tmp_path, bot_id="team_bot_a")
    # No file written; no error
    files = list((tmp_path / "observations").glob("**/*.jsonl"))
    assert files == []


def test_window_tuples_yields_from_storage(tmp_path):
    t = _good_tuple(id="t1")
    write_tuples(
        [t],
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        day=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    w = window(
        "team_bot_a",
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        shared_dir=tmp_path,
    )
    got = list(w.tuples())
    assert len(got) == 1
    assert got[0].id == "t1"


# ─────────────────────────────────────────────────────────────────────────────
# Clustering
# ─────────────────────────────────────────────────────────────────────────────


def _seed_cluster_tuples(tmp_path):
    """Seed a few tuples across different (noun, verb) cells and sessions."""
    day = datetime(2026, 5, 1, tzinfo=timezone.utc)
    ts = day.isoformat()
    tuples = [
        _good_tuple(
            id="a1",
            session_id="s1",
            noun="fitness",
            verb="tracking",
            mood="enthusiastic",
            engagement=3,
            timestamp_start=ts,
            timestamp_end=ts,
        ),
        _good_tuple(
            id="a2",
            session_id="s2",
            noun="fitness",
            verb="tracking",
            mood="neutral",
            engagement=4,
            timestamp_start=ts,
            timestamp_end=ts,
        ),
        _good_tuple(
            id="b1",
            session_id="s1",
            noun="email",
            verb="drafting",
            mood="neutral",
            engagement=2,
            timestamp_start=ts,
            timestamp_end=ts,
        ),
        _good_tuple(
            id="c1",
            session_id="s3",
            noun="fitness",
            verb="planning",
            mood=None,
            engagement=1,
            timestamp_start=ts,
            timestamp_end=ts,
        ),
    ]
    write_tuples(tuples, shared_dir=tmp_path, bot_id="team_bot_a", day=day)
    return day


def test_clusters_group_by_noun_verb(tmp_path):
    day = _seed_cluster_tuples(tmp_path)
    w = window(
        "team_bot_a",
        start=day,
        end=day + timedelta(days=1),
        shared_dir=tmp_path,
    )
    view = w.clusters(ClusterSpec(group_by=["noun", "verb"]))
    assert len(view) == 3
    fitness_tracking = view.by_key(("fitness", "tracking"))
    assert fitness_tracking is not None
    assert fitness_tracking.tuple_count == 2
    assert fitness_tracking.engagement_total == 7
    assert fitness_tracking.distinct_sessions == 2


def test_clusters_group_by_noun_only(tmp_path):
    day = _seed_cluster_tuples(tmp_path)
    w = window(
        "team_bot_a",
        start=day,
        end=day + timedelta(days=1),
        shared_dir=tmp_path,
    )
    view = w.clusters(ClusterSpec(group_by=["noun"]))
    assert len(view) == 2  # fitness, email
    fitness = view.by_key(("fitness",))
    assert fitness is not None
    assert fitness.tuple_count == 3


def test_clusters_mood_distribution(tmp_path):
    day = _seed_cluster_tuples(tmp_path)
    w = window(
        "team_bot_a",
        start=day,
        end=day + timedelta(days=1),
        shared_dir=tmp_path,
    )
    view = w.clusters(ClusterSpec(group_by=["noun", "verb"]))
    ft = view.by_key(("fitness", "tracking"))
    assert ft.mood_distribution.get("enthusiastic", 0) == 1
    assert ft.mood_distribution.get("neutral", 0) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────────────


def test_default_extractor_returns_one_tuple():
    transcript = Transcript(
        bot_id="team_bot_a",
        session_id="s1",
        turns=[
            Turn(role="user", text="hi", timestamp="2026-05-01T12:00:00+00:00"),
            Turn(
                role="assistant",
                text="hello",
                timestamp="2026-05-01T12:00:05+00:00",
            ),
        ],
    )
    tuples = extract_tuples(transcript)
    assert len(tuples) == 1
    assert tuples[0].noun == "other"
    assert tuples[0].verb == "exploring"


def test_extraction_drops_unknown_verbs():
    """Extractor returning an unknown verb should drop that row, not crash."""
    def bad_extractor(transcript, vocab):
        return [
            {
                "noun": "fitness",
                "verb": "moonwalking",  # not in vocabulary
                "mood": "neutral",
                "engagement": 3,
                "segment_id": "seg-0",
                "timestamp_start": "2026-05-01T12:00:00+00:00",
                "timestamp_end": "2026-05-01T12:05:00+00:00",
                "confidence": 0.9,
            }
        ]

    set_extractor(bad_extractor)
    try:
        transcript = Transcript(bot_id="team_bot_a", session_id="s1", turns=[])
        tuples = extract_tuples(transcript)
        assert tuples == []
    finally:
        reset_extractor()


def test_extraction_coerces_unknown_mood_to_none():
    def extractor(transcript, vocab):
        return [
            {
                "noun": "fitness",
                "verb": "tracking",
                "mood": "chaotic",  # not in vocab
                "engagement": 3,
                "segment_id": "seg-0",
                "timestamp_start": "2026-05-01T12:00:00+00:00",
                "timestamp_end": "2026-05-01T12:05:00+00:00",
                "confidence": 0.9,
            }
        ]

    set_extractor(extractor)
    try:
        transcript = Transcript(bot_id="team_bot_a", session_id="s1", turns=[])
        tuples = extract_tuples(transcript)
        assert len(tuples) == 1
        assert tuples[0].mood is None
    finally:
        reset_extractor()


def test_extraction_drops_low_confidence():
    def extractor(transcript, vocab):
        return [
            {
                "noun": "fitness",
                "verb": "tracking",
                "mood": "neutral",
                "engagement": 3,
                "segment_id": "seg-0",
                "timestamp_start": "2026-05-01T12:00:00+00:00",
                "timestamp_end": "2026-05-01T12:05:00+00:00",
                "confidence": 0.05,  # too low
            }
        ]

    set_extractor(extractor)
    try:
        transcript = Transcript(bot_id="team_bot_a", session_id="s1", turns=[])
        tuples = extract_tuples(transcript)
        assert tuples == []
    finally:
        reset_extractor()


def test_extraction_uses_transcript_source_hash():
    transcript = Transcript(
        bot_id="team_bot_a",
        session_id="s1",
        turns=[
            Turn(role="user", text="hi", timestamp="2026-05-01T12:00:00+00:00"),
        ],
    )
    tuples = extract_tuples(transcript)
    assert len(tuples) == 1
    assert tuples[0].source_hash == transcript.text_hash
