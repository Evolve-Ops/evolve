"""tests/test_seed_model_config.py — provisioning.seed_model_config_if_empty unit tests.

The seed function is the fix for the atlas-daily-digest forge failure
("No API key found for provider 'openai'"): OC's onboard writes a
minimal openclaw.json with no model.primary, so the agent falls back to
OpenAI defaults at first invocation. This stage writes a default
catalog + tier assignments from packages/analyzer/model_registry.RECOMMENDED
for the first LLM provider the bot has an auth-profile entry for.

Coverage:
- provider selection (preferred wins, then order, then fallback)
- catalog assembly from RECOMMENDED
- idempotence: skip when primary already set
- soft-fail when oc_full_config_get returns None
- no LLM provider in auth-profiles → seeded=False with clear reason
- write_result None → ok=False with informative reason

These tests are unit-level: they mock oc_full_config_get / set and the
auth-profile reader. End-to-end seeding against a real openclaw.json
gets exercised by the existing test_provision_bot.test_happy_path
suite (which mocks the call) plus the manual smoke step on the mini.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Import oc_cli upfront so the lazy import inside seed_model_config_if_empty
# resolves to the cached module — and so `patch("oc_cli.oc_full_config_*")`
# in the tests has something to attach to.
import oc_cli  # noqa: E402,F401

from evolve_admin import provisioning  # noqa: E402
from evolve_admin.provisioning import (  # noqa: E402
    _default_catalog_for_provider,
    _pick_provider,
    seed_model_config_if_empty,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "port": 19000},
                 "atlas": {"role": "member", "port": 19031}},
        "members": ["evo", "atlas"],
    }))
    return p


# ── _pick_provider ──────────────────────────────────────────────────────────


def test_pick_provider_honors_preferred_when_available():
    assert _pick_provider(["anthropic", "openai"], preferred="openai") == "openai"


def test_pick_provider_falls_back_to_anthropic_when_preferred_unavailable():
    """Q4 of the wizard spec mandates Sonnet (Anthropic) is the forge
    builder default — so when picking from a list with multiple LLMs
    we should pick anthropic first if available, regardless of order."""
    assert _pick_provider(["openai", "google", "anthropic"]) == "anthropic"


def test_pick_provider_walks_preference_order():
    """No anthropic, but openai is next in the preference list."""
    assert _pick_provider(["google", "openai", "xai"]) == "openai"


def test_pick_provider_returns_none_for_only_non_llm_providers():
    """Brave / runway aren't LLMs — they shouldn't trigger seeding."""
    assert _pick_provider(["brave", "runway"]) is None


def test_pick_provider_returns_none_for_empty_list():
    assert _pick_provider([]) is None


# ── _default_catalog_for_provider ──────────────────────────────────────────


def test_default_catalog_for_anthropic_member_seeds_tier2_primary():
    """REGRESSION (post-#1765-revert): member bots seed with tier2
    (workhorse) primary, same as primary bots. Background work routes
    to tier3 via the trigger anchor (#1737/#1764) — flipping `primary`
    achieved no cost win on background turns AND silently degraded
    human-facing chat on member bots (Slack/Telegram users got tier3
    Haiku with no in-channel escalation path)."""
    primary, catalog, tiers = _default_catalog_for_provider(
        "anthropic", role="member",
    )
    assert primary == "anthropic/claude-sonnet-4-6", (
        f"member bots must seed with workhorse primary; got {primary!r}. "
        f"If reintroducing role-based primary dispatch, first solve the "
        f"chat-surface escalation problem (per-bot default-tier picker, "
        f"evo keyword) — see PR #1765 history."
    )
    model_ids = {m["id"] for m in catalog}
    assert "anthropic/claude-opus-4-7" in model_ids       # tier1
    assert "anthropic/claude-sonnet-4-6" in model_ids     # tier2
    assert "anthropic/claude-haiku-4-5" in model_ids      # tier3
    assert all(m["provider"] == "anthropic" for m in catalog)
    assert set(tiers.keys()) == {"tier1", "tier2", "tier3"}


def test_default_catalog_for_anthropic_primary_role_also_tier2():
    """Primary bots have always seeded with tier2 primary. No change."""
    primary, _catalog, _tiers = _default_catalog_for_provider(
        "anthropic", role="primary",
    )
    assert primary == "anthropic/claude-sonnet-4-6"


def test_default_catalog_role_defaults_to_tier2_primary():
    """Caller may omit role — the default is the workhorse-first
    (tier2 primary) seed, matching the corrected post-#1765-revert
    behavior across all role variants."""
    primary, _catalog, _tiers = _default_catalog_for_provider("anthropic")
    assert primary == "anthropic/claude-sonnet-4-6"


def test_default_catalog_dedupes_repeated_model_ids():
    """RECOMMENDED for openai reuses the same gpt-4o across tiers — the
    catalog must not list it multiple times."""
    primary, catalog, tiers = _default_catalog_for_provider("openai")
    model_ids = [m["id"] for m in catalog]
    assert len(model_ids) == len(set(model_ids)), "duplicate model ids in catalog"


def test_default_catalog_for_unknown_provider_returns_empty():
    primary, catalog, tiers = _default_catalog_for_provider("nonexistent")
    assert primary is None
    assert catalog == []
    assert tiers == {}


# ── seed_model_config_if_empty (the main entry point) ─────────────────────


def test_seeds_anthropic_catalog_when_bot_has_anthropic_key(network_path):
    """Atlas-style happy path: single-provider bot. The former tier0/judge
    seed is gone (judge-role collapse §5.4) — the seed writes the three
    ladder tiers only."""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic", "brave"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"], "tiers": {"tier2": {}}}, None)
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["ok"] is True
    assert result["seeded"] is True
    assert result["provider"] == "anthropic"
    # atlas seeds with tier2 (workhorse) primary regardless of role.
    # Post-#1765-revert: member bots no longer flip to tier3 primary —
    # background work still routes to tier3 via the trigger anchor
    # (PR #1737/#1764) + routing.backgroundTier, but human chat keeps
    # tier2 so Slack/Telegram users get workhorse-quality replies.
    assert result["primary"] == "anthropic/claude-sonnet-4-6"
    assert result["catalog_count"] >= 3
    # No judge fields in the result — the judge role is gone (judge-role
    # collapse §5.4); the second-provider recommendation lives on the AI
    # Optimization page, not in the seed result.
    assert "judge_provider" not in result
    assert "judge_model" not in result
    # Verify the write actually happened with the right shape
    m_set.assert_called_once()
    written_args = m_set.call_args
    updates = written_args[0][1] if len(written_args[0]) > 1 else written_args.kwargs.get("updates") or written_args[0][1]
    assert "catalog" in updates
    # #3566: the seed writes the rungs/roles shape, NOT the legacy tierN keys.
    # The internal tier selection still speaks tier0-tier3, but the payload is
    # run through migrate_evolve_tiers first, so a freshly provisioned bot is
    # born migrated. See test_seed_never_writes_legacy_tier_keys below.
    assert "tiers" not in updates
    assert "rungs" in updates and "roles" in updates
    _rungs = {r["id"]: r for r in updates["rungs"]}
    assert updates["roles"]["standard"] == "sonnet-class"
    assert "sonnet-class" in _rungs
    # No judge role and no judge-class rung anywhere in the payload — the
    # role was collapsed (design-judge-role-collapse-2026-08-21 §5.4).
    assert "judge" not in updates["roles"]
    assert "judge-class" not in _rungs


def test_catalog_sent_as_string_ids_to_oc(network_path):
    """REGRESSION: catalog payload to oc_full_config_set must be list[str].

    `_default_catalog_for_provider` returns list[dict] (each entry has
    {"id", "provider"} for our own UI/diagnostic use), but `oc_model.set_catalog`
    expects list[str] and uses each entry as a dict KEY (the catalog is
    stored as a `models: {<id>: {<meta>}}` dict in openclaw.json).

    Passing dicts here triggers "TypeError: unhashable type: 'dict'"
    inside oc_model.py, which used to surface as the maddeningly opaque
    'oc_full_config_set returned None (check oc_model.py logs)' — the
    logs in question live in the admin daemon stderr that operators
    can't read. This test pins the contract."""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"]}, None)
        seed_model_config_if_empty("atlas", network_path=network_path)

    written_args = m_set.call_args
    updates = written_args[0][1] if len(written_args[0]) > 1 else written_args.kwargs.get("updates") or written_args[0][1]
    sent_catalog = updates["catalog"]
    assert isinstance(sent_catalog, list) and len(sent_catalog) > 0, \
        "catalog payload must be a non-empty list"
    assert all(isinstance(x, str) for x in sent_catalog), (
        f"catalog payload MUST be list[str] of model IDs — got {sent_catalog!r}. "
        f"oc_model.set_catalog uses each entry as a dict key; passing dicts "
        f"trips 'unhashable type: dict' inside the oc_model.py subprocess."
    )
    # And each string should look like a model ID (provider/model form).
    assert all("/" in x for x in sent_catalog), (
        f"catalog model IDs should be in 'provider/model' form — got {sent_catalog!r}"
    )


def test_two_llm_keys_seed_has_no_judge_slot(network_path):
    """Bot has Anthropic + OpenAI keys → the seed still writes only the
    workhorse-provider ladder; there is no tier0/judge slot to fill (the
    judge role was collapsed — cross-vendor checking is the
    resolve_cross_vendor derivation over the tier chains at runtime)."""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic", "openai"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"], "tiers": {"tier2": {}}}, None)
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["seeded"] is True
    assert result["provider"] == "anthropic"
    assert "judge_provider" not in result

    written_args = m_set.call_args
    updates = written_args[0][1] if len(written_args[0]) > 1 else written_args.kwargs.get("updates") or written_args[0][1]
    assert "judge" not in (updates.get("roles") or {})
    assert all(r["id"] != "judge-class" for r in updates["rungs"])
    # Workhorse models still present.
    assert "anthropic/claude-sonnet-4-6" in updates["catalog"]


