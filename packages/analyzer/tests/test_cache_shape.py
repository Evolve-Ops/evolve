"""cache_shape — cross-turn cache, the ``auto`` tier, and why sessions rotate.

The fixtures are the two shapes the finding contrasts
(``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §2):

* the **measured day** — turns ten to thirty minutes apart against a
  five-minute cache, so the prefix re-warmed on nearly every turn;
* a **dense** session — turns seconds apart, where a one-hour cache would
  expire on exactly the same turns a five-minute one does and bill 2.00x
  instead of 1.25x for the privilege.

``auto`` has to pick ``1h`` on the first and ``5m`` on the second, and the
cross-turn hit rate has to report near-zero on the first where the per-call
token ratio reported 79-87%.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cache_shape as cs  # noqa: E402


DAY = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
PREFIX_TOKENS = 45_000


@pytest.fixture(autouse=True)
def _no_pod_reads(monkeypatch):
    """No test in this file may touch a deployed bot's openclaw.json. The
    default keeps the rotation classifier's input explicit — a test that
    wants a particular idle window overrides it."""
    monkeypatch.setattr(
        cs, "read_idle_reset_minutes", lambda bot_id: cs.DEFAULT_IDLE_RESET_MINUTES,
    )


def _turn(session: str, at: datetime, *, write: int = 0, read: int = 0) -> dict:
    return {
        "session_id": session,
        "ts": at.isoformat().replace("+00:00", "Z"),
        "cache_write_tokens": write,
        "cache_read_tokens": read,
    }


def sparse_session(
    session: str, start: datetime, *, turns: int = 12, gap_minutes: int = 18,
) -> list[dict]:
    """The finding's shape, one session: turns ~18 minutes apart, each one
    re-writing the ~45k prefix from cold.

    A cold turn reads NOTHING — that is the shipped cold-miss rule
    (``cache_shape.is_prefix_rewarm``: ``read == 0 and write > floor``), now
    shared with ``context_census.cache_report`` so the receipt and the census
    cannot report different counts for the same event. The intra-turn reads
    that made the day look healthy live on the CALL records, not here; see
    :func:`intra_turn_calls`.
    """
    return [
        _turn(session, start + timedelta(minutes=gap_minutes * i),
              write=PREFIX_TOKENS, read=0)
        for i in range(turns)
    ]


def sparse_day(*, turns: int = 12, gap_minutes: int = 18) -> list[dict]:
    return sparse_session("sess-sparse", DAY, turns=turns, gap_minutes=gap_minutes)


def sparse_week(
    *, days: int = 7, turns: int = 12, gap_minutes: int = 18,
) -> list[dict]:
    """:func:`sparse_day` repeated once a day for a week — one session per day.

    ``auto`` will not move a tier on less than ``MIN_WINDOW_DAYS_FOR_AUTO``
    days of OBSERVED history, and the observed span now comes out of the
    records, so any fixture that expects a decision has to cover real days.
    """
    rows: list[dict] = []
    for d in range(days):
        rows += sparse_session(
            f"sess-sparse-{d}", DAY + timedelta(days=d),
            turns=turns, gap_minutes=gap_minutes,
        )
    return rows


def intra_turn_calls(rows: list[dict], *, calls_per_turn: int = 13) -> list[dict]:
    """Expand turn rows into the model CALLS each turn made.

    One agentic turn made thirteen calls: call 1 wrote the prefix (or read it
    from a live cache), and calls 2-13 each re-read what call 1 had just
    written. Token-weighted over these, the day reports 79-87% — the number
    the finding says was hiding a day on which every turn re-warmed from cold.
    """
    calls: list[dict] = []
    for row in rows:
        calls.append(dict(row))
        for _ in range(calls_per_turn - 1):
            calls.append({**row, "cache_write_tokens": 0,
                          "cache_read_tokens": PREFIX_TOKENS})
    return calls


def dense_session_rows(session: str, start: datetime, *, turns: int = 30,
                       gap_seconds: int = 20) -> list[dict]:
    """Turns seconds apart inside one session — the cache never expires, so
    only the first turn writes."""
    rows = [_turn(session, start, write=PREFIX_TOKENS)]
    for i in range(1, turns):
        rows.append(_turn(
            session, start + timedelta(seconds=gap_seconds * i),
            write=0, read=PREFIX_TOKENS,
        ))
    return rows


def dense_session(*, turns: int = 30, gap_seconds: int = 20) -> list[dict]:
    return dense_session_rows(
        "sess-dense", DAY, turns=turns, gap_seconds=gap_seconds,
    )


def dense_week(*, days: int = 7, turns: int = 30) -> list[dict]:
    rows: list[dict] = []
    for d in range(days):
        rows += dense_session_rows(f"sess-dense-{d}", DAY + timedelta(days=d),
                                   turns=turns)
    return rows


# ── Gap distribution ─────────────────────────────────────────────────────────


def test_gaps_are_within_session_only():
    """A gap across a session boundary is a rotation, not a cache failure."""
    rows = [
        _turn("a", DAY),
        _turn("a", DAY + timedelta(minutes=10)),
        _turn("b", DAY + timedelta(hours=6)),
    ]
    stats = cs.gap_stats(rows)
    assert stats.gap_count == 1
    assert stats.p50_gap_seconds == pytest.approx(600)
    assert stats.session_count == 2


def test_writes_counted_per_tier():
    stats = cs.gap_stats(sparse_day(turns=12, gap_minutes=18))
    # One session primes once; each of the eleven 18-minute gaps outlives the
    # five-minute cache but none outlives the one-hour cache.
    assert stats.writes_short == 1 + 11
    assert stats.writes_long == 1
    assert stats.rewrite_factor == pytest.approx(12.0)


def test_rewrite_factor_is_one_when_nothing_would_be_saved():
    stats = cs.gap_stats(dense_session())
    assert stats.writes_short == stats.writes_long == 1
    assert stats.rewrite_factor == pytest.approx(1.0)


def test_unparseable_timestamps_contribute_no_gap():
    rows = [
        {"session_id": "a", "ts": "not a date"},
        {"session_id": "a", "ts": DAY.isoformat()},
        {"session_id": "a"},
    ]
    stats = cs.gap_stats(rows)
    assert stats.gap_count == 0
    assert stats.session_count == 1


def test_window_days_is_the_observed_span_not_the_request():
    """The gate's input comes out of the data. Echoing the caller's ``days``
    is how "needs 3 days of history" became unreachable: every production
    caller takes the 7-day default, so a bot four hours old reported 7."""
    assert cs.gap_stats(sparse_day()).window_days == 0          # ~3.3 hours
    assert cs.gap_stats(sparse_week(days=7)).window_days == 6   # day 0 → day 6
    assert cs.gap_stats([]).window_days == 0


# ── auto ─────────────────────────────────────────────────────────────────────


def test_auto_picks_1h_on_the_findings_gap_distribution():
    stats = cs.gap_stats(sparse_week())
    decision = cs.resolve_auto_retention(stats)
    assert decision.retention == "long"
    assert cs.RETENTION_LABEL[decision.retention] == "1h"
    assert "12.0x re-write factor" in decision.reason
    assert "18m" in decision.reason  # the median gap, in the operator's words
    assert "p95" not in decision.reason, (
        "a nearest-rank pick on this few gaps is the largest gap, not a "
        "percentile — it does not belong in operator-facing reasoning"
    )


def test_auto_picks_5m_on_a_dense_distribution():
    stats = cs.gap_stats(dense_week())
    decision = cs.resolve_auto_retention(stats)
    assert decision.retention == "short"
    assert cs.RETENTION_LABEL[decision.retention] == "5m"
    assert "1.0x re-write factor" in decision.reason


def test_auto_picks_5m_when_turns_are_hours_apart():
    """The trap the break-even exists to avoid: a bot whose turns are hours
    apart breaks a 1h cache exactly as reliably as a 5m one."""
    rows = [
        _turn("s", DAY + timedelta(hours=2 * i), write=PREFIX_TOKENS)
        for i in range(49)          # 48 two-hour gaps = four days of span
    ]
    stats = cs.gap_stats(rows)
    assert stats.window_days >= cs.MIN_WINDOW_DAYS_FOR_AUTO
    decision = cs.resolve_auto_retention(stats)
    assert decision.retention == "short"


def test_auto_declines_on_too_few_gaps():
    """Enough history, not enough gaps — the other half of the gate."""
    rows = [_turn("s", DAY + timedelta(minutes=20 * i)) for i in range(4)]
    rows.append(_turn("s2", DAY + timedelta(days=5)))   # span clears the window gate
    stats = cs.gap_stats(rows)
    assert stats.window_days >= cs.MIN_WINDOW_DAYS_FOR_AUTO
    decision = cs.resolve_auto_retention(stats)
    assert decision.retention is None
    assert not decision.decided
    assert "declined" in decision.reason
    assert "Leaving the tier unset" in decision.reason


def test_auto_declines_on_too_short_a_window():
    """A ONE-DAY fixture, not a hand-passed ``window_days=1``. The branch has
    to be reachable from records a production caller could actually produce —
    it was not, and that is finding 3."""
    rows = sparse_day() + sparse_session(
        "sess-sparse-2", DAY + timedelta(days=1),
    )
    stats = cs.gap_stats(rows)
    assert stats.window_days == 1
    assert stats.gap_count >= cs.MIN_GAPS_FOR_ECONOMICS, (
        "the window gate must be what declines this, not the gap count"
    )
    decision = cs.resolve_auto_retention(stats)
    assert decision.retention is None
    assert "1d of history" in decision.reason


def test_auto_declines_on_one_busy_afternoon():
    """The scenario the unreachable gate let through: a bot deployed this
    morning, one four-hour session, thirteen gaps in the 15-20 minute band —
    a re-write factor around 14x, which under the old code wrote ``long`` and
    billed the 2.00x write premium off a sample of one afternoon."""
    rows = sparse_day(turns=14, gap_minutes=18)     # 13 gaps over 3h 54m
    stats = cs.gap_stats(rows)
    assert stats.gap_count == 13
    assert stats.window_days == 0
    assert stats.has_economics, "the gap gate would have let this through"
    assert stats.rewrite_factor > cs.LONG_RETENTION_BREAKEVEN
    decision = cs.resolve_auto_retention(stats)
    assert decision.retention is None
    assert "0d of history" in decision.reason


def test_explicit_settings_pass_through_unargued():
    dense = cs.gap_stats(dense_week())
    # "long" against data that says "short" — an operator's pin is not
    # re-litigated by the resolver.
    assert cs.resolve_retention("long", dense).retention == "long"
    assert cs.resolve_retention("short", dense).retention == "short"


def test_unset_stays_unset():
    """Unset is not a hidden auto: turning "the operator has not chosen" into
    a measured choice would move every bot's tier on the next deploy."""
    decision = cs.resolve_retention(None, cs.gap_stats(sparse_week()))
    assert decision.retention is None
    assert "OpenClaw's 5m default" in decision.reason


