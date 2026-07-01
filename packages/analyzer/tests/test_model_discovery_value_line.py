"""tests/test_model_discovery_value_line.py — pod-grounded value line (Bite 3).

Design: docs/design-recommendation-legibility-2026-06-12.md §"decision-grounding".

The value line joins a discovered model × this pod's observed tier usage ×
model pricing into ONE terse, CITED line. These tests pin the load-bearing
invariant — **cite-or-don't**: a price/savings number appears ONLY when both a
real cited price and real pod usage are present; an unpriced model says "can't
price yet", never a fabricated %.

All pricing + usage is injected — zero HTTP, zero live config, deterministic
clock. Bot ids are role placeholders (scrub guard); model names are real so the
family-map / pricing lookups exercise the real data.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.model_discovery.value_line import (  # noqa: E402
    ValueLine,
    compute_value_line,
    read_tier_usage,
)
from generators.model_discovery.observe import (  # noqa: E402
    ModelDiscoveryContext,
    _make_discovery_proposal,
)
from schema.proposal import AgentsAppend, Proposal, RiskTag  # noqa: E402
from schema.provenance import Provenance  # noqa: E402

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)

# Three priced anchors → an anchor-relative band scale. Geometric-midpoint
# boundaries: low|medium at sqrt(0.25*3)=~0.87/MTok, medium|high at
# sqrt(3*15)=~6.7/MTok. So <=0.87 low, <=6.7 medium, else high.
_ANCHORS = [
    {"provider": "anthropic", "model_id": "claude-haiku-4-5",
     "input_cost_per_token": 0.25e-6, "output_cost_per_token": 1.25e-6},
    {"provider": "anthropic", "model_id": "claude-sonnet-4-6",
     "input_cost_per_token": 3e-6, "output_cost_per_token": 15e-6},
    {"provider": "anthropic", "model_id": "claude-opus-4-8",
     "input_cost_per_token": 15e-6, "output_cost_per_token": 75e-6},
]

_CATALOG = {
    "rungs": [
        {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"],
         "costClass": "low"},
        {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"],
         "costClass": "medium"},
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"],
         "costClass": "high"},
    ],
}


def _cache(*extra: dict) -> dict:
    """Pricing cache = the three anchors + any per-test extra priced models."""
    return {"models": [*_ANCHORS, *extra]}


def _write_usage(shared_dir: Path, rows, *, now: datetime = NOW,
                 days_ago: int = 1) -> None:
    """Append tier-usage records. ``rows`` = list of
    (bot_id, role, qualified_model, count). All records land ``days_ago`` days
    before ``now`` (inside the default 7d window)."""
    ts = (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    date = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    for bot_id, role, model, count in rows:
        d = shared_dir / "cost" / "tier-usage" / bot_id
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"{date}.jsonl").open("a") as f:
            for _ in range(count):
                f.write(json.dumps({
                    "ts": ts, "tier": role, "model": model,
                    "context": "user-requested", "bot_id": bot_id,
                }) + "\n")


# ── Case 1: full cited price delta ────────────────────────────────────────────

def test_priced_cheaper(tmp_path):
    """Priced discovered model + same-band incumbent with usage → cited
    '~X% cheaper' delta, both prices shown, priced=True."""
    _write_usage(tmp_path, [("bot-alpha", "power", "anthropic/claude-opus-4-8", 10)])
    cache = _cache({"provider": "anthropic", "model_id": "claude-opus-4-9",
                    "input_cost_per_token": 12e-6, "output_cost_per_token": 60e-6})

    vl = compute_value_line(
        "anthropic", "claude-opus-4-9", shared_dir=tmp_path,
        pricing_cache=cache, catalog=_CATALOG, now=NOW,
    )
    assert isinstance(vl, ValueLine)
    assert vl.priced is True
    # 12 vs 15 → 20% cheaper, grounded in the 10 power-role calls on opus-4-8.
    assert "power" in vl.terse
    assert "10 calls" in vl.terse
    assert "claude-opus-4-8" in vl.terse
    assert "~20% cheaper" in vl.terse
    assert "$12.00/MTok" in vl.terse and "$15.00/MTok" in vl.terse


def test_priced_pricier(tmp_path):
    _write_usage(tmp_path, [("bot-alpha", "power", "anthropic/claude-opus-4-8", 4)])
    cache = _cache({"provider": "anthropic", "model_id": "claude-opus-4-9",
                    "input_cost_per_token": 20e-6, "output_cost_per_token": 90e-6})

    vl = compute_value_line(
        "anthropic", "claude-opus-4-9", shared_dir=tmp_path,
        pricing_cache=cache, catalog=_CATALOG, now=NOW,
    )
    assert vl is not None and vl.priced is True
    # 20 vs 15 → ~33% pricier.
    assert "~33% pricier" in vl.terse
    assert "4 calls" in vl.terse


def test_priced_comparable(tmp_path):
    """Within ±10% reads as 'about the same price', still priced=True."""
    _write_usage(tmp_path, [("bot-alpha", "power", "anthropic/claude-opus-4-8", 7)])
    cache = _cache({"provider": "anthropic", "model_id": "claude-opus-4-9",
                    "input_cost_per_token": 15.5e-6, "output_cost_per_token": 77e-6})

    vl = compute_value_line(
        "anthropic", "claude-opus-4-9", shared_dir=tmp_path,
        pricing_cache=cache, catalog=_CATALOG, now=NOW,
    )
    assert vl is not None and vl.priced is True
    assert "about the same price" in vl.terse
    assert "cheaper" not in vl.terse and "pricier" not in vl.terse


# ── Case 2: cite-or-don't — unpriced model says so, never a fake % ────────────

def test_unpriced_model_cannot_price(tmp_path):
    """xAI/grok is unpriced today. The line MUST surface usage honestly and say
    'can't price' — NEVER a fabricated savings %."""
    _write_usage(tmp_path, [("bot-beta", "standard", "anthropic/claude-sonnet-4-6", 8)])
    # grok-4 is absent from the pricing cache → unpriced. Its band comes from
    # the family map (grok → medium), which matches sonnet's medium band.
    cache = _cache()

    vl = compute_value_line(
        "xai", "grok-4", shared_dir=tmp_path,
        pricing_cache=cache, catalog=_CATALOG, now=NOW, provider_display="xAI",
    )
    assert vl is not None
    assert vl.priced is False
    assert "can't price" in vl.terse
    assert "xAI" in vl.terse
    # The load-bearing invariant: no fabricated price/percentage anywhere.
    assert "%" not in vl.terse
    assert "/MTok" not in vl.terse
    assert "cheaper" not in vl.terse and "pricier" not in vl.terse
    # Usage is still grounded.
    assert "standard" in vl.terse and "8 calls" in vl.terse


