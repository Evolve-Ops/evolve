"""tests/test_ai_optimization_tier_reorder.py — within-tier reorder control (UI).

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md (tier fallback-chain order).

The admin SPA has no JS test harness; the established pattern (see
test_ai_optimization_easy_setup.py) is to pin UI behaviour by asserting on
ai-optimization.js *source strings*.

These pin the desktop-webview fix: a tier's fallback chain must be reorderable
through a control that does NOT depend solely on native HTML5 drag-and-drop.
Native HTML5 DnD events do not fire in the Tauri/wry desktop webview (the same
webview-limitation class that made native confirm() a silent no-op — #3255 /
#3258), so DnD-only reorder is a silent no-op on the desktop app. The fix
restores accessible ↑/↓ move buttons as the reliable path; DnD stays only as a
browser-only progressive enhancement.
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


def _row_region(code: str) -> str:
    start = code.find("function _aiTierModelRow(")
    end = code.find("function _aiTierDragStart(")
    assert start != -1 and end != -1, "tier-model-row renderer not found"
    return code[start:end]


# ── 1. A non-DnD reorder control exists (the desktop-webview fix) ──────────────

def test_reorder_does_not_depend_only_on_native_dnd(js: str):
    code = _strip_comments(js)
    region = _row_region(code)
    # The row still offers native DnD (browser progressive enhancement) ...
    assert 'draggable="true"' in region and "ondragstart=" in region
    # ... but it must ALSO render a click-driven reorder control that does not
    # ride on native drag events — accessible ↑/↓ buttons wired to onclick.
    assert "ai-tier-move-btn" in region, "webview-safe move buttons must exist"
    assert region.count("onclick=\"_aiTierReorder(") >= 2, (
        "both ↑ and ↓ must invoke the reorder helper via a plain click "
        "(not a native drag event)"
    )


def test_move_buttons_are_accessible_buttons(js: str):
    code = _strip_comments(js)
    region = _row_region(code)
    # Real <button> elements (keyboard-operable), not bare unicode glyphs in a
    # non-interactive span, and each carries an aria-label naming the model.
    assert region.count("<button") >= 2
    assert region.count("aria-label=") >= 2
    assert "fallback chain" in region, "aria-label should describe the move"


# ── 2. Reorder routes through the single shared mutation helper ───────────────

def test_reorder_routes_through_shared_helper(js: str):
    code = _strip_comments(js)
    region = _row_region(code)
    # The buttons must NOT splice models[] themselves — they delegate to
    # _aiTierReorder(scope, key, from, to), the one place that mutates the
    # array, sets the dirty flag, and re-renders for both 'pod' and 'bot'.
    assert ".splice(" not in region, "row renderer must not mutate models[] itself"
    # The shared helper does the splice + dirty + re-render for both scopes.
    assert "function _aiTierReorder(" in code
    start = code.find("function _aiTierReorder(")
    end = code.find("async function _aiLoadListings(")
    helper = code[start:end]
    assert ".splice(" in helper
    assert "_aiPodDefaultsDirty = true" in helper and "_aiRenderPodDefaults()" in helper
    assert "_aiDirtySection = 'tiers'" in helper and "_aiRenderTiers(" in helper


# ── 3. End rows disable the out-of-range move (no auto-save on reorder) ───────

def test_end_rows_disable_out_of_range_move(js: str):
    code = _strip_comments(js)
    region = _row_region(code)
    # ↑ disabled at the head (i <= 0); ↓ disabled at the tail (i >= n - 1).
    assert "i <= 0" in region and "i >= n - 1" in region
    assert "disabled" in region


def test_reorder_is_pending_state_only(js: str):
    # Reorder is local/pending state — it sets the dirty flag and re-renders,
    # but never auto-persists; the operator still clicks the explicit save.
    code = _strip_comments(js)
    start = code.find("function _aiTierReorder(")
    end = code.find("async function _aiLoadListings(")
    helper = code[start:end]
    # No network write inside the reorder path.
    assert "api(" not in helper, "reorder must not POST — it is pending state only"
