"""tests/test_cost_spike_generator.py — cost_spike factory + observe tests.

Mirrors cache_ttl_tuner's test layout: each Signal → Proposal factory
is exercised with a hand-rolled Signal-like fixture, then observe() is
exercised end-to-end against a tmp Signal store populated via the
real signals.store.observe API.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.cost_spike.observe import (  # noqa: E402
    CostSpikeContext,
    observe,
)
from generators.cost_spike.signal_proposals import (  # noqa: E402
    make_cost_spike_proposal,
)
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Signal fixture ────────────────────────────────────────────────────────────


def _cost_spike_signal(
    *,
    sig_id: str = "sig-cs-1",
    bot_id: str = "admin_bot",
    cur: float = 14.0,
    prior: float = 3.5,
    ratio: float = 4.0,
    multiplier: float = 2.0,
    floor: float = 5.0,
    window_days: int = 7,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=sig_id,
        bot_id=bot_id,
        type="cost_spike",
        severity="warn",
        details={
            "bot_id": bot_id,
            "window_days": window_days,
            "cost_cur_usd": cur,
            "cost_prior_usd": prior,
            "ratio": ratio,
            "multiplier_threshold": multiplier,
            "floor_usd": floor,
        },
    )


# ── make_cost_spike_proposal ──────────────────────────────────────────────────


def test_factory_produces_well_formed_proposal():
    sig = _cost_spike_signal()
    p = make_cost_spike_proposal(sig)
    assert p.bot_id == "admin_bot"
    assert p.generator_id == "cost_spike"
    assert p.dimension == "cost"
    assert p.motivating_signals == ["sig-cs-1"]
    assert p.trigger_observations == ["cost_spike:admin_bot"]
    assert p.action.kind == "Investigation"
    assert p.urgency == "improvement"
    assert p.approval_audience == "pod_operator"
    assert p.risk_tag.blast_radius == "bot"
    assert p.risk_tag.reversibility == "manual"
    assert p.risk_tag.touches == []
    assert p.claim is None
    # Phase C-2 operator-first title (2026-06-04). Was "Investigate
    # <bot> cost spike — $X this Nd vs $Y prior"; humanized to drop
    # the slugs and lead with the operator question.
    assert (
        p.admin_surface_summary == "Look at why admin_bot's spend just jumped"
    )
    assert len(p.admin_surface_summary) <= 120


def test_factory_records_diagnostics_in_provenance():
    sig = _cost_spike_signal(cur=20.0, prior=5.0, ratio=4.0)
    p = make_cost_spike_proposal(sig)
    sigs = p.provenance.signals
    assert sigs["cost_cur_usd"] == 20.0
    assert sigs["cost_prior_usd"] == 5.0
    assert sigs["ratio"] == 4.0


def test_factory_handles_dict_signal():
    """Signals can arrive as dicts (e.g. from tests or hot reload)."""
    sig = {
        "id": "sig-dict-1",
        "bot_id": "team_bot_c",
        "type": "cost_spike",
        "details": {
            "cost_cur_usd": 30.0,
            "cost_prior_usd": 10.0,
            "ratio": 3.0,
            "multiplier_threshold": 2.0,
            "floor_usd": 5.0,
            "window_days": 7,
        },
    }
    p = make_cost_spike_proposal(sig)
    assert p.bot_id == "team_bot_c"
    assert p.motivating_signals == ["sig-dict-1"]


# ── observe() end-to-end ──────────────────────────────────────────────────────


def _write_cost_spike_signal(
    shared_dir: Path,
    *,
    bot_id: str,
    cur: float = 14.0,
    prior: float = 3.5,
    ratio: float = 4.0,
) -> str:
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature("cost_watchdog", "cost_spike", bot_id),
        producer="cost_watchdog",
        type="cost_spike",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: cost spike",
        details={
            "bot_id": bot_id,
            "window_days": 7,
            "cost_cur_usd": cur,
            "cost_prior_usd": prior,
            "ratio": ratio,
            "multiplier_threshold": 2.0,
            "floor_usd": 5.0,
        },
    )
    return sig.id


def test_observe_emits_one_proposal_per_firing_signal(tmp_path):
    _write_cost_spike_signal(tmp_path, bot_id="admin_bot")
    proposals = observe(CostSpikeContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.bot_id == "admin_bot"
    assert p.generator_id == "cost_spike"
    assert p.motivating_signals  # cross-link populated


def test_observe_filters_by_bot_id(tmp_path):
    _write_cost_spike_signal(tmp_path, bot_id="admin_bot")
    _write_cost_spike_signal(tmp_path, bot_id="team_bot_c")
    proposals = observe(CostSpikeContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 1
    assert proposals[0].bot_id == "admin_bot"


def test_observe_returns_empty_when_no_signals(tmp_path):
    proposals = observe(CostSpikeContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


def test_observe_ignores_non_cost_spike_signals(tmp_path):
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
    proposals = observe(CostSpikeContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []
