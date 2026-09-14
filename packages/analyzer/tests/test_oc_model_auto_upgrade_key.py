"""tests/test_oc_model_auto_upgrade_key.py — autoUpgrade write in oc_model.

spec-model-auto-upgrade-2026-07-30 §Config shape: a Custom bot's auto-upgrade
policy lives in that bot's evolve-tiers.json::autoUpgrade. json_full_config_set
accepts the block with partial-merge semantics (same shape as cascade /
userTierOverride), a key whitelist, and one extra rule the siblings don't have:
an EMPTY dict CLEARS the block — the "Reset to pod defaults" path (lifecycle
rule 2: the bot goes back to following the pod in full).

Locked here:
  1. {"autoUpgrade": {...}} writes to evolve-tiers.json (NOT openclaw.json).
  2. Partial-merge: an enabled-only flip leaves other knobs untouched.
  3. Whitelist: unknown keys are silently dropped.
  4. Empty dict clears the block entirely.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


@pytest.fixture
def fake_bot_env(tmp_path, monkeypatch):
    """Same shape as test_oc_model_user_tier_override.py — minimal
    openclaw.json, HOME pointed at the fake bot dir."""
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
    monkeypatch.setenv("HOME", str(home))
    return {"home": home, "oc_json": oc_json}


def _tiers_doc(env) -> dict:
    path = env["home"] / ".openclaw" / "evolve-tiers.json"
    return json.loads(path.read_text()) if path.exists() else {}


def test_auto_upgrade_writes_to_evolve_tiers_json(fake_bot_env):
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"autoUpgrade": {"enabled": True}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    assert _tiers_doc(fake_bot_env)["autoUpgrade"] == {"enabled": True}
    # openclaw.json untouched — pure evolve-tiers sibling.
    oc = json.loads(fake_bot_env["oc_json"].read_text())
    assert oc["agents"]["defaults"]["model"]["primary"] == "anthropic/claude-haiku-4-5"


def test_auto_upgrade_partial_merge_preserves_siblings(fake_bot_env):
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"autoUpgrade": {"enabled": True, "applyDay": "friday"}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"autoUpgrade": {"enabled": False}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    assert _tiers_doc(fake_bot_env)["autoUpgrade"] == {
        "enabled": False, "applyDay": "friday",
    }


def test_auto_upgrade_unknown_keys_dropped(fake_bot_env):
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"autoUpgrade": {"enabled": True, "rm_rf": "yes"}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    assert _tiers_doc(fake_bot_env)["autoUpgrade"] == {"enabled": True}


def test_auto_upgrade_empty_dict_clears_block(fake_bot_env):
    """Lifecycle rule 2: reset-to-pod-defaults clears the bot's own policy."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"autoUpgrade": {"enabled": True}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"autoUpgrade": {}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    assert "autoUpgrade" not in _tiers_doc(fake_bot_env)
