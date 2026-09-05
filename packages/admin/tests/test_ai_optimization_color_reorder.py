"""tests/test_ai_optimization_color_reorder.py — Phase 10a.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 7 (Phase 10),
workstreams A (visual consistency & color) and B (tier-row controls).

The admin SPA has no JS test harness; the established pattern is to pin UI
behaviour by asserting on ai-optimization.js *source strings* (and the
base.css token block for the provider-color palette). These pin that:

  1. Model chips are provider-COLORED via an EXPLICIT provider→token map —
     base.css `.ai-provider-<name>` classes set --chip-color from a named
     `--ai-provider-<name>` token. NOT a hash. The JS only slugifies the
     provider key into a class name (presentation data, no provider literals
     in routing logic).
  2. The provider-color tokens are named dark/light token PAIRS in base.css
     (both themes) — no raw hex in the chip component CSS.
  3. The catalog chips and the tier-row chips are the SAME component
     (_aiModelChip) — catalog⇄tier unification.
  4. Within-tier drag-to-reorder still exists (native HTML5 dragstart/dragover/
     drop) and mutates models[] then re-renders through the existing save path.
     (Phase 10c: DnD is now a browser-only progressive enhancement — the
     reliable reorder path is the ↑/↓ buttons; see item 5 and
     test_ai_optimization_tier_reorder.py.)
  5. Reorder routes through the single _aiTierReorder() mutation helper. The
     dead per-scope _aiTierMoveModel / _aiPodTierMoveModel helpers stay deleted
     (Phase 10a). Phase 10c RESTORED accessible ↑/↓ move buttons on each row as
     the webview-safe path — native HTML5 DnD does not fire in the Tauri/wry
     desktop app (#3255/#3258), so DnD-only was a silent no-op there — but they
     delegate to _aiTierReorder(), they do NOT resurrect the old helpers.
  6. Removal is now an inline × ON the chip (the chip component renders it from
     an onRemove handler), wired to the existing remove fns.
  7. The wizard preview rows are colored too, but stay READ-ONLY (no drag, no
     inline ×).
  8. No pool→tier drag-and-drop crept in (spec §Non-goals).
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
_BASE_CSS = (
    REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
    / "static" / "css" / "base.css"
)


@pytest.fixture(scope="module")
def js() -> str:
    return _AI_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return _BASE_CSS.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


# ── 1. Explicit provider→color via slugified CSS class (no hash) ──────────────

def test_provider_color_class_helper_exists(js: str):
    code = _strip_comments(js)
    # The Phase 9 hash helper is gone; color now comes from a slugified
    # `ai-provider-<name>` class resolved by _aiProviderColorClass.
    assert "function _aiProviderColorClass(" in code
    assert "function _aiProviderSlug(" in code
    assert "_aiProviderColorVar" not in code  # old hash helper removed
    assert "_AI_PROVIDER_COLOR_COUNT" not in code  # old hash count removed


def test_named_providers_are_presentation_data_not_logic(js: str):
    code = _strip_comments(js)
    # The named-provider set is presentation data (same category as the label
    # map). It feeds class-name selection only — never routing. The four bold
    # primaries are named so base.css can color them directly.
    assert "_AI_PROVIDER_COLOR_NAMED" in code
    assert "ai-provider-' + slug" in code or "'ai-provider-' + slug" in code
    # Unknown / overflow handling: empty → gray "unknown"; others → distinct
    # overflow palette (alt-N), chosen deterministically.
    assert "ai-provider-unknown" in code
    assert "_AI_PROVIDER_COLOR_OVERFLOW" in code


def test_chip_renderer_emits_provider_color_class(js: str):
    code = _strip_comments(js)
    assert "function _aiModelChip(" in code
    # The chip carries the provider color through the class, not an inline hex.
    assert "_aiProviderColorClass(provider)" in code
    assert "ai-model-provider-chip" in code


def test_model_to_provider_derivation_from_listings(js: str):
    code = _strip_comments(js)
    # Provider is resolved from the listings data (model id → provider key),
    # so there are no provider literals in the coloring logic.
    assert "function _aiProviderForModel(" in code
    assert "_aiListings" in code


def test_tier_chips_are_provider_colored(js: str):
    code = _strip_comments(js)
    # The shared tier-row renderer colors each chip via _aiModelChip + the
    # derived provider, in both the bot and pod editors.
    assert "function _aiTierModelRow(" in code
    assert "_aiModelChip(short, provider" in code
    assert "_aiProviderForModel(m)" in code


def test_wizard_preview_rows_are_colored(js: str):
    code = _strip_comments(js)
    # The read-only easy-setup preview rows also use the colored chip.
    assert re.search(
        r"_aiEasyPreviewRows\(catalog\).*_aiModelChip\(", code, re.DOTALL
    )


# ── 2. Provider-color palette = NAMED dark/light token PAIRS in base.css ──────

def test_provider_color_tokens_named_and_defined_both_themes(css: str):
    # Each named provider token must appear in BOTH the dark (:root) and light
    # ([data-theme="light"]) blocks — i.e. at least twice in the file. The
    # four bold primaries + the distinct overflow palette + unknown.
    for name in (
        "anthropic", "openai", "google", "xai",
        "alt-1", "alt-2", "alt-3", "unknown",
    ):
        token = f"--ai-provider-{name}:"
        assert css.count(token) >= 2, f"{token} missing a dark/light pair"
    # The old hash palette tokens are gone.
    assert "--ai-provider-0:" not in css
    assert "--ai-provider-5:" not in css


def test_provider_classes_map_to_named_tokens(css: str):
    # base.css owns the provider→token mapping: each .ai-provider-<name> class
    # sets --chip-color to the matching named token (presentation data).
    for name in ("anthropic", "openai", "google", "xai"):
        assert re.search(
            rf"\.ai-provider-{re.escape(name)}\s*\{{[^}}]*--chip-color:\s*var\(--ai-provider-{re.escape(name)}\)",
            css,
        ), f".ai-provider-{name} → token mapping missing"


def test_chip_component_css_uses_token_not_hex(css: str):
    # The chip component class references the --chip-color token, never a
    # bare hex color in the rule.
    m = re.search(r"\.ai-model-provider-chip\s*\{([^}]*)\}", css)
    assert m, "ai-model-provider-chip rule missing"
    body = m.group(1)
    assert "var(--chip-color" in body
    assert not re.search(r"#[0-9A-Fa-f]{3,6}", body), "raw hex in chip CSS"


# ── 3. Catalog ⇄ tier unification (same chip component) ───────────────────────

def test_catalog_uses_unified_chip(js: str):
    code = _strip_comments(js)
    body = re.search(
        r"function _aiRenderCatalog\([^)]*\)\s*\{(.*?)\n\}", code, re.DOTALL
    )
    assert body, "_aiRenderCatalog body not found"
    b = body.group(1)
    # Catalog now renders the unified provider-colored chip with an inline ×
    # wired to the existing _aiCatalogRemove — not the old ai-model-pill.
    assert "_aiModelChip(" in b
    assert "onRemove" in b
    assert "_aiCatalogRemove(" in b
    assert "ai-model-pill" not in b  # legacy catalog pill removed
    # The provider-key-status dot affordance is preserved (folded into chip).
    assert "keyDot" in b
    assert "No API key for" in b


# ── 4. Within-tier drag-to-reorder (native HTML5 DnD) still present ───────────

def test_drag_reorder_handlers_exist(js: str):
    code = _strip_comments(js)
    for fn in (
        "_aiTierDragStart", "_aiTierDragOver", "_aiTierDrop",
        "_aiTierDragEnd", "_aiTierReorder",
    ):
        assert f"function {fn}(" in code, f"{fn} missing"
    # The rows are actually draggable and wired to the handlers.
    assert 'draggable="true"' in code
    assert "ondragstart=" in code
    assert "ondrop=" in code


def test_drag_drop_mutates_models_and_rerenders(js: str):
    code = _strip_comments(js)
    # _aiTierReorder splices models[] then routes through the scope's existing
    # dirty+render side effects (the only reorder path now).
    body = re.search(
        r"function _aiTierReorder\([^)]*\)\s*\{(.*?)\n\}", code, re.DOTALL
    )
    assert body, "_aiTierReorder body not found"
    b = body.group(1)
    assert "splice" in b
    assert "_aiRenderPodDefaults()" in b
    assert "_aiRenderTiers(" in b


def test_drag_only_reorders_within_same_tier(js: str):
    code = _strip_comments(js)
    # Dragover/drop guard against cross-tier drops (scope + key must match) —
    # this is within-tier reorder, NOT pool→tier DnD.
    assert "_aiDragState.scope !== scope" in code
    assert "_aiDragState.key !== key" in code


# ── 5. ↑↓ buttons RESTORED (webview-safe); dead per-scope helpers stay gone ───

def test_reorder_buttons_restored_but_dead_move_helpers_stay_gone(js: str):
    code = _strip_comments(js)
    # The dead per-scope move primitives stay deleted — Phase 10c routes the
    # restored buttons through the shared _aiTierReorder(), not these helpers.
    assert "function _aiTierMoveModel(" not in code
    assert "function _aiPodTierMoveModel(" not in code
    # The shared tier row RENDERS accessible ↑/↓ move buttons again (Phase 10c)
    # — the reliable, webview-safe reorder path (native HTML5 DnD is a silent
    # no-op in the Tauri/wry desktop app). They delegate to _aiTierReorder().
    row = re.search(
        r"function _aiTierModelRow\([^)]*\)\s*\{(.*?)\n\}", code, re.DOTALL
    )
    assert row, "_aiTierModelRow body not found"
    r = row.group(1)
    assert 'title="Move up"' in r and 'title="Move down"' in r
    assert r.count("onclick=\"_aiTierReorder(") >= 2, \
        "move buttons must route through the shared reorder helper"
    # Real <button> elements with aria-labels (keyboard-operable, not bare
    # unicode glyphs in a non-interactive span).
    assert r.count("<button") >= 2 and r.count("aria-label=") >= 2
    # And still no right-column × button — removal stays inline on the chip.
    assert "btn-ghost btn-sm" not in r


def test_remove_is_inline_x_on_chip(js: str):
    code = _strip_comments(js)
    # _aiModelChip renders the inline × from an onRemove handler.
    chip = re.search(
        r"function _aiModelChip\([^)]*\)\s*\{(.*?)\n\}", code, re.DOTALL
    )
    assert chip, "_aiModelChip body not found"
    c = chip.group(1)
    assert "onRemove" in c
    assert "ai-chip-x" in c
    # The tier row wires the chip's × to the existing remove fn (per scope).
    assert "_aiTierRemoveModel(" in code
    assert "_aiPodTierRemoveModel(" in code
    row = re.search(
        r"function _aiTierModelRow\([^)]*\)\s*\{(.*?)\n\}", code, re.DOTALL
    )
    assert "onRemove" in row.group(1)


def test_inline_x_styled_via_token_not_inline_rgba(css: str):
    # The inline × on a chip is token-based (--danger on hover), no inline
    # rgba(0,0,0,…) per style-guide §7.
    m = re.search(r"\.ai-chip-x\s*\{([^}]*)\}", css)
    assert m, ".ai-chip-x rule missing"
    assert re.search(r"\.ai-chip-x:hover\s*\{[^}]*var\(--danger\)", css)


# ── 6. Wizard preview stays read-only (no drag, no inline ×) ──────────────────

def test_wizard_preview_is_read_only(js: str):
    code = _strip_comments(js)
    body = re.search(
        r"function _aiEasyPreviewRows\(catalog\)\s*\{(.*?)\n\}", code, re.DOTALL
    )
    assert body, "_aiEasyPreviewRows body not found"
    b = body.group(1)
    assert 'draggable="true"' not in b
    assert "ondragstart" not in b
    # The preview chip call passes no onRemove (no inline × on read-only rows).
    assert "onRemove" not in b


# ── 7. No pool→tier drag-and-drop crept in (spec §Non-goals) ──────────────────

def test_no_pool_to_tier_dnd(js: str):
    code = _strip_comments(js)
    # The deferred non-goal is dragging FROM a catalog/pool INTO a tier. The
    # picker (a <select>) is the only add path — there is no draggable pool.
    assert "ai-pool-drag" not in code
    assert "pool-to-tier" not in code
    # The "Add a model…" affordance is the validated picker, not a drop target.
    assert "Add a model…" in code


# ── 8. Observed-cost pill tooltip reads as an effective cost, not a rate ───────

def test_observed_cost_pill_tooltip_is_honest_effective_cost(js: str):
    # usd_per_1k_blended = total spend (cache cost INCLUDED) ÷ I/O volume, i.e.
    # an EFFECTIVE all-in cost per 1k I/O tokens — NOT a clean per-token list
    # rate. The chip tooltip must say so honestly (#2788 review nit): name it
    # an effective cost, flag that cache is included, and disclaim the clean
    # per-token reading.
    m = re.search(
        r'class="ai-observed-cost-pill"\s+title="([^"]*)"', js
    )
    assert m, "observed-cost pill tooltip not found"
    tip = m.group(1)
    assert "Effective $/1k" in tip
    assert "incl. cache" in tip
    assert "not a clean per-token list rate" in tip
    # Must NOT resurrect the overstated "rate"/"blended rate" framing that the
    # #2788 review flagged — the figure is a cost, not a clean traffic rate.
    assert "traffic rate" not in tip
    assert "Observed blended $/1k" not in tip
