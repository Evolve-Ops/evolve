"""Markup + JS contract tests for the Triage queue UI card (Phase 4b)."""

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


def test_triage_card_exists_under_page_inbox():
    """The triage queue lives inside #page-inbox so it sits alongside
    the existing inbox list — not on a separate tab."""
    html = _read_html()
    page_start = html.find('<div class="page" id="page-inbox">')
    assert page_start > 0
    next_page = html.find('<div class="page"', page_start + 10)
    inbox_block = html[page_start:next_page if next_page > 0 else len(html)]
    assert 'id="inbox-triage-card"' in inbox_block
    assert 'id="inbox-triage-tbody"' in inbox_block


def test_triage_card_starts_hidden():
    """Hidden by default — the loader reveals it when there's data,
    keeps it hidden when empty so non-maintainer pods don't see a
    perpetually-empty section."""
    html = _read_html()
    m = re.search(
        r'<div class="card" id="inbox-triage-card"[^>]*>',
        html,
    )
    assert m is not None
    assert "display:none" in m.group(0)


def test_triage_urgency_filter_present():
    """The urgency dropdown lets the operator scope to the most-urgent
    items. Pin the filter values that the API contract supports."""
    html = _read_html()
    assert 'id="inbox-triage-filter-urgency"' in html
    # The filter passes "p0", "p0,p1", "p0,p1,p2" to the API; pin the
    # documented values for both ends of the contract.
    for val in ('value="p0"', 'value="p0,p1"', 'value="p0,p1,p2"'):
        assert val in html


def test_triage_detail_card_present_and_hidden():
    """Detail view hangs off a click on the triage queue row."""
    html = _read_html()
    assert 'id="inbox-triage-detail-card"' in html
    assert 'id="inbox-triage-detail-title"' in html
    assert 'id="inbox-triage-detail-verdict"' in html
    assert 'id="inbox-triage-detail-draft"' in html
    # Detail card hidden by default.
    m = re.search(
        r'<div class="card" id="inbox-triage-detail-card"[^>]*>',
        html,
    )
    assert m is not None
    assert "display:none" in m.group(0)


# ── JS dispatch + function shape ─────────────────────────────────────────


def test_nav_dispatch_calls_load_inbox_triage():
    """nav() → page=inbox must also invoke loadInboxTriage so the
    triage queue actually renders when the operator enters the tab."""
    html = _read_html()
    m = re.search(
        r"if\s*\(\s*page\s*===?\s*['\"]inbox['\"]\s*\)\s*\{([^}]+)\}",
        html,
    )
    assert m is not None, "nav() dispatch for inbox must use braced form for multiple calls"
    assert "loadInboxTriage()" in m.group(1)


def test_load_inbox_triage_defined():
    html = _read_html()
    assert "async function loadInboxTriage()" in html


def test_load_inbox_triage_hits_api():
    html = _read_html()
    start = html.find("async function loadInboxTriage()")
    fn_body = html[start:start + 2000]
    assert "/api/inbox/triage" in fn_body


def test_load_inbox_triage_passes_urgency_filter():
    """The urgency-filter dropdown's value must flow through to the
    API as ?urgency=... — otherwise the filter is decorative."""
    html = _read_html()
    start = html.find("async function loadInboxTriage()")
    fn_body = html[start:start + 2000]
    assert "urgency=" in fn_body
    assert "encodeURIComponent" in fn_body


def test_load_inbox_triage_hides_card_when_empty():
    """Empty queue must hide the card — non-maintainer pods should
    never see a perpetually-empty section."""
    html = _read_html()
    start = html.find("async function loadInboxTriage()")
    fn_body = html[start:start + 2000]
    # The empty-list path explicitly sets display:none on the card.
    assert "display = 'none'" in fn_body or 'display = "none"' in fn_body


def test_triage_detail_open_close_functions_defined():
    html = _read_html()
    assert "function _inboxOpenTriageDetail(" in html
    assert "function _inboxCloseTriageDetail(" in html
    assert "function _inboxRenderTriageDetail(" in html


# ── Rendering safety ─────────────────────────────────────────────────────


def test_triage_row_escapes_untrusted_strings():
    """Every triage field comes from a GH issue authored by SOMEONE
    ELSE — they can include arbitrary text. Must escape before
    rendering with innerHTML."""
    html = _read_html()
    start = html.find("function _renderInboxTriage(")
    fn_body = html[start:start + 3000]
    # The inbound title, author, category labels, and recommendation
    # text all go through escHtml.
    assert "escHtml(" in fn_body
    # Row onclick passes the intake id — escape that too.
    assert "_inboxOpenTriageDetail('${escHtml(ix.id)}')" in fn_body


def test_triage_detail_body_uses_textContent():
    """The inbound issue body is fully untrusted (filed by random
    GitHub users). Render via textContent, never innerHTML."""
    html = _read_html()
    start = html.find("function _inboxRenderTriageDetail(")
    fn_body = html[start:start + 3000]
    # Find the body assignment.
    assert "bodyEl.textContent =" in fn_body or "bodyEl.textContent=" in fn_body


def test_triage_detail_draft_uses_textContent():
    """The classifier-produced draft is operator-trusted, but the issue
    body that influenced it isn't. textContent for the draft preview
    keeps it safe even if a malicious issue tries to inject something."""
    html = _read_html()
    start = html.find("function _inboxRenderTriageDetail(")
    fn_body = html[start:start + 4500]
    assert "draft.textContent" in fn_body
