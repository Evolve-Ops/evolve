"""tests/test_model_registry.py — pure-Python tests for model_registry.

Covers:
  - RECOMMENDED dict shape (M1)
  - check_bot_freshness against synthetic configs (M2)
  - Provider-key gating (no advisory for providers without keys) (D3 logic)
  - Last-check persistence round trip
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from model_registry import (  # noqa: E402
    RECOMMENDED,
    ModelAdvisory,
    ProviderDiversityAdvisory,
    check_bot_diversity,
    check_bot_freshness,
    check_pod_freshness,
    dismiss_advisory,
    is_dismissed,
    load_dismissals,
    load_last_check,
    providers_with_recommendations,
    recommendation_for,
    reset_dismissals,
    save_last_check,
)


# ── Registry shape ───────────────────────────────────────────────────────────


def test_recommended_covers_four_providers():
    """Spec calls out anthropic / openai / google / xai as the providers we know about."""
    assert set(RECOMMENDED.keys()) >= {"anthropic", "openai", "google", "xai"}


def test_each_recommendation_has_model_and_release_date():
    for provider, tiers in RECOMMENDED.items():
        for tier_id, entry in tiers.items():
            assert "model" in entry, f"{provider}/{tier_id} missing model"
            assert "released" in entry, f"{provider}/{tier_id} missing released"
            assert "/" in entry["model"], (
                f"{provider}/{tier_id} model {entry['model']!r} should be 'provider/name'"
            )
            assert entry["model"].split("/", 1)[0] == provider, (
                f"{provider}/{tier_id} model prefix doesn't match provider"
            )


def test_providers_with_recommendations_is_sorted():
    assert providers_with_recommendations() == sorted(RECOMMENDED.keys())


def test_recommendation_for_returns_none_for_gap():
    # anthropic intentionally has no tier0 (it's the workhorse — judge is openai)
    assert recommendation_for("anthropic", "tier0") is None
    # openai/tier3 exists
    assert recommendation_for("openai", "tier3") is not None


# ── check_bot_freshness ──────────────────────────────────────────────────────


def _all_recommended_tiers(provider: str) -> dict:
    """Helper: build a tier dict that exactly matches RECOMMENDED for one provider."""
    return {
        tier_id: {"models": [entry["model"]]}
        for tier_id, entry in RECOMMENDED[provider].items()
    }


def test_no_advisories_when_all_tiers_match_recommendation():
    bot_tiers = _all_recommended_tiers("anthropic")
    advisories = check_bot_freshness("team_bot_a", bot_tiers, {"anthropic"})
    assert advisories == []


def test_stale_tier_produces_advisory():
    bot_tiers = _all_recommended_tiers("anthropic")
    bot_tiers["tier2"]["models"] = ["anthropic/claude-sonnet-4-2"]  # stale
    advisories = check_bot_freshness("team_bot_a", bot_tiers, {"anthropic"})
    stale_t2 = [a for a in advisories if a.tier == "tier2"]
    assert len(stale_t2) == 1
    a = stale_t2[0]
    assert a.bot_id == "team_bot_a"
    assert a.provider == "anthropic"
    assert a.current_model == "anthropic/claude-sonnet-4-2"
    assert a.recommended_model == RECOMMENDED["anthropic"]["tier2"]["model"]
    assert a.is_stale is True


def test_provider_without_key_produces_no_advisory():
    """Even though anthropic/tier2 is misconfigured, we don't advise because no key."""
    bot_tiers = {"tier2": {"models": ["anthropic/claude-sonnet-4-2"]}}
    advisories = check_bot_freshness("team_bot_a", bot_tiers, set())  # no providers
    assert advisories == []


def test_empty_bot_tiers_produce_advisories_for_each_recommended_tier():
    """Bots with no tier config at all (e.g. Team_bot_c) must still get advisories so
    the user can populate tiers via the Update button. Under the prior
    iteration-over-bot's-tiers behavior these bots silently got zero advisories."""
    advisories = check_bot_freshness("team_bot_c", {}, {"anthropic"})
    # anthropic has tier1, tier2, tier3 in RECOMMENDED → 3 advisories, all current=None
    tiers_advised = {a.tier for a in advisories}
    assert tiers_advised == set(RECOMMENDED["anthropic"].keys())
    assert all(a.current_model is None for a in advisories)
    assert all(a.is_stale for a in advisories)


