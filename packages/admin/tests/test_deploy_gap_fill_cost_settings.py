"""Tests for ``deploy.gap_fill_cost_settings``.

Pin the heartbeat-subfield gap-fill that landed after the 2026-06-04 cost-cap
incident. The detector retirement that lives alongside (``cost_watchdog.
detect_heartbeat_no_model_override``) has its own retired-stub tests in
``packages/analyzer/tests/test_cost_watchdog.py``.
"""

from __future__ import annotations

from evolve_admin.deploy import (
    _BALANCED_COST_DEFAULTS,
    _BALANCED_HEARTBEAT_DEFAULTS,
    gap_fill_cost_settings,
)


def _bare_cfg() -> dict:
    return {}


def test_fresh_install_gets_balanced_heartbeat_defaults_with_2h_cadence_and_no_model() -> None:
    cfg = _bare_cfg()
    changed = gap_fill_cost_settings(cfg, snapshot=None)
    assert changed is True
    hb = cfg["agents"]["defaults"]["heartbeat"]
    assert hb["every"] == "2h"
    assert hb["isolatedSession"] is True
    assert hb["lightContext"] is True
    # ModelRouter is authoritative — no literal `model` field is written.
    assert "model" not in hb


def test_heartbeat_block_missing_every_gets_2h_default_subfield_fill() -> None:
    """The 2026-06-04 security-bot shape: heartbeat block exists, `every` absent —
    bot stays on OC's 30-min default until subfield gap-fill catches it."""
    cfg = {
        "agents": {
            "defaults": {
                "heartbeat": {
                    "isolatedSession": True,
                    "lightContext": True,
                    "model": "anthropic/claude-sonnet-4-6",
                }
            }
        }
    }
    changed = gap_fill_cost_settings(cfg, snapshot=None)
    assert changed is True
    hb = cfg["agents"]["defaults"]["heartbeat"]
    assert hb["every"] == "2h"
    # Operator's existing fields are preserved — gap-fill never overwrites.
    assert hb["model"] == "anthropic/claude-sonnet-4-6"


def test_snapshot_every_wins_over_default_when_block_is_partial() -> None:
    """When the operator's cost-settings snapshot has `every: 1h` but the
    live config dropped it, restore from snapshot — not the 2h default."""
    cfg = {
        "agents": {
            "defaults": {
                "heartbeat": {"isolatedSession": True, "lightContext": True}
            }
        }
    }
    snapshot = {"heartbeat": {"every": "1h"}}
    changed = gap_fill_cost_settings(cfg, snapshot=snapshot)
    assert changed is True
    assert cfg["agents"]["defaults"]["heartbeat"]["every"] == "1h"


def test_existing_every_is_preserved_when_present() -> None:
    cfg = {
        "agents": {
            "defaults": {
                "model": {"primary": "x"},  # prevent contextPruning/etc fill
                "heartbeat": {"every": "30m", "isolatedSession": True, "lightContext": True},
                "contextPruning": {"mode": "off"},
                "compaction": {"mode": "off"},
                "bootstrapTotalMaxChars": 1,
                "bootstrapMaxChars": 1,
            }
        }
    }
    changed = gap_fill_cost_settings(cfg, snapshot={"heartbeat": {"every": "1h"}})
    # Heartbeat subfields fully present + all block-level cost fields present —
    # gap-fill is a no-op. Snapshot's `every: 1h` is ignored because the
    # operator's existing `every: 30m` wins.
    assert changed is False
    assert cfg["agents"]["defaults"]["heartbeat"]["every"] == "30m"


def test_heartbeat_block_missing_entirely_restores_from_snapshot_first() -> None:
    cfg = _bare_cfg()
    snapshot = {"heartbeat": {"every": "1h", "isolatedSession": True, "lightContext": True}}
    changed = gap_fill_cost_settings(cfg, snapshot=snapshot)
    assert changed is True
    assert cfg["agents"]["defaults"]["heartbeat"]["every"] == "1h"


def test_other_cost_fields_still_get_block_level_gap_fill() -> None:
    cfg = _bare_cfg()
    gap_fill_cost_settings(cfg, snapshot=None)
    defaults = cfg["agents"]["defaults"]
    assert defaults["contextPruning"] == _BALANCED_COST_DEFAULTS["contextPruning"]
    assert defaults["compaction"] == _BALANCED_COST_DEFAULTS["compaction"]
    assert defaults["bootstrapTotalMaxChars"] == 100_000
    assert defaults["bootstrapMaxChars"] == 40_000


def test_snapshot_none_value_for_non_heartbeat_field_is_respected_as_opt_out() -> None:
    """Snapshot can set a field to None to mean "operator explicitly disabled
    this default" — gap-fill should NOT inject the balanced default."""
    cfg = _bare_cfg()
    snapshot = {"compaction": None}
    gap_fill_cost_settings(cfg, snapshot=snapshot)
    assert "compaction" not in cfg["agents"]["defaults"]


def test_balanced_heartbeat_defaults_omit_model_key() -> None:
    """Regression guard: a future operator must not silently re-add a model
    literal to the heartbeat defaults — ModelRouter would ignore it on the
    heartbeat path, and the config would lie about what runs. If you genuinely
    want to override tier3 routing for heartbeats, update tier3.models[0] in
    the bot's evolve-tiers.json instead."""
    assert "model" not in _BALANCED_HEARTBEAT_DEFAULTS
