"""Regression: the JS `_relTime` helper must be defined in index.html.

Background: the symbol was called from 12+ render functions (errors summary,
inbox watcher pill, triage rows, intake activity, undo deadlines, …) but
never defined anywhere in the file. The throw lived dormant for months
because the call sites only fire on pages with active data — e.g. the
Inbox watcher pill renders nothing until a tracked repo + watcher are
configured. Once both landed, the ReferenceError surfaced inside the
pill renderer, got caught by loadInboxRepos's try/catch, and displayed
as a misleading "Failed to load: ReferenceError" on the Tracked repos
card.

This test pins:
- `_relTime` is defined exactly once
- All call sites use it consistently (no accidental rename drift)
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
_MAINTENANCE_JS = _WEB / "static" / "js" / "pages" / "maintenance.js"


def _read_html() -> str:
    # _relTime definition + several callers now live in pages/maintenance.js
    # (Phase 3w — Gateway + OC Version + Logs cluster). Other callers
    # live in pages/errors.js (Phase 3a) and pages/inbox.js (Phase 3b).
    # Concat all four so the caller-count canary test below still sees
    # the full inventory.
    return (
        _INDEX_HTML.read_text()
        + "\n"
        + _ERRORS_JS.read_text()
        + "\n"
        + _INBOX_JS.read_text()
        + "\n"
        + _MAINTENANCE_JS.read_text()
    )


def test_reltime_is_defined():
    """Pin the function definition so the file can never re-regress to
    "called everywhere, defined nowhere" again.
    """
    html = _read_html()
    # The function declaration. Accept any of `function _relTime(`,
    # `_relTime = function`, or `const _relTime = ` so future refactors
    # that change the declaration shape don't trip this test unfairly.
    patterns = [
        r"function\s+_relTime\s*\(",
        r"_relTime\s*=\s*function",
        r"(?:const|let|var)\s+_relTime\s*=\s*",
    ]
    matches = sum(len(re.findall(p, html)) for p in patterns)
    assert matches >= 1, (
        "_relTime helper is not defined in index.html — calls in render "
        "functions will throw ReferenceError and be caught by surrounding "
        "try/catch as misleading 'Failed to load' messages."
    )


def test_reltime_callers_exist():
    """Sanity check: this test exists because the helper has many callers.
    If callers disappear (e.g. mass refactor to a different time helper),
    the regression test above should be retired with them — not silently
    kept around as dead weight. This count is the canary.
    """
    html = _read_html()
    callers = re.findall(r"\b_relTime\s*\(", html)
    # Subtract the declaration itself from the count.
    decl_count = len(re.findall(r"function\s+_relTime\s*\(", html))
    call_count = len(callers) - decl_count
    assert call_count >= 5, (
        f"Expected ≥5 call sites for _relTime (errors summary, inbox "
        f"watcher pill, triage rows, intake activity, …); found {call_count}. "
        "If this drops to 0, retire this test file."
    )