def test_auto_with_unmeasurable_turns_declines():
    decision = cs.resolve_retention("auto", None)
    assert decision.retention is None
    assert "could not measure" in decision.reason


# ── Cross-turn cache ─────────────────────────────────────────────────────────


def test_cross_turn_hit_rate_ignores_intra_turn_reads():
    """The whole point: token-weighted over the day's model CALLS the ratio is
    ~92%; counted per turn it is 0%. Same day, same traffic."""
    rows = sparse_day(turns=12)
    calls = intra_turn_calls(rows)
    token_ratio = (
        sum(c["cache_read_tokens"] for c in calls)
        / sum(c["cache_read_tokens"] + c["cache_write_tokens"] for c in calls)
    )
    assert token_ratio > 0.9

    ct = cs.cross_turn_cache(rows)
    assert ct.eligible == 11          # every turn but the session's first
    assert ct.rewarms == 11
    assert ct.hit_rate == pytest.approx(0.0)


def test_a_turn_that_also_read_is_not_counted_as_a_rewarm():
    """The shared predicate's stricter half, pinned. A record showing a big
    write AND a nonzero read is evidence that SOME cached prefix survived, so
    neither the receipt nor the census counts it — before they shared one
    rule, the receipt did and the census did not."""
    rows = [
        _turn("s", DAY, write=PREFIX_TOKENS),
        _turn("s", DAY + timedelta(minutes=18), write=PREFIX_TOKENS, read=3_000),
    ]
    ct = cs.cross_turn_cache(rows)
    assert ct.eligible == 1
    assert ct.rewarms == 0
    assert cs.is_prefix_rewarm(3_000, PREFIX_TOKENS) is False