def test_skips_when_primary_already_set(network_path):
    """Idempotence guard: don't clobber a tuned bot.

    If the operator has manually set model.primary, seeding would
    overwrite their choice. The function must return seeded=False
    with a clear reason."""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas",
                             "primary": "anthropic/claude-opus-4-7",
                             "catalog": [{"id": "anthropic/claude-opus-4-7"}],
                             "tiers": {"tier1": {"models": ["anthropic/claude-opus-4-7"]}}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["ok"] is True
    assert result["seeded"] is False
    assert "already configured" in result["reason"]
    m_set.assert_not_called()


def test_seeds_when_primary_set_but_catalog_empty(network_path):
    """Edge case: primary is set but catalog has no entries. Still want
    to seed to give the operator the full catalog. (Primary alone
    without catalog membership is broken state — both must be set
    together for the AI Optimization UI to work.)"""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas",
                             "primary": "anthropic/claude-sonnet-4-6",
                             "catalog": [],
                             "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error",
               return_value=({"catalog": ["x"]}, None)) as m_set:
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["seeded"] is True
    m_set.assert_called_once()


def test_no_llm_provider_returns_seeded_false_with_clear_reason(network_path):
    """Bot has only brave key → can't seed (no LLM provider available).

    Error must be operator-meaningful:
      - names the BOT (what the operator clicked Seed for),
      - calls out that the present profiles are non-LLM (so the
        operator doesn't think "I added a key, why isn't it working"),
      - points at the UI surface where they fix it (Plugins → Credentials),
      - explicitly notes keys are per-bot so they don't assume a key
        configured for a different bot applies.
    """
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["brave"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["ok"] is True
    assert result["seeded"] is False
    reason = result["reason"]
    assert "no LLM provider" in reason
    assert "atlas" in reason, "error must name the bot the operator was seeding for"
    assert "brave" in reason, "error must surface which non-LLM providers ARE present"
    assert "non-LLM" in reason, "error must label them as non-LLM so the operator knows what changed"
    assert "Plugins" in reason and "Credentials" in reason, \
        "error must point at the UI surface, not a filepath"
    assert "per-bot" in reason, \
        "error must explain that keys are per-bot (so configuring a key for a different bot does not apply)"
    # Make sure we DID NOT regress to the old filepath-mentioning form.
    assert "auth-profiles.json" not in reason
    m_set.assert_not_called()


def test_no_llm_provider_with_no_profiles_skips_non_llm_note(network_path):
    """Bot has no profiles at all → still actionable error, but skip the
    'these are non-LLM' note (no profiles to mention)."""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=[]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["seeded"] is False
    reason = result["reason"]
    assert "atlas" in reason
    assert "Plugins" in reason and "Credentials" in reason
    # No non-LLM-providers list when there are none.
    assert "non-LLM" not in reason
    m_set.assert_not_called()


def test_oc_config_get_returns_none_is_soft_fail(network_path):
    """When we can't read the bot's openclaw.json, return ok=False
    with an explanatory reason — don't crash the caller."""
    with patch("oc_cli.oc_full_config_get", return_value=None), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["ok"] is False
    assert "could not read openclaw.json" in result["reason"]
    m_set.assert_not_called()


def test_oc_config_set_returns_none_is_soft_fail(network_path):
    """When the write subprocess fails, return ok=False — don't pretend
    we succeeded. AND surface the structured error message from oc_model.py
    so the operator sees the actual cause, not 'check the logs'."""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error",
               return_value=(None, "unhashable type: 'dict'")):
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["ok"] is False
    assert result["seeded"] is False
    # Real error message must reach the operator — not "check oc_model.py logs"
    # which point at a file they can't read.
    assert "unhashable type: 'dict'" in result["reason"], (
        f"error from oc_model.py must propagate up to operator-facing reason; "
        f"got: {result['reason']!r}"
    )
    # Bot name should be in the reason too (operator clicked Seed for a
    # specific bot — naming it helps disambiguate in a multi-bot pod).
    assert "atlas" in result["reason"]


