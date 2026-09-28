"""Structural test: render bugs must not be displayed as API failures.

Context: PR #1671 surfaced a class of bug where the Inbox card showed
"Failed to load: ReferenceError: _relTime is not defined" — but the API
call had succeeded. The misleading message came from ``loadInboxRepos``
wrapping BOTH the fetch and the subsequent ``_renderInboxRepos`` call
in a single try/catch, so a JS bug inside the render path was caught
and presented to the user as if the network had failed.

This file pins the fix shape: for each loader that displays a
"Failed to load…" message on catch, the render call must live OUTSIDE
the try block that contains the fetch. JS render bugs should throw to
the console (where dev tools surface them); only real network/parse
failures should hit the "Failed to load" UI path.

We assert this statically because the admin web layer is a single big
``index.html`` with no jest/vitest runner — markup-contract checks
against the source text are the only available enforcement mechanism.
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
_ERRORS_JS = _WEB / "static" / "js" / "pages" / "errors.js"
_INBOX_JS = _WEB / "static" / "js" / "pages" / "inbox.js"
_APPS_JS = _WEB / "static" / "js" / "pages" / "apps.js"


def _read_html() -> str:
    # The _FIXED_LOADERS list pinned by this test now spans four files:
    #   - loadErrors → pages/errors.js (Phase 3a)
    #   - loadInbox / loadInboxRepos → pages/inbox.js (Phase 3b)
    #   - _fetchLessons / openAdoptModal → pages/apps.js (Phase 3i)
    # Concat all four so the brace-matching function-body extractor below
    # can still find every loader regardless of which file it lives in.
    return (
        _INDEX_HTML.read_text()
        + "\n"
        + _ERRORS_JS.read_text()
        + "\n"
        + _INBOX_JS.read_text()
        + "\n"
        + _APPS_JS.read_text()
    )


def _extract_function(text: str, name: str) -> str:
    """Return the source of ``async function <name>(...)`` by brace-matching.

    Raises ``AssertionError`` if the function isn't found.
    """
    pat = re.compile(r"async function " + re.escape(name) + r"\s*\(")
    m = pat.search(text)
    assert m, f"function not found: {name}"
    start = m.start()
    # Find the first '{' after the signature, then balance braces.
    brace_idx = text.find("{", m.end())
    assert brace_idx > 0, f"opening brace not found for {name}"
    depth = 0
    i = brace_idx
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in {name}")


def _try_blocks_containing_fetch(body: str) -> list[str]:
    """Return the inner bodies of any ``try { ... }`` blocks that contain
    an ``await fetch(`` call."""
    blocks: list[str] = []
    idx = 0
    while True:
        m = re.search(r"\btry\s*\{", body[idx:])
        if not m:
            break
        open_brace = idx + m.end() - 1  # position of the '{'
        # Balance braces
        depth = 0
        i = open_brace
        while i < len(body):
            ch = body[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    inner = body[open_brace + 1 : i]
                    if "await fetch(" in inner:
                        blocks.append(inner)
                    idx = i + 1
                    break
            i += 1
        else:
            break
    return blocks


# Loaders fixed in this PR. Each had its render call moved outside the
# fetch try-block so a render-side exception no longer surfaces as a
# misleading "Failed to load" message.
_FIXED_LOADERS = (
    "loadInboxRepos",
    "loadInbox",
    "loadErrors",
    "_fetchLessons",
    "openAdoptModal",
)


def test_fixed_loaders_have_no_render_call_inside_fetch_try():
    """For each fixed loader, the try-block that contains ``await fetch(``
    must NOT contain a ``_render…(``  or ``render…(`` call.

    This is the structural equivalent of "stub fetch to succeed, stub
    render to throw, assert the catch path didn't fire" — if a render
    call lives inside the try, a render-time exception will be caught
    and reported as a load failure, which is the bug we're guarding
    against.
    """
    text = _read_html()
    render_call = re.compile(r"\b_?[rR]ender\w*\s*\(")
    failures: list[str] = []
    for name in _FIXED_LOADERS:
        body = _extract_function(text, name)
        for block in _try_blocks_containing_fetch(body):
            m = render_call.search(block)
            if m:
                snippet = block[max(0, m.start() - 40) : m.end() + 40].strip()
                failures.append(f"{name}: render call inside fetch try-block — {snippet!r}")
    assert not failures, (
        "Render calls must live outside the fetch try-block so render-side "
        "JS errors don't surface as misleading 'Failed to load' API messages. "
        "Offending sites:\n  " + "\n  ".join(failures)
    )


def test_fixed_loaders_still_use_failed_to_load_message_on_real_failure():
    """Sanity check that the fix didn't accidentally remove the catch
    handler. Each fixed loader should still display "Failed to load"
    when the fetch genuinely throws.
    """
    text = _read_html()
    for name in _FIXED_LOADERS:
        body = _extract_function(text, name)
        assert "Failed to load" in body, (
            f"{name} no longer displays 'Failed to load' on network failure — "
            "the catch handler may have been removed by mistake"
        )
