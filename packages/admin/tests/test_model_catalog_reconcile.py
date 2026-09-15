"""tests/test_model_catalog_reconcile.py — catalog-tier reconcile unit tests.

The reconcile module fixes the runtime correctness bug discovered when
Pod_admin noticed team_bot_a's catalog had only 4 Anthropic models, while its
tiers referenced google/xai/openai models. OpenClaw uses the catalog
as the runtime whitelist — tier entries naming non-catalog models are
silently dropped, so those tier slots effectively don't exist.

Coverage:
- `reconcile_catalog` adds tier-referenced models to catalog (invariant 1)
- Tolerates string vs dict catalog entry shapes
- `add_recommended_for_credentialed=True` adds RECOMMENDED models too
- `unchanged=True` when no additions needed (idempotence)
- `find_catalog_drift` returns tier_member_missing findings
- `find_catalog_drift` returns recommended_missing findings
- Drift findings flag `is_recommended` correctly
- Empty / malformed inputs handled gracefully
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.model_catalog import (  # noqa: E402
    ReconcileResult,
    find_catalog_drift,
    reconcile_catalog,
    scope_credentialed_to_bot,
)


# ── scope_credentialed_to_bot — per-bot picker credential scoping ───────────
#
# The per-bot catalog picker / free-text validation must offer only providers
# the bot can run; fail-open to the pod-wide union when the bot is unknown.


def _pod_set(*provs):
    def _fn():
        return set(provs)
    return _fn


def test_scope_returns_bot_set_when_creds_read():
    def bot(b):
        return {"anthropic"} if b == "team-bot-a" else set()
    pod = _pod_set("anthropic", "openai", "xai")
    assert scope_credentialed_to_bot("team-bot-a", bot, pod) == {"anthropic"}


def test_scope_falls_open_to_pod_when_no_bot_id():
    def bot(_b):
        return {"anthropic"}
    pod = _pod_set("anthropic", "openai")
    assert scope_credentialed_to_bot("", bot, pod) == {"anthropic", "openai"}


def test_scope_falls_open_when_bot_has_no_creds():
    # An empty bot set must NOT blank the picker — fall open to the pod union.
    def bot(_b):
        return set()
    pod = _pod_set("anthropic", "openai")
    assert scope_credentialed_to_bot("team-bot-a", bot, pod) == {"anthropic", "openai"}


def test_scope_falls_open_when_bot_creds_unreadable():
    # A read failure (e.g. unreadable auth-profiles) fails open, never raises.
    def boom(_b):
        raise OSError("permission denied")
    pod = _pod_set("anthropic")
    assert scope_credentialed_to_bot("team-bot-a", boom, pod) == {"anthropic"}


# ── reconcile_catalog — invariant 1 (tier members → catalog) ────────────────


def test_team_bot_a_shape_reconciles_correctly():
    """The exact situation Pod_admin saw: anthropic-only catalog, mixed-
    provider tiers. Reconcile must add the missing models."""
    catalog = [
        {"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"},
        {"id": "anthropic/claude-opus-4-6", "provider": "anthropic"},
        {"id": "anthropic/claude-haiku-4-5", "provider": "anthropic"},
        {"id": "anthropic/claude-sonnet-4-5", "provider": "anthropic"},
    ]
    tiers = {
        "tier1": {"models": ["anthropic/claude-opus-4-7", "openai/gpt-4o",
                              "google/gemini-2.5-pro", "xai/grok-4"]},
        "tier2": {"models": ["anthropic/claude-sonnet-4-6", "google/gemini-2.5-pro",
                              "xai/grok-4", "openai/gpt-4o"]},
        "tier3": {"models": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini",
                              "google/gemini-2.0-flash", "xai/grok-4-mini"]},
    }
    result = reconcile_catalog(catalog, tiers)
    assert isinstance(result, ReconcileResult)
    assert result.unchanged is False
    added_ids = set(result.added_from_tiers)
    # All 6 cross-provider tier models that weren't in catalog
    expected = {
        "anthropic/claude-opus-4-7",   # in tier1, not in original catalog
        "openai/gpt-4o",
        "google/gemini-2.5-pro",
        "xai/grok-4",
        "openai/gpt-4o-mini",
        "google/gemini-2.0-flash",
        "xai/grok-4-mini",
    }
    assert added_ids == expected
    # Catalog count should now be 4 original + 7 added = 11
    catalog_ids = {e.get("id") if isinstance(e, dict) else e
                    for e in result.new_catalog}
    assert "openai/gpt-4o" in catalog_ids
    assert "google/gemini-2.0-flash" in catalog_ids
    # Provider derived from model_id
    new_entries = {e["id"]: e for e in result.new_catalog
                   if isinstance(e, dict) and e["id"] in expected}
    assert new_entries["openai/gpt-4o"]["provider"] == "openai"


def test_credentialed_filter_skips_uncredentialed_providers():
    """REGRESSION: when the bot has only an Anthropic key, reconcile
    should NOT auto-add openai/google/xai models to the catalog —
    adding them without an API key would promote a runtime-graceful
    "silent drop" into a runtime-fatal "no API key for openai" error
    (the same trap Seed defaults was built to avoid). The skipped
    models must be reported via skipped_uncredentialed so the UI
    can explain "we added X, left Y as silent drops"."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    tiers = {
        "tier1": {"models": ["anthropic/claude-opus-4-7", "openai/gpt-4o",
                              "google/gemini-2.5-pro"]},
        "tier3": {"models": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"]},
    }
    # Bot has only Anthropic auth-profile entry
    result = reconcile_catalog(
        catalog, tiers, credentialed_providers={"anthropic"},
    )

    # ONLY Anthropic models added
    added = set(result.added_from_tiers)
    assert added == {
        "anthropic/claude-opus-4-7",
        "anthropic/claude-haiku-4-5",
    }, f"only Anthropic models should be auto-added; got {added}"

    # The uncredentialed providers' models are reported as skipped
    skipped = set(result.skipped_uncredentialed)
    assert skipped == {
        "openai/gpt-4o",
        "google/gemini-2.5-pro",
        "openai/gpt-4o-mini",
    }, f"non-anthropic models should be skipped; got {skipped}"

    # Verify the catalog itself: only anthropic models present
    catalog_ids = {
        e.get("id") if isinstance(e, dict) else e
        for e in result.new_catalog
    }
    assert "openai/gpt-4o" not in catalog_ids
    assert "google/gemini-2.5-pro" not in catalog_ids
    assert "anthropic/claude-opus-4-7" in catalog_ids