def test_incumbent_unpriced_cannot_price(tmp_path):
    """Discovered model IS priced, but the same-band incumbent the pod runs is
    not in any pricing catalog → no delta can be cited → 'can't price'."""
    # opus-4-7 is unpriced (not in cache) but family-maps to high — same band as
    # the priced discovered opus-4-9.
    _write_usage(tmp_path, [("bot-alpha", "power", "anthropic/claude-opus-4-7", 5)])
    cache = _cache({"provider": "anthropic", "model_id": "claude-opus-4-9",
                    "input_cost_per_token": 12e-6, "output_cost_per_token": 60e-6})

    vl = compute_value_line(
        "anthropic", "claude-opus-4-9", shared_dir=tmp_path,
        pricing_cache=cache, catalog=_CATALOG, now=NOW,
    )
    assert vl is not None and vl.priced is False
    assert "can't price" in vl.terse
    assert "%" not in vl.terse
    assert "claude-opus-4-7" in vl.terse


# ── Case 3: priced, but no same-band usage in the pod ─────────────────────────

def test_priced_but_no_same_band_usage(tmp_path):
    """Discovered model is priced (high band) but the pod ran only medium-band
    models → cite the price, say there's no current spend to compare."""
    _write_usage(tmp_path, [("bot-beta", "standard", "anthropic/claude-sonnet-4-6", 6)])
    cache = _cache({"provider": "anthropic", "model_id": "claude-opus-4-9",
                    "input_cost_per_token": 12e-6, "output_cost_per_token": 60e-6})

    vl = compute_value_line(
        "anthropic", "claude-opus-4-9", shared_dir=tmp_path,
        pricing_cache=cache, catalog=_CATALOG, now=NOW,
    )
    assert vl is not None and vl.priced is True
    assert "$12.00/MTok" in vl.terse
    assert "high" in vl.terse  # names the empty band
    assert "no bot here ran" in vl.terse


# ── Case 4: nothing to ground honestly → None ─────────────────────────────────

