"""Judge tier (tier0) defaults to the workhorse (tier2) model when the
bot hasn't assigned anything to tier0 explicitly.

This is the runtime half of the "judge isn't required" rule. The setup
checklist no longer requires tier0 (see _detect_ai_optim_tiers); without
this fallback, bots that finished setup with only tier2 assigned would
hit DEFAULT_TIERS["tier0"] = "openai/gpt-4o" at judge-call time and
error out if they don't have OpenAI credentials. Mirroring tier2 keeps
judging working with whatever provider the operator already picked.
"""

import sys
from pathlib import Path

import pytest

ANALYZER_DIR = Path(__file__).resolve().parents[1]
if str(ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYZER_DIR))

import models  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_primary_bot(monkeypatch):
    """Replace primary_bot.bot_tier_models with a fixture-controlled lookup
    so tests can inject per-bot tier assignments without touching disk."""
    fake_assignments: dict[tuple[str, str], list[str]] = {}

    def fake_bot_tier_models(_config, bot_id, tier):
        return list(fake_assignments.get((bot_id, tier), []))

    def fake_primary_bot_id(_config):
        return "team_bot_a"

    import primary_bot  # type: ignore

    monkeypatch.setattr(primary_bot, "bot_tier_models", fake_bot_tier_models)
    monkeypatch.setattr(primary_bot, "primary_bot_id", fake_primary_bot_id)
    return fake_assignments


def test_tier0_mirrors_tier2_when_unassigned(_stub_primary_bot):
    """Bot has tier2 assigned (anthropic), no tier0 → tier0 resolves to the
    same anthropic model rather than the hardcoded openai/gpt-4o default."""
    _stub_primary_bot[("team_bot_a", "tier2")] = ["anthropic/claude-sonnet-4-6"]
    resolved = models.resolve_tier("tier0", {}, bot_id="team_bot_a")
    assert resolved == "anthropic/claude-sonnet-4-6"


def test_tier0_honors_explicit_assignment(_stub_primary_bot):
    """If the operator picks a tier0 model explicitly, that wins over the
    workhorse-mirror fallback — the cross-provider recommendation is real,
    it just isn't required."""
    _stub_primary_bot[("team_bot_a", "tier2")] = ["anthropic/claude-sonnet-4-6"]
    _stub_primary_bot[("team_bot_a", "tier0")] = ["openai/gpt-4o"]
    resolved = models.resolve_tier("tier0", {}, bot_id="team_bot_a")
    assert resolved == "openai/gpt-4o"


def test_tier0_falls_back_to_tier2_default_when_neither_assigned(_stub_primary_bot):
    """Bot has nothing assigned for tier0 OR tier2 → tier0 falls back to
    DEFAULT_TIERS["tier2"] rather than DEFAULT_TIERS["tier0"]. Keeps the
    judge call on the same provider the workhorse would use."""
    resolved = models.resolve_tier("tier0", {}, bot_id="team_bot_a")
    expected = models.DEFAULT_TIERS["tier2"]["models"][0]
    assert resolved == expected


def test_tier2_still_uses_its_own_default_when_unassigned(_stub_primary_bot):
    """Sanity: the new tier0-special-case must not leak into tier2 resolution."""
    resolved = models.resolve_tier("tier2", {}, bot_id="team_bot_a")
    expected = models.DEFAULT_TIERS["tier2"]["models"][0]
    assert resolved == expected