def test_partial_tier_coverage_advises_only_missing_tiers():
    """Bot has tier2 set correctly but tier1 and tier3 missing — advise the missing ones."""
    bot_tiers = {"tier2": {"models": [RECOMMENDED["anthropic"]["tier2"]["model"]]}}
    advisories = check_bot_freshness("team_bot_a", bot_tiers, {"anthropic"})
    tiers_advised = {a.tier for a in advisories}
    assert "tier2" not in tiers_advised
    # tier1 and tier3 still need attention
    assert {"tier1", "tier3"}.issubset(tiers_advised)


def test_advisory_when_tier_missing_one_provider_entirely():
    """anthropic key present, tier2 has only an openai model — anthropic side advises."""
    # Use the current openai recommendation directly so this test stays
    # robust to RECOMMENDED.openai updates (e.g., the 2026-06-11 correction
    # to gpt-4.1 after live /v1/models verification).
    current_openai_t2 = RECOMMENDED["openai"]["tier2"]["model"]
    bot_tiers = {"tier2": {"models": [current_openai_t2]}}
    advisories = check_bot_freshness("team_bot_a", bot_tiers, {"anthropic", "openai"})
    anthropic_t2 = [a for a in advisories if a.provider == "anthropic" and a.tier == "tier2"]
    openai_t2 = [a for a in advisories if a.provider == "openai" and a.tier == "tier2"]
    assert len(anthropic_t2) == 1
    assert anthropic_t2[0].current_model is None
    # openai/tier2 now matches the recommendation, so no openai advisory at tier2
    assert openai_t2 == []


def test_no_advisory_for_provider_tier_pair_not_in_registry():
    """anthropic has no tier0 entry in RECOMMENDED — must not advise even if bot has anthropic key."""
    bot_tiers = _all_recommended_tiers("anthropic")
    advisories = check_bot_freshness("team_bot_a", bot_tiers, {"anthropic"})
    tier0_advisories = [a for a in advisories if a.tier == "tier0"]
    assert tier0_advisories == []


def test_check_pod_freshness_aggregates_across_bots():
    """Both bots are evaluated against the registry; advisories carry the right bot_id."""
    bot_configs = {
        "team_bot_a": {"tiers": _all_recommended_tiers("anthropic")},
        "admin_bot": {"tiers": {}},  # empty — should produce advisories
    }
    advisories = check_pod_freshness(bot_configs, {"anthropic"})
    bots_advised = {a.bot_id for a in advisories}
    assert "team_bot_a" not in bots_advised  # team_bot_a fully covered
    assert "admin_bot" in bots_advised   # admin_bot has nothing


def test_case_insensitive_model_comparison():
    """Don't generate spurious advisories from casing differences."""
    bot_tiers = _all_recommended_tiers("anthropic")
    bot_tiers["tier2"]["models"] = [RECOMMENDED["anthropic"]["tier2"]["model"].upper()]
    advisories = check_bot_freshness("team_bot_a", bot_tiers, {"anthropic"})
    tier2_advisories = [a for a in advisories if a.tier == "tier2"]
    assert tier2_advisories == []


# ── Last-check persistence ───────────────────────────────────────────────────


def test_load_last_check_returns_empty_when_no_file(tmp_path):
    assert load_last_check(tmp_path) == {}


def test_save_and_load_last_check_roundtrip(tmp_path):
    advisories = [
        ModelAdvisory(
            bot_id="team_bot_a",
            tier="tier2",
            provider="anthropic",
            current_model="anthropic/claude-sonnet-4-2",
            recommended_model="anthropic/claude-sonnet-4-6",
            recommended_released="2026-04-15",
            is_stale=True,
        ),
    ]
    summary = save_last_check(tmp_path, advisories, {"anthropic", "openai"})
    assert summary["advisory_count"] == 1
    assert summary["providers_checked"] == ["anthropic", "openai"]
    assert "checked_at" in summary

    loaded = load_last_check(tmp_path)
    assert loaded["advisory_count"] == 1
    assert loaded["advisories"][0]["bot_id"] == "team_bot_a"
    # Verify the file is at the expected path inside shared_dir
    assert (tmp_path / "model-freshness" / "last-check.json").exists()


def test_save_last_check_overwrites_previous(tmp_path):
    save_last_check(tmp_path, [], {"anthropic"})
    first = load_last_check(tmp_path)
    save_last_check(tmp_path, [], {"openai"})
    second = load_last_check(tmp_path)
    assert first["providers_checked"] == ["anthropic"]
    assert second["providers_checked"] == ["openai"]


# ── check_bot_diversity ──────────────────────────────────────────────────────


