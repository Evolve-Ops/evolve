"""tests/test_deploy_detect_provider_model.py — tier-resolved primary seed.

Locks the architectural rule from 2026-05-28 operator pushback:

  **Defaults are by tiers, not by hardcoded model names.**

When deploy needs to seed ``agents.defaults.model.primary``, it MUST
resolve the model from the bot's own tier config first (the floor tier
for member bots; the workhorse tier for primary bots). Admin code only
chooses WHICH TIER applies to the role — it never hardcodes the model
name. The model name comes from one place: the bot's tier config. If
the operator updates tier3 to point at a newer Haiku, the next deploy
honors that without anyone touching admin code.

The RECOMMENDED registry fallback only runs when the bot has no tier
config at all (true new bot, pre-Seed-Defaults). Once tiers exist —
whether operator-set or seeded — the tier walk wins.

Background:
  Personal_bot's primary flipped Haiku→Sonnet on 2026-05-28 with no admin-UI
  write event in the audit log. The previous deploy code hardcoded
  "anthropic/claude-sonnet-4-6" when the model field was empty,
  inverting the cost-leak architecture (every OC leak escalates to
  primary, so primary=Sonnet on tier3-dominant bots = 5× every leak).
  The first round of fix (2026-05-28) replaced the hardcoded Sonnet
  with hardcoded Haiku — still wrong because it duplicated the model
  choice between admin code and the tier config. This round of fix
  moves the source of truth to the bot's tier config.
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

from evolve_admin.deploy import (  # noqa: E402
    _detect_provider_model,
    _floor_model_for_role_from_registry,
    _resolve_primary_from_tier_config,
)


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_primary_from_tier_config — the tier walk
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_member_picks_tier2_first_model():
    """Post-#1765-revert: member bot picks tier2 (workhorse) primary
    just like primary bots. Background work still routes to tier3 via
    the trigger anchor (PR #1737/#1764) + routing.backgroundTier,
    independent of `primary`. So flipping primary to tier3 achieved
    no cost win on background turns AND silently degraded human chat
    on member bots (Slack/Telegram users got Haiku replies with no
    in-channel escalation path)."""
    cfg = {
        "tiers": {
            "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
            "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
        }
    }
    assert _resolve_primary_from_tier_config(cfg, "member") == "anthropic/claude-sonnet-4-6", (
        "member bots must resolve primary to tier2 (workhorse) — see PR #1765 history"
    )


def test_resolve_primary_picks_tier2_first_model():
    """Primary bot always picks tier2 (workhorse). No change."""
    cfg = {
        "tiers": {
            "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
            "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
        }
    }
    assert _resolve_primary_from_tier_config(cfg, "primary") == "anthropic/claude-sonnet-4-6"


def test_resolve_role_none_picks_tier2_first_model():
    """Caller may omit role — workhorse-first applies to all roles
    after the post-#1765-revert correction."""
    cfg = {
        "tiers": {
            "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
            "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
        }
    }
    assert _resolve_primary_from_tier_config(cfg, "") == "anthropic/claude-sonnet-4-6"


def test_resolve_falls_through_to_tier3_when_tier2_empty():
    """Bot only has tier3 defined (no tier2). Resolves to tier3 — the
    walk goes tier2 → tier3 → tier1, so a partial config still resolves."""
    cfg = {"tiers": {"tier3": {"models": ["anthropic/claude-haiku-4-5"]}}}
    assert _resolve_primary_from_tier_config(cfg, "member") == "anthropic/claude-haiku-4-5"


def test_resolve_returns_none_when_no_tiers():
    """No tier config at all → None, so the caller hits the RECOMMENDED
    fallback. This is the "true new bot, before AI Optimization ran"
    case — admin code picks a starter; once tiers land, the tier
    walk wins."""
    assert _resolve_primary_from_tier_config({}, "member") is None
    assert _resolve_primary_from_tier_config({"tiers": {}}, "member") is None


def test_resolve_reads_legacy_tiers_under_agents_defaults_model():
    """Some bots have ``tiers`` under ``agents.defaults.model`` (the
    legacy nested layout, pre-2026-05-25 simplification). The walk
    looks under both locations so legacy configs still resolve."""
    cfg = {
        "agents": {
            "defaults": {
                "model": {
                    "tiers": {
                        "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
                    },
                },
            },
        },
    }
    assert _resolve_primary_from_tier_config(cfg, "member") == "anthropic/claude-sonnet-4-6"


def test_resolve_picks_first_model_when_tier_lists_multiple():
    """Admin_bot-shape: tier2 has multiple cross-provider models
    [Sonnet, Grok, GPT-4.1, Gemini]. The first-in-the-list wins —
    that mirrors OC's own preference order so deploy seed and OC
    runtime agree on which model is 'the workhorse for this tier'."""
    cfg = {
        "tiers": {
            "tier2": {"models": [
                "anthropic/claude-sonnet-4-6",
                "xai/grok-4-1-fast",
                "openai/gpt-4.1",
                "google/gemini-3.1-pro-preview",
            ]},
        }
    }
    assert _resolve_primary_from_tier_config(cfg, "primary") == "anthropic/claude-sonnet-4-6"


# ─────────────────────────────────────────────────────────────────────────────
# _floor_model_for_role_from_registry — the RECOMMENDED fallback
# ─────────────────────────────────────────────────────────────────────────────


def test_registry_fallback_for_anthropic_picks_sonnet():
    """Post-#1765-revert: registry fallback always picks tier2 (workhorse)
    regardless of role — see module-level comment block in deploy.py."""
    assert _floor_model_for_role_from_registry(
        "anthropic", "member",
    ) == "anthropic/claude-sonnet-4-6"
    assert _floor_model_for_role_from_registry(
        "anthropic", "primary",
    ) == "anthropic/claude-sonnet-4-6"


def test_registry_fallback_for_openai_picks_gpt4o():
    """Same workhorse-first principle for OpenAI: tier2 is gpt-4o."""
    assert _floor_model_for_role_from_registry(
        "openai", "member",
    ) == "openai/gpt-4o"


def test_registry_fallback_returns_none_for_unknown_provider():
    assert _floor_model_for_role_from_registry("unknown-provider", "member") is None


# ─────────────────────────────────────────────────────────────────────────────
# _detect_provider_model — end-to-end (tier first, registry fallback)
# ─────────────────────────────────────────────────────────────────────────────


def _write_auth_profiles(tmp_path: Path, bot_user: str, providers: list[str]):
    auth_path = (
        tmp_path / f"home-{bot_user}" / ".openclaw" / "agents" / "main"
        / "agent" / "auth-profiles.json"
    )
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    profiles = {
        f"prof-{i}": {"provider": p}
        for i, p in enumerate(providers)
    }
    auth_path.write_text(json.dumps({"profiles": profiles}))
    return auth_path


def test_detect_uses_tier_config_when_present(monkeypatch, tmp_path):
    """When cfg has a tier3 model, deploy MUST seed from there — not
    from RECOMMENDED. This is the architectural rule: tier config is
    the single source of truth for model selection."""
    cfg = {"tiers": {
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
    }}
    # auth-profiles read should not even be consulted when tier resolves
    # — assert that by NOT writing an auth file. If the code falls
    # through to the registry path, _bot_user_for would try to read a
    # file that isn't there and return None instead of Haiku.
    assert _detect_provider_model(
        "personal_bot", bot_user="personal_bot", role="member", cfg=cfg,
    ) == "anthropic/claude-haiku-4-5"


def test_detect_falls_back_to_registry_when_no_tier_config(
    tmp_path, monkeypatch,
):
    """No tier config → walk to RECOMMENDED fallback via auth-profiles
    detection. Anthropic-keyed bot resolves to tier2 (Sonnet) workhorse
    regardless of role — see post-#1765-revert in deploy.py."""
    bot_user = "personal_bot"
    _write_auth_profiles(tmp_path, bot_user, ["anthropic"])
    monkeypatch.setattr(
        "evolve_admin.deploy._bot_user_for", lambda bid: bot_user,
    )
    monkeypatch.setattr(
        "evolve_admin.deploy._user_home",
        lambda user: tmp_path / f"home-{user}",
    )
    # cfg is None → falls through to registry path
    assert _detect_provider_model(
        "personal_bot", bot_user=bot_user, role="member",
    ) == "anthropic/claude-sonnet-4-6"


def test_detect_role_defaults_to_workhorse_with_tier_config(monkeypatch):
    """Calling without role kwarg lands on tier2 (workhorse) — the
    role-aware floor-first walk was reverted. All roles now use the
    workhorse-first tier walk."""
    cfg = {"tiers": {
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
        "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
    }}
    assert _detect_provider_model(
        "personal_bot", bot_user="personal_bot", cfg=cfg,
    ) == "anthropic/claude-sonnet-4-6"


def test_detect_returns_none_when_no_cfg_and_no_llm_provider(
    tmp_path, monkeypatch,
):
    """No tier config + only non-LLM auth profile → None. Deploy then
    leaves agents.defaults.model unset, and the operator gets a 'no
    LLM key' message from Seed Defaults rather than a silent write."""
    bot_user = "ghost"
    _write_auth_profiles(tmp_path, bot_user, ["brave"])
    monkeypatch.setattr(
        "evolve_admin.deploy._bot_user_for", lambda bid: bot_user,
    )
    monkeypatch.setattr(
        "evolve_admin.deploy._user_home",
        lambda user: tmp_path / f"home-{user}",
    )
    assert _detect_provider_model("ghost", bot_user=bot_user, role="member") is None


def test_detect_tier_config_wins_even_with_unusual_tier_models(monkeypatch):
    """The whole point of the rule: deploy seed mirrors whatever the
    operator put in tier2 (the workhorse tier) — even if that's Opus
    because the operator chose it deliberately. We don't second-guess
    the tier config from admin code. Post-#1765-revert: tier walk is
    workhorse-first (tier2 → tier3 → tier1) for all roles."""
    cfg = {"tiers": {
        "tier3": {"models": ["anthropic/claude-sonnet-4-6"]},
        "tier2": {"models": ["anthropic/claude-opus-4-7"]},  # operator's call
    }}
    assert _detect_provider_model(
        "weirdo", bot_user="weirdo", role="member", cfg=cfg,
    ) == "anthropic/claude-opus-4-7"
