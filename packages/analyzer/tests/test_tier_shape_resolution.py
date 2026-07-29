"""Regression tests for rungs/roles shape resolution on the read side.

Guards the 2026-06-09 incident: the tier→role migration left readers
speaking only the legacy ``{tiers: {tierN: {models}}}`` shape, so a migrated
``evolve-tiers.json`` (rungs/roles) resolved to ``[]`` — the empty result
that made the admin Tier Resolution card render every bot's allocations as
"erased". These tests pin:

  - new-shape file resolves each tierN through its role → rung → models;
  - structured ``judge`` role ({rung, provider}) resolves to its rung;
  - mixed-shape file (rungs + stale tiers): the NEW shape wins on read;
  - legacy-only file: unchanged behavior;
  - bot_tier_models routes through the resolver end-to-end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import primary_bot as pb  # noqa: E402


# A real migrated shape: rungs (cost-ordered) + roles incl. structured judge.
NEW_SHAPE = {
    "rungs": [
        {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"], "costClass": "low"},
        {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6", "openai/gpt-4o"], "costClass": "medium"},
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ],
    "roles": {
        "fast": "haiku-class",
        "standard": "sonnet-class",
        "power": "opus-class",
        "judge": {"rung": "sonnet-class", "provider": "not-standard"},
    },
}

LEGACY_SHAPE = {
    "tiers": {
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
        "tier2": {"models": ["anthropic/claude-sonnet-4-6", "openai/gpt-4o"]},
        "tier1": {"models": ["anthropic/claude-opus-4-6"]},
        "tier0": {"models": ["openai/gpt-4o"]},
    },
}


def test_new_shape_resolves_each_tier_through_role():
    assert pb.resolve_tier_chain(NEW_SHAPE, "tier3") == [
        "anthropic/claude-haiku-4-5", "openai/gpt-4o-mini",
    ]
    assert pb.resolve_tier_chain(NEW_SHAPE, "tier2") == [
        "anthropic/claude-sonnet-4-6", "openai/gpt-4o",
    ]
    assert pb.resolve_tier_chain(NEW_SHAPE, "tier1") == ["anthropic/claude-opus-4-8"]


def test_new_shape_judge_resolves_via_structured_role():
    # tier0 → judge → {rung: sonnet-class} → that rung's models.
    assert pb.resolve_tier_chain(NEW_SHAPE, "tier0") == [
        "anthropic/claude-sonnet-4-6", "openai/gpt-4o",
    ]


def test_new_shape_missing_role_falls_back_to_default_rung():
    # A partially-migrated file with rungs but no roles map: the canonical
    # default rung for the role is used so a read still finds the cluster.
    data = {"rungs": [
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ]}
    assert pb.resolve_tier_chain(data, "tier1") == ["anthropic/claude-opus-4-8"]


def test_legacy_only_shape_unchanged():
    assert pb.resolve_tier_chain(LEGACY_SHAPE, "tier3") == ["anthropic/claude-haiku-4-5"]
    assert pb.resolve_tier_chain(LEGACY_SHAPE, "tier1") == ["anthropic/claude-opus-4-6"]


def test_mixed_shape_new_wins_on_read():
    # The pollution shape: rungs present (new model) PLUS a stale legacy
    # tiers key (old model). The gateway loader ignores tiers when rungs
    # exist — the reader must match: new shape wins.
    mixed = dict(NEW_SHAPE)
    mixed["tiers"] = {"tier1": {"models": ["anthropic/claude-opus-4-6"]}}  # stale
    assert pb.resolve_tier_chain(mixed, "tier1") == ["anthropic/claude-opus-4-8"]


def test_unconfigured_tier_returns_empty():
    sparse = {"rungs": [
        {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
    ], "roles": {"fast": "haiku-class"}}
    # tier1 → power → opus-class default rung, which isn't present → [].
    assert pb.resolve_tier_chain(sparse, "tier1") == []


def test_bot_tier_models_routes_through_resolver(tmp_path, monkeypatch):
    path = tmp_path / "evolve-tiers.json"
    path.write_text(json.dumps(NEW_SHAPE))
    monkeypatch.setattr(pb, "_bot_evolve_tiers_path", lambda network, bot_id: path)
    network = {"bots": {"b": {"user": "b"}}}
    assert pb.bot_tier_models(network, "b", "tier2") == [
        "anthropic/claude-sonnet-4-6", "openai/gpt-4o",
    ]


def test_bot_tier_models_legacy_file_still_works(tmp_path, monkeypatch):
    path = tmp_path / "evolve-tiers.json"
    path.write_text(json.dumps(LEGACY_SHAPE))
    monkeypatch.setattr(pb, "_bot_evolve_tiers_path", lambda network, bot_id: path)
    network = {"bots": {"b": {"user": "b"}}}
    assert pb.bot_tier_models(network, "b", "tier3") == ["anthropic/claude-haiku-4-5"]
