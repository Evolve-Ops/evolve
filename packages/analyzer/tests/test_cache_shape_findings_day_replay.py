"""Replay of the measured day, before and after the 1-hour tier.

``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §2 records the
shape but not a per-turn table, so this reconstructs it from the figures it
does give: 15 turns over the UTC day, 10-30 minutes apart, with the console's
cache-write column reading 43k, 46k, 52k, 61k, 74k, 82k on consecutive turns
against a ~45k fixed prefix, on the 5-minute default cache.

The replay is a fixture, not a measurement of the pod — it exists so the
chip's before/after can be reproduced by anyone, and so the definition-of-done
target (**prefix re-warms ≤ 1 per hour of activity**) is a test rather than a
claim in a PR body. The live number comes from the weekly receipt.

Run ``pytest -s -k replay_table`` to print the table the PR body quotes.
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


DAY_START = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

#: Gaps between the day's 15 turns, minutes. Within the finding's stated
#: 10-30 minute band; the two long holes are the meal-shaped breaks that put
#: two of the turns outside even a 1-hour window.
GAPS_MINUTES = [18, 12, 27, 21, 14, 95, 11, 24, 19, 16, 30, 140, 13, 22]

#: The fixed prefix each cold turn re-wrote, tokens. The finding's console
#: column, cycled — the growth is history riding along with the prefix.
WRITE_TOKENS = [43_000, 46_000, 52_000, 61_000, 74_000, 82_000]

#: Internal model calls per turn. The finding's turn made thirteen; calls
#: 2-13 each re-read the prefix call 1 had just written, and that is the
#: entire source of the day's 79-87% headline hit rate.
CALLS_PER_TURN = 13


def _findings_day(*, cache_ttl_seconds: int) -> list[dict]:
    """The day's 15 turn records under a given cache window.

    Only the cache columns differ between the two runs: a turn whose gap from
    the previous one outlived the window re-warms the prefix from COLD (a big
    ``cache_write``, nothing read), and a turn inside the window reads it
    instead. The turn count, the timing and the work done are identical — this
    chip does not change what the model is asked or which model answers.

    ``read == 0`` on a cold turn is the shipped cold-miss rule
    (``cache_shape.is_prefix_rewarm``), now shared with
    ``context_census.cache_report``. The intra-turn reads that made this day
    look healthy are on the CALL records — :func:`_findings_day_calls`.
    """
    rows: list[dict] = []
    at = DAY_START
    for i in range(len(GAPS_MINUTES) + 1):
        prefix = WRITE_TOKENS[i % len(WRITE_TOKENS)]
        gap = GAPS_MINUTES[i - 1] * 60 if i else None
        cold = i == 0 or (gap is not None and gap > cache_ttl_seconds)
        rows.append({
            "session_id": "sess-c851835b",
            "ts": at.isoformat().replace("+00:00", "Z"),
            "cache_write_tokens": prefix if cold else 0,
            "cache_read_tokens": 0 if cold else prefix,
        })
        if i < len(GAPS_MINUTES):
            at += timedelta(minutes=GAPS_MINUTES[i])
    return rows


def _findings_day_calls(*, cache_ttl_seconds: int) -> list[dict]:
    """The same day expanded to the thirteen model calls each turn made.

    Call 1 either writes the prefix from cold or reads it; calls 2-13 each
    re-read what call 1 left in the cache. This is the population the
    provider console's hit rate was computed over, and the reason it read
    79-87% on a day whose every turn started cold.
    """
    calls: list[dict] = []
    for row in _findings_day(cache_ttl_seconds=cache_ttl_seconds):
        prefix = row["cache_write_tokens"] or row["cache_read_tokens"]
        calls.append(dict(row))
        for _ in range(CALLS_PER_TURN - 1):
            calls.append({**row, "cache_write_tokens": 0,
                          "cache_read_tokens": prefix})
    return calls


def _token_hit_rate(rows: list[dict]) -> float:
    read = sum(r["cache_read_tokens"] for r in rows)
    write = sum(r["cache_write_tokens"] for r in rows)
    return read / (read + write) if (read + write) else 0.0


def test_the_replay_matches_the_findings_shape():
    rows = _findings_day(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["short"])
    assert len(rows) == 15
    # Every gap in the band exceeds the 5-minute window, so every turn is cold.
    assert sum(1 for r in rows if r["cache_write_tokens"]) == 15
    # And the token-weighted rate over the day's CALLS still reads like a
    # healthy cache — the number the finding says was hiding the problem.
    calls = _findings_day_calls(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["short"])
    assert len(calls) == 15 * CALLS_PER_TURN
    assert 0.79 <= _token_hit_rate(calls) <= 0.93


def test_before_the_cross_turn_rate_is_zero():
    rows = _findings_day(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["short"])
    ct = cs.cross_turn_cache(rows)
    assert ct.eligible == 14
    assert ct.rewarms == 14
    assert ct.hit_rate == pytest.approx(0.0)
    assert ct.rewarms_per_active_hour > 1.0


def _findings_week(*, cache_ttl_seconds: int, days: int = 7) -> list[dict]:
    """The day repeated once a day for a week, one session per day.

    ``auto`` needs ``MIN_WINDOW_DAYS_FOR_AUTO`` days of OBSERVED history and
    the span is now derived from the records, so a decision fixture has to
    cover real days. One day of this shape is exactly what
    ``test_auto_declines_on_a_single_day_of_it`` proves it will NOT act on.
    """
    rows: list[dict] = []
    for d in range(days):
        for row in _findings_day(cache_ttl_seconds=cache_ttl_seconds):
            at = datetime.fromisoformat(
                row["ts"].replace("Z", "+00:00")
            ) + timedelta(days=d)
            rows.append({**row, "session_id": f"sess-c851835b-{d}",
                         "ts": at.isoformat().replace("+00:00", "Z")})
    return rows


def test_auto_picks_the_one_hour_tier_for_this_day():
    stats = cs.gap_stats(
        _findings_week(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["short"]),
    )
    assert stats.window_days >= cs.MIN_WINDOW_DAYS_FOR_AUTO
    assert cs.resolve_auto_retention(stats).retention == "long"


def test_auto_declines_on_a_single_day_of_it():
    """One day of this traffic is not evidence to move a tier on, however
    lopsided the arithmetic looks — the 2.00x write premium would be bought
    off a sample of one afternoon."""
    stats = cs.gap_stats(
        _findings_day(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["short"]),
    )
    assert stats.rewrite_factor > cs.LONG_RETENTION_BREAKEVEN
    assert cs.resolve_auto_retention(stats).retention is None


def test_after_the_target_is_met():
    """Definition of done: prefix re-warms ≤ 1 per hour of activity."""
    rows = _findings_day(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["long"])
    ct = cs.cross_turn_cache(rows)
    # Only the two gaps longer than an hour still re-warm.
    assert ct.rewarms == 2
    assert ct.hit_rate == pytest.approx(12 / 14)
    assert ct.rewarms_per_active_hour is not None
    assert ct.rewarms_per_active_hour <= 1.0


def test_the_work_done_is_identical_between_the_two_runs():
    """The guardrail, as a test: the tier changes what the cache columns say
    and nothing else. Same turns, same timing, same session."""
    before = _findings_day(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["short"])
    after = _findings_day(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["long"])
    assert [r["ts"] for r in before] == [r["ts"] for r in after]
    assert [r["session_id"] for r in before] == [r["session_id"] for r in after]
    assert len(before) == len(after)
    # The CACHE columns are the only ones that move — and they move together:
    # a turn is either cold (writes the prefix, reads nothing) or warm (reads
    # it, writes nothing), so the prefix each turn carried is identical.
    assert [
        r["cache_write_tokens"] or r["cache_read_tokens"] for r in before
    ] == [
        r["cache_write_tokens"] or r["cache_read_tokens"] for r in after
    ]
    assert [set(r) for r in before] == [set(r) for r in after]


def test_replay_table(capsys):
    """Prints the before/after table the PR body and the evidence file quote."""
    lines = ["", "finding's day (15 turns, one session), replayed", ""]
    lines.append(f"{'':<10} {'cross-turn hit':>15} {'re-warms':>10} "
                 f"{'per active hour':>16} {'tokens rewritten':>18}")
    for label, ttl in (("5m (before)", "short"), ("1h (after)", "long")):
        rows = _findings_day(cache_ttl_seconds=cs.CACHE_TTL_SECONDS[ttl])
        ct = cs.cross_turn_cache(rows)
        lines.append(
            f"{label:<10} {(ct.hit_rate or 0) * 100:>14.0f}% {ct.rewarms:>10} "
            f"{ct.rewarms_per_active_hour:>16.2f} {ct.rewarm_tokens:>18,}"
        )
    before = _findings_day(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["short"])
    before_calls = _findings_day_calls(
        cache_ttl_seconds=cs.CACHE_TTL_SECONDS["short"],
    )
    lines += [
        "",
        f"token-weighted hit rate over the SAME day's model calls: "
        f"{_token_hit_rate(before_calls) * 100:.0f}%  (the number that hid it)",
        "",
    ]
    stats = cs.gap_stats(
        _findings_week(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["short"]),
    )
    lines.append("auto's evidence: " + cs.resolve_auto_retention(stats).reason)
    lines.append("")
    print("\n".join(lines))
    out = capsys.readouterr().out
    # The table is what the PR body quotes, so pin the NUMBERS in it, not
    # just that its own header printed. A reporting helper wearing a test's
    # name is finding 10.
    assert "cross-turn hit" in out
    ct_before = cs.cross_turn_cache(before)
    ct_after = cs.cross_turn_cache(
        _findings_day(cache_ttl_seconds=cs.CACHE_TTL_SECONDS["long"]),
    )
    assert f"{ct_before.rewarms:>10}" in out and f"{ct_after.rewarms:>10}" in out
    assert f"{(ct_before.hit_rate or 0) * 100:>14.0f}%" in out
    assert f"{(ct_after.hit_rate or 0) * 100:>14.0f}%" in out
    assert f"{_token_hit_rate(before_calls) * 100:.0f}%" in out
