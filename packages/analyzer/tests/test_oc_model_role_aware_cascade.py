"""tests/test_oc_model_role_aware_cascade.py — default tierCascade contract.

History:
  • PR #1765 introduced role-aware default cascades — member bots got
    a floor-first cascade ([tier3, tier2, tier1]) so primary derived
    to the tier3 floor. Reasoning: "member bots' dominant work is
    background → save cost by defaulting to Haiku."
  • That was wrong on two counts:
      1. Background work was ALREADY routing to tier3 via the
         trigger anchor (PR #1737 / #1764), independent of primary.
         No additional cost reduction on background turns.
      2. It silently degraded human-facing chat on member bots —
         Slack/Telegram/Discord users got tier3 (Haiku) replies with
         no in-channel way to escalate (the chip surface is admin-UI-only).
  • This revert restores the workhorse-first default for ALL roles.
    The per-bot default-tier picker (auto/fast/standard/power) is the
    correct path for operator/user-driven defaults — coming as a
    follow-up.

Coverage:
- default_tier_cascade_for_role returns workhorse-first for any role
- json_full_config_set derives tier2 primary for member, primary, and
  unspecified roles (no role-based divergence)
- Explicit tierCascade in updates wins over the default
- Explicit tierCascade in existing evolve-tiers.json wins
- Routing-only updates don't rewrite primary
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
from oc_model import (  # noqa: E402
    DEFAULT_TIER_CASCADE,
    default_tier_cascade_for_role,
    json_full_config_set,
)


# Pretend `openclaw config validate` succeeded — binary may not exist locally.
class _OkValidate:
    returncode = 0
    stdout = '{"valid": true, "issues": []}'
    stderr = ""


@pytest.fixture(autouse=True)
def _stub_openclaw_validate(monkeypatch):
    monkeypatch.setattr(oc_model.subprocess, "run", lambda *a, **kw: _OkValidate())


@pytest.fixture
def fresh_bot(tmp_path, monkeypatch):
    """Build a bot home with empty openclaw.json + no evolve-tiers.json.

    Returns (oc_json_path, home_dir). monkeypatches Path.home to the
    tmp dir so _tiers_path resolves into the fixture.
    """
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({
        "agents": {"defaults": {"model": {}, "models": {}}},
    }, indent=2))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return oc_json, home


# ── default_tier_cascade_for_role — role is currently ignored ──────────────


def test_default_cascade_is_workhorse_first_for_primary():
    assert default_tier_cascade_for_role("primary") == ["tier2", "tier3", "tier1"]


def test_default_cascade_is_ALSO_workhorse_first_for_member(monkeypatch):
    """REGRESSION (post-#1765-revert): member bots no longer flip to
    floor-first cascade. The role-aware divergence created a silent
    Haiku-for-humans regression on Slack/Telegram. Trigger-anchor
    routing (PR #1737/#1764) already handles cost on background work
    without touching `primary`."""
    cascade = default_tier_cascade_for_role("member")
    assert cascade == ["tier2", "tier3", "tier1"], (
        f"member bots must use workhorse-first default (Slack/Telegram "
        f"users can't escalate from chat); got {cascade}. If reintroducing "
        f"role-based dispatch, first address the chat-surface escalation "
        f"problem (per-bot default-tier picker, evo keyword, etc.)."
    )


def test_default_cascade_for_unknown_role_keeps_workhorse_first():
    assert default_tier_cascade_for_role(None) == ["tier2", "tier3", "tier1"]
    assert default_tier_cascade_for_role("something-new") == ["tier2", "tier3", "tier1"]


def test_default_cascade_constant_matches_helper():
    """The exposed constant + the helper must agree — direct importers
    of DEFAULT_TIER_CASCADE shouldn't see different behavior than callers
    going through the helper."""
    assert DEFAULT_TIER_CASCADE == default_tier_cascade_for_role(None)
    assert DEFAULT_TIER_CASCADE == default_tier_cascade_for_role("member")
    assert DEFAULT_TIER_CASCADE == default_tier_cascade_for_role("primary")


# ── json_full_config_set primary derivation ────────────────────────────────


_TIER_PAYLOAD = {
    "tiers": {
        "tier1": {"models": ["anthropic/claude-opus-4-7"]},
        "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
    },
}


def _read_primary(oc_json: Path) -> str:
    return json.loads(oc_json.read_text())["agents"]["defaults"]["model"]["primary"]


def test_member_role_derives_tier2_primary(fresh_bot):
    """Post-#1765-revert: member bots use the workhorse-first default
    (= tier 2 = Sonnet for Anthropic) just like primary bots. Background
    work still gets tier3 via the trigger anchor + routing config."""
    oc_json, _ = fresh_bot
    json_full_config_set("personal_bot", _TIER_PAYLOAD, oc_json_path=oc_json, role="member")
    assert _read_primary(oc_json) == "anthropic/claude-sonnet-4-6"


def test_primary_role_derives_tier2_primary(fresh_bot):
    """Primary bots have always defaulted to tier 2 primary. No change."""
    oc_json, _ = fresh_bot
    json_full_config_set("evo", _TIER_PAYLOAD, oc_json_path=oc_json, role="primary")
    assert _read_primary(oc_json) == "anthropic/claude-sonnet-4-6"


def test_role_none_derives_tier2_primary(fresh_bot):
    """Pre-role-aware callers (role=None) get the same workhorse-first."""
    oc_json, _ = fresh_bot
    json_full_config_set("legacy", _TIER_PAYLOAD, oc_json_path=oc_json)
    assert _read_primary(oc_json) == "anthropic/claude-sonnet-4-6"


# ── Operator's explicit tierCascade wins over default ──────────────────────


def test_explicit_tierCascade_in_updates_overrides_default(fresh_bot):
    """An operator who explicitly sets a non-default cascade still wins.
    This is the path the per-bot default-tier picker (coming) will use:
    when the operator picks 'fast' for a bot, the cascade gets set to
    floor-first explicitly and the writer respects it."""
    oc_json, _ = fresh_bot
    json_full_config_set(
        "personal_bot",
        {**_TIER_PAYLOAD, "tierCascade": ["tier3", "tier2", "tier1"]},
        oc_json_path=oc_json,
        role="member",
    )
    assert _read_primary(oc_json) == "anthropic/claude-haiku-4-5"


def test_explicit_tierCascade_in_existing_evolve_tiers_overrides_default(fresh_bot, tmp_path):
    """When evolve-tiers.json already has an explicit tierCascade, a
    subsequent tiers-only update keeps that cascade."""
    oc_json, home = fresh_bot
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({
        "tiers": {},
        "tierCascade": ["tier1", "tier3", "tier2"],
    }))
    json_full_config_set(
        "personal_bot", _TIER_PAYLOAD, oc_json_path=oc_json, role="member",
    )
    # Operator's tier1-first cascade wins → primary = tier1 (Opus)
    assert _read_primary(oc_json) == "anthropic/claude-opus-4-7"


# ── No-tier-update calls don't touch primary ───────────────────────────────


def test_routing_only_update_does_not_rewrite_primary(fresh_bot):
    """Routing/cascade-only updates must not retroactively rewrite
    primary — heal is opt-in via tier writes, not every change."""
    oc_json, _ = fresh_bot
    json_full_config_set(
        "personal_bot", _TIER_PAYLOAD, oc_json_path=oc_json, role="primary",
    )
    initial_primary = _read_primary(oc_json)
    json_full_config_set(
        "personal_bot",
        {"routing": {"backgroundTier": "tier3"}},
        oc_json_path=oc_json,
        role="member",
    )
    assert _read_primary(oc_json) == initial_primary
