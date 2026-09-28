"""``load_turns`` selects turn files by the UTC day, because that is how they
are NAMED.

``TurnObserver.writeTurnToShared`` does::

    const date = new Date().toISOString().slice(0, 10);
    const filePath = path.join(turnsDir, `turns-${date}.jsonl`);

``toISOString()`` is always UTC, so ``turns-2026-08-27.jsonl`` holds the UTC
day's turns and every ``ts`` is a ``Z`` timestamp. The reader's default was a
naive ``datetime.now()`` — LOCAL — which asked for local-dated filenames the
writer never creates.

West of UTC that is not a rounding error, it is a daily blind window. Observed
live on the reference pod (US/Pacific) at 2026-08-26 20:55 local = 2026-08-27
03:55 UTC: ``turns-2026-08-27.jsonl`` was being actively appended to, while
``turns-2026-08-26.jsonl`` had stopped growing at 16:55 local (00:00 UTC). A
``days=1`` load anchored on the local date read the stale file and saw none of
the evening's turns — every day, for the whole 17:00→24:00 local stretch.

These tests pin the boundary in both directions and do not depend on the
machine's real clock or zone.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import usage_analytics as ua  # noqa: E402


# US/Pacific is UTC-7 in August. 20:55 local is 03:55 the NEXT day in UTC —
# the exact instant the blind window was observed on the reference pod.
_PACIFIC_AUG = timezone(timedelta(hours=-7))
_EVENING_LOCAL = datetime(2026, 8, 26, 20, 55, tzinfo=_PACIFIC_AUG)
_UTC_DAY = "2026-08-27"      # what the writer names the file
_LOCAL_DAY = "2026-08-26"    # what the old naive-local default asked for


def _turn(ts: str) -> dict:
    return {"ts": ts, "model": "anthropic/claude-sonnet-4-5",
            "provider": "anthropic", "session_id": "s-1", "cost": 0.01,
            "input_tokens": 10, "output_tokens": 10}


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A pod with turns in BOTH the local-named and UTC-named files.

    Only the UTC-named file is the one the writer would really be appending
    to at ``_EVENING_LOCAL``; the other stands in for the previous UTC day.
    """
    turns_dir = tmp_path / "placeholder_bot" / "turns"
    turns_dir.mkdir(parents=True)
    (turns_dir / f"turns-{_UTC_DAY}.jsonl").write_text(
        json.dumps(_turn(f"{_UTC_DAY}T03:55:00Z")) + "\n"
    )
    (turns_dir / f"turns-{_LOCAL_DAY}.jsonl").write_text(
        json.dumps(_turn(f"{_LOCAL_DAY}T18:00:00Z")) + "\n"
    )
    monkeypatch.setattr(
        ua, "_find_turns_dirs",
        lambda bot_id, network_path=None: [turns_dir],
    )
    return turns_dir


def _days_loaded(turns: list[dict]) -> set[str]:
    return {(t.get("ts") or "")[:10] for t in turns}


def test_default_end_date_is_the_utc_day_not_the_local_day(pod, monkeypatch):
    """THE finding, asserted on the file that gets read.

    At 20:55 US/Pacific the current UTC day is already the 27th. A
    ``days=1`` load must read ``turns-2026-08-27.jsonl`` — the file being
    written — not the local-dated one that stopped growing at 17:00.
    """
    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                # What a naive datetime.now() would have returned: local.
                return _EVENING_LOCAL.astimezone(_PACIFIC_AUG).replace(tzinfo=None)
            return _EVENING_LOCAL.astimezone(tz)

    monkeypatch.setattr(ua, "datetime", _FrozenDT)

    loaded = _days_loaded(ua.load_turns("placeholder_bot", days=1))

    assert loaded == {_UTC_DAY}, (
        "load_turns anchored its window on the LOCAL day — west of UTC that "
        "silently drops the turn file currently being appended to"
    )


def test_an_aware_end_date_is_converted_to_its_utc_day(pod):
    """A caller may pass an aware datetime in any zone; it names an instant.

    ``_EVENING_LOCAL`` is the 26th in Pacific but the 27th in UTC, and the
    file it must resolve to is the UTC one.
    """
    loaded = _days_loaded(
        ua.load_turns("placeholder_bot", days=1, end_date=_EVENING_LOCAL)
    )
    assert loaded == {_UTC_DAY}


def test_an_aware_utc_end_date_is_unchanged(pod):
    """The already-correct callers (spend_alert, cost_watchdog,
    provisioning_budget all pass ``datetime.now(timezone.utc)``) keep their
    exact behaviour — this change must not move them."""
    end = datetime(2026, 8, 27, 3, 55, tzinfo=timezone.utc)
    loaded = _days_loaded(
        ua.load_turns("placeholder_bot", days=1, end_date=end)
    )
    assert loaded == {_UTC_DAY}


def test_a_naive_end_date_is_honoured_verbatim(pod):
    """A naive ``end_date`` is the caller's explicit calendar choice.

    Reinterpreting it (as UTC, or as local) would override that choice.
    ``tile_metrics._as_end_date`` relies on this: it passes naive midnight of
    the day it wants and expects exactly that day's file.
    """
    end = datetime(2026, 8, 26, 0, 0)
    loaded = _days_loaded(
        ua.load_turns("placeholder_bot", days=1, end_date=end)
    )
    assert loaded == {_LOCAL_DAY}


def test_a_two_day_window_spans_the_utc_boundary(pod):
    """``days=2`` from the UTC day covers both files — the widening a caller
    bucketing by a POD-LOCAL day needs, since a local day straddles two UTC
    files."""
    end = datetime(2026, 8, 27, 3, 55, tzinfo=timezone.utc)
    loaded = _days_loaded(
        ua.load_turns("placeholder_bot", days=2, end_date=end)
    )
    assert loaded == {_LOCAL_DAY, _UTC_DAY}
