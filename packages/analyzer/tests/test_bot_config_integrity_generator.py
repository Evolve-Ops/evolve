"""tests/test_bot_config_integrity_generator.py — generator + applier tests.

The bot_config_integrity generator dispatches to a registry of checks
(``checks/catalog_tier_drift.py``, ``checks/catalog_provider_coverage.py``).
The dispatcher reads the bot's openclaw.json once and fans out; each
check is independently testable.

Coverage map:
  - dispatcher reads config once, calls each check, returns the union
  - dispatcher returns [] cleanly on oc_cli unavailable or config-None
  - one check failing doesn't poison the loop for other checks
  - catalog_tier_drift check: team_bot_a shape → 1 proposal with 7 missing
  - catalog_tier_drift check: in-sync state → 0 proposals
  - catalog_tier_drift check: dedup across tiers
  - catalog_provider_coverage check: team_bot_a had only Anthropic catalog
    + Google + xAI + OpenAI keys → 3 proposals (one per missing provider)
  - catalog_provider_coverage check: provider already in catalog → skip
  - catalog_provider_coverage check: confidence is meaningfully lower
    than tier_drift (0.65 vs 0.95)
  - Risk tag invariant: every proposal is bot/auto/model_catalog
  - ReconcileModelCatalogApplier (existing from PR #1703): still
    consumes the actions both checks produce
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ANALYZER = Path(__file__).parent.parent
_ADMIN = _ANALYZER.parent / "admin"
for _p in (str(_ANALYZER), str(_ADMIN)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schema.proposal import (  # noqa: E402
    ReconcileModelCatalog,
    action_from_dict,
    action_to_dict,
)
from arbiter.appliers.base import get_applier  # noqa: E402
from arbiter.appliers import model_catalog as _mc_mod  # noqa: E402,F401
from generators.bot_config_integrity.observe import (  # noqa: E402
    BotConfigIntegrityContext,
    observe,
)
from generators.bot_config_integrity.checks import (  # noqa: E402
    CHECKS,
    catalog_provider_coverage,
    catalog_tier_drift,
)


# ── Action serialization (unchanged from #1707) ──────────────────────────────


def test_reconcile_model_catalog_action_kind():
    a = ReconcileModelCatalog(bot_id="team_bot_a", add_models=["openai/gpt-4o"])
    assert a.kind == "ReconcileModelCatalog"


def test_reconcile_action_round_trips():
    a = ReconcileModelCatalog(
        bot_id="team_bot_a",
        add_models=["openai/gpt-4o", "google/gemini-2.5-pro"],
    )
    d = action_to_dict(a)
    assert d["kind"] == "ReconcileModelCatalog"
    a2 = action_from_dict(d)
    assert a2.bot_id == "team_bot_a"
    assert a2.add_models == ["openai/gpt-4o", "google/gemini-2.5-pro"]


# ── Dispatcher behavior ─────────────────────────────────────────────────────


def _team_bot_a_shaped_config():
    """openclaw.json shape matching team_bot_a's actual state on 2026-05-28."""
    return {
        "catalog": [
            {"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"},
            {"id": "anthropic/claude-opus-4-6", "provider": "anthropic"},
            {"id": "anthropic/claude-haiku-4-5", "provider": "anthropic"},
            {"id": "anthropic/claude-sonnet-4-5", "provider": "anthropic"},
        ],
        "tiers": {
            "tier1": {"models": ["anthropic/claude-opus-4-7", "openai/gpt-4o",
                                  "google/gemini-2.5-pro", "xai/grok-4"]},
            "tier2": {"models": ["anthropic/claude-sonnet-4-6", "google/gemini-2.5-pro",
                                  "xai/grok-4", "openai/gpt-4o"]},
            "tier3": {"models": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini",
                                  "google/gemini-2.0-flash", "xai/grok-4-mini"]},
        },
    }


def test_dispatcher_reads_config_once_and_calls_each_check(tmp_path):
    """The dispatcher reads openclaw.json once and dispatches; each
    check operates on the parsed config dict."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    cfg = _team_bot_a_shaped_config()

    drift_called = []
    coverage_called = []

    def fake_drift_run(c, cfg_arg):
        drift_called.append(cfg_arg)
        return []

    def fake_coverage_run(c, cfg_arg):
        coverage_called.append(cfg_arg)
        return []

    with patch("generators.bot_config_integrity.observe._full_config_get_safe",
               return_value=lambda b: cfg), \
         patch.object(catalog_tier_drift, "run", side_effect=fake_drift_run), \
         patch.object(catalog_provider_coverage, "run", side_effect=fake_coverage_run):
        observe(ctx)

    assert len(drift_called) == 1
    assert len(coverage_called) == 1
    assert drift_called[0] is cfg
    assert coverage_called[0] is cfg


def test_dispatcher_returns_union_of_check_proposals(tmp_path):
    """One check returns [p1], another returns [p2] → observe returns [p1, p2]."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    p1 = MagicMock(name="proposal_from_drift_check")
    p2 = MagicMock(name="proposal_from_coverage_check")
    with patch("generators.bot_config_integrity.observe._full_config_get_safe",
               return_value=lambda b: _team_bot_a_shaped_config()), \
         patch.object(catalog_tier_drift, "run", return_value=[p1]), \
         patch.object(catalog_provider_coverage, "run", return_value=[p2]):
        result = observe(ctx)
    assert result == [p1, p2]


def test_dispatcher_one_check_failing_doesnt_block_others(tmp_path):
    """A check raising an exception is swallowed; other checks still run.
    Contract: "don't poison the loop"."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    p_from_coverage = MagicMock(name="proposal_from_coverage_check")
    with patch("generators.bot_config_integrity.observe._full_config_get_safe",
               return_value=lambda b: _team_bot_a_shaped_config()), \
         patch.object(catalog_tier_drift, "run", side_effect=RuntimeError("boom")), \
         patch.object(catalog_provider_coverage, "run", return_value=[p_from_coverage]):
        result = observe(ctx)
    assert result == [p_from_coverage]


def test_dispatcher_returns_empty_when_oc_cli_unavailable(tmp_path):
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    with patch("generators.bot_config_integrity.observe._full_config_get_safe",
               return_value=None):
        assert observe(ctx) == []


def test_dispatcher_returns_empty_when_config_unreadable(tmp_path):
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    with patch("generators.bot_config_integrity.observe._full_config_get_safe",
               return_value=lambda b: None):
        assert observe(ctx) == []


def test_registry_includes_both_known_checks():
    """Drop-in protection: if someone removes a check from CHECKS, this
    test fires."""
    check_names = {c.CHECK_NAME for c in CHECKS}
    assert "catalog_tier_drift" in check_names
    assert "catalog_provider_coverage" in check_names


# ── catalog_tier_drift check ────────────────────────────────────────────────


def test_catalog_tier_drift_team_bot_a_shape(tmp_path):
    """Team_bot_a's exact case produces one proposal with all 7 missing models."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    proposals = catalog_tier_drift.run(ctx, _team_bot_a_shaped_config())
    assert len(proposals) == 1
    p = proposals[0]
    assert p.bot_id == "team_bot_a"
    assert p.generator_id == "bot_config_integrity"
    assert p.action.kind == "ReconcileModelCatalog"
    missing = set(p.action.add_models)
    expected = {
        "anthropic/claude-opus-4-7",
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "google/gemini-2.5-pro",
        "google/gemini-2.0-flash",
        "xai/grok-4",
        "xai/grok-4-mini",
    }
    assert missing == expected
    # Confidence reflects mechanical correctness
    assert p.provenance.confidence >= 0.9


def test_catalog_tier_drift_dedupes_across_tiers(tmp_path):
    """openai/gpt-4o appears in team_bot_a's tier1 + tier2; one entry in add_models."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    proposals = catalog_tier_drift.run(ctx, _team_bot_a_shaped_config())
    assert len(proposals[0].action.add_models) == len(set(proposals[0].action.add_models))


def test_catalog_tier_drift_emits_nothing_when_in_sync(tmp_path):
    cfg = {
        "catalog": [
            {"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"},
            {"id": "anthropic/claude-haiku-4-5", "provider": "anthropic"},
        ],
        "tiers": {
            "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
            "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
        },
    }
    ctx = BotConfigIntegrityContext(bot_id="evo", shared_dir=tmp_path)
    assert catalog_tier_drift.run(ctx, cfg) == []


def test_catalog_tier_drift_risk_tag_invariant(tmp_path):
    """Risk tag is bot/auto/model_catalog — routing layer keys off this."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    p = catalog_tier_drift.run(ctx, _team_bot_a_shaped_config())[0]
    assert p.risk_tag.blast_radius == "bot"
    assert p.risk_tag.reversibility == "auto"
    assert p.risk_tag.touches == ["model_catalog"]


