"""tests/test_per_bot_gen_config.py — per-bot generator config overrides.

Verifies ``_resolve_gen_config`` flattens ``record.config["per_bot"]``
correctly and that the resulting dict is honored by the gateway and
efficiency context factories (the two non-budget generators that read
operator-tunable fields off ``gen_config``).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generator_runner import (  # noqa: E402
    _make_efficiency_hawk_ctx,
    _make_gateway_diagnostician_ctx,
    _resolve_gen_config,
)

_NOW = datetime(2026, 5, 8, tzinfo=timezone.utc)


# ── _resolve_gen_config ─────────────────────────────────────────────────────


def test_resolve_gen_config_flat_pass_through():
    cfg = {"window_days": 14, "min_incidents": 7}
    assert _resolve_gen_config(cfg, "team_bot_a") == cfg


def test_resolve_gen_config_per_bot_overrides_flat_field():
    cfg = {
        "window_days": 14,
        "per_bot": {"team_bot_a": {"window_days": 21}},
    }
    out = _resolve_gen_config(cfg, "team_bot_a")
    assert out == {"window_days": 21}


def test_resolve_gen_config_other_bot_unaffected():
    cfg = {
        "window_days": 14,
        "per_bot": {"team_bot_a": {"window_days": 21}},
    }
    assert _resolve_gen_config(cfg, "ellie") == {"window_days": 14}


def test_resolve_gen_config_shallow_merges_nested_dicts():
    cfg = {
        "cost": {"min_total_cost_usd": 1.0, "background_share_threshold": 0.7},
        "per_bot": {"team_bot_a": {"cost": {"background_share_threshold": 0.5}}},
    }
    out = _resolve_gen_config(cfg, "team_bot_a")
    # min_total_cost_usd preserved, background_share_threshold overridden.
    assert out["cost"] == {
        "min_total_cost_usd": 1.0,
        "background_share_threshold": 0.5,
    }


def test_resolve_gen_config_strips_per_bot_section_when_no_bot_id():
    cfg = {"window_days": 14, "per_bot": {"team_bot_a": {"window_days": 21}}}
    out = _resolve_gen_config(cfg, None)
    assert out == {"window_days": 14}


# ── Factories honor the resolved config ─────────────────────────────────────


def test_gateway_diagnostician_picks_up_resolved_overrides(tmp_path):
    network = {"bots": {"team_bot_a": {}}}
    record_config = {
        "window_days": 14,
        "per_bot": {"team_bot_a": {"window_days": 21, "min_incidents": 9}},
    }
    resolved = _resolve_gen_config(record_config, "team_bot_a")
    ctx = _make_gateway_diagnostician_ctx(
        shared_dir=tmp_path,
        network_config=network,
        bot_id="team_bot_a",
        gen_config=resolved,
        now=_NOW,
    )
    assert ctx.window_days == 21
    assert ctx.min_incidents == 9


def test_resolve_gen_config_handles_missing_per_bot_section():
    # Pre-existing flat configs (no per_bot key) must keep working
    # unchanged — this is the back-compat invariant.
    cfg = {"window_days": 14}
    assert _resolve_gen_config(cfg, "team_bot_a") == {"window_days": 14}


def test_resolve_gen_config_handles_empty_dict_and_non_dict():
    assert _resolve_gen_config({}, "team_bot_a") == {}
    assert _resolve_gen_config(None, "team_bot_a") == {}


def test_gateway_factory_falls_back_to_dataclass_default_when_unset(tmp_path):
    # No flat value, no per-bot override → context retains its dataclass
    # default. This is the "nothing configured anywhere" path the UI
    # surfaces as `pod_default`.
    ctx = _make_gateway_diagnostician_ctx(
        shared_dir=tmp_path,
        network_config={"bots": {"team_bot_a": {}}},
        bot_id="team_bot_a",
        gen_config={},
        now=_NOW,
    )
    assert ctx.window_days == 7  # GatewayDiagnosticianContext default


def test_efficiency_hawk_picks_up_per_bot_cost_overrides(tmp_path):
    network = {"bots": {"team_bot_a": {"role": "member"}}}
    record_config = {
        "cost": {"background_share_threshold": 0.7},
        "per_bot": {
            "team_bot_a": {"cost": {"background_share_threshold": 0.55}}
        },
    }
    resolved = _resolve_gen_config(record_config, "team_bot_a")
    ctx = _make_efficiency_hawk_ctx(
        shared_dir=tmp_path,
        network_config=network,
        bot_id="team_bot_a",
        gen_config=resolved,
        now=_NOW,
    )
    assert ctx.cost_overrides == {"background_share_threshold": 0.55}
