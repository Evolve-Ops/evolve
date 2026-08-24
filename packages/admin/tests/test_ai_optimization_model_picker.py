"""tests/test_ai_optimization_model_picker.py — validated picker + Phase 10b
catalog-as-pool restructure.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 6 (Phase 9
picker) + §Addendum 7 workstream C / Phase 10b (catalog-as-pool).

The admin SPA has no JS test harness; the established pattern across
packages/admin/tests is to pin UI behaviour by asserting on ai-optimization.js
*source strings*. These pin that:

  1. The shared validated-picker renderer (`_aiModelPickerRow`) exists and
     groups candidates by provider (optgroup) sourced from either the cached
     `_aiListings` (full listings) or an explicit `pool` of qualified ids.
  2. Phase 10b catalog-as-pool:
       • the CATALOG add-model control offers the exhaustive listings picker
         (boldLatest + freeText);
       • the BOT tier editor is constrained to the bot's catalog
         (pool:_aiCatalog, freeText:false);
       • the POD tier editor keeps the full listings (no pool, freeText:false);
       • free-text lives in the CATALOG ONLY — the tier editors pick only.
  3. The free-text "add anyway" path validates against the listing
     (`_aiValidateAndAdd`) rather than blindly adding a typed id.
  4. A "refreshed at … · Refresh" control is wired to the refresh endpoint
     (catalog + POD-tier listings sources).
  5. Tier-fit is a SOFT signal (suggested, not a hard gate) — all pool/listing
     models stay pickable.
  6. The latest-in-family option is marked in the catalog's exhaustive picker.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_JS = (
    REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
    / "static" / "js" / "pages" / "ai-optimization.js"
)


@pytest.fixture(scope="module")
def js() -> str:
    return _AI_JS.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


# ── 1. Shared picker renderer + grouped candidate sources ─────────────────────

def test_picker_renderer_exists(js: str):
    assert "function _aiModelPickerRow(" in js


def test_picker_groups_by_provider(js: str):
    code = _strip_comments(js)
    # Candidate source is the cached listings (or an explicit pool), grouped
    # into <optgroup> per provider.
    assert "_aiListings" in code
    assert "credentialed_providers" in code
    assert "<optgroup" in code


def test_picker_supports_explicit_pool(js: str):
    code = _strip_comments(js)
    # Phase 10b: the picker can be constrained to an explicit pool of qualified
    # ids (the bot catalog), grouping by the "<prov>/<model>" prefix.
    assert "Array.isArray(opts.pool)" in code


def test_picker_loads_listings_from_endpoint(js: str):
    code = _strip_comments(js)
    assert "/api/models/listings" in code
    assert "function _aiLoadListings(" in code


# ── 2. Phase 10b — catalog-as-pool wiring ─────────────────────────────────────

def test_catalog_uses_picker_full_listings_freetext_boldlatest(js: str):
    code = _strip_comments(js)
    # The catalog's add-model control is the picker. It sources from the full
    # listings (NO pool key), bolds latest-in-family, and keeps free-text.
    m = re.search(r"function _aiRenderCatalog\(botId, config\).*?el\.innerHTML", code, re.DOTALL)
    assert m, "could not locate _aiRenderCatalog body"
    catalog_body = m.group(0)
    assert "_aiModelPickerRow(" in catalog_body
    assert "boldLatest: true" in catalog_body
    assert "freeText: true" in catalog_body
    # The catalog picker draws from the full listings (the pool), not a `pool:`.
    assert "pool:" not in catalog_body


def test_bot_tier_editor_constrained_to_catalog_pool(js: str):
    code = _strip_comments(js)
    # The bot Custom tier editor's picker is pool-constrained to _aiCatalog and
    # offers no free-text (it picks only from the catalog).
    m = re.search(
        r"function _aiRenderTiersCustom\(botId, config, el\).*?const ladderSections",
        code, re.DOTALL,
    )
    assert m, "could not locate _aiRenderTiersCustom body"
    body = m.group(0)
    assert "pool: _aiCatalog" in body
    assert "freeText: false" in body


def test_pod_tier_editor_full_listings_no_freetext(js: str):
    code = _strip_comments(js)
    # The POD default editor keeps the FULL listings (no pool — intended
    # asymmetry: the pod default isn't gated by a per-bot catalog) and offers
    # no free-text.
    m = re.search(
        r"function _aiRenderPodDefaults\(\).*?const ladderSections",
        code, re.DOTALL,
    )
    assert m, "could not locate _aiRenderPodDefaults body"
    body = m.group(0)
    assert "freeText: false" in body
    assert "pool:" not in body            # pod editor is NOT catalog-gated
    assert "rerenderFnName: '_aiPodDefaults'" in body


# ── 3. Free-text is CATALOG-ONLY and validates against the listing ────────────

def test_free_text_removed_from_tier_editors(js: str):
    code = _strip_comments(js)
    # The retired per-tier free-text handlers and their inputs are gone — tier
    # editors pick only.
    assert "_aiTierAddFromInput" not in code
    assert "_aiPodTierAddFromInput" not in code
    assert "ai-tier-add-input-" not in code
    assert "ai-pod-tier-add-input-" not in code


def test_catalog_free_text_validates_against_listing(js: str):
    code = _strip_comments(js)
    assert "function _aiValidateAndAdd(" in code
    assert "/api/models/validate-model" in code
    # The catalog free-text add routes through the validator (canonical-only).
    assert re.search(
        r"function _aiCatalogAdd\(botId\)\s*\{[^}]*_aiValidateAndAdd", code, re.DOTALL,
    )


def test_validation_surfaces_nearest_match_suggestion(js: str):
    code = _strip_comments(js)
    # The miss path mentions the suggestion (nearest-match) to the operator.
    assert "suggestion" in code
    assert "did you mean" in code


# ── 4. refreshed-at + Refresh control (listings sources) ──────────────────────

def test_refresh_control_wired(js: str):
    code = _strip_comments(js)
    assert "/api/models/listings/refresh" in code
    assert "function _aiRefreshListings(" in code
    assert "refreshed " in code  # "refreshed <ts>" label
    # Catalog + POD-tier sources have a Refresh hook; the bot tier editor draws
    # from the catalog pool so it has no listings Refresh control any more.
    assert "_aiPodDefaults_refresh" in code
    assert "function _aiCatalog_refresh(" in code


# ── 5. Tier-fit is a SOFT signal (suggested, not a hard gate) ─────────────────

def test_tier_fit_is_soft_suggested_not_a_gate(js: str):
    code = _strip_comments(js)
    # "suggested" sorting/marking exists ...
    assert "suggested" in code
    assert "(suggested)" in code
    # ... but tier-fit is never a hard gate: non-recommended models stay
    # pickable. The only exclusion is non-recommended models already in this
    # tier (re-listing them adds nothing); recommended ones are always shown.
    assert "already.has(lc)" in code
    assert "suggested.has(lc)" in code


# ── 6. Latest-in-family bolding (catalog exhaustive picker) ───────────────────

def test_latest_in_family_marked_in_catalog(js: str):
    code = _strip_comments(js)
    # The picker reads the discovery-annotated is_family_latest flag and marks
    # the option (textual ● glyph — option font-weight is unreliable).
    assert "is_family_latest" in code
    assert "boldLatest" in code
    # The marker is a glyph prefix, pinned so a regression to font-weight fails.
    assert "● " in code


# ── 7. Degraded providers are surfaced, not silently dropped ──────────────────

def test_degraded_providers_surfaced(js: str):
    code = _strip_comments(js)
    assert "Couldn't refresh" in code
    assert "degraded" in code


# ── 8. A listings fetch failure logs (no silent empty picker) ─────────────────

def test_listing_fetch_failure_logs(js: str):
    code = _strip_comments(js)
    # The swallowed-fetch path must console.warn, not silently yield an empty
    # picker (the silent-monitor lesson).
    assert re.search(r"console\.warn\(\s*'ai-optimization: model listings", code)


# ── 9. Recommended-set marking + "(already added)" visibility (Addendum 6) ────
#
# Spec §Addendum 6 (Phase 9 §v1 item 1): tier-appropriateness is a SOFT
# "suggested" signal. The bot Custom tier editor must (a) star the RECOMMENDED
# set for the tier and (b) keep an already-added recommended model VISIBLE as a
# disabled "(already added)" option so the operator can see it's present (and
# won't add a worse one thinking the recommended one is missing).


def test_picker_renders_already_added_for_present_suggested(js: str):
    code = _strip_comments(js)
    # A recommended id that's ALSO already in the tier renders the "(already
    # added)" marker (still starred), not excluded.
    assert "(already added)" in code
    assert re.search(r"isSug\s*&&\s*isAdded", code), (
        "picker must special-case recommended-AND-already-added"
    )
    # ... and that option is disabled so it can't be re-picked (a no-op/dup).
    assert "isAdded ?" in code
    assert "disabled" in code


def test_picker_stars_absent_suggested(js: str):
    code = _strip_comments(js)
    # A recommended id NOT yet added renders the pickable "★ … (suggested)".
    assert "(suggested)" in code
    assert re.search(r"`★ \$\{label\} \(suggested\)`", code)


def test_picker_keeps_recommended_visible_when_already_added(js: str):
    code = _strip_comments(js)
    # The already-filter keeps a model when it's NOT already added OR it's
    # recommended — i.e. recommended models are never filtered out, so the full
    # recommended set is always present in the dropdown.
    assert re.search(
        r"!already\.has\(lc\)\s*\|\|\s*suggested\.has\(lc\)", code,
    ), "recommended models must survive the already-added filter"


def test_bot_tier_editor_suggests_default_cluster_not_current(js: str):
    code = _strip_comments(js)
    # The Custom tier editor must feed the RECOMMENDED set (the resolved default
    # cluster, rv.defaultModels) as `suggested`, NOT the bot's current models
    # (which are all in `already`, so suggesting them would star nothing).
    m = re.search(
        r"function _aiRenderTiersCustom\(botId, config, el\).*?const ladderSections",
        code, re.DOTALL,
    )
    assert m, "could not locate _aiRenderTiersCustom body"
    body = m.group(0)
    assert "suggested: (rv.defaultModels || [])" in body
    # It must NOT pass the current models as the suggested set.
    assert "suggested: (rv.models" not in body


def test_pod_tier_editor_suggests_code_default_cluster_not_current(js: str):
    code = _strip_comments(js)
    # The POD default editor edits the POD's tier definitions, so its RECOMMENDED
    # set is the CODE-default cluster (the layer ABOVE the pod — what "Reset to
    # Evolve defaults" would put here), surfaced per-role as
    # `view.roleDefaultModels[roleId]`. It must feed THAT as `suggested`, NOT the
    # pod's current `models` (which are all in `already`, so suggesting them
    # would star nothing — the bug this fixes). `already` stays the pod's current
    # models.
    m = re.search(
        r"function _aiRenderPodDefaults\(\).*?const ladderSections",
        code, re.DOTALL,
    )
    assert m, "could not locate _aiRenderPodDefaults body"
    body = m.group(0)
    # The recommended set is pulled per-role from the payload's roleDefaultModels.
    assert "view.roleDefaultModels" in body
    assert re.search(r"suggested:\s*suggested\b", body)
    # It must NOT pass the pod's current models as the suggested set (the old
    # mis-wiring: suggested == already == models → nothing ever starred).
    assert "suggested: models" not in body
    # `already` stays the pod's current models so present ones grey out.
    assert "already: models" in body


# ── 10. Per-bot picker is scoped to the bot's credentialed providers ──────────
#
# The listings cache is the POD union of every credentialed provider. The
# per-bot CATALOG picker (and its free-text "type a model ID" path) must be
# scoped to THAT bot's credentialed providers, so a bot is never offered/able to
# add a model from a provider it holds no key for (e.g. an `xai/` id on a bot
# without an xAI key). The POD default editor stays pod-wide.


def test_picker_intersects_listings_with_bot_credentialed_providers(js: str):
    code = _strip_comments(js)
    # The full-listings branch of _aiModelPickerRow intersects the pod-wide
    # credentialed set with the optional per-bot `credentialedProviders` opt.
    assert "opts.credentialedProviders" in code
    # The intersection is a filter over the pod credentialed set, not a replace
    # (so the listings' provider order/keys still drive grouping).
    assert re.search(r"credentialed\s*=\s*credentialed\.filter\(", code), (
        "picker must filter the pod credentialed set by the bot set, not replace it"
    )


def test_catalog_picker_passes_bot_credentialed_providers(js: str):
    code = _strip_comments(js)
    # The CATALOG add-model control feeds the bot's credentialed providers into
    # the picker so it's scoped; the POD editor (asserted elsewhere) does NOT.
    m = re.search(
        r"function _aiRenderCatalog\(botId, config\).*?el\.innerHTML",
        code, re.DOTALL,
    )
    assert m, "could not locate _aiRenderCatalog body"
    catalog_body = m.group(0)
    assert "credentialedProviders: botCredentialedProviders" in catalog_body
    # The bot set is derived from keyStatus (no provider literals).
    assert "function _aiBotCredentialedProviders(" in code
    assert "config.keyStatus" in code


def test_pod_tier_editor_not_scoped_to_a_bot(js: str):
    code = _strip_comments(js)
    # The POD default editor stays pod-wide: it must NOT pass a per-bot
    # credentialedProviders scope into its picker.
    m = re.search(
        r"function _aiRenderPodDefaults\(\).*?const ladderSections",
        code, re.DOTALL,
    )
    assert m, "could not locate _aiRenderPodDefaults body"
    body = m.group(0)
    assert "credentialedProviders" not in body


def test_free_text_validation_scoped_to_bot(js: str):
    code = _strip_comments(js)
    # The catalog free-text path passes bot_id to the validator so the server
    # scopes the candidate set to the bot's credentialed providers.
    m = re.search(
        r"function _aiCatalogAdd\(botId\)\s*\{.*?\n\}", code, re.DOTALL,
    )
    assert m, "could not locate _aiCatalogAdd body"
    body = m.group(0)
    assert "botId" in body
    assert "_aiBotCredentialedProviders" in body
    # The validator forwards bot_id to the endpoint and scopes the local index.
    assert re.search(r"bot_id:\s*o\.botId", code) or "bot_id: o.botId" in code
    assert "_aiListedIdIndex(o.credentialedProviders)" in code
