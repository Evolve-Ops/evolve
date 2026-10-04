"""tests/test_default_role_resolution.py — D-DT4 pod/bot default-role resolver.

internal/decision-default-tier-power-2026-09-23.md. Pins three functions in
primary_bot.py:

  - sanitize_default_role — fast|standard|power only; max/junk/absent falls
    back to the product default ("power").
  - resolve_bot_default_conversation_role — the ONE resolver deploy.py and
    routes_admin_config both call: bot's own explicit userTierOverride.
    defaultTier wins; "auto"/absent/unrecognized falls through to
    network.json::models.defaultRole, sanitized.
  - effective_default_role_and_model — (role, model) pair for the
    user-tier-override PUT's effectiveDefaultRole/effectiveDefaultModel echo.

No real bot/user names appear; placeholders only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import primary_bot  # noqa: E402


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    """Point the bot tiers-path resolver at a tmp file so no pwd lookup runs."""
    tiers_path = tmp_path / "evolve-tiers.json"
    monkeypatch.setattr(
        primary_bot, "_bot_evolve_tiers_path",
        lambda network, bot_id: tiers_path,
    )
    return {"tiers_path": tiers_path, "network": {"bots": {"a_bot": {}}}}


# ── sanitize_default_role ───────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("fast", "fast"),
    ("standard", "standard"),
    ("power", "power"),
])
def test_sanitize_default_role_accepts_classifier_roles(raw, expected):
    assert primary_bot.sanitize_default_role(raw) == expected


@pytest.mark.parametrize("raw", ["max", "auto", "", None, 42, True, "junk"])
def test_sanitize_default_role_falls_back_to_product_default(raw):
    # `max` is pull-only (spec §max #3) and must never be a pod default,
    # same as any other unrecognized value.
    assert primary_bot.sanitize_default_role(raw) == "power"
    assert primary_bot.sanitize_default_role(raw) == primary_bot.DEFAULT_MODEL_CATALOG["defaultRole"]


# ── resolve_bot_default_conversation_role ───────────────────────────────────


def test_no_bot_doc_and_no_pod_key_resolves_to_product_default(bot_env):
    assert primary_bot.resolve_bot_default_conversation_role(
        bot_env["network"], "a_bot",
    ) == "power"


def test_pod_default_overrides_the_product_default(bot_env):
    network = {**bot_env["network"], "models": {"defaultRole": "standard"}}
    assert primary_bot.resolve_bot_default_conversation_role(
        network, "a_bot",
    ) == "standard"


def test_explicit_per_bot_default_beats_the_pod_default(bot_env):
    bot_env["tiers_path"].write_text(json.dumps({
        "userTierOverride": {"defaultTier": "fast"},
    }))
    network = {**bot_env["network"], "models": {"defaultRole": "power"}}
    assert primary_bot.resolve_bot_default_conversation_role(
        network, "a_bot",
    ) == "fast"


def test_per_bot_auto_falls_through_to_the_pod_default(bot_env):
    bot_env["tiers_path"].write_text(json.dumps({
        "userTierOverride": {"defaultTier": "auto"},
    }))
    network = {**bot_env["network"], "models": {"defaultRole": "standard"}}
    assert primary_bot.resolve_bot_default_conversation_role(
        network, "a_bot",
    ) == "standard"


def test_per_bot_max_is_unrecognized_and_falls_through(bot_env):
    # A bot-wide default may never be `max` (pull-only) — same rule the
    # routes_admin_config PUT validator enforces at write time; this is the
    # read-side defense for a hand-edited file.
    bot_env["tiers_path"].write_text(json.dumps({
        "userTierOverride": {"defaultTier": "max"},
    }))
    network = {**bot_env["network"], "models": {"defaultRole": "standard"}}
    assert primary_bot.resolve_bot_default_conversation_role(
        network, "a_bot",
    ) == "standard"


def test_per_bot_defaultRole_key_is_read_same_as_defaultTier(bot_env):
    # The plugin's userTierOverride shape accepts either key
    # (defaultRole preferred, defaultTier the legacy alias) — this resolver
    # must accept both, same as ModelRouter._resolveOperatorDefaultRole.
    bot_env["tiers_path"].write_text(json.dumps({
        "userTierOverride": {"defaultRole": "fast"},
    }))
    assert primary_bot.resolve_bot_default_conversation_role(
        bot_env["network"], "a_bot",
    ) == "fast"


# ── effective_default_role_and_model ────────────────────────────────────────


def test_effective_default_role_and_model_resolves_the_concrete_model(bot_env):
    role, model = primary_bot.effective_default_role_and_model(
        bot_env["network"], "a_bot",
    )
    assert role == "power"
    assert model == "anthropic/claude-opus-4-8"


def test_effective_default_role_and_model_honors_pod_override(bot_env):
    network = {**bot_env["network"], "models": {"defaultRole": "fast"}}
    role, model = primary_bot.effective_default_role_and_model(network, "a_bot")
    assert role == "fast"
    assert model == "anthropic/claude-haiku-4-5"
