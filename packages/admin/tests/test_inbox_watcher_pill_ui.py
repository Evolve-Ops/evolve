"""Markup + JS contract tests for the inbound-watcher feature pill (PR 4c).

The pill lives on the Inbox tab right below the Tracked Repos card and
lets the operator turn the watcher launchd job on/off without dropping
to the CLI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_WEB = _ADMIN_PKG / "evolve_admin" / "web"
_INDEX_HTML = _WEB / "index.html"
_INBOX_JS = _WEB / "static" / "js" / "pages" / "inbox.js"


def _read_html() -> str:
    # Inbox JS now lives in pages/inbox.js (Phase 3b of the source split).
    # Concat so the existing regex-shape assertions stay valid.
    return _INDEX_HTML.read_text() + "\n" + _INBOX_JS.read_text()


# ── Markup ────────────────────────────────────────────────────────────────


def test_watcher_pill_lives_inside_tracked_repos_card():
    """The watcher pill must be inside the Tracked Repos card —
    its show/hide gate ties to repo count, so coupling them in markup
    keeps the behavior obvious."""
    html = _read_html()
    repos_card_idx = html.find('id="inbox-repos-list"')
    add_modal_idx = html.find('id="inbox-add-repo-modal"')
    assert 0 < repos_card_idx < add_modal_idx
    block = html[repos_card_idx:add_modal_idx]
    assert 'id="inbox-watcher-pill"' in block


def test_watcher_pill_starts_hidden():
    """The pill default-hides so a fresh standard install isn't asking
    'enable this thing' before the operator has tracked any repos."""
    html = _read_html()
    m = re.search(
        r'<div[^>]*id="inbox-watcher-pill"[^>]*>',
        html,
    )
    assert m is not None
    assert "display:none" in m.group(0)


def test_watcher_pill_has_required_children():
    """Pin the load-bearing inner element ids the JS writes into."""
    html = _read_html()
    for elem_id in (
        "inbox-watcher-indicator",
        "inbox-watcher-state",
        "inbox-watcher-sub",
        "inbox-watcher-toggle",
        "inbox-watcher-log",
    ):
        assert f'id="{elem_id}"' in html, f"missing {elem_id}"


# ── JS function shape ─────────────────────────────────────────────────────


def test_load_features_function_defined():
    html = _read_html()
    assert "async function loadInboxFeatures()" in html


def test_render_pill_function_defined():
    html = _read_html()
    assert "function _renderInboxWatcherPill()" in html


def test_toggle_function_defined():
    html = _read_html()
    assert "async function _inboxWatcherToggle()" in html


# ── API wiring ────────────────────────────────────────────────────────────


def test_load_hits_feature_status_endpoint():
    html = _read_html()
    start = html.find("async function loadInboxFeatures()")
    body = html[start:start + 1200]
    assert "/api/features/inbound_issues_watcher" in body


def test_toggle_posts_to_feature_endpoint():
    html = _read_html()
    start = html.find("async function _inboxWatcherToggle()")
    body = html[start:start + 2000]
    assert "/api/features/inbound_issues_watcher" in body
    assert "method: 'POST'" in body or 'method: "POST"' in body


def test_toggle_uses_confirm_before_enabling():
    """Turning the watcher ON starts polling third-party repos — needs
    an explicit confirm so it isn't a one-click destination from the
    Tracked Repos card."""
    html = _read_html()
    start = html.find("async function _inboxWatcherToggle()")
    body = html[start:start + 2000]
    assert "confirmModal(" in body


def test_toggle_sends_enabled_field():
    """The API contract requires {enabled: bool}. Verify the body shape."""
    html = _read_html()
    start = html.find("async function _inboxWatcherToggle()")
    body = html[start:start + 2000]
    assert "enabled:" in body or '"enabled":' in body or "'enabled':" in body


# ── Nav dispatch ──────────────────────────────────────────────────────────


def test_nav_dispatch_loads_features():
    """The Inbox tab's nav dispatch must call loadInboxFeatures so the
    pill hydrates on tab entry — otherwise the operator sees stale state."""
    html = _read_html()
    m = re.search(
        r"if\s*\(\s*page\s*===?\s*['\"]inbox['\"]\s*\)\s*\{([^}]+)\}",
        html,
    )
    assert m is not None
    assert "loadInboxFeatures()" in m.group(1)


# ── Rendering safety ─────────────────────────────────────────────────────


def test_render_uses_textContent_for_state_label():
    """The status string is operator-trusted but the relative-time hint
    is computed from a server-supplied ISO. textContent for both keeps
    the rendering simple and XSS-safe."""
    html = _read_html()
    start = html.find("function _renderInboxWatcherPill()")
    body = html[start:start + 3000]
    assert ".textContent" in body
    # Critically: no innerHTML for the sub-text or state strings, which
    # would let a malicious feature-name payload (forwards-compat) inject.
    # We allow innerHTML elsewhere in the file for static markup setup
    # but the pill renderer must avoid it.
    assert "sub.innerHTML" not in body
    assert "stateEl.innerHTML" not in body
