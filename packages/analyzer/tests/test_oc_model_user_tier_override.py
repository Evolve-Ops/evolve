"""tests/test_oc_model_user_tier_override.py — userTierOverride
write/read in oc_model (audit #69 Phase A).

The userTierOverride block in evolve-tiers.json holds the per-bot
operator-controlled defaults the plugin's ModelRouter reads:

  - enabled / dailyCap / allowBotInitiated — existing fields used by
    SetTierTool + admin-UI chip surfaces (PR #1780)
  - defaultTier — Phase A picker: "auto" / "fast" / "standard" / "power"

oc_model.json_full_config_set accepts the block with partial-merge
semantics (same shape as cascade above). This file locks the merge
behavior + whitelist behavior so a single endpoint can ship partial
writes without coupling to siblings.

Locked here:
  1. json_full_config_set accepts {"userTierOverride": {...}} and
     writes it to evolve-tiers.json (NOT openclaw.json) — pure routing
     config, no impact on catalog / primary / fallbacks.
  2. Partial-merge: sending one field leaves the others untouched.
  3. Allowed-keys whitelist: unknown keys are silently dropped without
     poisoning the block. The boundary 400 lives at the endpoint; here
     we lock the storage-layer safety net.
  4. json_full_config returns the userTierOverride block (empty dict
     when absent) so the UI can render the picker from one read.
  5. userTierOverride writes do NOT recompute the flat fallback list
     or touch openclaw.json — purely an evolve-tiers.json sibling.
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
    """Same shape as test_oc_model_cascade.py — minimal openclaw.json,
    empty evolve-tiers.json, HOME pointed at the fake bot dir."""
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


# ── Happy path: each field writes to evolve-tiers.json ────────────────────


def test_default_tier_writes_to_evolve_tiers_json(fake_bot_env):
    """PUT userTierOverride.defaultTier=fast writes to evolve-tiers.json
    and leaves openclaw.json untouched."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {"defaultTier": "fast"}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    assert tiers_path.exists()
    written = json.loads(tiers_path.read_text())
    assert written["userTierOverride"] == {"defaultTier": "fast"}

    # openclaw.json's primary untouched — userTierOverride is pure
    # routing config, never recomputes the fallback list.
    oc_after = json.loads(fake_bot_env["oc_json"].read_text())
    assert oc_after["agents"]["defaults"]["model"]["primary"] == (
        "anthropic/claude-haiku-4-5"
    )


@pytest.mark.parametrize("choice", ["auto", "fast", "standard", "power"])
def test_each_default_tier_choice_persists(fake_bot_env, choice):
    """All four enum values write through cleanly."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {"defaultTier": choice}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    written = json.loads(tiers_path.read_text())
    assert written["userTierOverride"]["defaultTier"] == choice


# ── Partial-merge semantics ───────────────────────────────────────────────


def test_partial_merge_preserves_existing_fields(fake_bot_env):
    """Set defaultTier first, then send a dailyCap-only update — both
    fields must end up in the same block. This is the load-bearing
    invariant: the AI Optimization page's standalone picker mustn't
    clobber the operator's existing dailyCap (set via CLI or the chip
    surfaces)."""
    # Set up: existing block from a prior write.
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {
            "enabled": True,
            "dailyCap": 25,
            "allowBotInitiated": False,
        }},
        oc_json_path=fake_bot_env["oc_json"],
    )

    # Action: send defaultTier-only update.
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {"defaultTier": "standard"}},
        oc_json_path=fake_bot_env["oc_json"],
    )

    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    written = json.loads(tiers_path.read_text())
    assert written["userTierOverride"] == {
        "enabled": True,
        "dailyCap": 25,
        "allowBotInitiated": False,
        "defaultTier": "standard",
    }


def test_partial_merge_overwrites_same_field(fake_bot_env):
    """A second write to the same field overwrites cleanly."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {"defaultTier": "fast"}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {"defaultTier": "power"}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    written = json.loads(tiers_path.read_text())
    assert written["userTierOverride"]["defaultTier"] == "power"


# ── Whitelist behavior ────────────────────────────────────────────────────


def test_unknown_keys_silently_dropped(fake_bot_env):
    """Unknown keys are dropped at the storage layer (the boundary 400
    lives at the endpoint). This is the defense-in-depth net: even if
    a future caller bypasses the endpoint, drive-by writers can't
    poison the block. Defaults to the conservative shape (silent drop
    rather than reject the whole write) so legitimate sibling updates
    in the same payload still land."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {
            "defaultTier": "fast",
            "rogueField": "x",
            "anotherBad": 99,
        }},
        oc_json_path=fake_bot_env["oc_json"],
    )
    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    written = json.loads(tiers_path.read_text())
    assert written["userTierOverride"] == {"defaultTier": "fast"}


def test_non_dict_payload_leaves_existing_block_intact(fake_bot_env):
    """Non-dict incoming payload (shouldn't happen post-validation) is
    rejected by leaving the existing block untouched — matches the
    cascade endpoint's safety stance."""
    # First, seed a real block.
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {"defaultTier": "fast"}},
        oc_json_path=fake_bot_env["oc_json"],
    )

    # Now attempt a malformed write (str instead of dict).
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": "junk"},
        oc_json_path=fake_bot_env["oc_json"],
    )

    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    written = json.loads(tiers_path.read_text())
    # Existing block intact.
    assert written["userTierOverride"] == {"defaultTier": "fast"}


# ── Read path ─────────────────────────────────────────────────────────────


def test_json_full_config_returns_user_tier_override(fake_bot_env):
    """The read path surfaces userTierOverride so the UI can render the
    picker without a second fetch."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {"defaultTier": "power", "dailyCap": 5}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    cfg = oc_model.json_full_config("admin_bot", fake_bot_env["oc_json"])
    assert cfg["userTierOverride"] == {"defaultTier": "power", "dailyCap": 5}


def test_json_full_config_returns_empty_dict_when_absent(fake_bot_env):
    """Bots that have never written the block get {} — the UI can
    distinguish "operator never set this" from "operator set 'auto'"
    even though they route identically."""
    cfg = oc_model.json_full_config("admin_bot", fake_bot_env["oc_json"])
    assert cfg["userTierOverride"] == {}


# ── userTierOverride write is pure routing ────────────────────────────────


def test_user_tier_override_write_does_not_recompute_fallback(fake_bot_env):
    """The fallback list (openclaw.json's model.primary + fallbacks) is
    a function of tiers + tierCascade. userTierOverride is routing
    precedence — touching it MUST NOT trigger a recompute. Verified by
    asserting openclaw.json is byte-identical before/after."""
    before = fake_bot_env["oc_json"].read_bytes()
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={"userTierOverride": {"defaultTier": "fast"}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    after = fake_bot_env["oc_json"].read_bytes()
    assert before == after


# ── Coexistence with cascade + tiers in the same write ────────────────────


def test_user_tier_override_alongside_cascade(fake_bot_env):
    """Both can be written in one call without cross-clobbering — same
    payload shape as the existing cascade tests, just with two
    siblings."""
    oc_model.json_full_config_set(
        bot="admin_bot",
        updates={
            "cascade": {"enabled": True},
            "userTierOverride": {"defaultTier": "standard"},
        },
        oc_json_path=fake_bot_env["oc_json"],
    )
    tiers_path = fake_bot_env["home"] / ".openclaw" / "evolve-tiers.json"
    written = json.loads(tiers_path.read_text())
    assert written["cascade"] == {"enabled": True}
    assert written["userTierOverride"] == {"defaultTier": "standard"}