def test_no_usage_returns_none(tmp_path):
    """No tier-usage at all → no pod grounding → return None (the card shows no
    value line rather than a bare, ungrounded price)."""
    cache = _cache({"provider": "anthropic", "model_id": "claude-opus-4-9",
                    "input_cost_per_token": 12e-6, "output_cost_per_token": 60e-6})

    vl = compute_value_line(
        "anthropic", "claude-opus-4-9", shared_dir=tmp_path,
        pricing_cache=cache, catalog=_CATALOG, now=NOW,
    )
    assert vl is None


def test_unpriced_and_no_usage_returns_none(tmp_path):
    """Unpriced model AND no usage → nothing honest to say → None."""
    vl = compute_value_line(
        "xai", "grok-4", shared_dir=tmp_path,
        pricing_cache=_cache(), catalog=_CATALOG, now=NOW, provider_display="xAI",
    )
    assert vl is None


# ── read_tier_usage windowing ─────────────────────────────────────────────────

def test_read_tier_usage_within_window(tmp_path):
    _write_usage(tmp_path, [
        ("bot-alpha", "power", "anthropic/claude-opus-4-8", 3),
        ("bot-beta", "standard", "anthropic/claude-sonnet-4-6", 2),
    ])
    usage = read_tier_usage(tmp_path, now=NOW, window_days=7)
    assert usage[("power", "anthropic/claude-opus-4-8")] == 3
    assert usage[("standard", "anthropic/claude-sonnet-4-6")] == 2


def test_read_tier_usage_excludes_out_of_window_by_filename(tmp_path):
    """A record 30 days ago (its own file, outside the candidate-date set) is
    never counted."""
    _write_usage(tmp_path, [("bot-alpha", "power", "anthropic/claude-opus-4-8", 9)],
                 days_ago=30)
    usage = read_tier_usage(tmp_path, now=NOW, window_days=7)
    assert sum(usage.values()) == 0


def test_read_tier_usage_excludes_old_record_in_recent_file(tmp_path):
    """The ts filter excludes an old record even if it sits in a recent-named
    file — the boundary is the record ts, not the filename."""
    d = tmp_path / "cost" / "tier-usage" / "bot-alpha"
    d.mkdir(parents=True, exist_ok=True)
    recent_date = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    old_ts = (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_ts = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / f"{recent_date}.jsonl").write_text(
        json.dumps({"ts": old_ts, "tier": "power",
                    "model": "anthropic/claude-opus-4-8", "bot_id": "bot-alpha"}) + "\n"
        + json.dumps({"ts": fresh_ts, "tier": "power",
                      "model": "anthropic/claude-opus-4-8", "bot_id": "bot-alpha"}) + "\n"
    )
    usage = read_tier_usage(tmp_path, now=NOW, window_days=7)
    assert usage[("power", "anthropic/claude-opus-4-8")] == 1


def test_read_tier_usage_missing_dir_is_empty(tmp_path):
    assert read_tier_usage(tmp_path, now=NOW) == {}


def test_read_tier_usage_tolerates_bad_lines(tmp_path):
    d = tmp_path / "cost" / "tier-usage" / "bot-alpha"
    d.mkdir(parents=True, exist_ok=True)
    date = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    ts = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / f"{date}.jsonl").write_text(
        "not json\n"
        + json.dumps({"ts": ts, "tier": "power",
                      "model": "anthropic/claude-opus-4-8", "bot_id": "bot-alpha"}) + "\n"
        + "{bad\n"
    )
    usage = read_tier_usage(tmp_path, now=NOW)
    assert usage[("power", "anthropic/claude-opus-4-8")] == 1


# ── schema: the value_line field round-trips and is backward-compatible ───────

def _new_proposal(**overrides) -> Proposal:
    base = dict(
        id="p-1",
        bot_id="team-bot-a",
        generator_id="model_discovery",
        dimension="substrate_health",
        trigger_observations=["t"],
        provenance=Provenance(technique="x"),
        problem="problem",
        action=AgentsAppend(bot_id="team-bot-a", section="X", content="y"),
        risk_tag=RiskTag(blast_radius="pod", reversibility="manual", touches=[]),
    )
    base.update(overrides)
    return Proposal(**base)


def test_value_line_defaults_to_none():
    assert _new_proposal().value_line is None


def test_value_line_roundtrips():
    p = _new_proposal(value_line="your `power` role ran 10 calls; ~20% cheaper")
    restored = Proposal.from_dict(p.to_dict())
    assert restored.value_line == "your `power` role ran 10 calls; ~20% cheaper"


