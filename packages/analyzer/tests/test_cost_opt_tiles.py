"""tests/test_cost_opt_tiles.py — per-bot tile assembly for Cost Optimization page.

Locks the contract for the Cost Optimization tile row:
  - grade letter mapping from numeric score
  - tier bucketing for model-mix bar (haiku → tier3, sonnet → tier2, etc.)
  - chip prioritization (critical > warn > info, with tail-truncation)
  - chip detectors (breaker_tripped, primary_off_floor, config_drift,
    cascade_live)
  - assembled tile shape

These are unit tests at the module level; the endpoint test is in
packages/admin/tests/test_cost_opt_tiles_endpoint.py.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import cost_opt_tiles as cot  # noqa: E402


# ── Grade letter mapping ────────────────────────────────────────────────────


@pytest.mark.parametrize("score,expected", [
    (100, "A"), (95, "A"), (90, "A"),
    (89, "B"), (85, "B"), (80, "B"),
    (79, "C"), (75, "C"), (70, "C"),
    (69, "D"), (65, "D"), (60, "D"),
    (59, "F"), (40, "F"), (0, "F"),
])
def test_grade_letter_thresholds(score, expected):
    assert cot._grade_letter(score) == expected


# ── Model → tier bucketing ─────────────────────────────────────────────────


@pytest.mark.parametrize("model,expected_tier", [
    # tier3 — cheap / floor
    ("anthropic/claude-haiku-4-5",         "tier3"),
    ("openai/gpt-4o-mini",                 "tier3"),
    ("google/gemini-2.0-flash",            "tier3"),
    ("xai/grok-4-mini",                    "tier3"),
    # tier2 — workhorse
    ("anthropic/claude-sonnet-4-6",        "tier2"),
    ("openai/gpt-4o",                      "tier2"),
    ("openai/gpt-4.1",                     "tier2"),
    ("google/gemini-2.5-pro",              "tier2"),
    ("xai/grok-4",                         "tier2"),
    # tier1 — power (opus-class)
    ("anthropic/claude-opus-4-7",          "tier1"),
    # premium — Fable-class (frontier) gets its OWN bucket (Phase 4 F1),
    # distinct from tier1, so 2× Fable spend is visible separately in the
    # mix tile. Must beat the broader anthropic/opus patterns and not fall
    # to "unknown" or collapse into tier1.
    ("anthropic/claude-fable-5",           "premium"),
    # Unrecognized → unknown (renders gray, not silently misattributed)
    ("some-other/model-name",              "unknown"),
    ("",                                   "unknown"),
])
def test_model_to_tier_buckets(model, expected_tier):
    assert cot._model_to_tier(model) == expected_tier


def test_model_to_tier_is_case_insensitive():
    """Substring match must work regardless of model-id case — some
    providers return uppercase model names in the cost_event records."""
    assert cot._model_to_tier("Anthropic/Claude-HAIKU-4-5") == "tier3"
    assert cot._model_to_tier("OPENAI/GPT-4O") == "tier2"


# ── Model-mix aggregation ──────────────────────────────────────────────────


def _write_rollup(shared_dir: Path, bot_id: str, d: date, by_model: dict):
    """Write a cost_rollup file for one (bot, date)."""
    p = shared_dir / "metrics" / bot_id / f"cost-{d.isoformat()}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "bot_id": bot_id, "date": d.isoformat(),
        "by_model": by_model,
    }))


def test_aggregate_mix_sums_across_window(tmp_path):
    """7d window aggregates each day's by_model into per-tier totals.
    Personal_bot-shape: dominantly Haiku with a sliver of Sonnet."""
    today = date(2026, 5, 28)
    for offset in range(7):
        _write_rollup(tmp_path, "personal_bot", today - timedelta(days=offset), {
            "anthropic/claude-haiku-4-5": {"event_count": 20, "cost_usd": 0.30},
            "anthropic/claude-sonnet-4-6": {"event_count": 2, "cost_usd": 0.10},
        })
    mix = cot.aggregate_model_mix(tmp_path, "personal_bot", days=7, today=today)
    assert mix["total_turns"] == 22 * 7
    assert pytest.approx(mix["total_cost"], abs=0.001) == (0.40 * 7)
    by_tier = {t["tier"]: t for t in mix["by_tier"]}
    assert by_tier["tier3"]["turns"] == 140
    assert by_tier["tier2"]["turns"] == 14
    assert pytest.approx(by_tier["tier3"]["turn_share"], abs=0.001) == 140 / 154
    # Dominant model surfaces for the tier — tooltip needs it
    assert by_tier["tier3"]["dominant_model"] == "anthropic/claude-haiku-4-5"


def test_aggregate_mix_empty_when_no_rollups(tmp_path):
    """No rollup files in the window → empty by_tier (not zero-counts).
    The UI renders this as a gray 'no data' bar."""
    mix = cot.aggregate_model_mix(tmp_path, "ghost", days=7,
                                  today=date(2026, 5, 28))
    assert mix["total_turns"] == 0
    assert mix["total_cost"] == 0.0
    assert mix["by_tier"] == []


def test_aggregate_mix_buckets_unknown_separately(tmp_path):
    """A model that doesn't match any tier family → 'unknown' bucket.
    Surfacing this honestly lets operators see when their cost is
    going to a model the registry doesn't know about (e.g. a custom
    fine-tune or a new provider release)."""
    today = date(2026, 5, 28)
    _write_rollup(tmp_path, "weird", today, {
        "anthropic/claude-haiku-4-5": {"event_count": 10, "cost_usd": 0.10},
        "experimental/foo-9000": {"event_count": 5, "cost_usd": 0.50},
    })
    mix = cot.aggregate_model_mix(tmp_path, "weird", days=1, today=today)
    by_tier = {t["tier"]: t for t in mix["by_tier"]}
    assert by_tier["unknown"]["turns"] == 5
    assert by_tier["unknown"]["cost"] == 0.50


def test_aggregate_mix_dominant_model_within_tier(tmp_path):
    """When multiple models in one tier, dominant = highest cost (not
    highest turn count). A handful of expensive turns drives the cost
    even when turn count is similar."""
    today = date(2026, 5, 28)
    _write_rollup(tmp_path, "admin_bot", today, {
        "anthropic/claude-sonnet-4-6": {"event_count": 10, "cost_usd": 5.00},
        "openai/gpt-4o": {"event_count": 12, "cost_usd": 2.00},
    })
    mix = cot.aggregate_model_mix(tmp_path, "admin_bot", days=1, today=today)
    by_tier = {t["tier"]: t for t in mix["by_tier"]}
    assert by_tier["tier2"]["dominant_model"] == "anthropic/claude-sonnet-4-6"


# ── Chip detectors ─────────────────────────────────────────────────────────


def test_cascade_chip_fires_when_enabled(tmp_path, monkeypatch):
    """Treatment-group bot — cascade.enabled=true in evolve-tiers.json.
    Chip shows up as informational green."""
    fake_home = tmp_path / "Users" / "admin_bot"
    oc_dir = fake_home / ".openclaw"
    oc_dir.mkdir(parents=True)
    (oc_dir / "evolve-tiers.json").write_text(json.dumps({
        "cascade": {"enabled": True},
    }))
    monkeypatch.setattr(
        cot, "Path",
        lambda p: oc_dir / "evolve-tiers.json"
        if "evolve-tiers.json" in str(p) else Path(p),
    )
    chip = cot.detect_cascade_chip(tmp_path, "admin_bot", "admin_bot")
    assert chip is not None
    assert chip["id"] == "cascade_live"
    assert chip["severity"] == "info"


def test_cascade_chip_silent_when_disabled(tmp_path, monkeypatch):
    """Control-group bot — cascade.enabled=false (or absent) returns None,
    so the chip row stays focused on real problems."""
    fake_home = tmp_path / "Users" / "personal_bot"
    oc_dir = fake_home / ".openclaw"
    oc_dir.mkdir(parents=True)
    (oc_dir / "evolve-tiers.json").write_text(json.dumps({
        "tiers": {"tier3": {"models": ["x"]}},
        # no cascade key
    }))
    monkeypatch.setattr(
        cot, "Path",
        lambda p: oc_dir / "evolve-tiers.json"
        if "evolve-tiers.json" in str(p) else Path(p),
    )
    assert cot.detect_cascade_chip(tmp_path, "personal_bot", "personal_bot") is None


def test_breaker_chip_fires_when_tripped_and_unexpired(tmp_path):
    """L1 cost breaker active right now → red chip."""
    breaker_path = tmp_path / "breakers" / "security_bot" / "cost.json"
    breaker_path.parent.mkdir(parents=True)
    breaker_path.write_text(json.dumps({
        "tripped_at": "2026-05-29T05:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }))
    chip = cot.detect_breaker_tripped_chip(tmp_path, "security_bot")
    assert chip is not None
    assert chip["id"] == "breaker_tripped"
    assert chip["severity"] == "critical"


def test_breaker_chip_fires_when_tripped_indefinite(tmp_path):
    """Breaker with no expires_at (indefinite) stays lit until reset."""
    breaker_path = tmp_path / "breakers" / "security_bot" / "cost.json"
    breaker_path.parent.mkdir(parents=True)
    breaker_path.write_text(json.dumps({
        "tripped_at": "2026-05-29T05:00:00Z",
    }))
    assert cot.detect_breaker_tripped_chip(tmp_path, "security_bot") is not None


def test_breaker_chip_silent_after_expiry(tmp_path):
    """Past-expires_at file means the TTL elapsed but the reaper hasn't
    unlinked it yet. Treat as cleared — chip drops."""
    breaker_path = tmp_path / "breakers" / "security_bot" / "cost.json"
    breaker_path.parent.mkdir(parents=True)
    breaker_path.write_text(json.dumps({
        "tripped_at": "2026-05-29T05:00:00Z",
        "expires_at": "2026-05-29T06:00:00Z",
    }))
    assert cot.detect_breaker_tripped_chip(tmp_path, "security_bot") is None


def test_breaker_chip_silent_when_no_file(tmp_path):
    """Most bots never tripped the breaker → no file → no chip."""
    assert cot.detect_breaker_tripped_chip(tmp_path, "team_bot_a") is None


# Retired 2026-06-04: test_primary_off_floor_chip_* tests removed
# alongside primary_model_floor_advisor and detect_primary_off_floor_chip.
# See docs/decision-retire-primary-model-floor-advisor-2026-06-04.md.


def test_detect_primary_off_floor_chip_is_gone():
    """The chip detector was retired 2026-06-04. Pin its absence so a
    future refactor doesn't quietly bring back the lower-primary
    surface that conflated background routing with human chat."""
    assert not hasattr(cot, "detect_primary_off_floor_chip"), (
        "detect_primary_off_floor_chip should be retired — the generator "
        "that fed it (primary_model_floor_advisor) was removed because "
        "its 'lower primary to the floor tier' framing collapsed the "
        "distinction between trigger-anchored background sessions and "
        "intentional Sonnet human-chat usage (the PR #1774 regression "
        "class). Operator-driven default-tier tuning now uses "
        "userTierOverride.defaultTier (Phase A)."
    )


def test_config_drift_chip_fires_on_firing_signal(tmp_path):
    """cost_watchdog's config_drift Signal currently firing → chip with
    the drifted dotpath in the detail."""
    firing = tmp_path / "signals" / "firing"
    firing.mkdir(parents=True)
    # Phase B: the chip reads through signals.store.iter_active, which only
    # yields records that deserialize, and matches on ``type`` (the real
    # cost_watchdog discriminator) — not a top-level ``kind`` field, which
    # the production signal never carried.
    (firing / "sig-789.json").write_text(json.dumps({
        "id": "sig-789", "state": "firing",
        "bot_id": "personal_bot",
        "producer": "cost_watchdog",
        "type": "config_drift",
        "signature": "cost_watchdog:config_drift:personal_bot",
        "flavor": "maintenance", "severity": "warn", "scope": "bot",
        "details": {"dotpath": "agents.defaults.model.primary"},
    }))
    chip = cot.detect_config_drift_chip(tmp_path, "personal_bot")
    assert chip is not None
    assert "agents.defaults.model.primary" in chip["detail"]


# ── Chip prioritization ─────────────────────────────────────────────────────


def test_prioritize_truncates_to_three_keeping_critical():
    """5 chips → 3 visible; critical wins first slot, warn fills the rest,
    info gets dropped.

    (Fixture previously used ``primary_off_floor`` for one of the warn
    slots; replaced with ``config_drift`` after the floor advisor was
    retired 2026-06-04 — see decision-retire-primary-model-floor-advisor.)
    """
    chips = [
        {"id": "cascade_live",    "severity": "info",     "label": "cascade"},
        {"id": "cost_spike",      "severity": "warn",     "label": "spike"},
        {"id": "breaker_tripped", "severity": "critical", "label": "breaker"},
        {"id": "config_drift",    "severity": "warn",     "label": "drift"},
        {"id": "bloat",           "severity": "warn",     "label": "bloat"},
    ]
    out = cot.prioritize_chips(chips, max_visible=3)
    ids = [c["id"] for c in out]
    assert ids[0] == "breaker_tripped"     # critical wins
    assert "cost_spike" in ids             # warn fills
    assert "config_drift" in ids           # warn fills (next by chip-id rank)
    assert "cascade_live" not in ids       # info drops when slots full


def test_prioritize_promotes_info_when_row_otherwise_empty():
    """No critical or warn chips, just cascade_live → cascade chip survives
    rather than leaving the row blank."""
    chips = [
        {"id": "cascade_live", "severity": "info", "label": "cascade"},
    ]
    out = cot.prioritize_chips(chips, max_visible=3)
    assert len(out) == 1
    assert out[0]["id"] == "cascade_live"


def test_prioritize_orders_warn_chips_by_actionability():
    """Among warn chips, cost_spike (acute) ranks above bloat (slow trend)
    — operator scanning the row sees acute issues first."""
    chips = [
        {"id": "bloat",       "severity": "warn", "label": "bloat"},
        {"id": "cost_spike",  "severity": "warn", "label": "spike"},
        {"id": "cache_low",   "severity": "warn", "label": "cache"},
    ]
    out = cot.prioritize_chips(chips, max_visible=3)
    assert [c["id"] for c in out] == ["cost_spike", "bloat", "cache_low"]


# ── Top-level tile assembly ────────────────────────────────────────────────


def test_build_tile_returns_grade_and_spend_shape(tmp_path):
    """End-to-end on a minimal fixture — tile carries the expected keys
    and the grade letter matches the score."""
    network = {"bots": {"admin_bot": {"role": "member", "user": "admin_bot"}}}
    tile = cot.build_tile(
        shared_dir=tmp_path,
        bot_id="admin_bot",
        bot_data={"role": "member"},
        network=network,
        today=date(2026, 5, 28),
        openclaw_settings={},
    )
    assert tile["bot_id"] == "admin_bot"
    assert tile["grade"] in {"A", "B", "C", "D", "F"}
    assert isinstance(tile["score"], int)
    assert "spend" in tile and "usd_28d" in tile["spend"]
    assert "model_mix" in tile and "by_tier" in tile["model_mix"]
    assert "chips" in tile and isinstance(tile["chips"], list)


def test_build_all_tiles_preserves_bot_order(tmp_path):
    """Caller controls bot order — tile row matches the requested
    sequence so treatment/control grouping in the UI stays stable."""
    network = {"bots": {
        "admin_bot": {"role": "member", "user": "admin_bot"},
        "personal_bot": {"role": "member", "user": "personal_bot"},
        "security_bot": {"role": "member", "user": "security_bot"},
    }}
    tiles = cot.build_all_tiles(
        tmp_path, ["security_bot", "admin_bot", "personal_bot"], network,
        today=date(2026, 5, 28),
    )
    assert [t["bot_id"] for t in tiles] == ["security_bot", "admin_bot", "personal_bot"]
