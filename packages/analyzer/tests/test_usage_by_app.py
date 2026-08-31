"""tests/test_usage_by_app.py — per-app usage rollup (AL-1.3).

The load-bearing assertions are the reader contract (brief §2): grades
stay additive (``total`` is scheduled + explicit and NEVER absorbs
inferred), ``none`` is reported rather than dropped, Evolve's own
overhead is split out of the unattributed bucket, and pre-AL-1.1
(schema_version 4) records count as unattributed without being confused
for attribution failures.
"""

from __future__ import annotations

import json
import stat
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import usage_by_app  # noqa: E402
from usage_by_app import (  # noqa: E402
    SCHEMA_VERSION,
    classify_grade,
    has_attributed_turns,
    is_legacy_schema,
    load_usage_by_app,
    rollup_bot,
    run_usage_by_app,
    usage_by_app_path,
    write_usage_by_app,
)


TODAY = date(2026, 8, 17)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _annotation(
    *,
    app_id: str | None = None,
    grade: str | None = None,
    session_id: str = "s-1",
    tokens: int = 10,
    cost: float = 0.01,
    ts: str = "2026-08-17T12:00:00.000Z",
    schema_version: int = 5,
    **extra,
) -> dict:
    rec = {
        "type": "turn_annotation",
        "schema_version": schema_version,
        "ts": ts,
        "session_id": session_id,
        "input_tokens": tokens,
        "output_tokens": tokens * 2,
        "cost_estimated": cost,
    }
    if schema_version >= 5:
        rec["app_id"] = app_id
        rec["app_attribution"] = grade or "none"
        rec["app_confidence"] = 1.0 if grade in ("scheduled", "explicit") else None
    rec.update(extra)
    return rec


def _write_annotations(shared: Path, bot: str, day: date, records: list[dict]) -> None:
    ann_dir = shared / "annotations" / bot
    ann_dir.mkdir(parents=True, exist_ok=True)
    path = ann_dir / f"{day.isoformat()}.jsonl"
    with path.open("a") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")


def _write_cost_events(shared: Path, bot: str, day: date, events: list[dict]) -> None:
    ann_dir = shared / "annotations" / bot
    ann_dir.mkdir(parents=True, exist_ok=True)
    path = ann_dir / f"cost_events-{day.isoformat()}.jsonl"
    with path.open("a") as handle:
        for event in events:
            handle.write(json.dumps({"type": "cost_event", **event}) + "\n")


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    return tmp_path / "evolve"


# ── The additive rule (brief §2) ─────────────────────────────────────────────

def test_grades_are_additive_never_collapsed(shared: Path) -> None:
    """total == scheduled + explicit; inferred rides beside it, never inside."""
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="scheduled", cost=0.10),
        _annotation(app_id="app-a", grade="scheduled", cost=0.10),
        _annotation(app_id="app-a", grade="explicit", cost=0.20),
        _annotation(app_id="app-a", grade="inferred", cost=0.50),
    ])

    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    window = payload["apps"]["app-a"]["d1"]

    assert window["scheduled"]["turns"] == 2
    assert window["explicit"]["turns"] == 1
    assert window["inferred"]["turns"] == 1
    # The whole point: 3, not 4 — and the inferred dollars stay out too.
    assert window["total"]["turns"] == 3
    assert window["total"]["cost_estimated"] == pytest.approx(0.40)
    assert window["inferred"]["cost_estimated"] == pytest.approx(0.50)
    # No key in the app entry silently sums inferred into the total.
    assert set(window) == {"total", "scheduled", "explicit", "inferred"}


def test_tokens_and_windows_bucket_by_day(shared: Path) -> None:
    for offset, cost in ((0, 1.0), (3, 2.0), (10, 4.0), (40, 8.0)):
        _write_annotations(shared, "team_bot_a", TODAY - timedelta(days=offset), [
            _annotation(app_id="app-a", grade="explicit", cost=cost, tokens=5),
        ])

    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    app = payload["apps"]["app-a"]

    assert app["d1"]["total"]["turns"] == 1
    assert app["d7"]["total"]["turns"] == 2
    # The 40-days-ago record is outside every window and never counted.
    assert app["d30"]["total"]["turns"] == 3
    assert app["d30"]["total"]["cost_estimated"] == pytest.approx(7.0)
    assert app["d7"]["total"]["input_tokens"] == 10
    assert app["d7"]["total"]["output_tokens"] == 20


def test_first_and_last_seen_span_the_window(shared: Path) -> None:
    _write_annotations(shared, "team_bot_a", TODAY - timedelta(days=5), [
        _annotation(app_id="app-a", grade="explicit",
                    ts="2026-08-12T08:00:00.000Z"),
    ])
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="explicit",
                    ts="2026-08-17T09:30:00.000Z"),
    ])

    app = rollup_bot(shared, "team_bot_a", today=TODAY)["apps"]["app-a"]
    assert app["first_seen_ts"] == "2026-08-12T08:00:00.000Z"
    assert app["last_seen_ts"] == "2026-08-17T09:30:00.000Z"