def test_oc_config_set_no_error_message_falls_back_gracefully(network_path):
    """If oc_full_config_set_with_error returns (None, None) — failure with
    no extractable message — we still return a useful error, just point at
    the daemon log instead of pretending we have a message."""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error",
               return_value=(None, None)):
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["ok"] is False
    assert result["seeded"] is False
    assert "atlas" in result["reason"]
    assert "admin-ui.err.log" in result["reason"]


def test_preferred_provider_override_works(network_path):
    """When caller specifies --provider openai and the bot has both
    anthropic + openai keys, honor the operator's explicit choice
    even though anthropic is the natural preference."""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic", "openai"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error",
               return_value=({"catalog": ["x"]}, None)):
        result = seed_model_config_if_empty(
            "atlas", preferred_provider="openai", network_path=network_path,
        )

    assert result["seeded"] is True
    assert result["provider"] == "openai"
    assert result["primary"].startswith("openai/")


# ─────────────────────────────────────────────────────────────────────────────
# Cascade-by-default seed (Phase 3 cutover, post-shadow review)
# ─────────────────────────────────────────────────────────────────────────────


def test_seed_writes_cascade_enabled_true_when_absent(network_path):
    """Newly-provisioned bot with no cascade block on disk: seed writes
    cascade.enabled=true so the Phase 3 controller is live from day 1.

    Pre-fix: the seed only wrote {catalog, tiers}; cascade was left
    absent (false-by-default at read time). The cascade controller
    computed verdicts but never drove routing — bots that never had
    an operator explicitly turn cascade on missed out on dynamic
    struggle-based tier moves entirely."""
    with patch.object(provisioning, "_read_existing_cascade_enabled",
                      return_value=None), \
         patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"], "cascade": {"enabled": True}}, None)
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["seeded"] is True
    # Audit trail surfaces the cascade-seed for operator visibility
    # (the entry's details get persisted to the audit log).
    # We can't directly assert on the audit call shape here without a
    # mock, so we rely on the call-args inspection below.

    written_args = m_set.call_args
    updates = written_args[0][1] if len(written_args[0]) > 1 else written_args.kwargs.get("updates") or written_args[0][1]
    assert updates.get("cascade") == {"enabled": True}, (
        f"seed must inject cascade.enabled=true when bot has no cascade "
        f"block on disk; got updates={updates!r}"
    )


