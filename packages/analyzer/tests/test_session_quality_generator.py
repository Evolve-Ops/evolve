"""tests/test_session_quality_generator.py — session_quality factory + observe tests.

Mirrors cost_spike / cache_ttl_tuner layout: each Signal → Proposal
factory is exercised with a hand-rolled Signal-like fixture, then
observe() is exercised end-to-end against a tmp Signal store.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.session_quality.observe import (  # noqa: E402
    SessionQualityContext,
    observe,
)
from generators.session_quality.signal_proposals import (  # noqa: E402
    make_session_quality_proposal,
)
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Signal fixture ────────────────────────────────────────────────────────────


def _session_quality_signal(
    *,
    sig_id: str = "sig-sq-1",
    bot_id: str = "admin_bot",
    avg: float = 0.65,
    threshold: float = 0.50,
    window_days: int = 7,
    qualifying_days: int = 7,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=sig_id,
        bot_id=bot_id,
        type="session_quality",
        severity="warn",
        details={
            "bot_id": bot_id,
            "window_days": window_days,
            "maintenance_ratio_avg": avg,
            "threshold": threshold,
            "qualifying_days": qualifying_days,
        },
    )


# ── make_session_quality_proposal ─────────────────────────────────────────────


def test_factory_produces_well_formed_proposal():
    sig = _session_quality_signal()
    p = make_session_quality_proposal(sig)
    assert p.bot_id == "admin_bot"
    assert p.generator_id == "session_quality"
    assert p.dimension == "cost"
    assert p.motivating_signals == ["sig-sq-1"]
    assert p.trigger_observations == ["session_quality:admin_bot"]
    assert p.action.kind == "Investigation"
    assert p.urgency == "improvement"
    assert p.approval_audience == "pod_operator"
    assert p.risk_tag.blast_radius == "bot"
    assert p.risk_tag.reversibility == "manual"
    assert p.risk_tag.touches == []
    assert p.claim is None
    # Phase C-11 humanized title.
    assert (
        p.admin_surface_summary
        == "admin_bot is mostly doing maintenance instead of work"
    )
    assert len(p.admin_surface_summary) <= 120


def test_factory_records_diagnostics_in_provenance():
    sig = _session_quality_signal(avg=0.80, threshold=0.50, qualifying_days=5)
    p = make_session_quality_proposal(sig)
    sigs = p.provenance.signals
    assert sigs["maintenance_ratio_avg"] == 0.80
    assert sigs["threshold"] == 0.50
    assert sigs["qualifying_days"] == 5


def test_factory_handles_dict_signal():
    sig = {
        "id": "sig-dict-1",
        "bot_id": "team_bot_c",
        "type": "session_quality",
        "details": {
            "maintenance_ratio_avg": 0.70,
            "threshold": 0.50,
            "window_days": 7,
            "qualifying_days": 6,
        },
    }
    p = make_session_quality_proposal(sig)
    assert p.bot_id == "team_bot_c"
    assert p.motivating_signals == ["sig-dict-1"]


# ── observe() end-to-end ──────────────────────────────────────────────────────


def _write_session_quality_signal(
    shared_dir: Path,
    *,
    bot_id: str,
    avg: float = 0.65,
) -> str:
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature("cost_watchdog", "session_quality", bot_id),
        producer="cost_watchdog",
        type="session_quality",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: session quality",
        details={
            "bot_id": bot_id,
            "window_days": 7,
            "maintenance_ratio_avg": avg,
            "threshold": 0.50,
            "qualifying_days": 7,
        },
    )
    return sig.id


def test_observe_emits_one_proposal_per_firing_signal(tmp_path):
    _write_session_quality_signal(tmp_path, bot_id="admin_bot")
    proposals = observe(SessionQualityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.bot_id == "admin_bot"
    assert p.generator_id == "session_quality"
    assert p.motivating_signals  # cross-link populated


def test_observe_filters_by_bot_id(tmp_path):
    _write_session_quality_signal(tmp_path, bot_id="admin_bot")
    _write_session_quality_signal(tmp_path, bot_id="team_bot_c")
    proposals = observe(SessionQualityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 1
    assert proposals[0].bot_id == "admin_bot"


def test_observe_returns_empty_when_no_signals(tmp_path):
    proposals = observe(SessionQualityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


def test_observe_ignores_non_session_quality_signals(tmp_path):
    """Other cost_watchdog signal types must not be consumed."""
    signals_store.observe(
        tmp_path,
        signature=make_signature("cost_watchdog", "daily_spend_high", "admin_bot"),
        producer="cost_watchdog",
        type="daily_spend_high",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="admin_bot: daily spend high",
        details={"cost_usd": 5.0},
    )
    proposals = observe(SessionQualityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []
