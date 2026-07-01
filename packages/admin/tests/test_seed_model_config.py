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
    _pick_judge_provider,
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
    """RECOMMENDED for openai uses the same gpt-4o for tier0/1/2 — the
    catalog must not list it 3 times."""
    primary, catalog, tiers = _default_catalog_for_provider("openai")
    model_ids = [m["id"] for m in catalog]
    assert len(model_ids) == len(set(model_ids)), "duplicate model ids in catalog"


def test_default_catalog_for_unknown_provider_returns_empty():
    primary, catalog, tiers = _default_catalog_for_provider("nonexistent")
    assert primary is None
    assert catalog == []
    assert tiers == {}


# ── _pick_judge_provider ──────────────────────────────────────────────────


def test_judge_picker_picks_second_provider_when_two_llms_present():
    """anthropic workhorse + openai available → openai judge (NOT degraded)."""
    p, m, degraded = _pick_judge_provider(
        ["anthropic", "openai"], workhorse_provider="anthropic",
    )
    assert p == "openai"
    # Should be the cheapest tier of openai (tier3 = gpt-4o-mini), not gpt-4o.
    assert m == "openai/gpt-4o-mini", (
        f"judge should be cheapest tier of second provider; got {m!r}"
    )
    assert degraded is False, "cross-provider judge is NOT degraded"


def test_judge_picker_skips_workhorse_even_when_first_in_preference():
    """If openai is the workhorse, we must NOT pick openai as judge —
    that's same-provider self-eval. Pick the next available LLM in
    preference order."""
    p, m, degraded = _pick_judge_provider(
        ["openai", "anthropic"], workhorse_provider="openai",
    )
    assert p == "anthropic"
    assert m == "anthropic/claude-haiku-4-5"
    assert degraded is False


def test_judge_picker_walks_preference_order():
    """anthropic workhorse, both openai + google available → openai wins."""
    p, m, degraded = _pick_judge_provider(
        ["anthropic", "google", "openai"], workhorse_provider="anthropic",
    )
    assert p == "openai", (
        f"judge picker should walk _LLM_PROVIDER_PREFERENCE; got {p!r}"
    )
    assert degraded is False


def test_judge_picker_degrades_to_same_provider_when_only_workhorse_present():
    """REGRESSION (Pod_admin feedback 2026-05-29): when no cross-provider LLM
    is available, the picker MUST return the workhorse provider's own
    cheap tier with degraded=True — better to have a sub-optimal judge
    that functions (and nags the operator) than no judge at all."""
    p, m, degraded = _pick_judge_provider(
        ["anthropic", "brave"], workhorse_provider="anthropic",
    )
    assert p == "anthropic", (
        f"single-provider should fall back to same-provider judge; got {p!r}"
    )
    assert m == "anthropic/claude-haiku-4-5", (
        f"judge should be cheapest tier of workhorse provider; got {m!r}"
    )
    assert degraded is True, (
        "same-provider judge MUST be flagged degraded so the operator "
        "sees a nag in the result reason — independent evaluation is "
        "compromised when judge == workhorse provider"
    )


def test_judge_picker_degrades_when_only_non_llm_providers_alongside_workhorse():
    """brave / runway in the auth list are non-LLM providers and shouldn't
    be picked as judge. Falls back to same-provider degraded mode."""
    p, m, degraded = _pick_judge_provider(
        ["anthropic", "brave", "runway"], workhorse_provider="anthropic",
    )
    assert p == "anthropic"
    assert degraded is True


def test_judge_picker_returns_triple_signature_consistently():
    """Every code path returns a 3-tuple — verify the signature contract
    so callers can safely destructure."""
    # Cross-provider path
    result = _pick_judge_provider(["anthropic", "openai"], workhorse_provider="anthropic")
    assert len(result) == 3
    # Degraded path
    result = _pick_judge_provider(["anthropic"], workhorse_provider="anthropic")
    assert len(result) == 3


# ── seed_model_config_if_empty (the main entry point) ─────────────────────