def test_seed_preserves_operator_cascade_false(network_path):
    """When the operator has explicitly set cascade.enabled=false on
    a bot (via PUT /api/admin/config/<bot>/cascade), a subsequent
    re-seed (CLI or web wizard) must NOT silently flip it back to
    true. The whole point of the conditional check is to make the
    operator's opt-out sticky."""
    with patch.object(provisioning, "_read_existing_cascade_enabled",
                      return_value=False), \
         patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"]}, None)
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["seeded"] is True
    written_args = m_set.call_args
    updates = written_args[0][1] if len(written_args[0]) > 1 else written_args.kwargs.get("updates") or written_args[0][1]
    assert "cascade" not in updates, (
        f"seed must NOT include cascade in the write when operator has "
        f"explicit setting on disk; got updates={updates!r}"
    )


def test_seed_preserves_operator_cascade_true(network_path):
    """When the operator already turned cascade on (idempotent re-seed
    case), the seed shouldn't re-write the same value redundantly —
    the merge semantics of oc_model.json_full_config_set would just
    overwrite with the same value, but skipping the include keeps the
    audit log honest about what the seed actually changed."""
    with patch.object(provisioning, "_read_existing_cascade_enabled",
                      return_value=True), \
         patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"]}, None)
        seed_model_config_if_empty("atlas", network_path=network_path)

    written_args = m_set.call_args
    updates = written_args[0][1] if len(written_args[0]) > 1 else written_args.kwargs.get("updates") or written_args[0][1]
    assert "cascade" not in updates, (
        f"seed should skip cascade in the write when operator already has "
        f"it set (even to the same value); got updates={updates!r}"
    )


