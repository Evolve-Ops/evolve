"""tests/test_wizard_floor_model.py — wizard.py starter-primary tests.

Post-#1765-revert: the wizard's ``_new_bot_openclaw_config`` builder
resolves the starter primary via the tier registry — workhorse-first
(tier2 → tier3 → tier1) for all roles. The model id comes from
``model_registry.RECOMMENDED`` rather than being hardcoded.

History:
  This file originally pinned a role-aware floor walk (member → tier3,
  primary → tier2) — closing the wizard duplicate of PR #1736. That
  rule was reverted along with PR #1765 after the Slack/Telegram chat
  surface couldn't escalate from tier3 (Haiku) replies. Today all
  starters land on tier2 (workhorse); background work routes to tier3
  via the trigger anchor independent of `primary`. See deploy.py module
  comment for the full architectural history.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.wizard import (  # noqa: E402
    _new_bot_openclaw_config,
    _wizard_floor_model,
)


# ── _wizard_floor_model: the role→tier resolver ─────────────────────────────


def test_floor_model_anthropic_resolves_sonnet():
    """Workhorse-first walk → tier2 model = Sonnet for Anthropic."""
    assert _wizard_floor_model("anthropic", "member") == "anthropic/claude-sonnet-4-6"
    assert _wizard_floor_model("anthropic", "primary") == "anthropic/claude-sonnet-4-6"


def test_floor_model_openai_resolves_gpt4o():
    """Workhorse-first walk → tier2 model = gpt-4o for OpenAI."""
    assert _wizard_floor_model("openai", "member") == "openai/gpt-4o"
    assert _wizard_floor_model("openai", "primary") == "openai/gpt-4o"


def test_floor_model_unknown_provider_returns_empty_string():
    """Provider not in RECOMMENDED → empty string. Caller writes the
    field empty, and deploy fills it via tier-resolved path on the
    next deploy. Don't fabricate a model name."""
    assert _wizard_floor_model("unknown-provider", "member") == ""


def test_floor_model_role_is_ignored_post_revert():
    """REGRESSION (post-#1765-revert): any role value resolves to the
    same workhorse-first model. Reintroducing role-based dispatch here
    requires first addressing the Slack/Telegram-can't-escalate problem."""
    expected = "anthropic/claude-sonnet-4-6"
    for role in ("member", "primary", "guest", "", "future-role"):
        assert _wizard_floor_model("anthropic", role) == expected, (
            f"role={role!r} should resolve to {expected!r}; "
            f"got {_wizard_floor_model('anthropic', role)!r}"
        )


# ── _new_bot_openclaw_config: end-to-end ────────────────────────────────────


def test_new_bot_config_defaults_to_member_with_workhorse_primary():
    """The wizard creates member bots by default; post-#1765-revert
    they land on tier2 (workhorse) primary just like primary bots."""
    cfg = _new_bot_openclaw_config(
        name="newbot", provider="anthropic", port=19099,
    )
    primary = cfg["agents"]["defaults"]["model"]["primary"]
    assert primary == "anthropic/claude-sonnet-4-6"


def test_new_bot_config_role_does_not_affect_primary():
    """Explicit role kwarg doesn't change the starter primary —
    workhorse-first applies to all roles after the post-#1765-revert."""
    cfg_primary = _new_bot_openclaw_config(
        name="adminbot", provider="anthropic", port=19099, role="primary",
    )
    cfg_member = _new_bot_openclaw_config(
        name="memberbot", provider="anthropic", port=19099, role="member",
    )
    assert cfg_primary["agents"]["defaults"]["model"]["primary"] == "anthropic/claude-sonnet-4-6"
    assert cfg_member["agents"]["defaults"]["model"]["primary"] == "anthropic/claude-sonnet-4-6"


def test_new_bot_config_openai_resolves_gpt4o():
    cfg = _new_bot_openclaw_config(
        name="openaibot", provider="openai", port=19099,
    )
    primary = cfg["agents"]["defaults"]["model"]["primary"]
    assert primary == "openai/gpt-4o"
