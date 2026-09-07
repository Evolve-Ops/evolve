"""tests/test_signal_maturity_gate.py — fresh-pod "no-data-yet" maturity gate.

Falsifiable proof artifact for Mechanism 3 of
internal/spec-transient-signal-suppression-2026-06-23.md.

Covers the gate predicate (age + data-point dimensions, fail-open semantics)
and the shared ``settle_gate.pod_age`` anchor it reuses.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_util import atomic_write_json  # noqa: E402
from signals import maturity_gate, settle_gate  # noqa: E402

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


def _stamp_bringup(shared_dir: Path, started_at: datetime) -> None:
    atomic_write_json(
        shared_dir / "pod-bringup.json",
        {"started_at": started_at.isoformat().replace("+00:00", "Z"),
         "trigger": "fresh-wizard"},
        mode=0o644,
    )


# ── settle_gate.pod_age anchor ──────────────────────────────────────────────


def test_pod_age_none_without_markers(tmp_path):
    """No markers (established/upgraded pod) → age undeterminable."""
    assert settle_gate.pod_age(tmp_path, now=_NOW) is None
    assert settle_gate.pod_first_seen(tmp_path) is None


def test_pod_age_reads_bringup_marker(tmp_path):
    _stamp_bringup(tmp_path, _NOW - timedelta(days=3))
    age = settle_gate.pod_age(tmp_path, now=_NOW)
    assert age is not None
    assert abs(age.total_seconds() - 3 * 86400) < 1


def test_pod_age_falls_back_to_settled_marker(tmp_path):
    """When only the settled marker exists, it's the age lower bound."""
    atomic_write_json(
        tmp_path / "pod-settled.json",
        {"settled_at": (_NOW - timedelta(days=10)).isoformat().replace("+00:00", "Z"),
         "trigger": "fresh-wizard"},
        mode=0o644,
    )
    age = settle_gate.pod_age(tmp_path, now=_NOW)
    assert age is not None
    assert abs(age.total_seconds() - 10 * 86400) < 1


# ── Age dimension ───────────────────────────────────────────────────────────


def test_young_pod_is_warming_up(tmp_path):
    """The canonical case: a 1-day-old pod is NOT mature for a 14-day req."""
    _stamp_bringup(tmp_path, _NOW - timedelta(days=1))
    v = maturity_gate.evaluate(
        tmp_path, min_age_days=14.0, min_data_points=1, data_points=2, now=_NOW,
    )
    assert v.mature is False
    assert v.warming_up is True
    assert "day" in v.reason
    assert v.pod_age_days is not None and round(v.pod_age_days) == 1


def test_old_pod_with_data_is_mature(tmp_path):
    """An established pod with history scores normally (real content)."""
    _stamp_bringup(tmp_path, _NOW - timedelta(days=40))
    v = maturity_gate.evaluate(
        tmp_path, min_age_days=14.0, min_data_points=1, data_points=12, now=_NOW,
    )
    assert v.mature is True
    assert v.reason == ""


def test_age_boundary_is_inclusive(tmp_path):
    """Exactly at the threshold counts as mature (>=)."""
    _stamp_bringup(tmp_path, _NOW - timedelta(days=14))
    v = maturity_gate.evaluate(
        tmp_path, min_age_days=14.0, min_data_points=1, data_points=5, now=_NOW,
    )
    assert v.mature is True


# ── Data dimension ──────────────────────────────────────────────────────────


def test_old_pod_with_no_data_is_warming_up(tmp_path):
    """Age satisfied but zero accumulated data → still "insufficient data"."""
    _stamp_bringup(tmp_path, _NOW - timedelta(days=40))
    v = maturity_gate.evaluate(
        tmp_path, min_age_days=14.0, min_data_points=1, data_points=0, now=_NOW,
    )
    assert v.mature is False
    assert "data point" in v.reason


def test_both_dimensions_must_pass(tmp_path):
    """Young AND data-poor → warming up, reason names both shortfalls."""
    _stamp_bringup(tmp_path, _NOW - timedelta(days=2))
    v = maturity_gate.evaluate(
        tmp_path, min_age_days=14.0, min_data_points=3, data_points=0, now=_NOW,
    )
    assert v.mature is False
    assert "day" in v.reason and "data point" in v.reason


# ── Fail-open semantics ─────────────────────────────────────────────────────


def test_unknown_age_fails_open_on_age(tmp_path):
    """No markers → age unknown → age dimension satisfied; data still gates."""
    # data ok → mature
    assert maturity_gate.evaluate(
        tmp_path, min_age_days=14.0, min_data_points=1, data_points=5, now=_NOW,
    ).mature is True
    # data short → warming up purely on the data dimension
    v = maturity_gate.evaluate(
        tmp_path, min_age_days=14.0, min_data_points=1, data_points=0, now=_NOW,
    )
    assert v.mature is False
    assert "data point" in v.reason
    assert "day" not in v.reason  # age never contributed a shortfall


def test_declared_but_unsupplied_data_count_fails_open(tmp_path):
    """min_data_points set but data_points=None → data dimension satisfied."""
    _stamp_bringup(tmp_path, _NOW - timedelta(days=40))
    v = maturity_gate.evaluate(
        tmp_path, min_age_days=14.0, min_data_points=5, data_points=None, now=_NOW,
    )
    assert v.mature is True


def test_no_thresholds_is_always_mature(tmp_path):
    """A producer that declares nothing is never gated."""
    _stamp_bringup(tmp_path, _NOW - timedelta(hours=1))
    assert maturity_gate.evaluate(tmp_path, now=_NOW).mature is True


# ── Module defaults ─────────────────────────────────────────────────────────


def test_rsi_review_defaults_present():
    assert maturity_gate.RSI_REVIEW_MIN_AGE_DAYS == 14.0
    assert maturity_gate.RSI_REVIEW_MIN_DATA_POINTS == 1