def test_value_line_serialized_even_when_none():
    """Always present as a key so the admin server branches on presence without
    a defensive null check (mirrors human_title / surface)."""
    assert "value_line" in _new_proposal().to_dict()


def test_value_line_absent_in_payload_loads_as_none():
    """A pre-2026-06-12 proposal on disk has no value_line key → loads None."""
    data = _new_proposal().to_dict()
    del data["value_line"]
    assert Proposal.from_dict(data).value_line is None


# ── wiring: observe.py attaches the value line to the AdoptModel proposal ──────

class _FakeSignal:
    """Minimal firing model_discovery Signal for _make_discovery_proposal."""

    def __init__(self, details: dict):
        self.id = "sig-1"
        self.body = "heuristic rationale"
        self.type = "model_discovery"
        self.details = details


def _discovery_details(provider: str, model_id: str, cost_class: str) -> dict:
    return {
        "provider": provider,
        "model_id": model_id,
        "qualified_id": f"{provider}/{model_id}",
        "suggested_rung": "a new high rung",
        "suggested_rung_slug": "opus-class",
        "suggested_cost_class": cost_class,
        "suggested_position": 0,
        "suggested_rationale": "frontier line",
        "cost_band_source": "pricing",
        "cost_band_evidence": {"input_cost_per_token": 12e-6},
        "evidence": {"context_window": 200000},
    }


def test_observe_attaches_value_line(tmp_path):
    """End-to-end: a discovered priced model with same-band pod usage yields a
    Proposal whose value_line is the cited delta, woven into the body too."""
    # Pricing cache on disk (compute_value_line reads {shared}/model-pricing.json).
    (tmp_path / "model-pricing.json").write_text(json.dumps(_cache(
        {"provider": "anthropic", "model_id": "claude-opus-4-9",
         "input_cost_per_token": 12e-6, "output_cost_per_token": 60e-6})))
    _write_usage(tmp_path, [("bot-alpha", "power", "anthropic/claude-opus-4-8", 10)])

    ctx = ModelDiscoveryContext(
        bot_id=None, shared_dir=tmp_path,
        network={"models": {"rungs": _CATALOG["rungs"]}}, now=NOW,
    )
    prop = _make_discovery_proposal(
        ctx, _FakeSignal(_discovery_details("anthropic", "claude-opus-4-9", "high")),
    )
    assert prop is not None
    # The terse line is on the card field; the derivation is woven into the body.
    assert prop.value_line is not None
    assert "power" in prop.value_line and "cheaper" in prop.value_line
    assert "claude-opus-4-8" in prop.value_line
    assert "Pod-grounded value" in prop.problem


def test_observe_unpriced_value_line_is_honest(tmp_path):
    """An unpriced provider (xAI) with same-band usage gets a 'can't price'
    value line — never a fabricated %."""
    (tmp_path / "model-pricing.json").write_text(json.dumps(_cache()))
    _write_usage(tmp_path, [("bot-beta", "standard", "anthropic/claude-sonnet-4-6", 8)])

    ctx = ModelDiscoveryContext(
        bot_id=None, shared_dir=tmp_path,
        network={"models": {"rungs": _CATALOG["rungs"]}}, now=NOW,
    )
    prop = _make_discovery_proposal(
        ctx, _FakeSignal(_discovery_details("xai", "grok-4", "medium")),
    )
    assert prop is not None
    assert prop.value_line is not None
    assert "can't price" in prop.value_line
    assert "%" not in prop.value_line


def test_observe_no_usage_leaves_value_line_none(tmp_path):
    """No pod usage → no honest grounding → value_line stays None, proposal
    still emitted (model_discovery surfaces the model regardless)."""
    (tmp_path / "model-pricing.json").write_text(json.dumps(_cache(
        {"provider": "anthropic", "model_id": "claude-opus-4-9",
         "input_cost_per_token": 12e-6, "output_cost_per_token": 60e-6})))

    ctx = ModelDiscoveryContext(
        bot_id=None, shared_dir=tmp_path,
        network={"models": {"rungs": _CATALOG["rungs"]}}, now=NOW,
    )
    prop = _make_discovery_proposal(
        ctx, _FakeSignal(_discovery_details("anthropic", "claude-opus-4-9", "high")),
    )
    assert prop is not None
    assert prop.value_line is None
