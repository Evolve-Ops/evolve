"""tests/test_ai_optimization_inherited_provenance.py — Phase 7 (Addendum 4).

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 4 — un-overloading
the AI-Optimization model panels:

  1. The POD-tab engine panel is reframed as INFORMATIONAL (it mirrors the
     primary bot; it is not a bot-defaults editor). "Engine tier defaults" is
     renamed to "Engine models (background work)".
  2. The per-bot Tier Definitions panel renders from the MERGED
     resolve_roles_with_provenance view (config.roles): each role shows its
     resolved model(s) + a provenance badge (Evolve default / pod / this bot).
     A default/pod-covered role renders the inherited model with a Customize
     action instead of "(empty — add models below)"; a bot-override role shows
     the editable list plus Revert-to-default. Both Customize and Revert write
     through the existing safe tiers PUT (oc_model._save_tiers_file).

These are source-level assertions on the rendered helpers in ai-optimization.js
and the static copy in index.html (the admin SPA has no JS test harness; the
established pattern across packages/admin/tests is to scan the web source — see
test_ai_optimization_rank_presentation.py). No real bot/user names appear;
placeholders only.
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
_INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


@pytest.fixture(scope="module")
def js() -> str:
    return _AI_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


# ── 1. Engine panel reframe (informational, mirrors primary) ──────────────────


def test_engine_panel_renamed_to_engine_models(html: str):
    """The POD-tab panel title is reframed away from "defaults"."""
    assert "Engine models (background work)" in html, (
        "engine panel must be renamed to 'Engine models (background work)'"
    )
    # The old overloaded title is gone from the panel heading.
    assert "Engine tier defaults" not in html, (
        "old 'Engine tier defaults' panel title must be retired"
    )


def test_engine_panel_copy_is_informational(html: str):
    """Explainer states the engine mirrors the primary bot's config and is not
    a bot-defaults editor — configured by configuring the primary bot."""
    region = html[html.find("ai-pod-view") : html.find("ai-bot-view")]
    assert "informational" in region.lower(), (
        "engine panel must be framed as informational"
    )
    assert "primary bot" in region.lower()
    # No new engine-override config control is introduced (deferred). The
    # pre-existing engine override card may remain, but the reframed explainer
    # must not invite editing bot defaults from here.


def test_engine_body_intro_recontextualizes_provenance_as_readonly(js: str):
    """The engine-body render labels the source chips as the engine's resolved
    provenance (read-only), not an editable defaults surface."""
    m = re.search(r"function _aiRenderPodEngine\(.*?\n\}", js, re.DOTALL)
    assert m, "_aiRenderPodEngine not found"
    body = m.group(0)
    assert "read-only" in body.lower(), (
        "engine panel intro must mark the resolved models read-only"
    )


# ── 2. Layer-chip CSS (reused by the Phase 8 mode chips) ──────────────────────


def test_provenance_chip_classes_are_token_colored(html: str):
    """The ai-layer-chip variants are defined with token vars, not hex — checked
    against base.css so the badge themes in both modes. Phase 8 reuses these
    classes for the Use-defaults / Custom mode chips."""
    css = (
        REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "static"
        / "css" / "base.css"
    ).read_text(encoding="utf-8")
    assert ".ai-layer-chip.ai-layer-pod" in css
    assert ".ai-layer-chip.ai-layer-bot" in css
    assert ".ai-layer-chip.ai-layer-default" in css
    # The variants reference token vars (var(--accent)/var(--green)), not hex.
    chip_block = css[css.find(".ai-layer-chip") : css.find(".ai-layer-chip") + 600]
    assert "var(--" in chip_block
    assert "#" not in chip_block, "layer chips must not use hex colors"


# ── 3. Phase 8 (Addendum 5): the per-tier provenance control is RETIRED ───────
# The mixed per-tier BOT/DEFAULT provenance + per-tier Customize/Revert is
# replaced by ONE per-bot toggle. These assertions guard the retirement so the
# old control can't silently come back.


def test_per_tier_provenance_badge_helper_retired(js: str):
    """`_aiProvenanceBadge` (the per-tier THIS BOT / EVOLVE DEFAULT badge) is
    gone — provenance is now whole-bot, not per-tier."""
    assert "function _aiProvenanceBadge(" not in js, (
        "per-tier provenance badge helper must be retired in Phase 8"
    )


def test_per_tier_customize_revert_handlers_retired(js: str):
    """The per-tier `_aiTierCustomize` / `_aiTierRevert` handlers and their
    shared `_aiPersistTiersWrite` / `_AI_STOREKEY_TO_ROLE` are gone."""
    assert "_aiTierCustomize(" not in js
    assert "_aiTierRevert(" not in js
    assert "_aiPersistTiersWrite(" not in js
    assert "_AI_STOREKEY_TO_ROLE" not in js


# ── 4. The per-bot Use-defaults / Custom toggle ───────────────────────────────


def test_render_dispatches_on_custom_tiers_flag(js: str):
    """`_aiRenderTiers` branches on `config.customTiers` (the RAW per-bot
    rungs/roles presence the config GET attaches) — never on the merged view."""
    m = re.search(r"function _aiRenderTiers\(botId, config\)\s*\{(.*?)\n\}", js, re.DOTALL)
    assert m, "_aiRenderTiers not found"
    body = m.group(1)
    assert "config.customTiers" in body
    assert "_aiRenderTiersCustom(" in body
    assert "_aiRenderTiersUseDefaults(" in body


def test_use_defaults_mode_is_readonly_preview_with_customize(js: str):
    """Use-defaults mode renders a read-only preview from the resolver view
    (config.roles) and the single 'Customize this bot' control — no per-tier
    add/remove/Customize affordances."""
    m = re.search(r"function _aiRenderTiersUseDefaults\([^)]*\)\s*\{(.*?)\n\}\n\n", js, re.DOTALL)
    assert m, "_aiRenderTiersUseDefaults not found"
    body = m.group(1)
    # Reads the resolved roles view, derived (never hardcoded model names).
    assert "config.roles" in body or "config && config.roles" in body
    assert "rv.models" in body and "rv.resolvedModel" in body
    # The one control is whole-bot Customize.
    assert "Customize this bot" in body
    assert "_aiCustomizeBot()" in body
    # No per-tier editing in the read-only preview.
    assert "_aiTierAddModel(" not in body
    assert "_aiTierRemoveModel(" not in body


def test_custom_mode_is_editable_with_reset(js: str):
    """Custom mode renders the editable per-tier list plus the single 'Reset to
    pod defaults' control."""
    m = re.search(r"function _aiRenderTiersCustom\([^)]*\)\s*\{(.*?)\n\}\n\n", js, re.DOTALL)
    assert m, "_aiRenderTiersCustom not found"
    body = m.group(1)
    # Editable per-tier affordances survive under Custom. Reorder is drag-only
    # (Phase 10a removed the broken ↑↓ buttons / _aiTierMoveModel); the tier
    # rows render via the draggable _aiTierModelRow and removal is the inline ×.
    assert "_aiTierAddModel(" in body
    assert "_aiTierRemoveModel(" in body
    assert "_aiTierModelRow(" in body
    # Whole-bot reset control.
    assert "Reset to pod defaults" in body
    assert "_aiResetBotToDefaults()" in body
    # The Save Tiers control still writes per-model edits.
    assert "_aiSaveTiers()" in body


def test_customize_handler_posts_tier_mode_custom(js: str):
    """`_aiCustomizeBot` POSTs the mode flip to the tier-mode endpoint and
    re-fetches — the seed is materialized server-side (no model literals in
    JS)."""
    m = re.search(r"async function _aiCustomizeBot\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL)
    assert m, "_aiCustomizeBot not found"
    body = m.group(1)
    assert "/tier-mode" in body
    assert "mode: 'custom'" in body
    assert "api('PUT'" in body
    assert "_aiRenderBotData(" in body


def test_reset_handler_confirms_and_posts_tier_mode_default(js: str):
    """`_aiResetBotToDefaults` confirms (it discards custom config) then POSTs
    the default-mode flip and re-fetches."""
    m = re.search(r"async function _aiResetBotToDefaults\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL)
    assert m, "_aiResetBotToDefaults not found"
    body = m.group(1)
    assert "confirmModal(" in body, "reset must confirm — it discards per-bot config"
    assert "/tier-mode" in body
    assert "mode: 'default'" in body
    assert "api('PUT'" in body
    assert "_aiRenderBotData(" in body