# ── "could not look" is not "zero" ───────────────────────────────────────────


def test_a_turn_with_no_cache_fields_is_unreadable_not_a_hit():
    """The inversion finding 1 named: a week whose records carry no cache
    fields used to report a PERFECT cross-turn hit rate, because an absent
    ``cache_write_tokens`` read as a zero-token write and zero is not a
    re-warm. Unmeasured must never render as measured-and-fine."""
    rows = [
        {"session_id": "s", "ts": (DAY + timedelta(minutes=18 * i)).isoformat()}
        for i in range(20)
    ]
    ct = cs.cross_turn_cache(rows)
    assert ct.unreadable == 19
    assert ct.eligible == 0
    assert ct.rewarms == 0
    assert ct.hit_rate is None, "not 100% — nothing was measured"
    assert ct.as_dict()["unreadable_turns"] == 19


def test_unparseable_cache_fields_are_unreadable_too():
    rows = [
        _turn("s", DAY, write=PREFIX_TOKENS),
        {"session_id": "s", "ts": (DAY + timedelta(minutes=18)).isoformat(),
         "cache_write_tokens": None},
        {"session_id": "s", "ts": (DAY + timedelta(minutes=36)).isoformat(),
         "cache_write_tokens": "n/a"},
        _turn("s", DAY + timedelta(minutes=54), write=PREFIX_TOKENS),
    ]
    ct = cs.cross_turn_cache(rows)
    assert ct.unreadable == 2
    assert ct.eligible == 1
    assert ct.rewarms == 1
    assert ct.hit_rate == pytest.approx(0.0)


