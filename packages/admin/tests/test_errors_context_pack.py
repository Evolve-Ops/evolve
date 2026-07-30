"""tests/test_errors_context_pack.py — guard the Errors page-context pack.

Added in response to the May 2026 evo coverage audit. The Errors page
had ``pod_state.errors`` available as a read tool but no page-context
pack — evo was answering page-context questions ("what's that
CRITICAL one?") from stale heal-status logs instead of the
deduplicated /api/errors view the operator was looking at.

This test file pins:

  1. The ``errors`` entry in ``_EVO_CONTEXT_PACKS`` is present.
  2. The snapshot writer in ``loadErrors`` spreads ``..._prev`` so
     future concurrent writers don't clobber sibling fields
     (the lesson learned from PR #1366 / the 2026-05-20 snapshot bug).
  3. The pack surfaces ``pod_state.errors`` as a tool pointer.

Pattern lifted from ``test_security_context_snapshot`` — regex on the
HTML source. Same caveats: shape test, not behavior. Runtime is
exercised live on the mini.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_WEB = _ADMIN_PKG / "evolve_admin" / "web"
_INDEX_HTML = _WEB / "index.html"
_EVO_DRAWER_JS = _WEB/ "static" / "js" / "pages" / "evo-drawer.js"
_ERRORS_JS = _WEB / "static" / "js" / "pages" / "errors.js"


@pytest.fixture(scope="module")
def html() -> str:
    # The Errors page (loadErrors + the _renderErrorsX family) lives in
    # pages/errors.js (Phase 3a). _EVO_CONTEXT_PACKS lives in
    # pages/evo-drawer.js (Phase 3n). Concat all three so the existing
    # string-shape assertions stay valid.
    return (
        _INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _ERRORS_JS.read_text(encoding="utf-8")
        + "\n"
        + _EVO_DRAWER_JS.read_text(encoding="utf-8")
    )


def test_evo_context_packs_has_errors_entry(html: str):
    """The ``_EVO_CONTEXT_PACKS`` registry must have an 'errors' entry
    so the Errors page gets a context pack injected when the operator
    chats from it. Without this entry, evo falls back to a bare
    ``page_id: 'errors'`` block — no items, no headline, no tool
    pointers."""
    packs_start = html.find("const _EVO_CONTEXT_PACKS")
    assert packs_start > 0, (
        "could not locate the _EVO_CONTEXT_PACKS declaration. If the "
        "registry was renamed, update this test."
    )
    # Restrict the search window to between the opening of _EVO_CONTEXT_PACKS
    # and the next semicolon at column 0 (the closing }; of the object
    # literal). 'errors' might also appear in _EVO_PAGE_PROMPTS or in
    # comments — we want a hit inside the packs object.
    packs_end = html.find("\n};\n", packs_start)
    assert packs_end > packs_start, (
        "could not locate the closing of _EVO_CONTEXT_PACKS."
    )
    packs_block = html[packs_start:packs_end]
    # Either form is acceptable as the key declaration.
    assert (
        "'errors':" in packs_block
        or '"errors":' in packs_block
    ), (
        "the _EVO_CONTEXT_PACKS registry has no 'errors' entry. "
        "Without it, evo gets no page context when the operator "
        "chats from the Errors page — it falls back to fetching "
        "from heal-status logs instead of seeing what's on screen."
    )


def test_errors_snapshot_writer_spreads_prior(html: str):
    """The ``loadErrors`` snapshot writer MUST spread the prior value
    (``..._prev`` or ``...prev``) into the new snapshot so concurrent
    writers can't clobber siblings. This was the 2026-05-20
    backup_drift bug (PR #1366) — the same shape gets enforced here
    pre-emptively for the errors snapshot too."""
    # Find every assignment to window._evoContextSnapshots.errors and
    # confirm each one spreads.
    pattern = re.compile(
        r"window\._evoContextSnapshots\.errors\s*=\s*\{([\s\S]*?)\n\s*\};",
        re.MULTILINE,
    )
    matches = pattern.findall(html)
    assert matches, (
        "no assignments to window._evoContextSnapshots.errors found. "
        "If the snapshot writer was inlined or renamed, update this "
        "test to match the new shape."
    )
    for i, body in enumerate(matches):
        has_spread = bool(re.search(r"\.\.\.\s*_?prev\b", body))
        assert has_spread, (
            f"_evoContextSnapshots.errors assignment #{i+1} doesn't "
            f"spread the prior value. Without the spread, any future "
            f"concurrent writer (a poll, a periodic refresh) will "
            f"clobber sibling fields — the 2026-05-20 backup_drift bug "
            f"in another shape. Body was:\n{body[:300]}"
        )


def test_errors_pack_points_at_pod_state_errors(html: str):
    """The errors context pack must include ``pod_state.errors`` in its
    ``tool_pointers`` list. That's the read tool that gives evo the
    deep-dive per-bot error data when the page-context summary isn't
    enough."""
    packs_start = html.find("const _EVO_CONTEXT_PACKS")
    assert packs_start > 0
    start = html.find("'errors':", packs_start)
    if start < 0:
        start = html.find('"errors":', packs_start)
    assert start > 0, (
        "could not locate the 'errors' entry inside _EVO_CONTEXT_PACKS. "
        "If the key shape was refactored, update this test."
    )
    # 5000 chars covers any plausible builder body while staying inside
    # the same entry (matching the security-snapshot test's window).
    window = html[start:start + 5000]
    assert "pod_state.errors" in window, (
        "the 'errors' context-pack builder no longer points at "
        "``pod_state.errors`` as a tool. Without it, the model has no "
        "signpost for the deeper per-bot error data when an operator's "
        "follow-up question goes beyond what the page-context summary "
        "captures."
    )


def test_errors_pack_surfaces_top_items(html: str):
    """The errors context pack must surface an ``items`` list (the top
    N signatures by recency). Without it, the model gets a count but
    no specifics — it can't answer 'what's the most recent error?'
    without re-fetching."""
    packs_start = html.find("const _EVO_CONTEXT_PACKS")
    assert packs_start > 0
    start = html.find("'errors':", packs_start)
    if start < 0:
        start = html.find('"errors":', packs_start)
    assert start > 0
    window = html[start:start + 5000]
    # The 'items:' key must be present in the returned object (mirrors
    # other context packs — items is the model-facing canonical list).
    assert "items:" in window, (
        "the 'errors' context-pack no longer returns an ``items`` list. "
        "Every other context pack returns items so the model has a "
        "consistent surface — without it, evo would have to ask the "
        "operator to scroll or re-fetch."
    )
    # And it must include the timestamp + severity fields the model
    # would cite — concrete check that the items aren't just signatures.
    assert "last_seen" in window or "first_seen" in window, (
        "the 'errors' context-pack items don't include timestamp fields. "
        "The model needs first_seen / last_seen to answer 'when did "
        "this start?' or 'is this new?'."
    )
