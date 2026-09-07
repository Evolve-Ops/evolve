"""UI markup-contract tests for the MCP install modal's inline-token paste
affordance.

Same pattern as test_inbox_repos_ui — pin the load-bearing IDs, function
names, and POST body shape so a casual edit can't silently break the
round-trip with the install backend.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_INDEX_HTML = _ADMIN_PKG / "evolve_admin" / "web" / "index.html"


_PLUGINS_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "plugins.js"
def _read_html() -> str:
    return _INDEX_HTML.read_text() + "\n" + _PLUGINS_JS.read_text()


def test_install_modal_renders_token_input_per_env():
    """Each required_env must render BOTH a slot-name input (existing
    behavior) AND a token-value input (new inline-paste affordance).
    """
    html = _read_html()
    # The env-rendering helper is the right home for this contract.
    start = html.find("function _mcpRenderInstallEnvs(")
    assert start > 0, (
        "_mcpRenderInstallEnvs must exist — the openMcpInstallModal flow "
        "delegates env rendering here so the bot-picker onchange can re-render."
    )
    fn_body = html[start:start + 4000]
    # The slot-name input class stays the same (mcp-env-input) so the
    # existing submitMcpInstall keeps reading it.
    assert "mcp-env-input" in fn_body
    # The new token input has a distinct class so submit can collect it
    # separately.
    assert "mcp-env-token-input" in fn_body
    # And the token field must be type=password so it doesn't shoulder-surf.
    assert 'type="password"' in fn_body


def test_install_modal_auto_suggests_slot_from_keystore_hint():
    """The slot name input should default to the keystore_hint expanded
    against the picked bot — operator shouldn't have to invent the name."""
    html = _read_html()
    # Helper function that expands "github-*" → "github-team_bot_a" for the picked bot.
    assert "function _mcpExpandKeystoreHint(" in html
    # The render function uses it
    start = html.find("function _mcpRenderInstallEnvs(")
    fn_body = html[start:start + 4000]
    assert "_mcpExpandKeystoreHint(" in fn_body


def test_bot_picker_re_renders_envs_on_change():
    """When the operator picks a different bot, the auto-suggested slot
    names must update (e.g. "github-team_bot_a" → "github-personal_bot")."""
    html = _read_html()
    start = html.find("function openMcpInstallModal(")
    fn_body = html[start:start + 3000]
    # The onchange handler must wire to the env-render function.
    assert "botSel.onchange = _mcpRenderInstallEnvs" in fn_body, (
        "openMcpInstallModal must wire bot-picker onchange to re-render the "
        "env form — otherwise the auto-suggested slot name freezes on the "
        "first-picked bot."
    )


def test_submit_collects_both_slot_and_token():
    """submitMcpInstall must read both the slot-name and the token-value
    inputs, and POST the token-value dict only when at least one was
    filled."""
    html = _read_html()
    start = html.find("async function submitMcpInstall(")
    assert start > 0
    fn_body = html[start:start + 3000]
    # Reads both input classes
    assert "mcp-env-input" in fn_body
    assert "mcp-env-token-input" in fn_body
    # Sends token_values in the request body
    assert "token_values" in fn_body
    # Routes to the existing install endpoint (extended, not replaced)
    assert "/api/mcp-admin/install" in fn_body


def test_submit_omits_token_values_when_empty():
    """If the operator left every token field blank, the request body
    should NOT include `token_values` (preserves backwards-compatible
    behavior for operators who already have keystore entries)."""
    html = _read_html()
    start = html.find("async function submitMcpInstall(")
    fn_body = html[start:start + 3000]
    # The handler should conditionally attach token_values to reqBody
    # only when Object.keys(tokenValues).length > 0.
    assert "Object.keys(tokenValues).length" in fn_body, (
        "submitMcpInstall must conditionally attach token_values — bare "
        "no-tokens installs should send the same body shape as before."
    )


def test_modal_static_text_mentions_inline_paste():
    """The modal's static help text under the env form should reflect the
    new inline-paste capability, not the old "create the slot first" copy.
    """
    html = _read_html()
    # Locate the modal block
    start = html.find('id="mcp-install-modal"')
    assert start > 0
    # Take a window covering the whole modal
    window = html[start:start + 2000]
    # New copy mentions paste-inline
    assert "Paste a token inline" in window or "paste a token inline" in window.lower() or "token field blank" in window.lower(), (
        "Modal help text should describe the inline-paste flow, not the old "
        "'add keystore key first via Credentials' copy."
    )


def test_keystore_hint_expansion_handles_no_wildcard():
    """If a catalog entry has a literal keystore_hint with no `*`, the
    expansion helper should return it verbatim — not fail or interpolate."""
    html = _read_html()
    start = html.find("function _mcpExpandKeystoreHint(")
    fn_body = html[start:start + 600]
    # The helper checks for '*' before doing string replacement
    assert "indexOf('*')" in fn_body
