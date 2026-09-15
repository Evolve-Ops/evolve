"""Tests for the Tracked-repos UI card on the Inbox page (Phase 3).

Same markup-contract pattern as test_inbox_tab_ui — these tests pin
the load-bearing IDs, JS function names, and API-call wiring so a
casual edit can't silently break the round-trip with the backend.
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


def test_tracked_repos_card_exists():
    """The card sits inside #page-inbox and uses documented IDs."""
    html = _read_html()
    # Locate the inbox page block, then search inside it.
    page_start = html.find('<div class="page" id="page-inbox">')
    assert page_start > 0
    next_page = html.find('<div class="page"', page_start + 10)
    inbox_block = html[page_start:next_page if next_page > 0 else len(html)]

    assert "Tracked repos" in inbox_block
    assert 'id="inbox-repos-list"' in inbox_block
    assert 'id="inbox-repos-empty"' in inbox_block
    assert 'onclick="_inboxOpenAddRepoModal()"' in inbox_block


def test_add_repo_modal_exists():
    html = _read_html()
    assert 'id="inbox-add-repo-modal"' in html
    # All form inputs must be present so the JS can read them.
    for field in (
        "inbox-add-repo-owner",
        "inbox-add-repo-repo",
        "inbox-add-repo-name",
        "inbox-add-repo-slot",
        "inbox-add-repo-default",
        "inbox-add-repo-error",
        "inbox-add-repo-submit",
    ):
        assert f'id="{field}"' in html, f"missing modal element: {field}"


def test_add_repo_modal_starts_hidden():
    """display:none on the modal wrapper prevents first-paint flash."""
    html = _read_html()
    m = re.search(
        r'<div id="inbox-add-repo-modal"[^>]*>',
        html,
    )
    assert m is not None
    assert "display:none" in m.group(0)


def test_nav_dispatch_loads_both_inbox_endpoints():
    """When the operator opens the Inbox tab, both loadInbox() AND
    loadInboxRepos() must fire — otherwise the Tracked-repos card
    sits at 'Loading…' forever."""
    html = _read_html()
    # Both call sites should appear in the same dispatch line.
    m = re.search(
        r"if\s*\(\s*page\s*===?\s*['\"]inbox['\"]\s*\)\s*\{[^}]*loadInbox\(\)[^}]*loadInboxRepos\(\)[^}]*\}",
        html,
    )
    assert m is not None, (
        "nav dispatch for page='inbox' must call BOTH loadInbox() and "
        "loadInboxRepos() — otherwise the tracked-repos card never loads"
    )


# ── JS function shapes ──────────────────────────────────────────────────


def test_load_inbox_repos_defined():
    assert "async function loadInboxRepos()" in _read_html()


def test_load_inbox_repos_hits_api_endpoint():
    html = _read_html()
    assert "/api/inbox/repos" in html


def test_add_modal_open_close_defined():
    html = _read_html()
    assert "function _inboxOpenAddRepoModal()" in html
    assert "function _inboxCloseAddRepoModal()" in html


def test_submit_add_repo_posts_to_endpoint():
    html = _read_html()
    assert "async function _inboxSubmitAddRepo()" in html
    # POST with JSON body must hit the documented endpoint.
    start = html.find("async function _inboxSubmitAddRepo()")
    fn_body = html[start:start + 1500]
    assert "method: 'POST'" in fn_body or 'method: "POST"' in fn_body
    assert "'/api/inbox/repos'" in fn_body or '"/api/inbox/repos"' in fn_body


def test_remove_repo_function_defined_and_uses_delete():
    html = _read_html()
    assert "async function _inboxRemoveRepo(" in html
    start = html.find("async function _inboxRemoveRepo(")
    fn_body = html[start:start + 1000]
    assert "method: 'DELETE'" in fn_body or 'method: "DELETE"' in fn_body
    # Must encodeURIComponent the name — defensive against malicious /
    # weird target names.
    assert "encodeURIComponent" in fn_body


def test_remove_repo_asks_for_confirmation():
    """Destructive action — must surface a confirm dialog so a
    misclick can't silently delete a target."""
    html = _read_html()
    start = html.find("async function _inboxRemoveRepo(")
    fn_body = html[start:start + 1000]
    assert "confirmModal(" in fn_body


# ── Rendering safety (XSS) ──────────────────────────────────────────────


def test_repo_row_escapes_owner_repo_name():
    """Owner/repo strings are operator-controlled but could be edited
    by hand in network.json — defending against angle-bracket leaks is
    cheap and keeps the contract consistent with the inbox rows above."""
    html = _read_html()
    # All four user-supplied fields must go through escHtml in
    # _renderInboxRepos.
    start = html.find("function _renderInboxRepos(")
    fn_body = html[start:start + 3000]
    assert "escHtml(r.owner)" in fn_body
    assert "escHtml(r.repo)" in fn_body
    assert "escHtml(r.name)" in fn_body
    assert "escHtml(r.tier_label)" in fn_body


def test_repo_row_escapes_self_login():
    """Login is from gh API — also untrusted. Pin escHtml."""
    html = _read_html()
    start = html.find("function _renderInboxRepos(")
    fn_body = html[start:start + 3000]
    assert "escHtml(r.self_login)" in fn_body
    assert "escHtml(r.token_slot)" in fn_body


