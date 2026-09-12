"""Reactivating a bot ACCEPTS the window: the breaker counts from there.

Operator, 2026-09-07: "if a bot is consciously brought back after a breaker
has tripped, then the operator is indicating that the previous spike is ok
(or is over). So it should not use that data to break again."

The defect: reactivation cleared the breaker file and nothing else, so the
next enforcement tick re-read the SAME window — today's raw spend — saw it
still over the cap, and tripped again on the very spend the operator had
just accepted. Observed live on one bot: three reactivations, three
immediate re-trips against "$27.49 >= $5.00".

The fix is an origin, not a ceiling. Reactivation records today's spend
total; every evaluation measures from there. A genuinely NEW spike of a
full cap's worth past the acceptance point still trips — that is the
property these tests exist to keep honest, because a marker that suppressed
real trips would be worse than the flood it replaces.

Chip: internal/dispatch/done/breaker-reactivate-accepts-the-window.md.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spend_alert  # noqa: E402
import spend_caps  # noqa: E402

BOT = "team_bot_a"
DAY = date(2026, 9, 7)


@dataclass
class _DaySpend:
    """The subset of ``spend_alert.DaySpend`` ``daily_cap_decision`` reads."""

    usd: float
    unpriced_turns: int = 0
    unpriced_providers: tuple = ()
    measurable: bool = True
    priced_turns: int = 10


# ── The marker itself ────────────────────────────────────────────────────────


def test_marker_round_trips(tmp_path):
    marker = spend_caps.write_accepted_through(
        tmp_path, BOT, spend_usd=27.49, day=DAY,
    )
    assert marker.accepted_usd == 27.49
    read = spend_caps.read_accepted_through(tmp_path, BOT, DAY)
    assert read is not None
    assert read.accepted_usd == 27.49
    assert read.accepted_at == marker.accepted_at
    assert read.bot_id == BOT


def test_no_marker_reads_as_none(tmp_path):
    assert spend_caps.read_accepted_through(tmp_path, BOT, DAY) is None


def test_marker_expires_with_the_window_it_was_taken_in(tmp_path):
    """The window IS the pod-local day, so tomorrow never sees it.

    Structural, not a timer: the file is named for the day. Today's spend
    restarts at $0 at the boundary, so a marker that survived it would be a
    permanent discount on a fresh window.
    """
    spend_caps.write_accepted_through(tmp_path, BOT, spend_usd=27.49, day=DAY)
    assert spend_caps.read_accepted_through(tmp_path, BOT, DAY) is not None
    tomorrow = DAY + timedelta(days=1)
    assert spend_caps.read_accepted_through(tmp_path, BOT, tomorrow) is None


def test_a_marker_stamped_for_another_day_reads_as_absent(tmp_path):
    """Belt and braces: even if a file for today carries yesterday's day
    stamp (a clock skew, a hand-edit), it is not honoured."""
    fp = spend_caps.accepted_through_path(tmp_path, BOT, DAY)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps({
        "bot_id": BOT, "day": "2026-09-06", "accepted_usd": 27.49,
        "accepted_at": "2026-09-06T21:40:00+00:00",
    }))
    assert spend_caps.read_accepted_through(tmp_path, BOT, DAY) is None


@pytest.mark.parametrize("payload", [
    "{ not json",
    json.dumps({"day": str(DAY), "accepted_usd": -5, "accepted_at": "x"}),
    json.dumps({"day": str(DAY), "accepted_usd": "lots", "accepted_at": "x"}),
    json.dumps({"day": str(DAY), "accepted_usd": 5.0}),
    json.dumps([1, 2, 3]),
])
def test_a_malformed_marker_reads_as_absent(tmp_path, payload):
    """"Cannot read" must mean "measure the raw window", never "discount".

    The failure direction matters: an unreadable marker can only make the
    breaker MORE willing to trip. The inverse default would turn an I/O
    glitch into an open-ended pass on a live bleed.
    """
    fp = spend_caps.accepted_through_path(tmp_path, BOT, DAY)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(payload)
    assert spend_caps.read_accepted_through(tmp_path, BOT, DAY) is None


def test_a_second_reactivation_moves_the_origin_forward(tmp_path):
    spend_caps.write_accepted_through(tmp_path, BOT, spend_usd=27.49, day=DAY)
    spend_caps.write_accepted_through(tmp_path, BOT, spend_usd=31.02, day=DAY)
    read = spend_caps.read_accepted_through(tmp_path, BOT, DAY)
    assert read.accepted_usd == 31.02


def test_clear_is_idempotent(tmp_path):
    spend_caps.write_accepted_through(tmp_path, BOT, spend_usd=1.0, day=DAY)
    assert spend_caps.clear_accepted_through(tmp_path, BOT, DAY) is True
    assert spend_caps.clear_accepted_through(tmp_path, BOT, DAY) is False


def test_spend_since_accepted_clamps_and_preserves_unknown(tmp_path):
    marker = spend_caps.write_accepted_through(
        tmp_path, BOT, spend_usd=27.49, day=DAY,
    )
    assert spend_caps.spend_since_accepted(32.69, marker) == pytest.approx(5.20)
    # Re-pricing downward is not credit.
    assert spend_caps.spend_since_accepted(20.0, marker) == 0.0
    # "I could not measure" is not zero.
    assert spend_caps.spend_since_accepted(None, marker) is None
    assert spend_caps.spend_since_accepted(9.0, None) == 9.0


# ── The decision that consumes it ────────────────────────────────────────────

LADDER = {"tier_downgrade": None, "l1_breaker": 5.0, "l2_breaker": None}


def test_same_window_spend_does_not_retrip_after_acceptance():
    """THE regression. $27.49 of spend, a $5 cap, and an accepted window:
    the rung must not fire on the spend the operator just accepted."""
    d = spend_alert.daily_cap_decision(
        _DaySpend(usd=27.49), threshold=100.0, ladder=LADDER,
        accepted_usd=27.49,
    )
    assert d["tripped"] == []
    assert d["effective_usd"] == pytest.approx(0.0)
    # The RAW total is still reported — the operator means that by
    # "today's spend", and the alert lane still uses it.
    assert d["usd"] == pytest.approx(27.49)
    assert d["accepted_usd"] == pytest.approx(27.49)


def test_five_more_turns_under_the_cap_still_do_not_retrip():
    for extra in (0.40, 1.10, 2.05, 3.30, 4.90):
        d = spend_alert.daily_cap_decision(
            _DaySpend(usd=27.49 + extra), threshold=100.0, ladder=LADDER,
            accepted_usd=27.49,
        )
        assert d["tripped"] == [], f"re-tripped after only ${extra:.2f} more"


def test_a_genuinely_new_spike_past_the_cap_still_trips():
    """The marker moves the ORIGIN; it never raises the ceiling."""
    d = spend_alert.daily_cap_decision(
        _DaySpend(usd=27.49 + 5.20), threshold=100.0, ladder=LADDER,
        accepted_usd=27.49,
    )
    assert d["tripped"] == ["l1_breaker"]
    assert d["effective_usd"] == pytest.approx(5.20)


def test_no_marker_is_exactly_the_pre_marker_behaviour():
    with_default = spend_alert.daily_cap_decision(
        _DaySpend(usd=27.49), threshold=100.0, ladder=LADDER,
    )
    explicit_zero = spend_alert.daily_cap_decision(
        _DaySpend(usd=27.49), threshold=100.0, ladder=LADDER, accepted_usd=0.0,
    )
    assert with_default["tripped"] == ["l1_breaker"]
    assert with_default == explicit_zero


def test_every_remediation_rung_measures_from_the_marker():
    ladder = {"tier_downgrade": 2.0, "l1_breaker": 5.0, "l2_breaker": 20.0}
    d = spend_alert.daily_cap_decision(
        _DaySpend(usd=30.0), threshold=100.0, ladder=ladder, accepted_usd=27.49,
    )
    # $2.51 since acceptance crosses only the lowest rung.
    assert d["tripped"] == ["tier_downgrade"]


def test_an_unmeasurable_day_still_enforces_on_its_priced_floor():
    """Audit B6's contract survives the marker: a floor over the cap is
    still over the cap, measured from the same origin."""
    d = spend_alert.daily_cap_decision(
        _DaySpend(usd=33.0, unpriced_turns=4, measurable=False),
        threshold=100.0, ladder=LADDER, accepted_usd=27.49,
    )
    assert d["verdict"] == "unmeasurable"
    assert d["tripped"] == ["l1_breaker"]


def test_load_failed_keeps_the_marker_keys():
    d = spend_alert.daily_cap_decision(
        None, threshold=100.0, ladder=LADDER, accepted_usd=27.49,
    )
    assert d["verdict"] == "load_failed"
    assert d["effective_usd"] is None
    assert d["accepted_usd"] == pytest.approx(27.49)


# ── The reader spend_alert uses ─────────────────────────────────────────────


def test_read_accepted_usd_returns_zero_when_absent(tmp_path):
    assert spend_alert._read_accepted_usd(tmp_path, BOT, DAY) == 0.0


def test_read_accepted_usd_returns_the_marker(tmp_path):
    spend_caps.write_accepted_through(tmp_path, BOT, spend_usd=27.49, day=DAY)
    assert spend_alert._read_accepted_usd(tmp_path, BOT, DAY) == 27.49


def test_read_accepted_usd_fails_toward_enforcing(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(spend_caps, "read_accepted_through", boom)
    assert spend_alert._read_accepted_usd(tmp_path, BOT, DAY) == 0.0


# ── What the card shows ─────────────────────────────────────────────────────


def test_the_flag_records_both_figures(tmp_path):
    marker = spend_caps.write_accepted_through(
        tmp_path, BOT, spend_usd=27.49, day=DAY,
    )
    spend_caps.write_enforcement_flag(
        tmp_path, BOT, "checkpoint", 5.20, 5.0, DAY,
        accepted=marker, spend_total=32.69,
    )
    flag = json.loads(
        (tmp_path / "spend-caps" / f"{BOT}-{DAY}.json").read_text()
    )
    # spend_at_trigger is the number that CROSSED the cap...
    assert flag["spend_at_trigger"] == pytest.approx(5.20)
    # ...and the day's raw total is kept beside it so the card can
    # explain the difference instead of looking like a lie.
    assert flag["spend_total"] == pytest.approx(32.69)
    assert flag["accepted_usd"] == pytest.approx(27.49)
    assert flag["accepted_at"] == marker.accepted_at


def test_the_flag_without_a_marker_keeps_the_old_meaning(tmp_path):
    spend_caps.write_enforcement_flag(tmp_path, BOT, "checkpoint", 27.49, 5.0, DAY)
    flag = json.loads(
        (tmp_path / "spend-caps" / f"{BOT}-{DAY}.json").read_text()
    )
    assert flag["spend_at_trigger"] == pytest.approx(27.49)
    assert flag["spend_total"] == pytest.approx(27.49)
    assert flag["accepted_usd"] is None
    assert flag["accepted_at"] is None


def _rec(bot_id=BOT, type_="cost"):
    @dataclass
    class R:
        bot_id: str
        type: str
        trip_id: str = "deadbeef"
        tripped_at: str = "2026-09-07T21:18:00+00:00"
        expires_at: str | None = None
        initiated_by: str = "auto:spend_alert"
        reason: str = "per-bot daily cap exceeded"
    return R(bot_id=bot_id, type=type_)


def test_the_card_projection_carries_the_acceptance(tmp_path):
    spend_caps.write_accepted_through(
        tmp_path, BOT, spend_usd=27.49,
        day=spend_caps._pod_today(),
        now=datetime(2026, 9, 7, 21, 40, tzinfo=timezone.utc),
    )
    entry = spend_caps.breaker_ui_entry(_rec(), tmp_path)
    assert entry["accepted_usd"] == pytest.approx(27.49)
    assert entry["accepted_at_label"] is not None
    assert len(entry["accepted_at_label"]) == 5


def test_the_card_projection_is_null_shaped_without_a_marker(tmp_path):
    entry = spend_caps.breaker_ui_entry(_rec(), tmp_path)
    assert entry["accepted_usd"] is None
    assert entry["accepted_at"] is None
    assert entry["trip_id"] == "deadbeef"


def test_pod_and_non_cost_records_are_never_given_an_acceptance(tmp_path):
    """A pod trip has no per-bot spend to accept, and a `full` halt is not
    a spend judgement — accepting a window on either would raise a cost
    ceiling for a reason that had nothing to do with cost."""
    spend_caps.write_accepted_through(
        tmp_path, BOT, spend_usd=27.49, day=spend_caps._pod_today(),
    )
    assert spend_caps.breaker_ui_entry(
        _rec(bot_id="pod"), tmp_path)["accepted_usd"] is None
    assert spend_caps.breaker_ui_entry(
        _rec(type_="full"), tmp_path)["accepted_usd"] is None