def test_a_readable_zero_write_is_still_a_hit():
    """The other side of the tri-state: a field that is PRESENT and zero was
    looked at, and a turn that wrote nothing re-read the prefix."""
    rows = [
        _turn("s", DAY, write=PREFIX_TOKENS),
        _turn("s", DAY + timedelta(minutes=2), write=0, read=PREFIX_TOKENS),
    ]
    ct = cs.cross_turn_cache(rows)
    assert ct.unreadable == 0
    assert ct.eligible == 1
    assert ct.hit_rate == pytest.approx(1.0)


def test_cross_turn_hit_rate_on_a_warm_session():
    ct = cs.cross_turn_cache(dense_session(turns=30))
    assert ct.eligible == 29
    assert ct.rewarms == 0
    assert ct.hit_rate == pytest.approx(1.0)


def test_first_turn_of_a_session_is_not_a_rewarm():
    rows = [_turn("s", DAY, write=PREFIX_TOKENS)]
    ct = cs.cross_turn_cache(rows)
    assert ct.eligible == 0
    assert ct.rewarms == 0
    assert ct.hit_rate is None, "no eligible turns is not a 0% hit rate"


def test_small_writes_are_not_rewarms():
    """Caching the conversation's own growth forward is not a prefix rewrite."""
    rows = [
        _turn("s", DAY, write=PREFIX_TOKENS),
        _turn("s", DAY + timedelta(minutes=1), write=200, read=PREFIX_TOKENS),
    ]
    ct = cs.cross_turn_cache(rows)
    assert ct.eligible == 1
    assert ct.rewarms == 0


def test_rewarms_per_active_hour_uses_active_span():
    rows = sparse_day(turns=12, gap_minutes=18)   # 11 gaps × 18m = 3.3h span
    ct = cs.cross_turn_cache(rows)
    assert ct.active_hours == pytest.approx(11 * 18 / 60, rel=1e-3)
    assert ct.rewarms_per_active_hour == pytest.approx(11 / (11 * 18 / 60), rel=1e-3)
    # The definition-of-done target the chip is measured against.
    assert ct.rewarms_per_active_hour > 1.0, "the finding's day misses the target"


