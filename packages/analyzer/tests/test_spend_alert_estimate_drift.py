"""The weekly receipt carries the estimate-vs-bill line, and drift is raised.

Every dollar on the receipt is an ESTIMATE. Until 2026-09-04 nothing said so
and nothing checked it, which is how a 3x overstatement rode the receipt, the
daily cap and the checkpoint message for months
(``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §1).

Two behaviours are pinned here: the receipt renders the comparison in BOTH
states (reconciled and not), and a ratio outside 0.9-1.1 raises the
``cost_estimate_drift`` Signal plus the ``cost.estimate_drift`` digest event
once per ISO week.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cost_reconcile as cr  # noqa: E402
import spend_alert  # noqa: E402
import turn_cost  # noqa: E402


TODAY = date(2026, 9, 7)          # a Monday
ISO_WEEK = TODAY.strftime("%G-W%V")


@pytest.fixture(autouse=True)
def _quiet_log(monkeypatch, tmp_path):
    """spend_alert._log appends to the REAL operator log file. Redirect it."""
    monkeypatch.setattr(spend_alert, "_LOG_FILE", tmp_path / "spend_alert.log")
    turn_cost.reset_pricing_catalog_cache()


@pytest.fixture
def dispatched(monkeypatch):
    """Capture _dispatch calls instead of messaging anyone."""
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(spend_alert, "_dispatch", _fake)
    return calls


@pytest.fixture
def observed(monkeypatch):
    """Capture signals.store.observe without writing the Signal store."""
    calls: list[dict] = []

    class _FakeStore:
        @staticmethod
        def observe(shared_dir, **kwargs):
            calls.append(kwargs)
            return type("Sig", (), {"id": "sig_test"})()

    import types
    fake_pkg = types.ModuleType("signals")
    fake_pkg.store = _FakeStore  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "signals", fake_pkg)
    monkeypatch.setitem(sys.modules, "signals.store", _FakeStore)  # type: ignore[arg-type]
    return calls


def _write_reconcile(shared_dir: Path, *, estimate: float, billed: float,
                     day: str = "2026-09-04") -> None:
    csv_path = shared_dir / "costs.csv"
    csv_path.write_text(f"date,model,cost\n{day},x,{billed}\n")
    from datetime import datetime, timezone
    turns = [{
        "ts": f"{day}T01:00:00Z",
        "model": "claude-opus-5", "provider": "anthropic",
        "input_tokens": 0, "output_tokens": 0,
        "cost": estimate, "cost_source": "provider",
    }]
    # Stamped with the real clock, and reconciling a FIXED day: the window is
    # on when the reconcile ran, so the day it read may be any age (F4).
    cr.write_reconcile(shared_dir, cr.reconcile(
        console_csv=csv_path, shared_dir=shared_dir, provider="anthropic",
        turns=turns, now=datetime.now(timezone.utc),
    ))


def test_receipt_carries_the_comparison_when_a_reconcile_exists(
    tmp_path, monkeypatch, dispatched,
):
    _write_reconcile(tmp_path, estimate=11.50, billed=11.08)
    monkeypatch.setattr(
        spend_alert, "_weekly_spend", lambda *_a, **_k: (11.5, {"bot_a": 11.5}),
    )
    spend_alert._maybe_send_weekly_summary(
        tmp_path, ["bot_a"], TODAY, 20.0, {},
    )
    summary = next(c for c in dispatched
                   if c["catalog_event"] == "cost.weekly_summary")
    breakdown = summary["payload"]["per_bot_breakdown"]
    assert "estimate vs provider bill: $11.50 vs $11.08" in breakdown
    assert "ratio 1.04" in breakdown


def test_receipt_says_not_reconciled_when_none_exists(
    tmp_path, monkeypatch, dispatched,
):
    monkeypatch.setattr(
        spend_alert, "_weekly_spend", lambda *_a, **_k: (11.5, {"bot_a": 11.5}),
    )
    spend_alert._maybe_send_weekly_summary(
        tmp_path, ["bot_a"], TODAY, 20.0, {},
    )
    summary = next(c for c in dispatched
                   if c["catalog_event"] == "cost.weekly_summary")
    assert (
        "estimate vs provider bill: not reconciled this week"
        in summary["payload"]["per_bot_breakdown"]
    )


def test_drift_outside_the_band_raises_signal_and_digest_event(
    tmp_path, dispatched, observed,
):
    _write_reconcile(tmp_path, estimate=32.09, billed=11.08)
    assert spend_alert.emit_estimate_drift(
        shared_dir=tmp_path, network={}, iso_week=ISO_WEEK,
    )
    event = next(c for c in dispatched
                 if c["catalog_event"] == "cost.estimate_drift")
    assert event["severity_name"] == "warning"
    assert event["dedup_key"] == f"spend_alert/estimate_drift/{ISO_WEEK}"
    assert event["payload"]["ratio"] == pytest.approx(2.8962, abs=1e-3)
    assert event["payload"]["estimate"] == pytest.approx(32.09)
    assert event["payload"]["console"] == pytest.approx(11.08)
    # …and the Alerts page carries the state, once per ISO week.
    signal = observed[0]
    assert signal["type"] == "cost_estimate_drift"
    assert signal["severity"] == "warn"
    assert signal["signature"].endswith(ISO_WEEK)
    assert signal["details"]["ratio"] == pytest.approx(2.8962, abs=1e-3)


def test_a_ratio_inside_the_band_raises_nothing(tmp_path, dispatched, observed):
    _write_reconcile(tmp_path, estimate=11.50, billed=11.08)
    assert not spend_alert.emit_estimate_drift(
        shared_dir=tmp_path, network={}, iso_week=ISO_WEEK,
    )
    assert dispatched == []
    assert observed == []


def test_no_reconcile_at_all_raises_nothing(tmp_path, dispatched, observed):
    assert not spend_alert.emit_estimate_drift(
        shared_dir=tmp_path, network={}, iso_week=ISO_WEEK,
    )
    assert dispatched == []
    assert observed == []
