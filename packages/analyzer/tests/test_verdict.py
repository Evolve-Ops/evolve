"""tests/test_verdict.py — the Verdict construction contract (D-CS7).

Each test here guards the one property the whole brief is about: a
control's pass condition must not be satisfiable by looking at nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from verdict import Verdict  # noqa: E402


def test_ok_verdict_requires_a_subject():
    with pytest.raises(ValueError, match="subject"):
        Verdict(state="ok", subject=None)
    with pytest.raises(ValueError, match="subject"):
        Verdict.ok(subject="")


def test_broken_verdict_requires_a_subject():
    with pytest.raises(ValueError, match="subject"):
        Verdict(state="broken", subject=None)
    with pytest.raises(ValueError, match="subject"):
        Verdict.broken(subject="", reason="daemon wedged")


def test_unknown_requires_a_reason():
    with pytest.raises(ValueError, match="reason"):
        Verdict(state="unknown", subject=None)
    with pytest.raises(ValueError, match="reason"):
        Verdict.unknown(reason="")


def test_unknown_may_carry_a_null_subject():
    v = Verdict.unknown("could not read the API")
    assert v.subject is None
    assert v.is_unknown


def test_unknown_may_also_carry_a_subject():
    v = Verdict.unknown("last tick's log tail could not be decoded", subject="repo-puller.log@123")
    assert v.subject == "repo-puller.log@123"


def test_ok_and_broken_round_trip_through_dict():
    ok = Verdict.ok("run:42", evidence={"created_at": "2026-09-01T00:00:00Z"})
    broken = Verdict.broken("run:43", "stale", evidence={"age_days": 30})
    assert Verdict.from_dict(ok.to_dict()) == ok
    assert Verdict.from_dict(broken.to_dict()) == broken
    assert ok.is_ok and not ok.is_broken and not ok.is_unknown
    assert broken.is_broken and broken.evidence["reason"] == "stale"


def test_invalid_state_is_rejected():
    with pytest.raises(ValueError, match="invalid verdict state"):
        Verdict(state="fine", subject="x")  # type: ignore[arg-type]
