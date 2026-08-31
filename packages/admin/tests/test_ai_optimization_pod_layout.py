"""tests/test_ai_optimization_pod_layout.py — Phase 10c (Addendum 7 D).

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 7 (Phase 10),
workstream D — POD-tab layout & copy:

  7.  The Model Freshness card is collapsible (.expand-icon / <details>),
      collapsed by default (no `open` attribute), with Check Now / Apply All
      reachable even while collapsed.
  8.  "How Evolve models work" explains the defaults/inheritance layering
      (Evolve code defaults <- pod defaults <- per-bot; Use pod defaults vs
      Custom) using the current role names (Fast / Standard / Power / Max /
      Judge) — NOT the retired Workhorse / Grunt.
  9.  The engine panel drops the per-row layer provenance chips; the role and
      judge cards no longer render _aiLayerLabel, and the engine intro no longer
      explains a per-row source chip.
  10. On the POD view the Default tier definitions card precedes the Engine
      models + Engine override cards (defaults lead, engine info follows).

Source-level assertions on index.html and ai-optimization.js — the admin SPA
has no JS test harness; the established pattern (see
test_ai_optimization_rank_presentation.py) is to scan the web source. No real
bot/user names appear; placeholders only.
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


def _freshness_card(html: str) -> str:
    """The Model Freshness card region (the <div class="card"> that wraps the
    pod-wide freshness panel), bounded by the Bot-tabs subtab bar that follows
    it in the markup."""
    # Anchor on the unique freshness panel ID (not the literal "Model Freshness"
    # text — that first matches the section COMMENT, and rfind from there walks
    # back into the PRIOR page's card, crossing a page boundary). The panel id
    # is inside the freshness card, so rfind lands on that card's own wrapper.
    panel = html.find('id="ai-freshness-panel"')
    assert panel != -1, "Model Freshness panel not found"
    card_open = html.rfind('<div class="card"', 0, panel)
    end = html.find('id="ai-bot-tabs"', panel)
    assert card_open != -1 and end != -1
    return html[card_open:end]


# ── Item 7: collapsible, collapsed-by-default Model Freshness ─────────────────


def test_freshness_card_is_collapsible_via_details(html: str):
    """The Model Freshness card uses a native <details>/<summary> with the
    repo's .expand-icon chevron — not a Unicode triangle (a lint BLOCK)."""
    region = _freshness_card(html)
    assert "<details" in region, "freshness card must be a collapsible <details>"
    assert "<summary" in region, "freshness card needs a <summary> header"
    assert "expand-icon" in region, (
        "collapse affordance must use the .expand-icon chevron (style-guide §9.13)"
    )
    # No Unicode expand triangles snuck in (those are a ui-style-lint BLOCK).
    for glyph in ("▸", "▾", "▼", "▶", "⟩"):
        assert glyph not in region, f"Unicode triangle {glyph!r} in freshness card"


def test_freshness_card_is_collapsed_by_default(html: str):
    """Collapsed by default — the <details> carries no `open` attribute."""
    region = _freshness_card(html)
    m = re.search(r"<details\b([^>]*)>", region)
    assert m, "no <details> tag in freshness card"
    assert "open" not in m.group(1), (
        "Model Freshness must be COLLAPSED by default (no `open` on <details>)"
    )


def test_freshness_actions_preserve_ids_and_stay_reachable(html: str):
    """The element IDs the JS targets survive, and Check Now / Apply All stay
    reachable while collapsed without toggling the card: they live in the
    <summary> flex row but stop event propagation on click so a button press
    never opens/closes the disclosure."""
    region = _freshness_card(html)
    for el_id in (
        'id="ai-freshness-panel"',
        'id="ai-freshness-check-btn"',
        'id="ai-freshness-apply-all-btn"',
    ):
        assert el_id in region, f"freshness card lost {el_id}"
    # Each action button guards its click so a press doesn't toggle the
    # <details> (the buttons sit inside <summary>).
    for btn_id, handler in (
        ("ai-freshness-check-btn", "_aiCheckModelFreshness"),
        ("ai-freshness-apply-all-btn", "_aiApplyAllFreshness"),
    ):
        m = re.search(rf'id="{btn_id}"[^>]*onclick="([^"]*)"', region)
        assert m, f"{btn_id} not found / has no onclick"
        onclick = m.group(1)
        assert "stopPropagation" in onclick, (
            f"{btn_id} must stopPropagation so its click doesn't toggle the card"
        )
        assert handler in onclick, f"{btn_id} must still call {handler}"


def test_freshness_collapsed_summary_surfaces_advisory_count(js: str):
    """Nice-to-have: the collapsed summary shows the advisory count so operators
    know to expand when drift exists. The renderer wires the count span."""
    assert "_aiSyncFreshnessSummaryCount(" in js
    m = re.search(
        r"function _aiSyncFreshnessSummaryCount\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL
    )
    assert m, "_aiSyncFreshnessSummaryCount not found"
    body = m.group(1)
    assert "ai-freshness-summary-count" in body


# ── Item 8: inheritance explainer (no stale Workhorse / Grunt) ────────────────


def _explainer(html: str) -> str:
    start = html.find("How Evolve models work")
    assert start != -1, "explainer card not found"
    # Bounded by the structural start of the next card (the Default tier
    # definitions card). We bound on its ID rather than the visible title,
    # because the explainer copy itself references "Default tier definitions".
    end = html.find('id="ai-pod-defaults-card"', start)
    assert end != -1
    return html[start:end]


