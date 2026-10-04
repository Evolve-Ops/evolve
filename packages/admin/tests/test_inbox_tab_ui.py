"""tests/test_inbox_tab_ui.py — Inbox tab markup + JS contract.

Phase 1b of the Issue Inbox spec. The Inbox tab is a list+detail UI
hanging off /api/inbox. These tests pin the load-bearing markup and JS
identifiers so a casual edit can't silently break the wiring:

  - Nav item under "Improve" with the correct data-page value + badge
  - Page block with the documented summary tiles + table + detail card
  - JS dispatch entry (``if (page === 'inbox') loadInbox()``)
  - JS function definitions for load / open / close / mark-seen
  - Fetch wiring to /api/inbox and the correct POST endpoint

If you're refactoring the Inbox UI, expect this file to need updates.
Most assertions match on substring (not full HTML) so reasonable
edits — adding a column, restyling, renaming an internal variable —
don't trip the suite, but the structural contract stays pinned.
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


# ── Nav ─────────────────────────────────────────────────────────────────────


def test_nav_item_exists_under_developer():
    """The Issues sidebar entry (data-page="inbox" internally) must
    live in the Developer nav-section. Was moved out of Improve into
    Developer alongside the page rename — the page caters to operators
    who file/track GitHub issues, which is a developer-shaped concern,
    not an Improvements-page-style RSI surface."""
    html = _read_html()
    dev_idx = html.find('<div class="nav-section">Developer</div>')
    assert dev_idx > 0, "Developer nav-section header not found"
    next_section_idx = html.find('<div class="nav-section">', dev_idx + 10)
    block = html[dev_idx:] if next_section_idx < 0 else html[dev_idx:next_section_idx]
    assert 'data-page="inbox"' in block, (
        "Issues nav item (data-page='inbox') must live in the Developer "
        "section. The data-page identifier kept the 'inbox' name for "
        "backwards-compat with URL hashes; only the operator-visible "
        "label changed to 'Issues'."
    )


def test_nav_label_is_issues_not_inbox():
    """User-facing rename 2026-06-04: 'Inbox' → 'Issues'. The internal
    data-page identifier stays 'inbox' (URL hash backwards-compat), but
    the visible link text and page H1 must both read 'Issues'."""
    html = _read_html()
    # The nav item's visible text follows the glyph and a space.
    assert 'data-page="inbox"' in html
    # The link text immediately follows ❗ — pin both the new and
    # absence-of-old to catch a half-renamed regression.
    assert '❗ Issues' in html, "Sidebar link text must be '❗ Issues'"
    assert '<h1>Issues</h1>' in html, "Page H1 must read 'Issues'"


def test_nav_item_has_badge_for_unread_count():
    """The sidebar badge is how the operator notices new activity from
    elsewhere in the UI. Without it the Inbox tab is invisible until
    the operator happens to navigate to it."""
    html = _read_html()
    assert 'id="badge-inbox"' in html


# ── Page block ──────────────────────────────────────────────────────────────


def test_page_block_exists():
    assert '<div class="page" id="page-inbox">' in _read_html()


def test_page_has_summary_tiles():
    """Three summary tiles set the operator's at-a-glance read: filed
    count, unread count, and the explanatory blurb tile."""
    html = _read_html()
    assert 'id="inbox-total"' in html
    assert 'id="inbox-unread"' in html


def test_page_has_list_table():
    html = _read_html()
    assert 'id="inbox-table"' in html
    assert 'id="inbox-tbody"' in html


def test_page_has_unread_filter_toggle():
    html = _read_html()
    assert 'id="inbox-filter-unread"' in html


def test_page_has_detail_card():
    """The detail card is hidden by default and shown on row click.
    Pin both the card and its inner elements."""
    html = _read_html()
    assert 'id="inbox-detail-card"' in html
    assert 'id="inbox-detail-title"' in html
    assert 'id="inbox-detail-body"' in html
    assert 'id="inbox-detail-activity"' in html
    assert 'id="inbox-mark-seen-btn"' in html


def test_detail_card_starts_hidden():
    """The detail card must start with display:none — otherwise it
    flashes on first render before any row is selected."""
    html = _read_html()
    # Find the detail card opening tag and check the inline style.
    m = re.search(
        r'<div class="card" id="inbox-detail-card"[^>]*>',
        html,
    )
    assert m is not None, "inbox-detail-card tag not found"
    assert "display:none" in m.group(0), (
        "Detail card must start hidden (display:none) to avoid first-paint flash"
    )


# ── JS dispatch ─────────────────────────────────────────────────────────────


def test_nav_dispatch_calls_load_inbox():
    """The page-switch code must invoke loadInbox when the operator
    navigates to the Inbox tab. Tolerant of additional load calls in
    the dispatch block — Phase 3 adds loadInboxRepos() and Phase 4b
    adds loadInboxTriage() alongside."""
    html = _read_html()
    m = re.search(
        r"if\s*\(\s*page\s*===?\s*['\"]inbox['\"]\s*\)\s*(\{[^}]*\}|loadInbox\s*\(\s*\))",
        html,
    )
    assert m is not None, "nav() dispatch must handle page === 'inbox'"
    assert "loadInbox()" in m.group(0), (
        "nav() dispatch for inbox must call loadInbox()"
    )


# ── JS function shape ──────────────────────────────────────────────────────


def test_load_inbox_function_defined():
    html = _read_html()
    assert "async function loadInbox()" in html


def test_load_inbox_fetches_api_inbox():
    """loadInbox must hit GET /api/inbox — pinning this prevents
    accidental hardcoding of a different URL during a refactor."""
    html = _read_html()
    assert "/api/inbox" in html


def test_unread_only_filter_appends_query_param():
    """The unread_only checkbox should drive the query string. Pin the
    parameter name so the backend contract stays stable."""
    html = _read_html()
    assert "unread_only=1" in html


def test_open_close_detail_functions_defined():
    html = _read_html()
    assert "function _inboxOpenDetail(" in html
    assert "function _inboxCloseDetail(" in html
    assert "function _inboxRenderDetail(" in html


def test_mark_seen_function_defined():
    html = _read_html()
    assert "async function _inboxMarkSeen()" in html


def test_mark_seen_posts_to_seen_endpoint():
    """The Mark-as-seen action must POST to the documented endpoint.
    Pinning the URL prevents drift from the backend route (PR 1a)."""
    html = _read_html()
    assert re.search(
        r"/api/evo/intake/\$\{[^}]+\}/seen",
        html,
    ), "Mark-as-seen must POST to /api/evo/intake/<id>/seen"


def test_mark_seen_called_with_post_method():
    html = _read_html()
    # The fetch call near _inboxMarkSeen should explicitly say method: 'POST'.
    # Find _inboxMarkSeen and check it includes the POST method.
    start = html.find("async function _inboxMarkSeen()")
    assert start > 0
    # Look in the next 800 chars (function body) for the POST verb.
    body = html[start:start + 800]
    assert "method: 'POST'" in body or 'method: "POST"' in body, (
        "_inboxMarkSeen must use POST"
    )


# ── Rendering safety ──────────────────────────────────────────────────────


def test_table_row_renders_with_escaped_intake_id():
    """The row's onclick passes the intake id into a JS string — must
    be escaped to defend against a hypothetical id with quotes in it.
    (Real intake ids are date-prefix + hex, but escaping is cheap and
    forward-compatible if the id format ever changes.)"""
    html = _read_html()
    # _renderInboxTable should produce onclick="_inboxOpenDetail('${escHtml(ix.id)}')"
    assert "_inboxOpenDetail('${escHtml(ix.id)}')" in html, (
        "Row onclick must escHtml() the intake id to avoid XSS / breakage"
    )


def test_detail_body_uses_textContent_not_innerHTML():
    """The intake body is operator-authored prose and could contain
    angle brackets; rendering with innerHTML would let those become
    HTML tags. Use textContent."""
    html = _read_html()
    # The exact line is bodyEl.textContent = ix.body || '';
    assert "bodyEl.textContent =" in html or "bodyEl.textContent=" in html, (
        "Detail body must render via textContent, not innerHTML"
    )


def test_activity_snippet_escaped():
    """Activity snippets come from maintainer comments — definitely
    untrusted input. Must go through escHtml() before reaching innerHTML."""
    html = _read_html()
    assert "escHtml(e.snippet)" in html


def test_activity_actor_escaped():
    html = _read_html()
    assert "escHtml(e.actor" in html


# ── Drafts card (added 2026-06-04) ──────────────────────────────────────────
#
# Surfaces pre-promotion intakes (those at {sharedDir}/intake/open/
# and triaged/) so an operator who said "evo, file this as a bug"
# can actually see, promote, or dismiss the resulting draft from the
# admin UI rather than only via evo chat.


def test_drafts_card_exists():
    """The Drafts card markup must be present on the Issues page so
    pre-promotion intakes have a UI affordance."""
    html = _read_html()
    assert 'id="inbox-drafts-card"' in html
    assert 'id="inbox-drafts-table"' in html
    assert 'id="inbox-drafts-tbody"' in html


def test_drafts_card_starts_hidden():
    """Empty by default — the card reveals itself once loadInboxDrafts
    finds at least one draft. Pinning prevents a flash of an empty
    table on first paint."""
    html = _read_html()
    m = re.search(
        r'<div class="card" id="inbox-drafts-card"[^>]*>',
        html,
    )
    assert m is not None
    assert "display:none" in m.group(0)


def test_drafts_summary_tile_exists():
    """The summary band now carries a Drafts count alongside Filed +
    Unread. Pin the id so the JS render can address it."""
    html = _read_html()
    assert 'id="inbox-drafts-count"' in html


def test_drafts_load_function_defined():
    html = _read_html()
    assert "async function loadInboxDrafts()" in html


def test_drafts_dispatch_wired():
    """The Issues page-switch must call loadInboxDrafts alongside the
    existing loadInbox / loadInboxRepos / loadInboxTriage."""
    html = _read_html()
    m = re.search(
        r"if\s*\(\s*page\s*===?\s*['\"]inbox['\"]\s*\)\s*\{[^}]*\}",
        html,
    )
    assert m is not None, "nav() dispatch block for inbox not found"
    assert "loadInboxDrafts()" in m.group(0)


def test_drafts_uses_intake_state_endpoints():
    """loadInboxDrafts must fetch the open + triaged states from the
    existing /api/evo/intake endpoint. Pinning the URL prevents drift
    from the backend contract (these endpoints predate this card)."""
    html = _read_html()
    start = html.find("async function loadInboxDrafts()")
    assert start > 0
    body = html[start:start + 2500]
    assert "/api/evo/intake?state=open" in body
    assert "/api/evo/intake?state=triaged" in body


def test_drafts_promote_posts_to_promote_endpoint():
    html = _read_html()
    assert "/intake/${encodeURIComponent(id)}/promote" in html


def test_drafts_dismiss_posts_to_dismiss_endpoint():
    html = _read_html()
    assert "/intake/${encodeURIComponent(id)}/dismiss" in html


def test_drafts_row_id_escaped():
    """Intake ids go into onclick handlers — must be escaped against
    a hypothetical id containing quotes. Same defense as the existing
    inbox row test."""
    html = _read_html()
    assert "const idEsc = escHtml(id)" in html


def test_drafts_detail_body_uses_textContent():
    """The draft body is operator/evo-authored prose; must render via
    textContent (not innerHTML) so angle brackets stay literal."""
    html = _read_html()
    start = html.find("function _inboxDraftsOpenDetail(")
    assert start > 0
    body = html[start:start + 1500]
    assert "bodyEl.textContent" in body