def test_read_existing_cascade_enabled_returns_none_when_file_missing(network_path, tmp_path):
    """When evolve-tiers.json doesn't exist (brand-new bot), the helper
    returns None — telling the seed to inject the default-on value."""
    nonexistent_bot = "this-bot-does-not-exist-12345"
    result = provisioning._read_existing_cascade_enabled(
        nonexistent_bot, network_path,
    )
    assert result is None


def test_read_existing_cascade_enabled_distinguishes_absent_from_false(
    network_path, tmp_path, monkeypatch,
):
    """The helper must distinguish 'block absent' from 'block present
    with enabled=false' — the WHOLE POINT of the conditional seed.

    Pre-fix attempt: using oc_full_config_get for the read masked
    absence as enabled=False (the API's read-default), making the
    distinction impossible. This test pins the direct-file read so
    a future refactor doesn't regress to the masked API.
    """
    import json as _json

    fake_bot_home = tmp_path / "fake-bot" / ".openclaw"
    fake_bot_home.mkdir(parents=True)
    fake_tiers_path = fake_bot_home / "evolve-tiers.json"

    # Redirect the helper's home resolution at our tmp dir. The helper
    # resolves the bot's home via the blessed _bot_home seam (8.3 sweep) —
    # patch that, not Path() string matching.
    monkeypatch.setattr(
        provisioning, "_bot_home",
        lambda bot_id, config=None: tmp_path / bot_id,
    )

    # Case 1: file exists but no cascade block → None (absent)
    fake_tiers_path.write_text(_json.dumps({
        "tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}},
    }))
    assert provisioning._read_existing_cascade_enabled(
        "fake-bot", network_path,
    ) is None

    # Case 2: cascade block present with enabled=false → False
    fake_tiers_path.write_text(_json.dumps({
        "cascade": {"enabled": False},
    }))
    assert provisioning._read_existing_cascade_enabled(
        "fake-bot", network_path,
    ) is False

    # Case 3: cascade block present with enabled=true → True
    fake_tiers_path.write_text(_json.dumps({
        "cascade": {"enabled": True},
    }))
    assert provisioning._read_existing_cascade_enabled(
        "fake-bot", network_path,
    ) is True

    # Case 4: cascade block present but no enabled key → None (treat
    # as unset; seed will inject the default)
    fake_tiers_path.write_text(_json.dumps({
        "cascade": {"some_future_field": "x"},
    }))
    assert provisioning._read_existing_cascade_enabled(
        "fake-bot", network_path,
    ) is None

    # Case 5: malformed JSON → None (fail soft)
    fake_tiers_path.write_text("not valid json {{")
    assert provisioning._read_existing_cascade_enabled(
        "fake-bot", network_path,
    ) is None