def test_the_target_shape_meets_the_definition_of_done():
    """One re-warm per hour of activity — what a 1h tier buys on the same
    traffic: the cache now survives the 18-minute gaps, so only the hourly
    boundary re-warms."""
    rows = []
    for i in range(12):
        at = DAY + timedelta(minutes=18 * i)
        # A re-warm once an hour; a warm read otherwise.
        cold = (i % 4 == 0)
        rows.append(_turn(
            "s", at,
            write=PREFIX_TOKENS if cold else 0,
            read=0 if cold else PREFIX_TOKENS,
        ))
    ct = cs.cross_turn_cache(rows)
    assert ct.rewarms == 2          # the hour boundaries at i=4 and i=8
    assert ct.rewarms_per_active_hour is not None
    assert ct.rewarms_per_active_hour <= 1.0


# ── Session rotation ─────────────────────────────────────────────────────────


def test_rotation_flags_a_session_per_message():
    rows = [
        _turn(f"sess-{i}", DAY + timedelta(minutes=18 * i), write=PREFIX_TOKENS)
        for i in range(12)
    ]
    stats = cs.rotation_stats(rows)
    assert stats.sessions == 12
    assert stats.single_turn_share == pytest.approx(1.0)

    verdict, reason = cs.classify_rotation(stats, cs.DEFAULT_IDLE_RESET_MINUTES)
    assert verdict == "per_message"
    assert cs.ROTATION_KNOB in reason
    assert "derive_session_id" in reason, "names where to look next"


def test_rotation_will_not_blame_a_window_it_cannot_see():
    """An unknown idle window cannot be ruled out, so naming "something
    upstream" would be naming the wrong cause."""
    rows = [
        _turn(f"sess-{i}", DAY + timedelta(minutes=18 * i), write=PREFIX_TOKENS)
        for i in range(12)
    ]
    verdict, _ = cs.classify_rotation(cs.rotation_stats(rows), None)
    assert verdict != "per_message"


def test_rotation_reads_healthy_when_sessions_accumulate_turns():
    rows = []
    for s in range(4):
        for t in range(8):
            rows.append(_turn(
                f"sess-{s}", DAY + timedelta(hours=6 * s, minutes=3 * t),
            ))
    stats = cs.rotation_stats(rows)
    verdict, reason = cs.classify_rotation(stats, cs.DEFAULT_IDLE_RESET_MINUTES)
    assert verdict == "healthy"
    assert "8.0 per session" in reason


def test_rotation_flags_an_idle_window_narrower_than_the_gaps():
    rows = []
    for s in range(3):
        for t in range(6):
            rows.append(_turn(
                f"sess-{s}", DAY + timedelta(hours=8 * s, minutes=20 * t),
            ))
    stats = cs.rotation_stats(rows)
    # A 20-minute idle window against a 20-minute median gap: ordinary pauses
    # rotate, and each rotation is a full prefix write.
    verdict, reason = cs.classify_rotation(stats, 20)
    assert verdict == "idle_rotation"
    assert "full prefix write" in reason


def test_rotation_says_so_when_there_is_nothing_to_say():
    verdict, reason = cs.classify_rotation(cs.rotation_stats([_turn("s", DAY)]), 120)
    assert verdict == "insufficient_data"
    assert "not enough" in reason


# ── The receipt ──────────────────────────────────────────────────────────────


def test_receipt_reports_the_cross_turn_number(monkeypatch):
    monkeypatch.setattr(
        cs, "load_turn_records",
        lambda bot_id, **kw: sparse_day() if bot_id == "bot_a" else [],
    )
    lines = cs.receipt_lines(["bot_a", "bot_b"])
    assert lines[0].startswith("cross-turn cache hit: 0%")
    assert "not token-weighted" in lines[0]
    assert lines[1].startswith("prefix re-warms:")
    assert "per active hour" in lines[1]


def test_receipt_never_silently_omits_the_line(monkeypatch):
    """Unreadable turns must not read as "checked and fine"."""
    monkeypatch.setattr(cs, "load_turn_records", lambda bot_id, **kw: None)
    lines = cs.receipt_lines(["bot_a"])
    assert lines == ["cross-turn cache hit: turns unreadable — not measured this week"]