# ── catalog_provider_coverage check ─────────────────────────────────────────


def test_catalog_provider_coverage_emits_per_missing_provider(tmp_path):
    """Team_bot_a has Anthropic catalog + Google/xAI/OpenAI keys → 3 proposals
    (one per provider missing from catalog).

    Each proposal independently approvable: operator can accept Google,
    skip OpenAI if they want.
    """
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    cfg = _team_bot_a_shaped_config()
    with patch.object(catalog_provider_coverage, "_read_auth_providers_safe",
                      return_value={"anthropic", "google", "xai", "openai"}):
        proposals = catalog_provider_coverage.run(ctx, cfg)
    providers_proposed = {p.provenance.signals["provider"] for p in proposals}
    # Anthropic already in catalog → no proposal for it
    assert "anthropic" not in providers_proposed
    # The three missing providers each get one proposal
    assert providers_proposed == {"google", "xai", "openai"}


def test_catalog_provider_coverage_confidence_below_tier_drift(tmp_path):
    """Coverage proposals must be visibly less confident than tier_drift
    so an auto-apply threshold can be set between them."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    with patch.object(catalog_provider_coverage, "_read_auth_providers_safe",
                      return_value={"google"}):
        proposals = catalog_provider_coverage.run(ctx, _team_bot_a_shaped_config())
    assert proposals, "expected a proposal for google"
    coverage_conf = proposals[0].provenance.confidence
    drift_proposal = catalog_tier_drift.run(ctx, _team_bot_a_shaped_config())[0]
    drift_conf = drift_proposal.provenance.confidence
    # Threshold-able gap between them — 0.65 vs 0.95
    assert coverage_conf < drift_conf - 0.2


def test_catalog_provider_coverage_skips_already_credentialed(tmp_path):
    """Anthropic is already in team_bot_a's catalog; no proposal for it."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    with patch.object(catalog_provider_coverage, "_read_auth_providers_safe",
                      return_value={"anthropic"}):
        proposals = catalog_provider_coverage.run(ctx, _team_bot_a_shaped_config())
    assert proposals == []


def test_catalog_provider_coverage_returns_nothing_when_no_credentials(tmp_path):
    """No credentialed providers → no proposal to emit."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    with patch.object(catalog_provider_coverage, "_read_auth_providers_safe",
                      return_value=set()):
        proposals = catalog_provider_coverage.run(ctx, _team_bot_a_shaped_config())
    assert proposals == []


def test_catalog_provider_coverage_recommended_models_match_registry(tmp_path):
    """With NO listings cache (tmp_path is empty), identity falls back to the
    offline RECOMMENDED table — and the proposal is flagged DEGRADED, never
    presenting the stale ids as "current" (Addendum 8 §C silent-monitor rule)."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    with patch.object(catalog_provider_coverage, "_read_auth_providers_safe",
                      return_value={"google"}):
        proposals = catalog_provider_coverage.run(ctx, _team_bot_a_shaped_config())
    google_proposal = next(p for p in proposals
                            if p.provenance.signals["provider"] == "google")
    add = set(google_proposal.action.add_models)
    assert "google/gemini-2.5-pro" in add
    assert "google/gemini-2.0-flash" in add
    # The fallback path must FLAG itself — RECOMMENDED is no longer "current".
    assert google_proposal.provenance.signals["model_id_source"] == "recommended_degraded"
    assert "offline" in google_proposal.problem.lower()


def test_catalog_provider_coverage_identity_from_listing_not_recommended(tmp_path):
    """When the listings cache HAS the provider, the model ids come from the
    LIVE listing (latest-in-family), NOT RECOMMENDED — and the proposal is NOT
    degraded (Addendum 8 §A/§C: identity from listings, RECOMMENDED demoted)."""
    import json
    import model_discovery as md
    # Seed a listings cache with a google model that is NOT in RECOMMENDED, so a
    # hit proves the listing — not the hand table — was the identity source.
    cache = {
        "refreshed_at": "2026-06-11T00:00:00Z",
        "providers": {
            "google": [
                {
                    "provider": "google",
                    "model_id": "gemini-9-pro",
                    "qualified_id": "google/gemini-9-pro",
                    "capabilities": ["chat"],
                    "family": "gemini-pro",
                    "is_family_latest": True,
                },
                {
                    # An OLDER family member — not family-latest → excluded.
                    "provider": "google",
                    "model_id": "gemini-2.5-pro",
                    "qualified_id": "google/gemini-2.5-pro",
                    "capabilities": ["chat"],
                    "family": "gemini-pro",
                    "is_family_latest": False,
                },
            ],
        },
        "degraded": [],
    }
    (tmp_path / md.LISTINGS_CACHE_NAME).write_text(json.dumps(cache))

    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    with patch.object(catalog_provider_coverage, "_read_auth_providers_safe",
                      return_value={"google"}):
        proposals = catalog_provider_coverage.run(ctx, _team_bot_a_shaped_config())
    google_proposal = next(p for p in proposals
                           if p.provenance.signals["provider"] == "google")
    add = set(google_proposal.action.add_models)
    # The live, latest-in-family id — NOT the RECOMMENDED gemini-2.5-pro.
    assert add == {"google/gemini-9-pro"}
    assert google_proposal.provenance.signals["model_id_source"] == "listing"