# ── #3566: the seed must never mint the deprecated legacy tierN shape ─────────
# Root cause of gh#3566: `seed_model_config_if_empty` wrote a legacy
# `{"tiers": {"tier1": ...}}` payload, and oc_model's writer is deliberately
# shape-preserving (a legacy-shaped evolve-tiers.json stays legacy on every
# subsequent partial `tiers` write). `evolve-admin migrate-model-roles` has no
# automatic caller, so nothing ever converted those files. Net effect: every
# bot provisioned after the June 2026 fleet migration was born un-migrated and
# ran on ModelRouter's synthesize-at-load fallback forever — observed live on
# two bots on each of the two production pods.
#
# The fallback in ModelRouter.ts cannot be deleted while a writer still mints
# the shape it exists to absorb, so this contract is the gate on that removal.


@pytest.mark.parametrize("providers", [
    ["anthropic"],            # single provider
    ["anthropic", "openai"],  # two providers (no judge slot either way)
])
def test_seed_never_writes_legacy_tier_keys(network_path, providers):
    """REGRESSION (gh#3566): the seed payload must be rungs/roles, never tierN.

    Guards the writer, not the file: if this assertion is relaxed, newly
    provisioned bots are born on the deprecated shape again and the
    ModelRouter legacy fallback can never be removed.
    """
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=providers), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"]}, None)
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["seeded"] is True, result["reason"]
    updates = m_set.call_args[0][1]

    assert "tiers" not in updates, (
        f"seed wrote the deprecated legacy `tiers` key: {updates.get('tiers')!r}. "
        f"Newly provisioned bots must be born on the rungs/roles shape — a "
        f"legacy-shaped evolve-tiers.json is never converted automatically "
        f"(migrate-model-roles has no caller), so it stays deprecated forever "
        f"and pins ModelRouter's synthesize-at-load fallback in place (gh#3566)."
    )
    # No tierN key may appear anywhere in the payload, at any nesting depth.
    blob = json.dumps(updates)
    for tier_key in ("tier0", "tier1", "tier2", "tier3"):
        assert f'"{tier_key}"' not in blob, (
            f"legacy tier key {tier_key!r} leaked into the seed payload: {updates!r}"
        )

    assert isinstance(updates.get("rungs"), list) and updates["rungs"], \
        f"seed must write a non-empty rungs list; got {updates.get('rungs')!r}"
    assert isinstance(updates.get("roles"), dict) and updates["roles"], \
        f"seed must write a roles map; got {updates.get('roles')!r}"
    # Canonical rung ids only — synthetic `*-default` ids are what the
    # migrator's Addendum-8 §D reconcile exists to clean up; don't create more.
    for rung in updates["rungs"]:
        assert not rung["id"].endswith("-default"), (
            f"seed must use canonical rung ids, got {rung['id']!r}"
        )
        assert rung.get("costClass"), f"rung {rung['id']!r} missing costClass"


