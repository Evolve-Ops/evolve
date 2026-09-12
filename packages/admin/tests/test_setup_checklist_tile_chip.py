"""Tests for the Overview tile-chip + Actions-menu wiring (step 4).

Three test files cover step 4:

  * test_setup_checklist_tile_chip.py (this file)
      — UI structural pins for the modal HTML, the chip-click dispatch
        switch in renderPodNode, and the Setup-checklist Actions ⋯ menu
        item gated on setup_in_actions_menu.

  * test_status_setup_chip.py
      — server.py /api/status path attaches the setup_progress chip to
        tile.health_chips when is_chip_visible, and the
        setup_in_actions_menu flag.

  * existing routes + detector tests still cover everything below.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
INDEX_HTML = _WEB / "index.html"
_BOT_DETAIL_JS = _WEB/ "static" / "js" / "pages" / "bot-detail.js"
_OVERVIEW_JS = _WEB/ "static" / "js" / "pages" / "overview.js"
_BASE_CSS = _WEB / "static" / "css" / "base.css"


def _html() -> str:
    # base.css holds the shell's CSS after the Phase-1 source split;
    # bot-detail.js holds the setup-checklist + handover + botLabel
    # functions (Phase 3ac); overview.js holds renderPodNode + the
    # chip-dispatch + Actions menu (Phase 3ai).
    return (
        INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _BASE_CSS.read_text(encoding="utf-8")
        + "\n"
        + _BOT_DETAIL_JS.read_text(encoding="utf-8")
        + "\n"
        + _OVERVIEW_JS.read_text(encoding="utf-8")
    )


# ── Modal HTML pins ───────────────────────────────────────────────────────


def test_setup_checklist_modal_overlay_exists():
    """The modal overlay must exist so openSetupChecklistModal has somewhere
    to add the .open class."""
    html = _html()
    assert 'id="setup-checklist-modal"' in html
    assert 'id="setup-checklist-modal-body"' in html
    assert 'id="setup-checklist-modal-title"' in html


def test_modal_closes_on_outside_click():
    """Match the cost-levers / breaker modal close-on-outside pattern."""
    html = _html()
    assert (
        "onclick=\"if(event.target===this)closeSetupChecklistModal()\""
        in html
    )


def test_modal_open_close_functions_defined():
    html = _html()
    assert "async function openSetupChecklistModal(botId)" in html
    assert "function closeSetupChecklistModal()" in html


# ── Chip dispatch in renderPodNode ────────────────────────────────────────


def test_chip_click_action_dispatches_to_modal():
    """The bot-tile chip renderer must recognise click_action and route
    'modal:setup_checklist' to openSetupChecklistModal."""
    html = _html()
    # Locate the chip dispatch block inside renderPodNode.
    chip_block = re.search(
        r"healthHtml = tile\.health_chips\.map\(c => \{(.+?)\}\)\.join\(''\);",
        html, re.DOTALL,
    )
    assert chip_block, "chip dispatch block not found in renderPodNode"
    body = chip_block.group(1)
    assert "click_action" in body
    assert "modal:setup_checklist" in body
    assert "openSetupChecklistModal" in body


def test_pod_chip_info_css_class_exists():
    """The setup_progress chip uses severity='info'. The CSS class must
    exist so the chip renders with the right colors."""
    html = _html()
    assert re.search(r"\.pod-chip-info\s*\{", html)


# ── Actions ⋯ menu integration ────────────────────────────────────────────


def test_actions_menu_setup_checklist_item_gated_on_flag():
    """The "Setup checklist" Actions ⋯ menu item must read
    b.tile.setup_in_actions_menu and conditionally render — when False,
    the menu stays clean (chip is the entry point)."""
    html = _html()
    fn = re.search(
        r"function _podTileMenuHtml\(id, b, isPrimary\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_podTileMenuHtml not found"
    body = fn.group(1)
    assert "setup_in_actions_menu" in body
    assert "openSetupChecklistModal" in body
    # The gate uses the flag directly, not "always-on"
    assert "b.tile && b.tile.setup_in_actions_menu" in body


# ── Status-element scoping (avoids modal vs Settings card collision) ──────


def test_status_div_id_is_per_bot():
    """When both the modal and the Settings card are mounted (theoretical
    edge case), document.getElementById on a shared id picks the wrong
    one. The render must scope the status id per-bot."""
    html = _html()
    fn = re.search(
        r"function _setupChecklistRender\(el, botId, data\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_setupChecklistRender not found"
    body = fn.group(1)
    assert "setup-checklist-status-${safeBot}" in body


def test_status_lookup_helper_exists():
    """A helper centralizes the per-bot status div lookup so action
    handlers don't repeat the id template literal."""
    html = _html()
    assert "function _setupChecklistStatusEl(botId)" in html


# ── Go-button bot id wiring ───────────────────────────────────────────────


def test_go_button_passes_bot_id():
    """_setupChecklistGo takes (deepLink, botId) so it can scope error
    messages to the right status div."""
    html = _html()
    assert "function _setupChecklistGo(deepLink, botId)" in html
    # And the row template calls it with both args
    assert "_setupChecklistGo('${escHtml(item.deep_link)}','${safeBot}')" in html


def test_go_button_closes_modal_before_navigating():
    """If the modal is open when Go fires, close it first — otherwise the
    overlay floats above the destination page."""
    html = _html()
    fn = re.search(
        r"function _setupChecklistGo\(deepLink, botId\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_setupChecklistGo not found"
    body = fn.group(1)
    assert "closeSetupChecklistModal()" in body