def test_credentialed_filter_adds_models_for_each_provider_bot_has_keys_for():
    """Multi-provider bot (Anthropic + OpenAI) should get both providers'
    tier-referenced models added to the catalog."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    tiers = {
        "tier1": {"models": ["anthropic/claude-opus-4-7", "openai/gpt-4o",
                              "google/gemini-2.5-pro"]},
    }
    result = reconcile_catalog(
        catalog, tiers, credentialed_providers={"anthropic", "openai"},
    )

    added = set(result.added_from_tiers)
    assert "anthropic/claude-opus-4-7" in added
    assert "openai/gpt-4o" in added
    # Google still skipped — no Google key
    assert "google/gemini-2.5-pro" not in added
    assert "google/gemini-2.5-pro" in result.skipped_uncredentialed


def test_credentialed_filter_none_preserves_legacy_behavior():
    """When credentialed_providers is None (legacy callers), reconcile
    adds every tier-referenced model regardless of provider. Important
    for back-compat with any non-credentials-aware caller (CLI, tests,
    arbiter appliers)."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    tiers = {
        "tier1": {"models": ["openai/gpt-4o", "google/gemini-2.5-pro"]},
    }
    result = reconcile_catalog(catalog, tiers)  # no credentialed_providers
    added = set(result.added_from_tiers)
    assert "openai/gpt-4o" in added
    assert "google/gemini-2.5-pro" in added
    # No skips reported when filter isn't active
    assert result.skipped_uncredentialed == []


def test_unchanged_when_catalog_already_has_everything():
    """Idempotence — no diffs when catalog is a superset of tier members."""
    catalog = [
        {"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"},
        {"id": "anthropic/claude-haiku-4-5", "provider": "anthropic"},
    ]
    tiers = {
        "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
    }
    result = reconcile_catalog(catalog, tiers)
    assert result.unchanged is True
    assert result.added_from_tiers == []


