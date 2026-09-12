"""Structural pins for the per-bot Setup checklist UI.

Regex-on-source checks against index.html — no browser. Each test name
maps to one invariant in the rendered markup or wiring. Mirrors the
pattern used by ``test_alerts_per_row_actions.py``.

Catches regressions like:
  - The card placeholder being removed or renamed
  - The render function not being called from loadConfigBot()
  - The "Stop showing on tile" toggle's action label / endpoint drift
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_BOT_DETAIL_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "bot-detail.js"
_POD_CONFIG_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "pod-config.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _POD_CONFIG_JS.read_text(encoding="utf-8") + "\n" + _BOT_DETAIL_JS.read_text(encoding="utf-8")


def test_card_placeholder_exists_under_per_bot_settings():
    """The card div with id=botcfg-setup-checklist must exist so
    renderBotSetupChecklistCard has somewhere to write its HTML."""
    html = _html()
    assert 'id="botcfg-setup-checklist"' in html


def test_card_title_says_setup_checklist():
    html = _html()
    assert re.search(
        r'<div class="card-title">Setup checklist<span class="help-btn">',
        html,
    ), "Setup checklist card-title missing"


def test_render_function_defined():
    html = _html()
    assert "async function renderBotSetupChecklistCard(botId)" in html


def test_render_function_called_from_load_config_bot():
    """loadConfigBot() must call renderBotSetupChecklistCard so the card
    populates when the operator switches bots."""
    html = _html()
    # Must appear inside the loadConfigBot body (between its opening line
    # and the next top-level async function).
    fn = re.search(
        r"async function loadConfigBot\(\)\s*\{(.+?)\n(?:async )?function ",
        html, re.DOTALL,
    )
    assert fn, "loadConfigBot function not found"
    body = fn.group(1)
    # Called twice — once for the no-bot reset, once for the selected bot.
    assert body.count("renderBotSetupChecklistCard(") >= 2


def test_go_button_clicks_data_page_nav_item():
    """The Go button uses the SPA's existing nav-click pattern
    (.nav-item[data-page=...]) instead of inventing its own router."""
    html = _html()
    assert "_setupChecklistGo" in html
    assert "document.querySelector(`.nav-item[data-page=" in html


def test_dismiss_endpoint_format_matches_backend_route():
    """Dismiss POST hits /api/admin/bots/<bot>/setup-checklist/items/<id>."""
    html = _html()
    fn = re.search(
        r"async function _setupChecklistSetState\(botId, itemId, state\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_setupChecklistSetState not found"
    body = fn.group(1)
    assert "/api/admin/bots/${encodeURIComponent(botId)}/setup-checklist/items/${encodeURIComponent(itemId)}" in body


def test_tile_toggle_calls_suppress_or_reset_endpoint():
    """The "Stop showing on tile" / "Show on tile" button POSTs to
    .../suppress or .../reset — the same names the backend exposes."""
    html = _html()
    fn = re.search(
        r"async function _setupChecklistTileToggle\(botId, action\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_setupChecklistTileToggle not found"
    body = fn.group(1)
    assert "/api/admin/bots/${encodeURIComponent(botId)}/setup-checklist/${action}" in body