# ── Quick-add suggestion chips ────────────────────────────────────────────


def test_suggestions_container_is_sibling_of_empty_state():
    """Regression: the suggestions div must be a sibling of #inbox-repos-empty,
    not nested inside it. The old structure (chips inside empty state) hid the
    whole suggestion row whenever ANY repo was configured, so the second
    preset's chip vanished as soon as the first preset was added.
    """
    html = _read_html()
    # Locate the empty-state opening tag, then find its closing </div>.
    empty_open = html.find('id="inbox-repos-empty"')
    assert empty_open > 0
    empty_close = html.find("</div>", empty_open)
    assert empty_close > 0
    empty_block = html[empty_open:empty_close]
    # If suggestions appears inside the empty state, the bug is back.
    assert 'id="inbox-repos-suggestions"' not in empty_block, (
        "inbox-repos-suggestions is nested inside inbox-repos-empty — chip row "
        "will disappear once any repo is configured. Move it to a sibling."
    )
    # And it must still exist somewhere in the page.
    assert 'id="inbox-repos-suggestions"' in html


def test_suggestions_rendered_in_both_paths():
    """_renderInboxRepos must call _renderInboxSuggestions whether the list
    is empty or populated — otherwise the chip row only updates when the
    list-empty branch fires, which is the inverse of what users see after
    adding their first preset.
    """
    html = _read_html()
    start = html.find("function _renderInboxRepos(")
    fn_body = html[start:start + 3000]
    # The call must appear before both the early-return for the empty case
    # and the post-render path for the populated case.
    assert fn_body.count("_renderInboxSuggestions()") >= 1
    # And the call must NOT be only inside the empty-list branch — i.e. it
    # must execute on every render. A simple proxy: the call appears before
    # the "repos.length === 0" early-return.
    call_idx = fn_body.find("_renderInboxSuggestions()")
    branch_idx = fn_body.find("repos.length === 0")
    assert 0 < call_idx < branch_idx, (
        "_renderInboxSuggestions() must run before the empty-list branch so "
        "it fires whether or not any repos are configured."
    )


def test_quick_add_handler_exists_and_posts_to_repos_endpoint():
    """The one-click handler must POST to /api/inbox/repos with the body
    shape the backend expects. Pin the function name and the fetch call."""
    html = _read_html()
    assert "function _inboxQuickAdd(" in html
    start = html.find("function _inboxQuickAdd(")
    fn_body = html[start:start + 2000]
    assert "/api/inbox/repos" in fn_body
    assert "'POST'" in fn_body or '"POST"' in fn_body
    # The body must include the preset's owner/repo/name/make_default so the
    # backend doesn't apply defaults that differ from what the user clicked.
    for field in ("owner", "repo", "name", "make_default"):
        assert f"{field}" in fn_body


# ── Intake-token modal (focused setup from "no token" inline link) ────────


def test_intake_token_modal_exists():
    """The focused setup modal must exist with the documented IDs the JS
    handlers read from."""
    html = _read_html()
    assert 'id="inbox-intake-token-modal"' in html
    for field in (
        "inbox-intake-token-slot",
        "inbox-intake-token-value",
        "inbox-intake-token-result",
        "inbox-intake-token-submit",
    ):
        assert f'id="{field}"' in html, f"missing modal element: {field}"


def test_intake_token_modal_starts_hidden():
    """Prevent first-paint flash."""
    html = _read_html()
    m = re.search(r'<div id="inbox-intake-token-modal"[^>]*>', html)
    assert m is not None
    assert "display:none" in m.group(0)


def test_no_token_text_is_clickable_link_to_modal():
    """The 'no token in <slot>' inline indicator in _renderInboxRepos must
    be a clickable link that opens the intake-token modal — otherwise the
    operator has no UI path to set up the token."""
    html = _read_html()
    start = html.find("function _renderInboxRepos(")
    fn_body = html[start:start + 3000]
    # The handler that opens the modal must appear in the rendered link
    assert "_inboxOpenIntakeTokenModal" in fn_body, (
        "_renderInboxRepos must render the 'no token' indicator as a clickable "
        "link calling _inboxOpenIntakeTokenModal(slot) — otherwise there's no "
        "UI path from the Inbox card to the token setup."
    )
    # And the slot must be passed through (so per-target custom slots work)
    assert "r.token_slot" in fn_body


def test_intake_token_handlers_exist():
    """All three lifecycle functions must be defined."""
    html = _read_html()
    for fn in (
        "function _inboxOpenIntakeTokenModal(",
        "function _inboxCloseIntakeTokenModal(",
        "async function _inboxSubmitIntakeToken(",
    ):
        assert fn in html, f"missing JS function: {fn}"


def test_intake_token_submit_posts_to_correct_endpoint():
    """The save handler must POST to /api/inbox/intake-token with token + slot."""
    html = _read_html()
    start = html.find("async function _inboxSubmitIntakeToken(")
    fn_body = html[start:start + 3000]
    assert "/api/inbox/intake-token" in fn_body
    assert "'POST'" in fn_body or '"POST"' in fn_body
    # The body must include both fields
    assert "token" in fn_body
    assert "slot" in fn_body
    # On success it must refresh the repos list so the row flips to "as @login"
    assert "loadInboxRepos()" in fn_body