def test_tolerates_string_catalog_entries():
    """Some legacy writes used plain strings instead of {id, provider}.
    Reconcile must accept either shape."""
    catalog = ["anthropic/claude-sonnet-4-6", "anthropic/claude-haiku-4-5"]
    tiers = {"tier2": {"models": ["openai/gpt-4o"]}}
    result = reconcile_catalog(catalog, tiers)
    assert result.unchanged is False
    assert "openai/gpt-4o" in result.added_from_tiers
    # The normalized result is dicts, not strings
    new_ids = {e.get("id") for e in result.new_catalog if isinstance(e, dict)}
    assert new_ids == {"anthropic/claude-sonnet-4-6",
                       "anthropic/claude-haiku-4-5", "openai/gpt-4o"}


def test_tolerates_dict_with_model_field_instead_of_id():
    """Some entries use {model: "..."} instead of {id: "..."}."""
    catalog = [{"model": "anthropic/claude-sonnet-4-6"}]
    tiers = {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}}
    result = reconcile_catalog(catalog, tiers)
    assert result.unchanged is True


def test_handles_empty_catalog_and_tiers():
    """Empty inputs don't crash; result is unchanged + empty."""
    result = reconcile_catalog([], {})
    assert result.unchanged is True
    assert result.new_catalog == []


def test_handles_tier_with_no_models_key():
    """Defensive: tier entry shape variance."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    tiers = {"tier1": {}, "tier2": {"models": []}}
    result = reconcile_catalog(catalog, tiers)
    assert result.unchanged is True


# ── reconcile_catalog — invariant 2 (RECOMMENDED for credentialed) ──────────


def test_add_recommended_for_credentialed_providers():
    """When `add_recommended_for_credentialed=True` and the bot has
    anthropic + google keys, RECOMMENDED tier1/2/3 entries for both
    are added (if not already in catalog)."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    result = reconcile_catalog(
        catalog, {},
        credentialed_providers={"anthropic", "google"},
        add_recommended_for_credentialed=True,
    )
    # Anthropic missing tier1/tier3; google missing tier1/tier2/tier3
    new_ids = {e.get("id") for e in result.new_catalog}
    assert "anthropic/claude-opus-4-7" in new_ids
    assert "anthropic/claude-haiku-4-5" in new_ids
    assert "google/gemini-2.5-pro" in new_ids


def test_add_recommended_off_by_default():
    """Don't make catalog additions unless explicitly opted in. The hot
    path (api_admin_config_set_tiers) sets this False so saving tiers
    doesn't accidentally add a bunch of recommended models the operator
    didn't ask for."""
    catalog = []
    result = reconcile_catalog(
        catalog, {},
        credentialed_providers={"anthropic", "openai"},
    )
    assert result.added_from_recommended == []
    assert result.new_catalog == []


# ── reconcile_catalog — invariant 3 (role-resolved → catalog) ───────────────
#
# The fix-half of find_catalog_drift's Type-3 (role_resolves_outside_catalog).
# Without this, "Reconcile catalog" walks tier members only and never adds the
# role-resolved DEFAULT model (max → claude-fable-5) the bot's tiers never
# name — so the drift banner persists and the Max pull keeps dying at OC.


def test_reconcile_adds_role_resolved_default_missing_from_catalog():
    """The headline case: max resolves (via code defaults) to claude-fable-5,
    which no tier names and the catalog lacks. reconcile must add it so the
    role_resolves_outside_catalog drift clears and the Max pull survives OC."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    # The bot's tiers doc never names the max default — only standard's model.
    tiers = {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}}
    resolved = {
        "standard": "anthropic/claude-sonnet-4-6",   # already in catalog
        "max": "anthropic/claude-fable-5",           # role-resolved default
    }
    result = reconcile_catalog(
        catalog, tiers,
        credentialed_providers={"anthropic"},
        resolved_role_models=resolved,
    )
    assert result.unchanged is False
    # The tier-walk adds nothing (standard's model already present); the role
    # pass adds the max default.
    assert result.added_from_tiers == []
    assert result.added_from_roles == ["anthropic/claude-fable-5"]
    new_ids = {e.get("id") for e in result.new_catalog if isinstance(e, dict)}
    assert "anthropic/claude-fable-5" in new_ids

    # And the drift detector that flagged it now sees no finding — detect and
    # fix share one source of truth, so reconcile actually clears the banner.
    findings = find_catalog_drift(
        "team_bot_a", result.new_catalog, tiers,
        {"anthropic"}, resolved_role_models=resolved,
    )
    assert [
        f for f in findings if f.kind == "role_resolves_outside_catalog"
    ] == []


def test_reconcile_role_resolved_respects_credentialed_filter():
    """A role resolving to an UNCREDENTIALED provider's model is skipped, not
    silently promoted to a fatal 'no API key' — same guard as the tier-walk."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    resolved = {"power": "openai/gpt-4o"}  # bot has no OpenAI key
    result = reconcile_catalog(
        catalog, {},
        credentialed_providers={"anthropic"},
        resolved_role_models=resolved,
    )
    assert result.added_from_roles == []
    assert "openai/gpt-4o" in result.skipped_uncredentialed
    new_ids = {e.get("id") for e in result.new_catalog if isinstance(e, dict)}
    assert "openai/gpt-4o" not in new_ids


