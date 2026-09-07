"""tests/test_chat_timestamp_ticker.py — structural lint on the chat-bubble
timestamp re-tick mechanism (#1402 follow-up).

The fix for "drawer/chat bubble timestamps stuck at 'just now' for 10+
minutes" is purely a JS change: each bubble renderer stores the raw
``msg.ts`` on the span as ``data-ts``, and a single setInterval ticker
re-applies ``ago(data-ts)`` every 30s. The original failure mode is the
``ago(msg.ts)`` string baked into static HTML at render time.

These tests are regex-on-source — they don't execute the JS, they just
verify the source has the right shape so a future refactor can't
silently regress to the static-only pattern.

Two surfaces share the fix because they share the ``.home-evo-time``
class: the Chat-page renderer (_homeChatRenderBubble) and the per-page
drawer renderer (_evoDrawerRenderBubble). One ticker selector covers
both.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WEB = Path(__file__).parent.parent / "evolve_admin" / "web"
_INDEX_HTML = _WEB / "index.html"
_HOME_JS = _WEB / "static" / "js" / "pages" / "home.js"
_EVO_DRAWER_JS = _WEB / "static" / "js" / "pages" / "evo-drawer.js"
_PAGE_CHAT_JS = _WEB / "static" / "js" / "pages" / "page-chat.js"


@pytest.fixture(scope="module")
def index_html() -> str:
    # Chat-bubble renderers live across three extracted files:
    #   _homeChatRenderBubble  → pages/home.js (Phase 3m)
    #   _evoDrawerRenderBubble → pages/evo-drawer.js (Phase 3n)
    #   _pageChatRenderBubble  → pages/page-chat.js (Phase 3q)
    # Concat all three so the existing regex-shape assertions stay valid.
    assert _INDEX_HTML.exists(), f"{_INDEX_HTML} not found"
    return (
        _INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _HOME_JS.read_text(encoding="utf-8")
        + "\n"
        + _EVO_DRAWER_JS.read_text(encoding="utf-8")
        + "\n"
        + _PAGE_CHAT_JS.read_text(encoding="utf-8")
    )


def test_home_evo_time_spans_carry_data_ts_attribute(index_html: str):
    """Both chat-bubble renderers must emit ``.home-evo-time`` spans
    with a ``data-ts`` attribute carrying the raw msg.ts. Without the
    attribute the ticker has nothing to re-read against."""
    # Count occurrences of the post-fix pattern. There should be at
    # least two (one for _homeChatRenderBubble, one for
    # _evoDrawerRenderBubble).
    pattern = re.compile(
        r'<span class="home-evo-time" data-ts="\$\{[^}]+\}">'
    )
    matches = pattern.findall(index_html)
    assert len(matches) >= 2, (
        f"Expected at least 2 .home-evo-time spans with data-ts "
        f"(Chat page + drawer); found {len(matches)}. The diagnosis "
        f"(#1402) specifies both renderers must store the timestamp "
        f"on the DOM for the ticker to re-read."
    )


def test_static_only_home_evo_time_pattern_is_absent(index_html: str):
    """The OLD pattern — ``<span class="home-evo-time">${escHtml(tsStr)}</span>``
    with NO data-ts attribute — must not appear anywhere. If it does,
    a bubble rendered with that pattern will freeze its timestamp
    text indefinitely because the ticker walks ``[data-ts]`` only."""
    # Look for the exact static-only opening tag (no data-ts).
    old_pattern = re.compile(
        r'<span class="home-evo-time">\$\{escHtml\(tsStr\)\}</span>'
    )
    matches = old_pattern.findall(index_html)
    assert not matches, (
        f"Found {len(matches)} static-only .home-evo-time span(s) "
        f"without data-ts. These bubbles will freeze at first-render "
        f"timestamp — the bug #1402 closed. Either rewrite them with "
        f"data-ts, or use a different class so the ticker selector "
        f"doesn't expect to re-tick them."
    )


def test_refresh_chat_timestamps_function_present(index_html: str):
    """The ticker function must exist. Without it the data-ts
    attribute is dead weight — bubbles still freeze."""
    assert "_refreshChatTimestamps" in index_html, (
        "_refreshChatTimestamps is missing from index.html. The "
        "data-ts attribute on .home-evo-time spans is useless without "
        "a function that re-reads them and re-applies ago()."
    )
    # And it must walk the right selector — anything more permissive
    # (eg .home-msg-meta) would tick non-timestamp DOM; anything
    # narrower would miss either Chat-page or drawer bubbles.
    assert ".home-evo-time[data-ts]" in index_html, (
        "the ticker no longer queries '.home-evo-time[data-ts]'. "
        "That selector is load-bearing — it covers both the Chat-page "
        "and drawer renderers (they share the class), and the "
        "data-ts predicate avoids ticking spans without a stored "
        "timestamp."
    )
    # The body must actually call ago() against the stored attribute —
    # not some other heuristic. ago() is the same function that
    # produced the first-render string, so re-applying it is the
    # minimal-surprise refresh.
    assert re.search(
        r"_refreshChatTimestamps[^}]+ago\(", index_html,
    ), (
        "_refreshChatTimestamps no longer calls ago() against the "
        "stored data-ts. Using a different time-formatter would "
        "produce inconsistent text between first render and re-tick."
    )


def test_chat_timestamp_ticker_setinterval_present(index_html: str):
    """A setInterval call must wire _refreshChatTimestamps to the
    periodic clock. Without it, the function exists but never runs —
    the same freeze bug from a slightly different angle."""
    # Match either a direct setInterval(_refreshChatTimestamps, …)
    # or an indirection through a start helper that does the same.
    direct = re.search(
        r"setInterval\(\s*_refreshChatTimestamps", index_html,
    )
    via_helper = re.search(
        r"setInterval\(\s*\n?\s*_refreshChatTimestamps", index_html,
    )
    assert direct or via_helper, (
        "no setInterval call wires _refreshChatTimestamps to the "
        "clock. The function exists but never runs — bubbles still "
        "freeze. Either restore the direct setInterval call or keep "
        "the equivalent start-helper pattern."
    )


def test_chat_timestamp_ticker_invoked_at_init(index_html: str):
    """The start-ticker helper must be called during init (alongside
    startHealthPolling), otherwise the ticker is defined but never
    fires. Mirrors the existing pattern for health polling."""
    # The start helper (or a direct setInterval call) must be
    # invoked at module-init scope, not just defined.
    assert (
        "startChatTimestampTicker()" in index_html
        or re.search(
            r"setInterval\(\s*_refreshChatTimestamps[^)]*\)\s*;",
            index_html,
        )
    ), (
        "startChatTimestampTicker() (or an equivalent module-init "
        "setInterval call) is missing from index.html init. The "
        "function exists but nothing turns it on — bubbles still "
        "freeze."
    )