def test_diversity_advisory_fires_for_single_provider_bot():
    """One LLM provider credentialed → soft advisory with the other registry
    providers as suggestions and both reasons (fallback + judge)."""
    adv = check_bot_diversity("evo", {"anthropic"})
    assert adv is not None
    assert adv.bot_id == "evo"
    assert adv.current_providers == ["anthropic"]
    # Suggestions are every other registry provider, sorted.
    assert "anthropic" not in adv.suggested_providers
    assert set(adv.suggested_providers) == set(RECOMMENDED.keys()) - {"anthropic"}
    assert sorted(adv.reasons) == ["fallback", "judge"]


def test_diversity_advisory_none_for_two_providers():
    assert check_bot_diversity("evo", {"anthropic", "openai"}) is None


def test_diversity_advisory_none_for_zero_providers():
    """A bot with no LLM credentials has a bigger problem (every tier is empty);
    the existing freshness check surfaces those. The diversity advisory only
    fires when exactly one provider is credentialed."""
    assert check_bot_diversity("evo", set()) is None


def test_diversity_advisory_ignores_non_llm_credentials():
    """Non-LLM credentials (telegram, brave, slack) don't count toward diversity —
    only providers in the RECOMMENDED registry are considered."""
    adv = check_bot_diversity("evo", {"anthropic", "telegram", "brave"})
    assert adv is not None
    assert adv.current_providers == ["anthropic"]


def test_diversity_advisory_to_dict_roundtrip():
    adv = ProviderDiversityAdvisory(
        bot_id="evo",
        current_providers=["anthropic"],
        suggested_providers=["openai", "google", "xai"],
        reasons=["fallback", "judge"],
    )
    d = adv.to_dict()
    assert d["bot_id"] == "evo"
    assert d["current_providers"] == ["anthropic"]
    assert d["reasons"] == ["fallback", "judge"]


# ── Dismissals ───────────────────────────────────────────────────────────────


def test_load_dismissals_empty_when_no_file(tmp_path):
    assert load_dismissals(tmp_path) == {}


def test_dismiss_then_is_dismissed(tmp_path):
    dismiss_advisory(tmp_path, "diversity", "evo")
    assert is_dismissed(tmp_path, "diversity", "evo") is True
    assert is_dismissed(tmp_path, "diversity", "team-bot-a") is False


def test_dismiss_persists_to_disk(tmp_path):
    dismiss_advisory(tmp_path, "diversity", "evo")
    data = load_dismissals(tmp_path)
    assert "diversity" in data
    assert "evo" in data["diversity"]
    # Timestamp shape: ISO-ish.
    assert isinstance(data["diversity"]["evo"], str)
    assert "T" in data["diversity"]["evo"]


def test_dismiss_overwrites_timestamp_on_redismissal(tmp_path):
    """Dismissing the same key twice updates the timestamp, doesn't duplicate."""
    dismiss_advisory(tmp_path, "diversity", "evo")
    first = load_dismissals(tmp_path)["diversity"]["evo"]
    # Force a different second — the storage layer must not error out on duplicate.
    dismiss_advisory(tmp_path, "diversity", "evo")
    second = load_dismissals(tmp_path)["diversity"]["evo"]
    # Just verify it's still a single entry.
    assert isinstance(second, str)
    assert len(load_dismissals(tmp_path)["diversity"]) == 1


def test_reset_dismissals_for_one_type(tmp_path):
    dismiss_advisory(tmp_path, "diversity", "evo")
    dismiss_advisory(tmp_path, "diversity", "team-bot-a")
    reset_dismissals(tmp_path, "diversity")
    data = load_dismissals(tmp_path)
    assert data.get("diversity", {}) == {}


def test_reset_dismissals_all(tmp_path):
    dismiss_advisory(tmp_path, "diversity", "evo")
    reset_dismissals(tmp_path, None)
    assert load_dismissals(tmp_path) == {}


# ── save_last_check now persists diversity_advisories ───────────────────────


def test_save_last_check_persists_diversity_advisories(tmp_path):
    advisory = ProviderDiversityAdvisory(
        bot_id="evo",
        current_providers=["anthropic"],
        suggested_providers=["openai", "google", "xai"],
        reasons=["fallback", "judge"],
    )
    summary = save_last_check(
        tmp_path, [], {"anthropic"},
        diversity_advisories=[advisory],
    )
    assert summary["diversity_advisories"][0]["bot_id"] == "evo"
    # And on reload
    loaded = load_last_check(tmp_path)
    assert loaded["diversity_advisories"][0]["bot_id"] == "evo"


def test_save_last_check_back_compat_no_diversity_kwarg(tmp_path):
    """Existing call sites that don't pass diversity_advisories still work; the
    summary has an empty list rather than missing the key."""
    summary = save_last_check(tmp_path, [], {"anthropic"})
    assert summary["diversity_advisories"] == []