def test_seed_payload_matches_what_the_migrator_would_produce(network_path):
    """The seed's rungs/roles must be byte-identical to migrating its own
    legacy equivalent — that equality is what makes the #3566 conversion
    provably behaviour-preserving rather than a re-tuning of the defaults.
    """
    from evolve_admin.migrate_model_roles import migrate_evolve_tiers

    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic", "openai"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"]}, None)
        seed_model_config_if_empty("atlas", network_path=network_path)

    updates = m_set.call_args[0][1]

    # Rebuild the legacy dict the seed derived internally, then migrate it.
    # Every input is derived INDEPENDENTLY from the seed's own output. (No
    # tier0 anywhere — the judge slot is gone with the judge-role collapse.)
    _, catalog, legacy_tiers = provisioning._default_catalog_for_provider(
        "anthropic", role="member",
    )
    legacy_tiers = dict(legacy_tiers)
    expected, _ = migrate_evolve_tiers({"tiers": legacy_tiers})

    assert updates["rungs"] == expected["rungs"], (
        "seed rungs diverged from the migrator's output — the seed must land "
        "exactly what `migrate-model-roles --apply` would produce"
    )
    assert updates["roles"] == expected["roles"], (
        "seed roles diverged from the migrator's output"
    )
    assert catalog, "sanity: default catalog should be non-empty"


def test_seed_carries_pod_auto_upgrade_block_when_writing_rungs(tmp_path):
    """REGRESSION (gh#3566 review): writing rungs makes the bot Custom, and
    `model_auto_upgrade.bot_policy` does NOT inherit the pod's `enabled` for a
    Custom bot — it falls back to the code default (False). So the seed must
    carry the pod's auto-upgrade block, exactly as the "Customize this bot"
    route does (lifecycle rule 1, routes_admin_config.py).

    Without this, every newly provisioned bot silently stops riding the latest
    model version even though the pod is configured to.
    """
    pod_auto_upgrade = {"enabled": True, "applyDay": "tue", "requireGa": True}
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "port": 19000},
                 "atlas": {"role": "member", "port": 19031}},
        "members": ["evo", "atlas"],
        "models": {"autoUpgrade": pod_auto_upgrade},
    }))

    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"]}, None)
        result = seed_model_config_if_empty("atlas", network_path=net)

    assert result["seeded"] is True, result["reason"]
    updates = m_set.call_args[0][1]
    assert updates["rungs"], "sanity: the seed writes rungs (bot becomes Custom)"
    assert updates.get("autoUpgrade") == pod_auto_upgrade, (
        f"seed must carry the pod autoUpgrade block onto a bot it makes Custom; "
        f"got {updates.get('autoUpgrade')!r}. Without it bot_policy() resolves "
        f"enabled=False (code-default) and the bot silently stops auto-upgrading."
    )


def test_seed_omits_auto_upgrade_when_pod_has_no_block(network_path):
    """No pod auto-upgrade block → seed nothing, so the bot resolves to the
    code default exactly as the pod does. Mirrors lifecycle rule 1's
    "No pod block → seed nothing" arm; guards against writing an empty dict.
    """
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"]}, None)
        seed_model_config_if_empty("atlas", network_path=network_path)

    updates = m_set.call_args[0][1]
    assert "autoUpgrade" not in updates, (
        f"no pod autoUpgrade block → seed must not write one; "
        f"got {updates.get('autoUpgrade')!r}"
    )


def test_bot_policy_regression_the_auto_upgrade_seed_prevents():
    """Pins the mechanism the seed compensates for, independently of the seed.

    A Custom bot with no own autoUpgrade key resolves to enabled=False even
    when the pod is enabled=True; carrying the pod block forward restores it.
    If this ever stops being true, the seed's autoUpgrade write can be dropped.
    """
    from model_auto_upgrade import bot_policy, pod_policy

    pod = pod_policy({"models": {"autoUpgrade": {"enabled": True}}})
    assert pod.enabled is True

    # Custom bot, no own block → code default, NOT the pod's True.
    bare = bot_policy(pod, {"rungs": [{"id": "haiku-class"}]}, custom=True)
    assert bare.enabled is False and bare.enabled_source == "code-default"

    # Same bot with the pod block seeded in → follows the pod again.
    seeded = bot_policy(
        pod, {"rungs": [{"id": "haiku-class"}], "autoUpgrade": {"enabled": True}},
        custom=True,
    )
    assert seeded.enabled is True
