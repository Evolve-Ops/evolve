"""tests/test_weekly_review_maturity.py — RSI Weekly Review fresh-pod fixes.

Falsifiable proof artifacts for the reports-side of
docs/spec-transient-signal-suppression-2026-06-23.md (Mechanism 3):

  * a zero/low-history pod renders "warming up / insufficient data"
    (informational), NOT "🔴 At Risk", and empty 0%-confidence proposals are
    not surfaced under Decisions Needed;
  * a mature pod renders the real score unchanged;
  * the ISO-week sentinel makes a same-week re-run a no-op (one emission) while
    a genuinely new week emits again.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import weekly_review  # noqa: E402

_REVIEW_DATE = date(2026, 6, 23)


def _red_health() -> dict:
    """A health block that WOULD render the scary red verdict."""
    return {
        "score": 27, "label": "At Risk", "icon": "🔴",
        "components": {"velocity": 50, "approval": 0, "measurement": 0,
                       "freshness": 100},
        "oldest_pending_days": 0,
    }


# ── Warming-up rendering ────────────────────────────────────────────────────


def test_warming_up_hides_at_risk_verdict():
    data = {
        "health_score": _red_health(),
        "maturity": {"mature": False, "reason": "pod is 1 day old (< 14-day minimum)",
                     "pod_age_days": 1.0, "data_points": 2},
        "velocity": {"this_week": 2, "week_avg_4w": 0.0, "trend_pct": 0},
        "funnel": {"generated": 2, "approved": 0, "applied": 0, "measured": 0,
                   "approval_rate": 0, "apply_rate": 0, "measurement_rate": 0},
        "pending_proposals": [],
    }
    report = weekly_review.build_report(data, None, _REVIEW_DATE)
    assert "Warming up" in report
    assert "🌱" in report
    # The scary verdict must NOT appear.
    assert "At Risk" not in report
    assert "🔴" not in report
    assert "27/100" not in report


def test_mature_pod_renders_real_score():
    healthy = {
        "score": 88, "label": "Healthy", "icon": "🟢",
        "components": {"velocity": 90, "approval": 85, "measurement": 80,
                       "freshness": 100},
        "oldest_pending_days": 2,
    }
    data = {
        "health_score": healthy,
        "maturity": {"mature": True, "reason": "", "pod_age_days": 40.0,
                     "data_points": 12},
        "velocity": {"this_week": 4, "week_avg_4w": 3.0, "trend_pct": 33},
        "funnel": {"generated": 12, "approved": 8, "applied": 6, "measured": 5,
                   "approval_rate": 67, "apply_rate": 75, "measurement_rate": 83},
        "pending_proposals": [],
    }
    report = weekly_review.build_report(data, None, _REVIEW_DATE)
    assert "88/100 — Healthy" in report
    assert "🟢" in report
    assert "Warming up" not in report


def test_no_maturity_key_renders_normally():
    """Backward-compat: absent maturity (mature pods / old callers) → real score."""
    data = {"health_score": _red_health(), "velocity": {}, "funnel": {},
            "pending_proposals": []}
    report = weekly_review.build_report(data, None, _REVIEW_DATE)
    assert "27/100 — At Risk" in report


# ── Empty-proposal filter ───────────────────────────────────────────────────


def test_is_empty_proposal_predicate():
    assert weekly_review._is_empty_proposal({"description": "", "confidence": 0})
    assert weekly_review._is_empty_proposal({"confidence": 0.0})  # no desc at all
    assert weekly_review._is_empty_proposal({"description": "   ", "confidence": 0})
    # Real description OR real confidence → kept.
    assert not weekly_review._is_empty_proposal(
        {"description": "Raise context cap", "confidence": 0})
    assert not weekly_review._is_empty_proposal(
        {"pattern_key": "darwin:no_data", "description": "", "confidence": 0.85})


def test_empty_proposals_not_in_decisions_needed():
    """Filtered display list → empty placeholders never reach the section."""
    data = {
        "health_score": {},
        "pending_proposals": [],  # the empty ones were filtered upstream in main()
    }
    report = weekly_review.build_report(data, None, _REVIEW_DATE)
    assert "No description" not in report
    assert "0% confidence" not in report
    assert "No proposals pending" in report


# ── ISO-week idempotency sentinel ───────────────────────────────────────────


def test_sentinel_roundtrip(tmp_path):
    week = "2026-W26"
    assert weekly_review._already_sent_this_week(tmp_path, week) is False
    weekly_review._mark_sent_this_week(tmp_path, week)
    assert weekly_review._already_sent_this_week(tmp_path, week) is True
    # A different week is not considered sent.
    assert weekly_review._already_sent_this_week(tmp_path, "2026-W27") is False


def test_send_report_stamps_sentinel_on_sent(tmp_path, monkeypatch):
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts.dispatcher import DispatchOutcome, DispatchResult

    calls: list = []

    def fake_send(*, shared_dir, network, source, severity, message=None,
                  payload=None, dedup_key=None, catalog_event=None, **_kw):
        calls.append(dedup_key)
        return DispatchOutcome(
            result=DispatchResult.SENT, source=source, severity=severity,
            dedup_key=dedup_key, catalog_event=catalog_event,
            channel="telegram", chat_id="1",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)
    ok = weekly_review.send_report("body", {"alerts": {}}, shared_dir=tmp_path)
    assert ok is True
    iso_week = date.today().strftime("%G-W%V")
    assert weekly_review._already_sent_this_week(tmp_path, iso_week) is True


def _run_main(tmp_path, monkeypatch, calls, *, argv=("weekly_review",)):
    """Drive main() with stubbed config + send_report (which stamps the
    sentinel like the real one)."""
    monkeypatch.setattr(weekly_review, "load_config", lambda *_a, **_k: {})
    monkeypatch.setattr(weekly_review, "get_shared_dir", lambda *_a, **_k: tmp_path)

    def fake_send_report(report, config, *, shared_dir):
        calls.append(date.today().strftime("%G-W%V"))
        weekly_review._mark_sent_this_week(shared_dir, date.today().strftime("%G-W%V"))
        return True

    monkeypatch.setattr(weekly_review, "send_report", fake_send_report)
    monkeypatch.setattr(sys, "argv", list(argv))
    weekly_review.main()


def test_main_skips_second_same_week_run(tmp_path, monkeypatch):
    """Two runs in the same ISO-week → exactly one emission."""
    calls: list = []
    _run_main(tmp_path, monkeypatch, calls)
    assert len(calls) == 1  # first run emits
    _run_main(tmp_path, monkeypatch, calls)
    assert len(calls) == 1  # second run is a no-op (sentinel)


def test_main_emits_again_on_new_week(tmp_path, monkeypatch):
    """A genuinely new ISO-week emits again (stale sentinel ⇒ not skipped)."""
    calls: list = []
    _run_main(tmp_path, monkeypatch, calls)
    assert len(calls) == 1
    # Simulate last week's stamp still on disk.
    weekly_review._mark_sent_this_week(tmp_path, "2000-W01")
    _run_main(tmp_path, monkeypatch, calls)
    assert len(calls) == 2  # current week differs from stored → emits


def test_main_always_flag_bypasses_sentinel(tmp_path, monkeypatch):
    calls: list = []
    _run_main(tmp_path, monkeypatch, calls)
    assert len(calls) == 1
    _run_main(tmp_path, monkeypatch, calls, argv=("weekly_review", "--always"))
    assert len(calls) == 2  # --always re-emits despite the sentinel
