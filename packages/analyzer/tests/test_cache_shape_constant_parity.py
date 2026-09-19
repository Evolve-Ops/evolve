"""Three modules carry Anthropic's prompt-cache numbers. They must agree.

``cache_shape`` (the ``auto`` resolver and the cross-turn metric),
``context_census`` (the cold-miss cause table) and the admin
``routes_analytics`` TTL-recommendation closure each declare the cache
windows and, in two cases, the write/read price multipliers. Merging them is
a bigger refactor than the chip that added the third copy should carry — so
instead this pins the equality, which is the rule the context-observability
spec states for exactly this shape: *whenever two readers exist for one
setting, assert they agree.*

A drift here means an operator-facing recommendation and a deploy-time
``auto`` decision are pricing the same cache differently. Fix the copy that
is wrong; do not update the baseline.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cache_shape as cs  # noqa: E402
import context_census as cc  # noqa: E402


_ROUTES_ANALYTICS = (
    _ANALYZER_DIR.parent / "admin" / "evolve_admin" / "web" / "routes_analytics.py"
)


def _routes_source() -> str:
    """The admin copy's source. A MISSING file fails rather than skips.

    Skipping was the wrong posture for a drift guard: in an analyzer-only
    shard the four ``routes_analytics`` parity tests would skip, the drift
    they exist to catch would ship, and the run would still be green. The
    admin package sits at a fixed relative path in this repo, so its absence
    is a broken checkout or a moved file — both of which are exactly the
    conditions under which these assertions stop being checked.
    """
    assert _ROUTES_ANALYTICS.exists(), (
        f"routes_analytics.py not found at {_ROUTES_ANALYTICS} — the cache "
        f"price/window parity guard cannot run. If the file moved, update "
        f"_ROUTES_ANALYTICS; do not let the guard skip."
    )
    return _ROUTES_ANALYTICS.read_text()


def test_census_ttl_minutes_match_cache_shape_seconds():
    for name, minutes in cc.CACHE_TTL_MINUTES.items():
        assert minutes * 60 == cs.CACHE_TTL_SECONDS[name], name
    assert cc.DEFAULT_CACHE_TTL_MINUTES * 60 == cs.CACHE_TTL_SECONDS[None]


def test_census_prefix_floor_matches_the_rewarm_floor():
    """The census's cold-miss floor and the receipt's re-warm floor are the
    same threshold under two names; a split would have the two surfaces
    disagree about whether a given turn missed."""
    assert cc.DEFAULT_PREFIX_FLOOR_TOKENS == cs.PREFIX_REWARM_FLOOR_TOKENS


def test_routes_analytics_cache_windows_match():
    src = _routes_source()
    assert (
        '_CACHE_TTL_SECONDS: dict[str | None, int] = '
        '{"long": 3600, "short": 300, None: 300}'
    ) in src, "routes_analytics' cache windows moved; reconcile with cache_shape"
    assert cs.CACHE_TTL_SECONDS == {"long": 3600, "short": 300, None: 300}


def test_routes_analytics_price_multipliers_match():
    src = _routes_source()
    assert (
        '_CACHE_WRITE_MULT: dict[str, float] = {"short": 1.25, "long": 2.00}'
    ) in src, "routes_analytics' write multipliers moved; reconcile with cache_shape"
    assert cs.CACHE_WRITE_MULT == {"short": 1.25, "long": 2.00}
    assert re.search(r"^\s*_CACHE_READ_MULT = 0\.10\s*$", src, re.M), (
        "routes_analytics' cache-read multiplier moved; reconcile with cache_shape"
    )
    assert cs.CACHE_READ_MULT == 0.10


def test_the_break_even_is_derived_not_typed():
    """Both copies compute the break-even from the multipliers rather than
    hard-coding 1.65 — so correcting a price corrects the threshold."""
    expected = (
        (cs.CACHE_WRITE_MULT["long"] - cs.CACHE_READ_MULT)
        / (cs.CACHE_WRITE_MULT["short"] - cs.CACHE_READ_MULT)
    )
    assert cs.LONG_RETENTION_BREAKEVEN == pytest.approx(expected)
    assert cs.LONG_RETENTION_BREAKEVEN == pytest.approx(1.652, abs=1e-3)
    src = _routes_source()
    assert "_LONG_RETENTION_BREAKEVEN = (" in src
    assert '(_CACHE_WRITE_MULT["long"] - _CACHE_READ_MULT)' in src


def test_min_gaps_for_economics_matches():
    src = _routes_source()
    assert f"_MIN_GAPS_FOR_ECONOMICS = {cs.MIN_GAPS_FOR_ECONOMICS}" in src


# ── The re-warm predicate ────────────────────────────────────────────────────
# Constants were pinned above; the RULE that uses them was not, and that is
# where the two surfaces actually disagreed: ``cross_turn_cache`` counted
# ``write > floor`` while ``cache_report`` counted ``read == 0 and write >
# floor``. One fixture, both surfaces, identical counts.

_FLOOR = cs.PREFIX_REWARM_FLOOR_TOKENS
_T0 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

#: (gap minutes from the previous row, cache_read, cache_write) per row after
#: the session's first. Deliberately mixed: clean re-warms, a warm read, a
#: sub-floor incremental write, and the case the two rules used to disagree
#: on — a big write that ALSO read (a partial prefix survived).
_ROWS = [
    (0, 0, 45_000),        # session's first call — the priming write
    (18, 0, 46_000),       # a clean re-warm
    (12, 45_000, 0),       # read the prefix back
    (27, 0, 52_000),       # a clean re-warm
    (9, 45_000, 200),      # a sub-floor incremental write, not a rewarm
    (21, 3_000, 61_000),   # wrote a prefix AND read one: the disagreement
    (14, 45_000, 0),       # read the prefix back
]


def _turn_rows() -> list[dict]:
    """``_ROWS`` as turn records — what ``cache_shape.cross_turn_cache`` reads."""
    rows: list[dict] = []
    at = _T0
    for gap, read, write in _ROWS:
        at = at + timedelta(minutes=gap)
        rows.append({
            "session_id": "sess-parity",
            "ts": at.isoformat().replace("+00:00", "Z"),
            "cache_read_tokens": read,
            "cache_write_tokens": write,
        })
    return rows


def _census_sessions():
    """``_ROWS`` as census calls — one call per turn, so the two surfaces are
    counting the same events and a difference can only be the predicate."""
    calls = []
    at = _T0
    for ordinal, (gap, read, write) in enumerate(_ROWS):
        at = at + timedelta(minutes=gap)
        calls.append(cc.Call(
            session_id="sess-parity",
            ordinal=ordinal,
            run_index=ordinal,          # one call per turn
            ts=at,
            model="claude-opus-4-5",
            provider="anthropic",
            input_tokens=100,
            cache_read=read,
            cache_write=write,
            output_tokens=50,
            cache_fields_present=True,
            recorded_cost=0.0,
            stop_reason="end_turn",
            history=[],
        ))
    return [cc.Session(session_id="sess-parity", calls=calls)]


def test_one_rewarm_predicate_serves_both_surfaces():
    """The receipt and the census must not publish two counts for one event."""
    receipt = cs.cross_turn_cache(_turn_rows())
    census = cc.cache_report(_census_sessions(), [], prefix_floor=_FLOOR)

    assert receipt.eligible == census["cross_turn_eligible"] == len(_ROWS) - 1
    assert receipt.rewarms == census["prefix_rewarms"]
    assert receipt.hit_rate == pytest.approx(census["cross_turn_hit_rate"])
    # And it is the strict rule that is shared: the 61k write that also read
    # 3k is NOT a re-warm on either surface.
    assert receipt.rewarms == 2


def test_the_predicate_is_the_censuss_cold_miss_rule():
    assert cs.is_prefix_rewarm(0, _FLOOR + 1, _FLOOR) is True
    assert cs.is_prefix_rewarm(0, _FLOOR, _FLOOR) is False        # at the floor
    assert cs.is_prefix_rewarm(1, _FLOOR + 1, _FLOOR) is False    # something was read