def test_explainer_describes_inheritance_layering(html: str):
    """The explainer names the three-layer defaults/inheritance model and the
    per-bot Use-pod-defaults / Custom choice (Item 8)."""
    region = _explainer(html)
    low = region.lower()
    assert "pod default" in low, "explainer must name the pod-defaults layer"
    assert "use pod defaults" in low, "explainer must mention Use pod defaults"
    assert "custom" in low, "explainer must mention the Custom override"
    assert "per-bot" in low, "explainer must name the per-bot layer"


def test_explainer_uses_current_role_names_not_legacy(html: str):
    """Current role vocabulary (Fast/Standard/Power/Max/Judge), not the retired
    Workhorse / Grunt (Item 8)."""
    region = _explainer(html)
    assert "Workhorse" not in region, "stale 'Workhorse' label in explainer"
    assert "Grunt" not in region, "stale 'Grunt' label in explainer"
    for role in ("Fast", "Standard", "Power", "Max", "Judge"):
        assert role in region, f"explainer should name the {role} role"


def test_explainer_keeps_primary_bot_engine_sentence(html: str):
    """One brief sentence still notes the engine resolves from the primary
    bot (the Engine panels sit below)."""
    region = _explainer(html)
    assert "primary bot" in region.lower()


# ── Item 9: engine panel drops per-row provenance chips ───────────────────────


def test_engine_role_card_no_layer_provenance_row(js: str):
    """`_aiRenderRoleCard` no longer renders the per-row _aiLayerLabel
    provenance (confusing noise on the engine panel)."""
    m = re.search(r"function _aiRenderRoleCard\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL)
    assert m, "_aiRenderRoleCard not found"
    assert "_aiLayerLabel(" not in m.group(1), (
        "engine role card must not render the layer provenance row (Item 9)"
    )


def test_engine_judge_card_no_layer_provenance_row(js: str):
    """`_aiRenderJudgeCard` likewise drops the provenance row."""
    m = re.search(r"function _aiRenderJudgeCard\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL)
    assert m, "_aiRenderJudgeCard not found"
    assert "_aiLayerLabel(" not in m.group(1), (
        "engine judge card must not render the layer provenance row (Item 9)"
    )


def test_engine_intro_drops_source_chip_explanation(js: str):
    """The engine-panel intro no longer explains a per-row source chip, but
    keeps the read-only framing."""
    m = re.search(r"function _aiRenderPodEngine\(.*?\n\}", js, re.DOTALL)
    assert m, "_aiRenderPodEngine not found"
    body = m.group(0)
    assert "source chip" not in body.lower(), (
        "engine intro must drop the source-chip explanation (Item 9)"
    )
    # Read-only framing survives (the panel is still informational).
    assert "read-only" in body.lower()


def test_layer_label_helper_preserved(js: str):
    """`_aiLayerLabel` is NOT deleted — after item 9 it has no callers, but the
    spec says keep it (reserved for a future evo-own-tab provenance surface);
    it's `_`-prefixed so eslint's varsIgnorePattern tolerates the dormant def.
    The `ai-layer-chip` CSS class is separately still LIVE — the Use-defaults/
    Custom toggle chips and the pod-defaults editor render it directly via
    inline <span>, independent of this function."""
    assert "function _aiLayerLabel(" in js
    assert "ai-layer-chip" in js


# ── Item 10: defaults lead, engine info follows ──────────────────────────────


def test_pod_view_defaults_card_precedes_engine_cards(html: str):
    """On the POD view, Default tier definitions comes before Engine models and
    Engine override (Item 10)."""
    i_defaults = html.find('id="ai-pod-defaults-card"')
    i_engine = html.find('id="ai-pod-engine-card"')
    i_override = html.find('id="ai-pod-override-card"')
    assert i_defaults != -1 and i_engine != -1 and i_override != -1
    assert i_defaults < i_engine, (
        "Default tier definitions must precede Engine models (Item 10)"
    )
    assert i_engine < i_override, (
        "Engine models must precede Engine override (unchanged relative order)"
    )


# ── Item 11: prominent easy-setup CTA at the top of the POD-default block ──────


def test_pod_easy_setup_is_prominent_primary_button(js: str):
    """The POD-default editor's Easy setup button is a prominent primary CTA,
    not a small secondary (btn-ghost btn-sm) button (Item 11)."""
    # The prominent CTA: full-size btn-primary wired to the pod easy-setup.
    assert (
        'class="btn btn-primary" onclick="_aiOpenEasySetup(\'pod\')"'
        in js
    ), "pod Easy setup must be a prominent btn-primary CTA"
    # The retired small/secondary variant is gone.
    assert 'btn btn-ghost btn-sm" onclick="_aiOpenEasySetup(\'pod\')"' not in js, (
        "the small secondary pod Easy setup button must be removed (Item 11)"
    )
    # Exactly one pod easy-setup trigger (moved, not duplicated).
    assert js.count("_aiOpenEasySetup('pod')") == 1


def test_pod_easy_setup_sits_above_the_manual_save(js: str):
    """The CTA leads the block — it appears before the manual Save default tiers
    button in the pod-defaults render (top of the tier-definitions block)."""
    i_easy = js.find('id="ai-pod-defaults-easy-btn"')
    i_save = js.find('id="ai-pod-defaults-save-btn"')
    assert i_easy != -1 and i_save != -1
    assert i_easy < i_save, (
        "Easy setup CTA must lead (appear above) the manual Save button (Item 11)"
    )
