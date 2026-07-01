"""tests/test_bloat_investigator.py — Phase 2 reference generator tests.

Two layers:
  1. Pure-Python attribution rules: feed a BloatEvidence, expect a
     specific cause_key.
  2. observe() end-to-end: fake the signals.store iter_active + the
     workspace size scan and assert a Proposal carrying the right
     root_cause_attribution block lands.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from generators.bloat_investigator.attribution import (  # noqa: E402
    BloatEvidence,
    attribute,
    rule_efficiency_drift_without_envelope,
    rule_growing_memory_drives_envelope,
    rule_static_bloat_drives_envelope,
)
from generators.bloat_investigator.observe import (  # noqa: E402
    BloatInvestigatorContext,
    observe,
)
from investigation.toolkit import CorrelatedSignal, FileSize  # noqa: E402


# ── attribution rules ────────────────────────────────────────────────────────


def _evidence(**overrides):
    base = dict(
        bot_id="security_bot",
        primary_signal_type="workspace_growth",
        primary_signal_signature="security_bot/memory/2026-05-02.md",
        context_bloat_files=[],
        growing_files=[],
        cache_envelope=None,
        efficiency_drift_tiers=[],
        top_files=[],
    )
    base.update(overrides)
    return BloatEvidence(**base)


def test_rule_growing_memory_fires_when_growth_plus_envelope():
    ev = _evidence(
        growing_files=[
            {"filename": "memory/2026-05-02.md",
             "growth_kb_per_day": 12.0,
             "current_kb": 196.0},
        ],
        cache_envelope={
            "ratio": 5.0,
            "cur_tokens_per_call": 25_000,
        },
    )
    result = rule_growing_memory_drives_envelope(ev)
    assert result is not None
    assert result.cause_key == "growing_memory_drives_envelope"
    assert result.primary_target == "memory/2026-05-02.md"
    assert result.confidence == 0.75  # no efficiency drift yet


def test_rule_growing_memory_higher_confidence_with_efficiency():
    ev = _evidence(
        growing_files=[
            {"filename": "memory/log.md",
             "growth_kb_per_day": 5.0,
             "current_kb": 100.0},
        ],
        cache_envelope={"ratio": 3.0, "cur_tokens_per_call": 20_000},
        efficiency_drift_tiers=[{"tier": "low", "ratio": 4.0}],
    )
    result = rule_growing_memory_drives_envelope(ev)
    assert result is not None
    assert result.confidence == 0.9
    assert result.evidence["efficiency_drift_present"] is True


def test_rule_growing_memory_picks_fastest_grower():
    ev = _evidence(
        growing_files=[
            {"filename": "a.md", "growth_kb_per_day": 2.0, "current_kb": 30},
            {"filename": "b.md", "growth_kb_per_day": 10.0, "current_kb": 80},
            {"filename": "c.md", "growth_kb_per_day": 5.0, "current_kb": 50},
        ],
        cache_envelope={"ratio": 3.0, "cur_tokens_per_call": 20_000},
    )
    result = rule_growing_memory_drives_envelope(ev)
    assert result.primary_target == "b.md"


def test_rule_growing_memory_silent_without_envelope():
    """No envelope signal → rule defers to a later rule (or ambiguous)."""
    ev = _evidence(
        growing_files=[
            {"filename": "a.md", "growth_kb_per_day": 5.0, "current_kb": 100},
        ],
    )
    assert rule_growing_memory_drives_envelope(ev) is None


def test_rule_static_bloat_fires_when_no_growth():
    ev = _evidence(
        context_bloat_files=[
            {"filename": "BIG.md", "size_kb": 200.0},
        ],
        cache_envelope={"ratio": 4.0, "cur_tokens_per_call": 30_000},
    )
    result = rule_static_bloat_drives_envelope(ev)
    assert result is not None
    assert result.cause_key == "static_bloat_drives_envelope"
    assert result.primary_target == "BIG.md"


def test_rule_static_bloat_defers_when_growth_present():
    """When workspace_growth is also firing, the growing rule should
    handle it — the static-bloat rule defers."""
    ev = _evidence(
        context_bloat_files=[{"filename": "BIG.md", "size_kb": 200.0}],
        growing_files=[
            {"filename": "BIG.md", "growth_kb_per_day": 5.0, "current_kb": 200},
        ],
        cache_envelope={"ratio": 4.0, "cur_tokens_per_call": 30_000},
    )
    assert rule_static_bloat_drives_envelope(ev) is None


def test_rule_efficiency_drift_without_envelope_fires():
    ev = _evidence(
        efficiency_drift_tiers=[
            {"tier": "high", "ratio": 3.0},
        ],
    )
    result = rule_efficiency_drift_without_envelope(ev)
    assert result is not None
    assert result.cause_key == "efficiency_drift_without_envelope"
    assert "envelope_stable" in result.evidence


def test_rule_efficiency_drift_defers_with_envelope():
    ev = _evidence(
        efficiency_drift_tiers=[{"tier": "low", "ratio": 4.0}],
        cache_envelope={"ratio": 3.0, "cur_tokens_per_call": 20_000},
    )
    assert rule_efficiency_drift_without_envelope(ev) is None


def test_attribute_falls_through_to_ambiguous():
    ev = _evidence(
        context_bloat_files=[
            {"filename": "BIG.md", "size_kb": 200.0},
        ],
        # No envelope signal, no growth signal, no drift signal.
        top_files=[("BIG.md", 200_000)],
    )
    result = attribute(ev)
    assert result.cause_key == "ambiguous"
    assert result.primary_target == "BIG.md"


def test_attribute_rule_order_growing_wins_over_static():
    """Both growing + static rules could match; growing fires first."""
    ev = _evidence(
        context_bloat_files=[{"filename": "x.md", "size_kb": 200.0}],
        growing_files=[
            {"filename": "x.md", "growth_kb_per_day": 5.0, "current_kb": 200},
        ],
        cache_envelope={"ratio": 3.0, "cur_tokens_per_call": 20_000},
    )
    result = attribute(ev)
    assert result.cause_key == "growing_memory_drives_envelope"


# ── observe() end-to-end ─────────────────────────────────────────────────────


def _csig(
    *,
    sig_id: str,
    sig_type: str,
    details: dict | None = None,
    signature: str = "",
) -> CorrelatedSignal:
    return CorrelatedSignal(
        signal_id=sig_id,
        type=sig_type,
        producer="cost_watchdog",
        severity="warn",
        title=f"{sig_type} fixture",
        signature=signature or f"security_bot/{sig_type}",
        details=details or {},
    )


def test_observe_emits_growing_memory_proposal(monkeypatch, tmp_path):
    """Security_bot shape: growth + envelope + efficiency drift all firing.
    Expect one Proposal with cause_key=growing_memory_drives_envelope."""
    fake = [
        _csig(
            sig_id="g1", sig_type="workspace_growth",
            details={
                "filename": "memory/2026-05-02.md",
                "growth_kb_per_day": 12.0,
                "current_kb": 196.0,
            },
        ),
        _csig(
            sig_id="g2", sig_type="cache_envelope_growth",
            details={"ratio": 5.0, "cur_tokens_per_call": 25_000},
        ),
        _csig(
            sig_id="g3", sig_type="efficiency_drift",
            details={"tier": "low", "ratio": 14.0},
        ),
    ]
    obs_mod = sys.modules["generators.bloat_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake)
    )
    monkeypatch.setattr(
        obs_mod, "file_top_contributors",
        lambda *a, **kw: [FileSize(path="memory/2026-05-02.md", size_bytes=196_000)],
    )

    ctx = BloatInvestigatorContext(bot_id="security_bot", shared_dir=tmp_path)
    out = observe(ctx)

    assert len(out) == 1
    p = out[0]
    rca = p.provenance.signals["root_cause_attribution"]
    assert rca["cause_key"] == "growing_memory_drives_envelope"
    assert rca["primary_target"] == "memory/2026-05-02.md"
    # Provenance carries the full evidence
    assert rca["evidence"]["growing_file_kb_per_day"] == 12.0
    assert rca["evidence"]["efficiency_drift_present"] is True
    # Motivating signals from all three firing types
    assert sorted(p.motivating_signals) == ["g1", "g2", "g3"]
    # Investigation-shape action
    assert p.action.kind == "Investigation"
    assert p.claim is None
    # Body includes the actionable fix line for this cause
    assert "memory/2026-05-02.md" in p.action.context


def test_observe_emits_ambiguous_when_only_context_bloat(monkeypatch, tmp_path):
    """Single signal type with no corroborating envelope = ambiguous;
    proposal still emitted so the operator gets evidence."""
    fake = [
        _csig(
            sig_id="g1", sig_type="context_bloat",
            details={"filename": "BIG.md", "size_kb": 200.0},
        ),
    ]
    obs_mod = sys.modules["generators.bloat_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake)
    )
    monkeypatch.setattr(
        obs_mod, "file_top_contributors",
        lambda *a, **kw: [FileSize(path="BIG.md", size_bytes=200_000)],
    )

    ctx = BloatInvestigatorContext(bot_id="personal_bot", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    rca = out[0].provenance.signals["root_cause_attribution"]
    assert rca["cause_key"] == "ambiguous"


def test_observe_silent_when_no_signals_firing(monkeypatch, tmp_path):
    obs_mod = sys.modules["generators.bloat_investigator.observe"]
    monkeypatch.setattr(obs_mod, "correlated_signals", lambda *a, **kw: [])
    ctx = BloatInvestigatorContext(bot_id="team_bot_a", shared_dir=tmp_path)
    assert observe(ctx) == []


def test_observe_suppresses_when_operator_declined_twice(monkeypatch, tmp_path):
    """If the operator has already dismissed this cause twice, the
    investigator goes silent — re-emitting the same proposal is noise."""
    fake = [
        _csig(
            sig_id="g1", sig_type="workspace_growth",
            details={
                "filename": "memory/log.md",
                "growth_kb_per_day": 12.0,
                "current_kb": 196.0,
            },
        ),
        _csig(
            sig_id="g2", sig_type="cache_envelope_growth",
            details={"ratio": 5.0, "cur_tokens_per_call": 25_000},
        ),
    ]
    obs_mod = sys.modules["generators.bloat_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake)
    )
    monkeypatch.setattr(
        obs_mod, "file_top_contributors", lambda *a, **kw: [],
    )
    # Operator declined this cause twice on this bot before.
    monkeypatch.setattr(
        obs_mod, "operator_already_declined",
        lambda *a, **kw: True,
    )
    ctx = BloatInvestigatorContext(bot_id="security_bot", shared_dir=tmp_path)
    assert observe(ctx) == []


def test_observe_renders_history_summary_in_body(monkeypatch, tmp_path):
    """When prior proposals exist (declined or approved), the body shows
    the recurrence context — operator sees they've seen this before."""
    fake = [
        _csig(
            sig_id="g1", sig_type="workspace_growth",
            details={
                "filename": "memory/log.md",
                "growth_kb_per_day": 12.0,
                "current_kb": 196.0,
            },
        ),
        _csig(
            sig_id="g2", sig_type="cache_envelope_growth",
            details={"ratio": 5.0, "cur_tokens_per_call": 25_000},
        ),
    ]
    obs_mod = sys.modules["generators.bloat_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake)
    )
    monkeypatch.setattr(
        obs_mod, "file_top_contributors", lambda *a, **kw: [],
    )
    monkeypatch.setattr(
        obs_mod, "operator_already_declined", lambda *a, **kw: False,
    )

    from investigation.proposal_history import ProposalHistoryEntry
    fake_entries = [
        ProposalHistoryEntry(
            proposal_id="p1", bot_id="security_bot",
            generator_id="bloat_investigator",
            status="dismissed", cause_key="growing_memory_drives_envelope",
            created_at="2026-05-15T12:00:00+00:00",
            motivating_signal_signatures=[],
        ),
    ]
    monkeypatch.setattr(
        obs_mod, "proposal_history", lambda *a, **kw: fake_entries,
    )

    ctx = BloatInvestigatorContext(bot_id="security_bot", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    # Provenance carries the rolled-up history summary
    history_block = out[0].provenance.signals.get("history")
    assert history_block is not None
    assert history_block["total"] == 1
    assert history_block["declined"] == 1
    # Body mentions prior proposals
    assert "Prior proposals" in out[0].action.context


def test_observe_one_proposal_per_bot_even_with_many_signals(
    monkeypatch, tmp_path
):
    """Multiple signals on the same bot → one investigation, not N."""
    fake = [
        _csig(sig_id=f"g{i}", sig_type="workspace_growth",
              details={"filename": f"f{i}.md",
                       "growth_kb_per_day": 5.0 + i,
                       "current_kb": 100.0 + i})
        for i in range(3)
    ] + [
        _csig(sig_id="env", sig_type="cache_envelope_growth",
              details={"ratio": 3.0, "cur_tokens_per_call": 20_000}),
    ]
    obs_mod = sys.modules["generators.bloat_investigator.observe"]
    monkeypatch.setattr(obs_mod, "correlated_signals", lambda *a, **kw: list(fake))
    monkeypatch.setattr(obs_mod, "file_top_contributors", lambda *a, **kw: [])

    ctx = BloatInvestigatorContext(bot_id="security_bot", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    # Picks the fastest-growing as primary target (f2.md has growth=7)
    rca = out[0].provenance.signals["root_cause_attribution"]
    assert rca["primary_target"] == "f2.md"