# ── Unattributed is shown, not hidden ────────────────────────────────────────

def test_unattributed_bucket_and_coverage_share(shared: Path) -> None:
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="scheduled", cost=0.25),
        _annotation(grade="none", cost=0.25),
        _annotation(grade="none", cost=0.25),
        _annotation(grade="none", cost=0.25),
    ])

    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    assert payload["unattributed"]["d1"]["turns"] == 3
    assert payload["unattributed"]["d1"]["cost_estimated"] == pytest.approx(0.75)

    coverage = payload["coverage"]["d1"]
    assert coverage["attributed_turns"] == 1
    assert coverage["unattributed_turns"] == 3
    assert coverage["unattributed_turns_share"] == pytest.approx(0.75)
    assert coverage["unattributed_cost_share"] == pytest.approx(0.75)


def test_coverage_share_is_none_not_zero_when_no_turns(shared: Path) -> None:
    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    assert payload["apps"] == {}
    # None means "nothing measured" — a 0.0 here would read as full coverage.
    assert payload["coverage"]["d7"]["unattributed_turns_share"] is None
    assert payload["coverage"]["d7"]["unattributed_cost_share"] is None


def test_inferred_counts_toward_coverage_denominator(shared: Path) -> None:
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="inferred", cost=0.5),
        _annotation(grade="none", cost=0.5),
    ])
    coverage = rollup_bot(shared, "team_bot_a", today=TODAY)["coverage"]["d1"]
    assert coverage["attributed_turns"] == 0
    assert coverage["inferred_turns"] == 1
    assert coverage["unattributed_turns_share"] == pytest.approx(0.5)


# ── Legacy schema tolerance ──────────────────────────────────────────────────

def test_schema_v4_records_count_as_unattributed_and_are_flagged(shared: Path) -> None:
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(schema_version=4, cost=0.1),
        _annotation(schema_version=4, cost=0.1),
        _annotation(grade="none", cost=0.1),
    ])

    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    assert payload["unattributed"]["d1"]["turns"] == 3
    # Two of the three are simply "written before attribution shipped".
    assert payload["unattributed"]["d1"]["legacy_schema_turns"] == 2
    assert payload["coverage"]["d1"]["legacy_schema_turns"] == 2


def test_missing_days_and_malformed_lines_are_skipped(shared: Path) -> None:
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="explicit"),
    ])
    ann_path = shared / "annotations" / "team_bot_a" / f"{TODAY.isoformat()}.jsonl"
    with ann_path.open("a") as handle:
        handle.write("{not json\n\n")
        handle.write(json.dumps({"type": "session_summary"}) + "\n")

    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    assert payload["apps"]["app-a"]["d1"]["total"]["turns"] == 1
    assert payload["unattributed"]["d1"]["turns"] == 0


# ── Evolve overhead split ────────────────────────────────────────────────────

def test_evolve_overhead_is_split_out_of_unattributed(shared: Path) -> None:
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(grade="none", session_id="forge-sess", cost=0.30),
        _annotation(grade="none", session_id="human-sess", cost=0.10),
    ])
    _write_cost_events(shared, "team_bot_a", TODAY, [
        {"session_id": "forge-sess", "trigger_kind": "forge"},
        {"session_id": "human-sess", "trigger_kind": "user_turn"},
    ])

    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    assert payload["evolve_overhead"]["d1"]["turns"] == 1
    assert payload["evolve_overhead"]["d1"]["cost_estimated"] == pytest.approx(0.30)
    # The overhead turn is NOT in unattributed, and NOT in the coverage
    # denominator — it is not an unknown app.
    assert payload["unattributed"]["d1"]["turns"] == 1
    assert payload["coverage"]["d1"]["app_turns_total"] == 1
    assert payload["coverage"]["d1"]["evolve_overhead_turns"] == 1


def test_attributed_turn_in_an_overhead_session_stays_with_its_app(shared: Path) -> None:
    """An app_id the plugin stamped always wins — overhead only ever
    reclassifies turns that had no attribution at all."""
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="scheduled", session_id="forge-sess"),
    ])
    _write_cost_events(shared, "team_bot_a", TODAY, [
        {"session_id": "forge-sess", "trigger_kind": "forge"},
    ])

    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    assert payload["apps"]["app-a"]["d1"]["scheduled"]["turns"] == 1
    assert payload["evolve_overhead"]["d1"]["turns"] == 0


def test_subagent_turns_are_not_overhead(shared: Path) -> None:
    """subagent = the bot doing the user's work in-session; only the
    Evolve-own kinds (summarizer/classifier/task_extractor/fallback/forge)
    are overhead."""
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(grade="none", session_id="sub-sess"),
    ])
    _write_cost_events(shared, "team_bot_a", TODAY, [
        {"session_id": "sub-sess", "trigger_kind": "subagent"},
    ])

    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    assert payload["evolve_overhead"]["d1"]["turns"] == 0
    assert payload["unattributed"]["d1"]["turns"] == 1


# ── Grade classification (fail toward "no signal") ───────────────────────────

@pytest.mark.parametrize("rec,expected", [
    ({"app_id": "a", "app_attribution": "scheduled"}, ("a", "scheduled")),
    ({"app_id": " a ", "app_attribution": "explicit"}, ("a", "explicit")),
    # A grade with no id, an id with no grade, and an unknown grade all
    # resolve to the honest none/None pair.
    ({"app_id": None, "app_attribution": "explicit"}, (None, "none")),
    ({"app_id": "a", "app_attribution": None}, (None, "none")),
    ({"app_id": "a", "app_attribution": "guessed"}, (None, "none")),
    ({"app_id": "", "app_attribution": "scheduled"}, (None, "none")),
    ({}, (None, "none")),
])
def test_classify_grade_fails_toward_no_signal(rec: dict, expected: tuple) -> None:
    assert classify_grade(rec) == expected


def test_is_legacy_schema() -> None:
    assert is_legacy_schema({"schema_version": 4}) is True
    assert is_legacy_schema({"schema_version": 3}) is True
    assert is_legacy_schema({}) is True
    assert is_legacy_schema({"schema_version": 5}) is False


# ── Write path ───────────────────────────────────────────────────────────────

def test_write_is_atomic_and_0644(shared: Path) -> None:
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="scheduled"),
    ])

    payload = run_usage_by_app("team_bot_a", shared, today=TODAY)
    out = usage_by_app_path(shared, "team_bot_a")

    assert out.exists()
    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == 0o644, f"expected 0644, got {oct(mode)}"
    # No temp files left behind.
    assert [p.name for p in out.parent.iterdir()] == [out.name]

    on_disk = json.loads(out.read_text())
    assert on_disk["schema_version"] == SCHEMA_VERSION
    assert on_disk["bot_id"] == "team_bot_a"
    assert on_disk["apps"] == payload["apps"]


def test_write_replaces_previous_payload(shared: Path) -> None:
    out = usage_by_app_path(shared, "team_bot_a")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('{"schema_version": 1, "apps": {"stale": {}}}')

    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="explicit"),
    ])
    run_usage_by_app("team_bot_a", shared, today=TODAY)

    assert "stale" not in json.loads(out.read_text())["apps"]


def test_dry_run_writes_nothing(shared: Path) -> None:
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="explicit"),
    ])
    run_usage_by_app("team_bot_a", shared, dry_run=True, today=TODAY)
    assert not usage_by_app_path(shared, "team_bot_a").exists()


# ── Reader helpers (used by the admin routes + the evo tool) ─────────────────

def test_load_usage_by_app_missing_and_malformed(shared: Path) -> None:
    assert load_usage_by_app(shared, "team_bot_a") == {}

    out = usage_by_app_path(shared, "team_bot_a")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("{ truncated")
    assert load_usage_by_app(shared, "team_bot_a") == {}


def test_has_attributed_turns_gates_the_usage_stats_fallback(shared: Path) -> None:
    assert has_attributed_turns({}) is False

    _write_annotations(shared, "team_bot_a", TODAY, [_annotation(grade="none")])
    assert has_attributed_turns(rollup_bot(shared, "team_bot_a", today=TODAY)) is False

    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="app-a", grade="explicit"),
    ])
    assert has_attributed_turns(rollup_bot(shared, "team_bot_a", today=TODAY)) is True


def test_one_bot_per_run_never_reads_another_bots_annotations(shared: Path) -> None:
    _write_annotations(shared, "team_bot_a", TODAY, [
        _annotation(app_id="team-bot-a-app", grade="explicit"),
    ])
    _write_annotations(shared, "team_bot_b", TODAY, [
        _annotation(app_id="team-bot-b-app", grade="explicit"),
    ])

    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    assert set(payload["apps"]) == {"team-bot-a-app"}


def test_cost_event_read_failure_degrades_to_unattributed(shared: Path, monkeypatch) -> None:
    """A cost-store hiccup must cost us the overhead split, not the rollup."""
    _write_annotations(shared, "team_bot_a", TODAY, [_annotation(grade="none")])

    import cost_rollup

    def _boom(*_args, **_kwargs):
        raise OSError("cost store unavailable")

    monkeypatch.setattr(cost_rollup, "iter_cost_events", _boom)
    payload = rollup_bot(shared, "team_bot_a", today=TODAY)
    assert payload["unattributed"]["d1"]["turns"] == 1
    assert payload["evolve_overhead"]["d1"]["turns"] == 0


def test_sweep_covers_the_evolve_bot_too() -> None:
    """``network.members`` omits the evolve bot, but it runs the plugin and
    writes annotations — and the admin readers list it, so a sweep that
    skipped it would leave one permanently "not measured" row."""
    assert usage_by_app.with_evolve_bot(["team_bot_a"]) == ["team_bot_a", "evolve"]
    # Idempotent — a members list that already names it isn't duplicated.
    assert usage_by_app.with_evolve_bot(["evolve"]) == ["evolve"]
