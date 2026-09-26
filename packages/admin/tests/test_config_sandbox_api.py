"""Tests for the read API: resolve() and customizations()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.config_sandbox import (
    Customization,
    Strength,
    customization_summary,
    customizations,
    resolve,
)
from evolve_admin.config_sandbox.schema import Store


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    """A temporary shared_dir that mimics the deploy layout."""
    sd = tmp_path / "shared"
    sd.mkdir()
    (sd / "generators").mkdir()
    (sd / "bot_guides").mkdir()
    return sd


@pytest.fixture
def network_json(tmp_path: Path) -> Path:
    return tmp_path / "network.json"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ─── resolve() ──────────────────────────────────────────────────────────────


def test_resolve_returns_default_when_file_missing(shared_dir, network_json):
    r = resolve(
        "better_engine.budget.monthly_cap_usd",
        shared_dir=shared_dir,
        network_json=network_json,
    )
    assert r.value == 50.00
    assert r.source == "default"


def test_resolve_reads_overridden_value(shared_dir, network_json):
    _write_json(
        shared_dir / "better-engine-config.json",
        {
            "schema_version": 1,
            "pod_defaults": {
                "budget": {"monthly_cap_usd": 75.0},
            },
            "bots": {},
        },
    )
    r = resolve(
        "better_engine.budget.monthly_cap_usd",
        shared_dir=shared_dir,
        network_json=network_json,
    )
    assert r.value == 75.0
    assert r.source == "store"


def test_resolve_per_bot_returns_missing_without_bot_id(shared_dir, network_json):
    r = resolve(
        "openclaw.plugins.evolve.tier",
        shared_dir=shared_dir,
        network_json=network_json,
    )
    # Per-bot key with no bot_id supplied — caller bug, signal explicitly.
    assert r.source == "missing"


def test_resolve_unknown_path_raises(shared_dir, network_json):
    with pytest.raises(KeyError):
        resolve("does.not.exist", shared_dir=shared_dir, network_json=network_json)


def test_resolve_network_key_from_network_json(shared_dir, network_json):
    _write_json(network_json, {"timezone": "America/Los_Angeles"})
    r = resolve(
        "network.timezone",
        shared_dir=shared_dir,
        network_json=network_json,
    )
    assert r.value == "America/Los_Angeles"
    assert r.source == "store"


# ─── customizations() ──────────────────────────────────────────────────────


def test_customizations_empty_when_nothing_overridden(shared_dir, network_json):
    out = customizations(shared_dir=shared_dir, network_json=network_json)
    assert out == []


def test_customizations_picks_up_pod_override(shared_dir, network_json):
    _write_json(
        shared_dir / "better-engine-config.json",
        {
            "schema_version": 1,
            "pod_defaults": {"budget": {"monthly_cap_usd": 75.0}},
            "bots": {},
        },
    )
    out = customizations(shared_dir=shared_dir, network_json=network_json)
    paths = [c.entry.path for c in out]
    assert "better_engine.budget.monthly_cap_usd" in paths
    c = next(c for c in out if c.entry.path == "better_engine.budget.monthly_cap_usd")
    assert c.current_value == 75.0
    assert c.stock_default == 50.00


def test_customizations_default_value_is_not_flagged(shared_dir, network_json):
    """Setting a key to the same value as the stock default is not a customization."""
    _write_json(
        shared_dir / "better-engine-config.json",
        {
            "schema_version": 1,
            "pod_defaults": {"budget": {"monthly_cap_usd": 50.00}},
            "bots": {},
        },
    )
    out = customizations(shared_dir=shared_dir, network_json=network_json)
    paths = [c.entry.path for c in out]
    assert "better_engine.budget.monthly_cap_usd" not in paths


def test_customizations_summary(shared_dir, network_json):
    _write_json(
        shared_dir / "better-engine-config.json",
        {
            "schema_version": 1,
            "pod_defaults": {
                "budget": {"monthly_cap_usd": 75.0},
                "conversational_approval": {"confidence_threshold": 0.9},
            },
            "bots": {},
        },
    )
    out = customizations(shared_dir=shared_dir, network_json=network_json)
    s = customization_summary(out)
    assert s["total"] == 2
    assert s["by_strength"]["free"] == 1            # monthly_cap_usd
    assert s["by_strength"]["shipped-policy"] == 1  # confidence_threshold


def test_customizations_include_stores_filter(shared_dir, network_json):
    _write_json(network_json, {"timezone": "Asia/Tokyo"})
    _write_json(
        shared_dir / "better-engine-config.json",
        {
            "schema_version": 1,
            "pod_defaults": {"budget": {"monthly_cap_usd": 75.0}},
            "bots": {},
        },
    )
    out = customizations(
        shared_dir=shared_dir,
        network_json=network_json,
        include_stores={Store.NETWORK},
    )
    stores = {c.entry.store for c in out}
    assert stores == {Store.NETWORK}


def test_customizations_doc_identity_picked_up(shared_dir, network_json):
    """A bot_guide is "customized" iff a non-empty file exists for that bot."""
    bot_id = "personal_bot"
    (shared_dir / "bot_guides" / f"{bot_id}.md").write_text(
        "Speak gently and use plain words.\n", encoding="utf-8"
    )
    out = customizations(
        bot_ids=[bot_id],
        shared_dir=shared_dir,
        network_json=network_json,
    )
    guide = [c for c in out if c.entry.path == "bot_guide.<bot_id>"]
    assert len(guide) == 1
    assert guide[0].bot_id == bot_id
    assert "Speak gently" in guide[0].current_value


# ─── Wildcard dotpath walk (PR A) ──────────────────────────────────────────
# The cacheRetention tunable uses a wildcard target_path
# ``agents.defaults.models.*.params.cacheRetention``. The stores layer's
# ``_walk`` must understand the ``*`` segment so the customizations UI
# can display the current value on disk without crashing.


def test_walk_wildcard_returns_value_when_all_anthropic_models_agree():
    from evolve_admin.config_sandbox.stores import _walk
    data = {
        "agents": {
            "defaults": {
                "models": {
                    "anthropic/claude-sonnet-4-6": {
                        "params": {"cacheRetention": "long"},
                    },
                    "anthropic/claude-haiku-4-5": {
                        "params": {"cacheRetention": "long"},
                    },
                    "openai/gpt-5.5": {},  # non-Anthropic, ignored by the wildcard
                },
            },
        },
    }
    result = _walk(data, "agents.defaults.models.*.params.cacheRetention")
    assert result == "long"


def test_walk_wildcard_missing_when_no_anthropic_models_set_field():
    """No Anthropic model has the field → walk returns MISSING (treated
    as "no override active" by the customization differ)."""
    from evolve_admin.config_sandbox.stores import _walk, _MISSING
    data = {
        "agents": {
            "defaults": {
                "models": {
                    "anthropic/claude-sonnet-4-6": {},  # no params
                    "openai/gpt-5.5": {"params": {"cacheRetention": "long"}},  # ignored
                },
            },
        },
    }
    result = _walk(data, "agents.defaults.models.*.params.cacheRetention")
    assert result is _MISSING


def test_walk_wildcard_returns_first_when_models_disagree():
    """If catalog models disagree (one short, one long) we surface the
    first found value — customizations differ then sees a divergence
    and the operator's UI shows the override row, prompting cleanup."""
    from evolve_admin.config_sandbox.stores import _walk
    data = {
        "agents": {
            "defaults": {
                "models": {
                    "anthropic/claude-sonnet-4-6": {
                        "params": {"cacheRetention": "long"},
                    },
                    "anthropic/claude-haiku-4-5": {
                        "params": {"cacheRetention": "short"},
                    },
                },
            },
        },
    }
    result = _walk(data, "agents.defaults.models.*.params.cacheRetention")
    # First child in dict-iteration order is sonnet; "long" wins.
    assert result == "long"


def test_walk_no_wildcard_unchanged_semantics():
    """Smoke: wildcard support must not regress the existing dotted walk."""
    from evolve_admin.config_sandbox.stores import _walk, _MISSING
    data = {"a": {"b": {"c": 42}}}
    assert _walk(data, "a.b.c") == 42
    assert _walk(data, "a.b.missing") is _MISSING
    assert _walk(data, "a.b") == {"c": 42}