# ── catalog_provider_coverage §C severity split ─────────────────────────────


def test_catalog_provider_coverage_advisory_severity_for_same_vendor_judge(tmp_path):
    """Soft-preference (2026-06-19): a judge that routes on Standard's own vendor
    is an ADVISORY, not a hard break. Adding a credentialed-but-uncataloged
    cross-vendor provider upgrades judge to an independent cross-vendor check —
    an improvement nudged above pure hygiene, NOT an operational fix.

    team_bot_a's catalog is anthropic-only, so judge routes same-vendor on
    anthropic; adding google/xai/openai gives it a cross-vendor option."""
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    with patch.object(catalog_provider_coverage, "_read_auth_providers_safe",
                      return_value={"anthropic", "google", "xai", "openai"}):
        proposals = catalog_provider_coverage.run(ctx, _team_bot_a_shaped_config())
    assert proposals
    for p in proposals:
        # Advisory, not hard_break — judge already routes (same vendor).
        assert p.provenance.signals["coverage_severity"] == "advisory"
        assert p.provenance.signals["fixes_hard_break_roles"] == []
        assert "judge" in p.provenance.signals["improves_advisory_roles"]
        # Off-ladder judge improvement → "improvement", NOT operational_urgent.
        assert p.urgency == "improvement"
        # Confidence unchanged so the (fix_risk-gated) auto-apply floor holds.
        assert p.provenance.confidence == 0.65
        # Honest copy: it routes today, this is a quality improvement not a fix.
        assert "isn't optional" not in p.problem
        assert "not required" in p.problem or "quality improvement" in p.problem


def test_catalog_provider_coverage_soft_when_role_still_routes(tmp_path):
    """No hard break (judge routes via a diverse in-catalog credentialed model)
    → coverage proposal stays the soft hygiene advisory, severity 'soft'."""
    cfg = _team_bot_a_shaped_config()
    # Put openai/gpt-4o (named in tier2) into the catalog so judge has a diverse,
    # routable model and no role is broken.
    cfg["catalog"].append({"id": "openai/gpt-4o", "provider": "openai"})
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path)
    with patch.object(catalog_provider_coverage, "_read_auth_providers_safe",
                      return_value={"anthropic", "openai", "google"}):
        proposals = catalog_provider_coverage.run(ctx, cfg)
    google = [p for p in proposals if p.provenance.signals["provider"] == "google"]
    assert google, "google is still missing from catalog → a coverage proposal"
    assert google[0].provenance.signals["coverage_severity"] == "soft"
    assert google[0].provenance.signals["fixes_hard_break_roles"] == []
    assert google[0].urgency == "hygiene"


# ── Applier (smoke — full coverage in PR #1707's prior tests) ───────────────


def test_applier_still_handles_reconcile_model_catalog_action():
    """The ReconcileModelCatalogApplier from PR #1707 still consumes
    actions emitted by either check — they share the same action shape."""
    action = ReconcileModelCatalog(
        bot_id="team_bot_a",
        add_models=["google/gemini-2.5-pro"],
    )
    initial = {"catalog": []}
    captured: dict = {}

    def fake_set(bot_id, updates):
        captured["updates"] = updates
        return updates

    with patch.object(_mc_mod, "_config_fns",
                      return_value=(lambda b: initial, fake_set)):
        applier = get_applier("ReconcileModelCatalog")
        result = applier.apply(action, "team_bot_a")
    assert result.ok is True
    assert "google/gemini-2.5-pro" in result.details["added"]