def test_reconcile_role_resolved_unchanged_when_already_in_catalog():
    """Idempotence — a role whose model is already in catalog adds nothing."""
    catalog = [{"id": "anthropic/claude-fable-5", "provider": "anthropic"}]
    resolved = {"max": "anthropic/claude-fable-5"}
    result = reconcile_catalog(
        catalog, {},
        credentialed_providers={"anthropic"},
        resolved_role_models=resolved,
    )
    assert result.unchanged is True
    assert result.added_from_roles == []


def test_reconcile_role_resolved_not_duplicated_by_tier_member():
    """When a model is BOTH tier-named and role-resolved (and missing), the
    tier-walk adds it first; the role pass sees it already in catalog and
    doesn't double-add."""
    catalog: list = []
    tiers = {"tier2": {"models": ["anthropic/claude-opus-4-7"]}}
    resolved = {"power": "anthropic/claude-opus-4-7"}
    result = reconcile_catalog(
        catalog, tiers,
        credentialed_providers={"anthropic"},
        resolved_role_models=resolved,
    )
    assert result.added_from_tiers == ["anthropic/claude-opus-4-7"]
    assert result.added_from_roles == []  # already added by the tier-walk


# ── find_catalog_drift ──────────────────────────────────────────────────────


def test_find_catalog_drift_flags_tier_members_not_in_catalog():
    """The team_bot_a case: tier names a model not in catalog → finding."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    tiers = {"tier2": {"models": ["anthropic/claude-sonnet-4-6", "openai/gpt-4o"]}}
    findings = find_catalog_drift("team_bot_a", catalog, tiers)
    tier_member_findings = [f for f in findings if f.kind == "tier_member_missing"]
    assert len(tier_member_findings) == 1
    assert tier_member_findings[0].model_id == "openai/gpt-4o"
    assert tier_member_findings[0].tier == "tier2"
    assert tier_member_findings[0].provider == "openai"
    assert tier_member_findings[0].bot_id == "team_bot_a"
    # openai/gpt-4o IS the RECOMMENDED for openai tier2, so the finding
    # should flag is_recommended=True (helps UI distinguish "operator
    # wanted this but it's misconfigured" from "off-registry model").
    assert tier_member_findings[0].is_recommended is True


def test_find_catalog_drift_doesnt_flag_present_models():
    """Models that ARE in catalog produce no drift finding."""
    catalog = [{"id": "openai/gpt-4o", "provider": "openai"}]
    tiers = {"tier2": {"models": ["openai/gpt-4o"]}}
    findings = find_catalog_drift("team_bot_a", catalog, tiers)
    tier_findings = [f for f in findings if f.kind == "tier_member_missing"]
    assert tier_findings == []


def test_find_catalog_drift_flags_off_registry_models_as_not_recommended():
    """A bot may use a model that isn't in RECOMMENDED (e.g. a beta
    preview). Still surface as drift, but mark is_recommended=False
    so the UI shows it differently from a misconfigured-but-recommended
    case."""
    catalog = []
    tiers = {"tier2": {"models": ["anthropic/claude-mystery-7-9"]}}
    findings = find_catalog_drift("team_bot_a", catalog, tiers)
    tier_findings = [f for f in findings if f.kind == "tier_member_missing"]
    assert len(tier_findings) == 1
    assert tier_findings[0].is_recommended is False


def test_find_catalog_drift_flags_recommended_missing_for_credentialed():
    """Provider credentialed but RECOMMENDED's tier1/2/3 not in catalog
    → "recommended_missing" finding per missing tier. Helps operators
    discover providers they could be using but aren't."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    findings = find_catalog_drift(
        "team_bot_a", catalog, {},
        credentialed_providers={"google", "anthropic"},
    )
    rec_missing = [f for f in findings if f.kind == "recommended_missing"]
    # google has 3 tier RECOMMENDEDs (tier1/2/3 all gemini-2.5-pro+2.0-flash);
    # anthropic has 3 too but sonnet is already in catalog
    google_ids = {f.model_id for f in rec_missing if f.provider == "google"}
    anthropic_ids = {f.model_id for f in rec_missing if f.provider == "anthropic"}
    assert "google/gemini-2.5-pro" in google_ids
    assert "google/gemini-2.0-flash" in google_ids
    assert "anthropic/claude-sonnet-4-6" not in anthropic_ids  # already in catalog
    assert "anthropic/claude-opus-4-7" in anthropic_ids
    assert "anthropic/claude-haiku-4-5" in anthropic_ids