def test_receipt_names_the_bots_it_could_not_read(monkeypatch):
    monkeypatch.setattr(
        cs, "load_turn_records",
        lambda bot_id, **kw: sparse_day() if bot_id == "bot_a" else None,
    )
    lines = cs.receipt_lines(["bot_a", "bot_b"])
    assert any("not counted" in ln and "bot_b" in ln for ln in lines)


def test_receipt_names_a_bot_that_rotates_per_message(monkeypatch):
    """A per-message rotation is upstream of every cache setting, so the
    re-warm figure gets its cause attached rather than being a number to
    stare at."""
    rows = [
        _turn(f"s{i}", DAY + timedelta(minutes=18 * i), write=PREFIX_TOKENS)
        for i in range(12)
    ]
    monkeypatch.setattr(cs, "load_turn_records", lambda bot_id, **kw: rows)
    monkeypatch.setattr(cs, "read_idle_reset_minutes", lambda bot_id: 120)
    lines = cs.receipt_lines(["bot_a"])
    assert any("bot_a:" in ln and "single turn" in ln for ln in lines)


def test_receipt_names_a_bot_the_pod_total_would_average_away(monkeypatch):
    """One bot re-warming 6x an hour against three healthy ones: the pod total
    lands comfortably inside the 1/h target and the pod reads as fixed. The
    per-bot line is what stops that."""
    hot = sparse_session("hot", DAY, turns=13, gap_minutes=10)   # 12 in 2h
    calm = dense_session_rows("calm", DAY, turns=40, gap_seconds=60 * 20)

    def _rows(bot_id, **kw):
        return hot if bot_id == "bot_hot" else calm

    monkeypatch.setattr(cs, "load_turn_records", _rows)
    lines = cs.receipt_lines(["bot_hot", "bot_b", "bot_c", "bot_d"])
    total_line = next(ln for ln in lines if ln.startswith("prefix re-warms:"))
    per_hour = float(total_line.split(",")[1].split()[0])
    assert per_hour <= cs.REWARMS_PER_ACTIVE_HOUR_TARGET, (
        "the pod total has to be inside the target for this test to mean "
        "anything — otherwise the per-bot line is not what surfaced it"
    )
    hot_line = next(ln for ln in lines if ln.strip().startswith("bot_hot:"))
    assert "per active hour" in hot_line and "above the" in hot_line
    quiet = [ln.strip() for ln in lines]
    assert not any(
        q.startswith(f"{b}:") for q in quiet for b in ("bot_b", "bot_c", "bot_d")
    )


def test_receipt_says_how_many_turns_it_could_not_read(monkeypatch):
    """An unmeasured turn is named, not folded into the rate."""
    rows = sparse_day(turns=6) + [
        {"session_id": "sess-sparse",
         "ts": (DAY + timedelta(minutes=18 * (6 + i))).isoformat()}
        for i in range(4)
    ]
    monkeypatch.setattr(cs, "load_turn_records", lambda bot_id, **kw: rows)
    lines = cs.receipt_lines(["bot_a"])
    assert any("4 turns not counted — no cache fields" in ln for ln in lines)


def test_receipt_stays_quiet_about_healthy_rotation(monkeypatch):
    """The silent majority must not bury the one bot that is paying."""
    rows = []
    for sess in range(3):
        for t in range(10):
            rows.append(_turn(
                f"s{sess}", DAY + timedelta(hours=8 * sess, minutes=3 * t),
                write=PREFIX_TOKENS if t == 0 else 0,
            ))
    monkeypatch.setattr(cs, "load_turn_records", lambda bot_id, **kw: rows)
    monkeypatch.setattr(cs, "read_idle_reset_minutes", lambda bot_id: 120)
    lines = cs.receipt_lines(["bot_a"])
    assert not any("bot_a:" in ln for ln in lines)


def test_receipt_distinguishes_all_single_turn_sessions(monkeypatch):
    rows = [_turn(f"s{i}", DAY + timedelta(hours=i)) for i in range(5)]
    monkeypatch.setattr(cs, "load_turn_records", lambda bot_id, **kw: rows)
    lines = cs.receipt_lines(["bot_a"])
    assert "nothing to measure" in lines[0]
    assert "single turn" in lines[0]
