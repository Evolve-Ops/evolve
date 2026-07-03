"""tests/test_oc_model_cascade.py — cascade.enabled write/read in oc_model.

The cascade key in evolve-tiers.json is the Phase-3 cutover flag for
tier-cascade routing. The plugin's ModelRouter consults it on every
``before_model_resolve`` to decide whether the CascadeController's
verdicts drive routing or stay shadow-only.

Locked here:
  1. json_full_config_set accepts {"cascade": {"enabled": bool}}
     and writes it to evolve-tiers.json (NOT openclaw.json).
  2. json_full_config reads the cascade key back, with
     ``enabled: False`` as the safe default when absent.
  3. Cascade writes don't touch the catalog / primary / fallbacks —
     it's a pure routing-precedence flag.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


@pytest.fixture
def fake_bot_env(tmp_path, monkeypatch):
    """Stand up a fake bot home with a minimal openclaw.json + empty
    evolve-tiers.json so oc_model can read/write through its real
    helpers."""
    home = tmp_path / "home-bot"
    home.mkdir()
    oc_dir = home / ".openclaw"
    oc_dir.mkdir()
    oc_json = oc_dir / "openclaw.json"
    oc_json.write_text(json.dumps({
        "agents": {"defaults": {"model": {
            "primary": "anthropic/claude-haiku-4-5",
            "fallbacks": [],
        }}},
    }))
    # _tiers_path reads from Path.home() — point HOME at our fake.
    monkeypatch.setenv("HOME", str(home))
    return {"home": home, "oc_json": oc_json}


def test_cascade_enabled_true_writes_to_evolve_tiers_json(fake_bot_env):
    """PUT cascade.enabled = True writes it to evolve-tiers.json under
    the cascade key. openclaw.json (the model.primary file) is NOT
    touched by the cascade write — it's a routing-precedence flag,
    not a model field."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"cascade": {"enabled": True}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    assert tiers_path.exists()
    written = json.loads(tiers_path.read_text())
    assert written["cascade"] == {"enabled": True}

    # And openclaw.json's primary field is untouched
    oc_after = json.loads(fake_bot_env["oc_json"].read_text())
    assert oc_after["agents"]["defaults"]["model"]["primary"] == "anthropic/claude-haiku-4-5"


def test_cascade_enabled_false_flips_back(fake_bot_env):
    """Once enabled, flipping back to False is a single write through
    the same code path — no separate disable endpoint."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"cascade": {"enabled": True}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"cascade": {"enabled": False}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    written = json.loads(tiers_path.read_text())
    assert written["cascade"] == {"enabled": False}


def test_cascade_write_preserves_existing_tiers_and_routing(
    fake_bot_env, monkeypatch,
):
    """Setting cascade must not nuke a previously-populated tiers /
    routing config in evolve-tiers.json. This is the merge-not-replace
    rule — the existing UI tier config stays intact when an operator
    flips the cascade switch.

    Pre-populating tiers triggers the openclaw.json fallback-list
    rewrite, which shells out to the openclaw CLI for schema
    validation. Stub _preserve_write here so the test runs in any
    environment — we're asserting cascade-merge behavior, not OC
    schema validation, which has its own dedicated tests."""
    monkeypatch.setattr(oc_model, "_preserve_write", lambda data, p: p.write_text(json.dumps(data)))

    # Pre-populate tiers + routing
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={
            "tiers": {
                "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
            },
            "routing": {"enabled": True, "maintenanceTier": "tier3"},
        },
        oc_json_path=fake_bot_env["oc_json"],
    )
    # Then flip cascade
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"cascade": {"enabled": True}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    written = json.loads(tiers_path.read_text())
    assert written["cascade"] == {"enabled": True}
    assert "tier3" in written["tiers"]
    assert written["tiers"]["tier3"]["models"] == ["anthropic/claude-haiku-4-5"]
    assert written["routing"]["enabled"] is True


def test_full_config_returns_cascade_disabled_by_default(fake_bot_env):
    """Read-side default: a bot with no cascade key in evolve-tiers.json
    returns cascade.enabled=False from json_full_config — matches the
    plugin's Phase-2-default behavior (shadow only)."""
    cfg = oc_model.json_full_config(
        bot="admin_bot", oc_json_path=fake_bot_env["oc_json"],
    )
    assert "cascade" in cfg
    assert cfg["cascade"]["enabled"] is False


def test_full_config_returns_cascade_enabled_after_set(fake_bot_env):
    """After flipping cascade.enabled=True, json_full_config reads it
    back — what the AI Optimization UI needs to render the toggle's
    current state correctly."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"cascade": {"enabled": True}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    cfg = oc_model.json_full_config(
        bot="admin_bot", oc_json_path=fake_bot_env["oc_json"],
    )
    assert cfg["cascade"]["enabled"] is True


def test_cascade_merge_does_not_clobber_unknown_subkeys(fake_bot_env):
    """When the cascade dict gains a sibling key in the future
    (e.g. "askHintCooldownSeconds"), updates to "enabled" alone must
    not wipe the sibling. Defensive merge tested here."""
    # Pre-populate cascade with two keys
    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({
        "cascade": {"enabled": False, "askHintCooldownSeconds": 1800},
    }))
    # Flip enabled only
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"cascade": {"enabled": True}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(tiers_path.read_text())
    assert written["cascade"]["enabled"] is True
    assert written["cascade"]["askHintCooldownSeconds"] == 1800