def test_seeds_anthropic_catalog_when_bot_has_anthropic_key(network_path):
    """Atlas-style happy path: single-provider bot. Post-#71-degraded:
    tier0 (judge) is now seeded with the same provider's cheap tier
    (degraded mode) so the judge tier FUNCTIONS rather than being
    silently unconfigured. Operator sees a nag in the result reason
    explaining independent evaluation is compromised."""
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
    # Single-provider: degraded same-provider judge — better than not
    # functioning. The operator nag in the result reason makes the
    # misconfig loud.
    assert result["judge_provider"] == "anthropic"
    assert result["judge_model"] == "anthropic/claude-haiku-4-5"
    assert result["judge_degraded"] is True
    # Reason must contain the DEGRADED marker so operators see the nag.
    reason = result["reason"]
    assert "DEGRADED" in reason, (
        f"reason must include DEGRADED marker for same-provider judge; got: {reason!r}"
    )
    assert "non-anthropic" in reason or "OpenAI" in reason or "Google" in reason, (
        f"reason should hint at adding a non-anthropic provider; got: {reason!r}"
    )
    # Verify the write actually happened with the right shape
    m_set.assert_called_once()
    written_args = m_set.call_args
    updates = written_args[0][1] if len(written_args[0]) > 1 else written_args.kwargs.get("updates") or written_args[0][1]
    assert "catalog" in updates
    assert "tiers" in updates
    assert "tier2" in updates["tiers"]
    # Post-#71-degraded: tier0 IS now seeded with the same-provider cheap
    # tier (rather than left unset). The original PR #1729 left it unset
    # to avoid a false sense of cross-model evaluation; Pod_admin's feedback
    # 2026-05-29 was that a functioning sub-optimal judge with operator
    # nag beats no judge at all. The nag lives in result["reason"]
    # (asserted above to include "DEGRADED").
    assert "tier0" in updates["tiers"]
    assert updates["tiers"]["tier0"] == {"models": ["anthropic/claude-haiku-4-5"]}


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


def test_seeds_cross_provider_tier0_when_bot_has_two_llm_keys(network_path):
    """Bot has Anthropic (workhorse) + OpenAI (judge) keys → seed should
    assign tier1/2/3 from Anthropic and tier0 from OpenAI's cheapest
    model. This is the design-correct cross-model judge: independent
    provider, low cost (tier3 grunt of the second provider)."""
    with patch.object(provisioning, "_read_auth_profile_providers",
                      return_value=["anthropic", "openai"]), \
         patch("oc_cli.oc_full_config_get",
               return_value={"bot": "atlas", "primary": None,
                             "catalog": [], "tiers": {}}), \
         patch("oc_cli.oc_full_config_set_with_error") as m_set:
        m_set.return_value = ({"catalog": ["x"], "tiers": {"tier0": {}, "tier2": {}}}, None)
        result = seed_model_config_if_empty("atlas", network_path=network_path)

    assert result["seeded"] is True
    assert result["provider"] == "anthropic"
    assert result["judge_provider"] == "openai"
    assert result["judge_model"] == "openai/gpt-4o-mini", (
        "judge model should be the cheapest tier of the second provider"
    )
    # Reason should announce the cross-model judge.
    reason = result["reason"]
    assert "tier0" in reason
    assert "openai" in reason
    assert "openai/gpt-4o-mini" in reason

    # And the actual write should include tier0 in tiers + the judge model
    # in the catalog (alongside the Anthropic workhorse models).
    written_args = m_set.call_args
    updates = written_args[0][1] if len(written_args[0]) > 1 else written_args.kwargs.get("updates") or written_args[0][1]
    assert "tier0" in updates["tiers"]
    assert updates["tiers"]["tier0"] == {"models": ["openai/gpt-4o-mini"]}
    assert "openai/gpt-4o-mini" in updates["catalog"], (
        "judge model must be added to the catalog too — otherwise the tier "
        "map references a model that's not declared in agents.defaults.models"
    )
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
