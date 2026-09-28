"""tests/test_observability_session_rollup.py — Phase 1 cascade rollup.

Covers:
  - rollup_sessions() pure aggregation (no I/O)
  - iter_turn_spans() filesystem walk across per-bot spans/ dirs and central observability/spans/
  - rollup_for_window() end-to-end helper
  - producer filter (only cascade_telemetry spans counted)
  - tier ordering for max_tier / min_tier
  - struggle aggregates
  - legacy session_class read-compat field

Per internal/spec-tier-cascade-2026-05-26.md § 2.3.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from observability.opik_client import OpikSpan  # noqa: E402
from observability.session_rollup import (  # noqa: E402
    SessionSummary,
    iter_turn_spans,
    rollup_for_window,
    rollup_sessions,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — build OpikSpans matching what CascadeTelemetry emits
# ─────────────────────────────────────────────────────────────────────────────


def _turn_span(
    *,
    session_id: str,
    turn_index: int,
    bot_id: str = "team_bot_a",
    tier_used: str = "tier2",
    tier_intended: str | None = None,
    tier_divergent: bool = False,
    tier_chosen_by: str = "classifier",
    trigger_kind: str | None = None,
    struggle_score: float | None = None,
    struggle_features: dict | None = None,
    struggle_raw: dict | None = None,
    legacy_session_class: str | None = None,
    end_time: datetime | None = None,
    duration_ms: int = 1500,
    cost_usd: float | None = None,
    is_error: bool = False,
    producer: str = "cascade_telemetry",
) -> OpikSpan:
    et = end_time or datetime(2026, 5, 26, 12, turn_index, 0, tzinfo=timezone.utc)
    st = et - timedelta(milliseconds=duration_ms)
    attrs: dict = {
        "session_id": session_id,
        "turn_index": turn_index,
        "cascade.tier_used": tier_used,
        "cascade.tier_intended": tier_intended if tier_intended is not None else tier_used,
        "cascade.tier_divergent": tier_divergent,
        "cascade.tier_chosen_by": tier_chosen_by,
    }
    if trigger_kind is not None:
        attrs["cascade.trigger_kind"] = trigger_kind
    if struggle_score is not None:
        attrs["cascade.struggle.score"] = struggle_score
    if struggle_features:
        for k, v in struggle_features.items():
            attrs[f"cascade.struggle.features.{k}"] = v
    if struggle_raw:
        for k, v in struggle_raw.items():
            attrs[f"cascade.struggle.raw.{k}"] = v
    if legacy_session_class:
        attrs["legacy.session_class"] = legacy_session_class

    return OpikSpan(
        name="bot_session_turn",
        start_time=st,
        end_time=et,
        type="general",
        trace_id=session_id,
        span_id=f"{session_id}:{turn_index}",
        bot_id=bot_id,
        producer=producer,
        attributes=attrs,
        tags=["cascade", "turn", f"tier:{tier_used}"],
        total_cost=cost_usd,
        error_info={"message": "boom"} if is_error else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# rollup_sessions — pure aggregation
# ─────────────────────────────────────────────────────────────────────────────


def test_rollup_groups_by_session_id():
    spans = [
        _turn_span(session_id="s1", turn_index=1),
        _turn_span(session_id="s1", turn_index=2),
        _turn_span(session_id="s2", turn_index=1),
    ]
    out = rollup_sessions(spans)
    assert len(out) == 2
    sids = {s.session_id for s in out}
    assert sids == {"s1", "s2"}


def test_rollup_skips_spans_without_session_id():
    bad = OpikSpan(
        name="bot_session_turn",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
        producer="cascade_telemetry",
        attributes={"turn_index": 1},  # no session_id
    )
    spans = [bad, _turn_span(session_id="s1", turn_index=1)]
    out = rollup_sessions(spans)
    assert len(out) == 1
    assert out[0].session_id == "s1"


def test_rollup_sorts_turns_by_index_within_session():
    # Submit out of order; start_time / end_time should match the first /
    # last span by turn_index, not by file order.
    spans = [
        _turn_span(
            session_id="s1", turn_index=3,
            end_time=datetime(2026, 5, 26, 12, 3, tzinfo=timezone.utc),
        ),
        _turn_span(
            session_id="s1", turn_index=1,
            end_time=datetime(2026, 5, 26, 12, 1, tzinfo=timezone.utc),
        ),
        _turn_span(
            session_id="s1", turn_index=2,
            end_time=datetime(2026, 5, 26, 12, 2, tzinfo=timezone.utc),
        ),
    ]
    out = rollup_sessions(spans)
    assert len(out) == 1
    s = out[0]
    assert s.turn_count == 3
    # start_time = first turn's start; end_time = last turn's end.
    assert s.start_time < s.end_time


def test_rollup_tier_distribution_and_max_min():
    spans = [
        _turn_span(session_id="s1", turn_index=1, tier_used="tier3"),
        _turn_span(session_id="s1", turn_index=2, tier_used="tier3"),
        _turn_span(session_id="s1", turn_index=3, tier_used="tier2"),
        _turn_span(session_id="s1", turn_index=4, tier_used="tier1"),
    ]
    out = rollup_sessions(spans)
    s = out[0]
    assert s.tier_distribution == {"tier3": 2, "tier2": 1, "tier1": 1}
    # tier1 is "more capable" than tier2/tier3 → max_tier = tier1
    assert s.max_tier == "tier1"
    assert s.min_tier == "tier3"


def test_rollup_struggle_aggregates():
    spans = [
        _turn_span(session_id="s1", turn_index=1, struggle_score=0.1),
        _turn_span(session_id="s1", turn_index=2, struggle_score=0.5),
        _turn_span(session_id="s1", turn_index=3, struggle_score=0.0),  # counts toward mean but not turns_with_struggle
        _turn_span(session_id="s1", turn_index=4, struggle_score=0.8),
    ]
    s = rollup_sessions(spans)[0]
    assert s.max_struggle == 0.8
    assert abs(s.mean_struggle - 0.35) < 1e-9
    assert s.turns_with_struggle == 3  # only zero excluded


def test_rollup_struggle_zero_when_no_signals():
    spans = [_turn_span(session_id="s1", turn_index=1)]  # no struggle_score
    s = rollup_sessions(spans)[0]
    assert s.max_struggle == 0.0
    assert s.mean_struggle == 0.0
    assert s.turns_with_struggle == 0


def test_rollup_chosen_by_counts():
    spans = [
        _turn_span(session_id="s1", turn_index=1, tier_chosen_by="classifier"),
        _turn_span(session_id="s1", turn_index=2, tier_chosen_by="classifier"),
        _turn_span(session_id="s1", turn_index=3, tier_chosen_by="user_request"),
    ]
    s = rollup_sessions(spans)[0]
    assert s.chosen_by_counts == {"classifier": 2, "user_request": 1}


def test_rollup_total_cost_summed():
    spans = [
        _turn_span(session_id="s1", turn_index=1, cost_usd=0.01),
        _turn_span(session_id="s1", turn_index=2, cost_usd=0.05),
        _turn_span(session_id="s1", turn_index=3, cost_usd=None),  # missing cost ok
    ]
    s = rollup_sessions(spans)[0]
    assert abs(s.total_cost_usd - 0.06) < 1e-9


def test_rollup_failed_turn_flag():
    spans = [
        _turn_span(session_id="s1", turn_index=1),
        _turn_span(session_id="s1", turn_index=2, is_error=True),
    ]
    s = rollup_sessions(spans)[0]
    assert s.has_failed_turn is True


def test_rollup_legacy_session_class_mode():
    # Mode = most common label. Three productive vs one maintenance → productive wins.
    spans = [
        _turn_span(session_id="s1", turn_index=1, legacy_session_class="productive"),
        _turn_span(session_id="s1", turn_index=2, legacy_session_class="productive"),
        _turn_span(session_id="s1", turn_index=3, legacy_session_class="maintenance"),
        _turn_span(session_id="s1", turn_index=4, legacy_session_class="productive"),
    ]
    s = rollup_sessions(spans)[0]
    assert s.legacy_session_class == "productive"


def test_rollup_legacy_session_class_none_when_no_spans_carry_it():
    spans = [_turn_span(session_id="s1", turn_index=1)]  # no legacy_session_class
    s = rollup_sessions(spans)[0]
    assert s.legacy_session_class is None


def test_rollup_bot_id_propagates():
    spans = [_turn_span(session_id="s1", turn_index=1, bot_id="team_bot_a")]
    s = rollup_sessions(spans)[0]
    assert s.bot_id == "team_bot_a"


def test_rollup_divergent_turn_count_is_summed():
    # tier_used != tier_intended on N turns → divergent_turn_count = N.
    # Critical per failure-mode review F8: this signal exposes when OC
    # silently dropped the model override and ran something other than
    # what cascade wanted.
    spans = [
        _turn_span(session_id="s1", turn_index=1, tier_used="tier2", tier_divergent=False),
        _turn_span(session_id="s1", turn_index=2, tier_used="tier2", tier_intended="tier3", tier_divergent=True),
        _turn_span(session_id="s1", turn_index=3, tier_used="tier2", tier_intended="tier3", tier_divergent=True),
        _turn_span(session_id="s1", turn_index=4, tier_used="tier3", tier_divergent=False),
    ]
    s = rollup_sessions(spans)[0]
    assert s.divergent_turn_count == 2


def test_rollup_trigger_kind_mode():
    # Mode wins. Three user_turn vs one heartbeat → user_turn.
    spans = [
        _turn_span(session_id="s1", turn_index=1, trigger_kind="user_turn"),
        _turn_span(session_id="s1", turn_index=2, trigger_kind="user_turn"),
        _turn_span(session_id="s1", turn_index=3, trigger_kind="heartbeat"),
        _turn_span(session_id="s1", turn_index=4, trigger_kind="user_turn"),
    ]
    s = rollup_sessions(spans)[0]
    assert s.trigger_kind == "user_turn"


def test_rollup_trigger_kind_none_when_no_spans_carry_it():
    spans = [_turn_span(session_id="s1", turn_index=1)]  # no trigger_kind
    s = rollup_sessions(spans)[0]
    assert s.trigger_kind is None


# ─────────────────────────────────────────────────────────────────────────────
# iter_turn_spans — filesystem walk
# ─────────────────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, spans: list[OpikSpan]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for s in spans:
            fh.write(json.dumps(s.to_dict()) + "\n")


def test_iter_turn_spans_reads_per_bot_spans_dir(tmp_path: Path):
    # Plugin writes to {sharedDir}/{botId}/spans/spans-YYYY-MM-DD.jsonl
    end_time = datetime.now(timezone.utc)
    spans = [
        _turn_span(session_id="s1", turn_index=1, bot_id="team_bot_a", end_time=end_time),
        _turn_span(session_id="s1", turn_index=2, bot_id="team_bot_a", end_time=end_time),
    ]
    day_str = end_time.date().isoformat()
    _write_jsonl(
        tmp_path / "team_bot_a" / "spans" / f"spans-{day_str}.jsonl",
        spans,
    )

    out = list(iter_turn_spans(tmp_path))
    assert len(out) == 2
    assert all(s.attributes.get("session_id") == "s1" for s in out)


def test_iter_turn_spans_filters_by_bot_id(tmp_path: Path):
    end_time = datetime.now(timezone.utc)
    day_str = end_time.date().isoformat()
    _write_jsonl(
        tmp_path / "team_bot_a" / "spans" / f"spans-{day_str}.jsonl",
        [_turn_span(session_id="s1", turn_index=1, bot_id="team_bot_a", end_time=end_time)],
    )
    _write_jsonl(
        tmp_path / "team_bot_c" / "spans" / f"spans-{day_str}.jsonl",
        [_turn_span(session_id="s2", turn_index=1, bot_id="team_bot_c", end_time=end_time)],
    )

    team_bot_a_only = list(iter_turn_spans(tmp_path, bot_id="team_bot_a"))
    assert len(team_bot_a_only) == 1
    assert team_bot_a_only[0].bot_id == "team_bot_a"


def test_iter_turn_spans_filters_by_producer(tmp_path: Path):
    # Mix cascade_telemetry spans with other producers; only cascade ones return.
    end_time = datetime.now(timezone.utc)
    day_str = end_time.date().isoformat()
    cascade_span = _turn_span(
        session_id="s1", turn_index=1, bot_id="team_bot_a", end_time=end_time,
    )
    other_span = _turn_span(
        session_id="s1", turn_index=1, bot_id="team_bot_a", end_time=end_time,
        producer="embedding_monitor",
    )
    _write_jsonl(
        tmp_path / "team_bot_a" / "spans" / f"spans-{day_str}.jsonl",
        [cascade_span, other_span],
    )

    out = list(iter_turn_spans(tmp_path))
    assert len(out) == 1
    assert out[0].producer == "cascade_telemetry"


def test_iter_turn_spans_respects_time_window(tmp_path: Path):
    # Write spans from 10 days ago — should NOT show in default 7-day window.
    old = datetime.now(timezone.utc) - timedelta(days=10)
    fresh = datetime.now(timezone.utc)
    _write_jsonl(
        tmp_path / "team_bot_a" / "spans" / f"spans-{old.date().isoformat()}.jsonl",
        [_turn_span(session_id="old", turn_index=1, end_time=old)],
    )
    _write_jsonl(
        tmp_path / "team_bot_a" / "spans" / f"spans-{fresh.date().isoformat()}.jsonl",
        [_turn_span(session_id="fresh", turn_index=1, end_time=fresh)],
    )

    default_window = list(iter_turn_spans(tmp_path))
    assert {s.attributes.get("session_id") for s in default_window} == {"fresh"}

    explicit_long_window = list(
        iter_turn_spans(tmp_path, since=datetime.now(timezone.utc) - timedelta(days=30))
    )
    assert {s.attributes.get("session_id") for s in explicit_long_window} == {"old", "fresh"}


def test_iter_turn_spans_tolerates_missing_dirs(tmp_path: Path):
    # Empty shared_dir with no bot dirs and no observability dir.
    out = list(iter_turn_spans(tmp_path))
    assert out == []


def test_iter_turn_spans_tolerates_malformed_json_lines(tmp_path: Path):
    end_time = datetime.now(timezone.utc)
    day_str = end_time.date().isoformat()
    spans_file = tmp_path / "team_bot_a" / "spans" / f"spans-{day_str}.jsonl"
    spans_file.parent.mkdir(parents=True)
    valid = _turn_span(session_id="s1", turn_index=1, bot_id="team_bot_a", end_time=end_time)
    spans_file.write_text(
        "not json\n"
        + json.dumps(valid.to_dict()) + "\n"
        + "{invalid: 'json'}\n"
        + "\n",  # blank line
        encoding="utf-8",
    )
    out = list(iter_turn_spans(tmp_path))
    assert len(out) == 1
    assert out[0].attributes.get("session_id") == "s1"


# ─────────────────────────────────────────────────────────────────────────────
# rollup_for_window — end-to-end
# ─────────────────────────────────────────────────────────────────────────────


def test_rollup_for_window_end_to_end(tmp_path: Path):
    end_time = datetime.now(timezone.utc)
    day_str = end_time.date().isoformat()
    _write_jsonl(
        tmp_path / "team_bot_a" / "spans" / f"spans-{day_str}.jsonl",
        [
            _turn_span(session_id="s1", turn_index=1, bot_id="team_bot_a", end_time=end_time, struggle_score=0.1),
            _turn_span(session_id="s1", turn_index=2, bot_id="team_bot_a", end_time=end_time, struggle_score=0.8, tier_used="tier1"),
            _turn_span(session_id="s2", turn_index=1, bot_id="team_bot_a", end_time=end_time, struggle_score=0.0),
        ],
    )

    summaries = rollup_for_window(tmp_path, bot_id="team_bot_a")
    assert len(summaries) == 2
    by_id = {s.session_id: s for s in summaries}
    assert by_id["s1"].turn_count == 2
    assert by_id["s1"].max_struggle == 0.8
    assert by_id["s1"].max_tier == "tier1"
    assert by_id["s2"].turn_count == 1


def test_rollup_for_window_sorts_recent_first(tmp_path: Path):
    older = datetime.now(timezone.utc) - timedelta(days=3)
    newer = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_jsonl(
        tmp_path / "team_bot_a" / "spans" / f"spans-{older.date().isoformat()}.jsonl",
        [_turn_span(session_id="old", turn_index=1, bot_id="team_bot_a", end_time=older)],
    )
    _write_jsonl(
        tmp_path / "team_bot_a" / "spans" / f"spans-{newer.date().isoformat()}.jsonl",
        [_turn_span(session_id="new", turn_index=1, bot_id="team_bot_a", end_time=newer)],
    )

    out = rollup_for_window(tmp_path)
    assert [s.session_id for s in out] == ["new", "old"]