def test_find_catalog_drift_empty_when_no_drift():
    """Sync state — no findings."""
    catalog = [
        {"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"},
        {"id": "anthropic/claude-opus-4-7", "provider": "anthropic"},
        {"id": "anthropic/claude-haiku-4-5", "provider": "anthropic"},
    ]
    tiers = {
        "tier1": {"models": ["anthropic/claude-opus-4-7"]},
        "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
    }
    findings = find_catalog_drift("team_bot_a", catalog, tiers,
                                   credentialed_providers={"anthropic"})
    assert findings == []


# ── role_resolves_outside_catalog (spec §Addendum3.D) ────────────────────────


def test_find_catalog_drift_flags_role_resolving_outside_catalog():
    """The default-coverage gap: max resolves to a code-default model the
    bot's catalog doesn't carry → a role_resolves_outside_catalog finding
    carrying the resolved model id and the role in `tier`."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    # max resolves (via defaults) to claude-fable-5, which the catalog lacks
    # and which no tier entry names.
    resolved = {
        "standard": "anthropic/claude-sonnet-4-6",   # in catalog → silent
        "max": "anthropic/claude-fable-5",           # NOT in catalog → flag
    }
    findings = find_catalog_drift(
        "team_bot_a", catalog, {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}},
        resolved_role_models=resolved,
    )
    role_findings = [f for f in findings if f.kind == "role_resolves_outside_catalog"]
    assert len(role_findings) == 1
    f = role_findings[0]
    assert f.model_id == "anthropic/claude-fable-5"
    assert f.tier == "max"
    assert f.provider == "anthropic"
    assert f.bot_id == "team_bot_a"


def test_find_catalog_drift_silent_when_resolved_role_model_in_catalog():
    """A role whose resolved model IS in catalog produces no finding."""
    catalog = [{"id": "anthropic/claude-fable-5", "provider": "anthropic"}]
    resolved = {"max": "anthropic/claude-fable-5"}
    findings = find_catalog_drift(
        "team_bot_a", catalog, {}, resolved_role_models=resolved,
    )
    assert [f for f in findings if f.kind == "role_resolves_outside_catalog"] == []


def test_find_catalog_drift_role_finding_not_duplicated_by_tier_member():
    """When a model is BOTH tier-named and role-resolved (and missing from
    catalog), only the tier_member_missing finding fires — the role pass
    suppresses the duplicate so the operator sees one row, not two."""
    catalog: list = []
    tiers = {"tier2": {"models": ["openai/gpt-4o"]}}
    resolved = {"standard": "openai/gpt-4o"}
    findings = find_catalog_drift(
        "team_bot_a", catalog, tiers, resolved_role_models=resolved,
    )
    tier_member = [f for f in findings if f.kind == "tier_member_missing"]
    role_outside = [f for f in findings if f.kind == "role_resolves_outside_catalog"]
    assert len(tier_member) == 1
    assert role_outside == []  # suppressed — same model_id already flagged


# ── credential-aware Type-1 split (spec §Addendum 10 §B) ─────────────────────
#
# A tier may name a model whose provider the bot has NO API key for. Reconcile
# leaves those as runtime-graceful silent drops (it never promotes them to a
# fatal "no API key"), so the "Reconcile catalog" affordance is a no-op for
# them. find_catalog_drift flags those provider_credentialed=False so the UI
# can route them to the missing-credentials fix instead. The flag mirrors
# reconcile's skip set exactly — detect and fix share one predicate.


def test_find_catalog_drift_uncredentialed_tier_member_not_reconcilable():
    """Tier names xai/grok-4 but the bot only has an anthropic key → the
    finding is provider_credentialed=False (Reconcile can't add it)."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    tiers = {"tier0": {"models": ["xai/grok-4"]}}
    findings = find_catalog_drift(
        "team_bot_a", catalog, tiers, credentialed_providers={"anthropic"},
    )
    tm = [f for f in findings if f.kind == "tier_member_missing"]
    assert len(tm) == 1
    assert tm[0].model_id == "xai/grok-4"
    assert tm[0].provider == "xai"
    assert tm[0].provider_credentialed is False


def test_find_catalog_drift_credentialed_tier_member_stays_reconcilable():
    """Tier names openai/gpt-4o and the bot HAS an openai key → the finding
    stays provider_credentialed=True (a genuine whitelist gap Reconcile fixes)."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    tiers = {"tier1": {"models": ["openai/gpt-4o"]}}
    findings = find_catalog_drift(
        "team_bot_a", catalog, tiers,
        credentialed_providers={"anthropic", "openai"},
    )
    tm = [f for f in findings if f.kind == "tier_member_missing"]
    assert len(tm) == 1
    assert tm[0].model_id == "openai/gpt-4o"
    assert tm[0].provider_credentialed is True


def test_find_catalog_drift_provider_credentialed_defaults_true_legacy():
    """Legacy callers that pass no credentialed_providers keep the old
    behavior: every tier-member-missing finding reads reconcilable (the
    None path matches reconcile_catalog, which adds them)."""
    catalog: list = []
    tiers = {"tier2": {"models": ["openai/gpt-4o"]}}
    findings = find_catalog_drift("team_bot_a", catalog, tiers)
    tm = [f for f in findings if f.kind == "tier_member_missing"]
    assert len(tm) == 1
    assert tm[0].provider_credentialed is True


def test_find_catalog_drift_unknown_provider_is_reconcilable():
    """A malformed model id (no provider/ prefix) has provider 'unknown' but
    is still reconcilable — reconcile_catalog adds it (its provider guard is
    falsy), so the detector must agree to keep detect/fix in sync."""
    catalog: list = []
    tiers = {"tier2": {"models": ["bare-model-no-slash"]}}
    findings = find_catalog_drift(
        "team_bot_a", catalog, tiers, credentialed_providers={"anthropic"},
    )
    tm = [f for f in findings if f.kind == "tier_member_missing"]
    assert len(tm) == 1
    assert tm[0].provider == "unknown"
    assert tm[0].provider_credentialed is True


def test_drift_credential_flag_matches_reconcile_skip_set():
    """The detector's provider_credentialed flag and reconcile's skip set are
    one source of truth: the models reconcile SKIPS (uncredentialed) are
    exactly the tier_member_missing findings flagged provider_credentialed=
    False, and the ones it ADDS are exactly the flagged-True findings."""
    catalog = [{"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}]
    tiers = {"tier1": {"models": ["openai/gpt-4o", "xai/grok-4"]}}
    cred = {"anthropic", "openai"}
    findings = find_catalog_drift(
        "team_bot_a", catalog, tiers, credentialed_providers=cred,
    )
    reconciled = reconcile_catalog(
        catalog, tiers, credentialed_providers=cred,
    )
    not_reconcilable = {
        f.model_id for f in findings
        if f.kind == "tier_member_missing" and not f.provider_credentialed
    }
    reconcilable = {
        f.model_id for f in findings
        if f.kind == "tier_member_missing" and f.provider_credentialed
    }
    assert not_reconcilable == set(reconciled.skipped_uncredentialed)
    assert reconcilable == set(reconciled.added_from_tiers)
