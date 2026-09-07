"""Tests for the provenance index + its integration with customizations()."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evolve_admin.config_sandbox import (
    by_path,
    customizations,
    forget,
    lookup,
    read_index,
    record,
)
from evolve_admin.config_sandbox.provenance import _composite_key, _scope


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
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


# ─── Scope / composite-key tests ─────────────────────────────────────────


def test_scope_pod():
    assert _scope(bot_id=None, gen_id=None) == "pod"


def test_scope_bot():
    assert _scope(bot_id="personal_bot", gen_id=None) == "bot:personal_bot"


def test_scope_gen():
    assert _scope(bot_id=None, gen_id="efficiency_hawk") == "gen:efficiency_hawk"


def test_scope_bot_wins_over_gen():
    """Schema entries are per_bot XOR per_generator. If both supplied, bot wins."""
    assert _scope(bot_id="personal_bot", gen_id="x") == "bot:personal_bot"


def test_composite_key_format():
    assert _composite_key("a.b.c", bot_id=None, gen_id=None) == "a.b.c@pod"
    assert _composite_key("x", bot_id="personal_bot", gen_id=None) == "x@bot:personal_bot"


# ─── record / lookup / forget round-trips ───────────────────────────────


def test_record_and_lookup(shared_dir):
    entry = by_path("better_engine.budget.monthly_cap_usd")
    pe = record(
        entry, 75.0,
        set_by="operator",
        reason="cost-of-living adjustment",
        shared_dir=shared_dir,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    assert pe.set_by == "operator"
    assert pe.set_value == 75.0
    assert pe.previous_default == 50.00

    looked = lookup(
        "better_engine.budget.monthly_cap_usd",
        shared_dir=shared_dir,
    )
    assert looked == pe


def test_record_overwrites_previous_entry(shared_dir):
    entry = by_path("better_engine.budget.monthly_cap_usd")
    record(entry, 60.0, set_by="operator", shared_dir=shared_dir,
           now=datetime(2026, 5, 1, tzinfo=timezone.utc))
    record(entry, 75.0, set_by="rsi:abc-123", shared_dir=shared_dir,
           now=datetime(2026, 5, 9, tzinfo=timezone.utc))

    pe = lookup(entry.path, shared_dir=shared_dir)
    assert pe is not None
    assert pe.set_by == "rsi:abc-123"
    assert pe.set_value == 75.0


def test_forget_removes_entry(shared_dir):
    entry = by_path("better_engine.budget.monthly_cap_usd")
    record(entry, 75.0, set_by="operator", shared_dir=shared_dir)
    assert lookup(entry.path, shared_dir=shared_dir) is not None
    assert forget(entry.path, shared_dir=shared_dir) is True
    assert lookup(entry.path, shared_dir=shared_dir) is None
    # Idempotent — forgetting an absent entry returns False, doesn't raise.
    assert forget(entry.path, shared_dir=shared_dir) is False


def test_per_bot_scope_isolation(shared_dir):
    """Recording for bot A doesn't leak to bot B."""
    entry = by_path("openclaw.plugins.evolve.tier")
    record(entry, "monitor", set_by="operator", bot_id="personal_bot", shared_dir=shared_dir)
    record(entry, "manage", set_by="operator", bot_id="team_bot_a", shared_dir=shared_dir)

    personal_bot = lookup(entry.path, bot_id="personal_bot", shared_dir=shared_dir)
    team_bot_a = lookup(entry.path, bot_id="team_bot_a", shared_dir=shared_dir)
    pod = lookup(entry.path, shared_dir=shared_dir)

    assert personal_bot is not None and personal_bot.set_value == "monitor"
    assert team_bot_a is not None and team_bot_a.set_value == "manage"
    assert pod is None


def test_corrupt_index_loads_as_empty(shared_dir):
    path = shared_dir / "sandbox" / "provenance.json"
    path.parent.mkdir()
    path.write_text("not json", encoding="utf-8")

    index = read_index(shared_dir)
    assert index.entries == {}


# ─── Integration: customizations() picks up explicit-at-default ─────────


def test_explicit_at_default_is_customized(shared_dir, network_json):
    """The case where current_value == stock_default but the operator
    explicitly chose it. customizations() must report it."""
    # Set the better-engine config to the same value as the stock default.
    _write_json(
        shared_dir / "better-engine-config.json",
        {
            "schema_version": 1,
            "pod_defaults": {"budget": {"monthly_cap_usd": 50.00}},
            "bots": {},
        },
    )
    # Without provenance, this is NOT a customization.
    out = customizations(shared_dir=shared_dir, network_json=network_json)
    assert not any(c.entry.path == "better_engine.budget.monthly_cap_usd" for c in out)

    # With provenance saying "operator explicitly chose 50.00":
    entry = by_path("better_engine.budget.monthly_cap_usd")
    record(entry, 50.00, set_by="operator", shared_dir=shared_dir)

    out = customizations(shared_dir=shared_dir, network_json=network_json)
    matches = [c for c in out if c.entry.path == "better_engine.budget.monthly_cap_usd"]
    assert len(matches) == 1
    c = matches[0]
    assert c.current_value == 50.00
    assert c.explicit_at_default is True
    assert c.provenance is not None
    assert c.provenance.set_by == "operator"


def test_provenance_attached_to_divergent_overrides(shared_dir, network_json):
    """When provenance exists AND value diverges, both are surfaced."""
    _write_json(
        shared_dir / "better-engine-config.json",
        {
            "schema_version": 1,
            "pod_defaults": {"budget": {"monthly_cap_usd": 75.00}},
            "bots": {},
        },
    )
    entry = by_path("better_engine.budget.monthly_cap_usd")
    record(
        entry, 75.00,
        set_by="rsi:proposal-xyz",
        reason="proposed by Budget Hawk after threshold breach",
        shared_dir=shared_dir,
    )

    out = customizations(shared_dir=shared_dir, network_json=network_json)
    matches = [c for c in out if c.entry.path == entry.path]
    assert len(matches) == 1
    c = matches[0]
    assert c.explicit_at_default is False
    assert c.provenance is not None
    assert c.provenance.set_by == "rsi:proposal-xyz"
    assert "Budget Hawk" in (c.provenance.reason or "")


def test_native_edit_without_provenance_is_still_customized(shared_dir, network_json):
    """Direct-file edits don't get provenance, but ARE still picked up
    as customizations because the value diverges."""
    _write_json(
        shared_dir / "better-engine-config.json",
        {
            "schema_version": 1,
            "pod_defaults": {"budget": {"monthly_cap_usd": 75.00}},
            "bots": {},
        },
    )
    out = customizations(shared_dir=shared_dir, network_json=network_json)
    matches = [c for c in out if c.entry.path == "better_engine.budget.monthly_cap_usd"]
    assert len(matches) == 1
    c = matches[0]
    assert c.current_value == 75.00
    assert c.provenance is None         # we honestly don't know who set it
    assert c.explicit_at_default is False
