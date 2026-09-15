"""tests/test_proposal_detail_modal.py — pin invariants on the proposal
detail modal's renderer.

The Recommendations page opens a detail modal when the operator clicks a
proposal title. The modal stitches together ~10 helper blocks (action,
claim, revert, risk, motivating signals, annotations, history, lineage,
revisions, refine). The bug this file guards against: any of those
helpers returning ``undefined`` (no return statement, or a fall-through
DOM lookup that misses on the Recommendations page) leaks the literal
string "undefined" into the rendered HTML, because JS template literals
stringify ``undefined`` rather than skipping it.

Operator-spotted instance: a per-proposal history helper named
``renderProposalHistory`` was silently shadowed by the page-level
``renderProposalHistory(all)`` declared later in the same script (JS
function hoisting picks the later top-level declaration). The page-level
function looks up ``proposal-history-list`` in the DOM, returns
undefined when it's missing, and that ``undefined`` rendered as an
orphan heading between the RISK and REFINE sections of the modal.

Fix: rename the modal helper to ``_renderProposalDetailHistory`` so it
matches the underscore-prefix convention the other modal helpers use
(``_renderRefineForm``, ``_renderRevisions``, ``_lineageDetail``) and
can no longer collide with the page-level renderer.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_SELF_IMPROVEMENT_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "self-improvement.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _SELF_IMPROVEMENT_JS.read_text(encoding="utf-8")


def test_proposal_detail_history_helper_has_unique_name():
    """The modal-only history helper must use the underscore-prefix name
    so it cannot be shadowed by the page-level renderProposalHistory(all)
    declared further down the same script. Two top-level
    ``function renderProposalHistory(...)`` declarations cause the later
    one to win via hoisting — the modal then calls the page-level
    renderer, which returns undefined when its DOM target isn't on the
    page, and the template literal stringifies that into a literal
    'undefined' between RISK and REFINE."""
    html = _html()
    assert "function _renderProposalDetailHistory(history)" in html, (
        "Modal history helper must be named _renderProposalDetailHistory "
        "to match the _renderRefineForm / _renderRevisions / _lineageDetail "
        "convention and avoid colliding with renderProposalHistory(all)."
    )


def test_proposal_detail_calls_renamed_history_helper():
    """The modal-only ``_renderProposalDetailHistory`` helper must be
    called from each renderer branch (the legacy renderer for
    pre-migration proposals + the Phase A v2 renderer for migrated
    proposals). Without this, the shadow-collision bug reappears
    even if both functions exist.

    Phase A (2026-06-04) split renderProposalDetail into a thin
    dispatcher + two implementations (V2 + Legacy); the helper call
    moved into each implementation. This test checks them both."""
    html = _html()
    # Both implementation bodies — V2 (for migrated proposals) and
    # Legacy (for pre-migration). The call to the modal-only helper
    # must appear in BOTH.
    for fn_name in ("_renderProposalDetailV2", "_renderProposalDetailLegacy"):
        fn = re.search(
            r"function " + re.escape(fn_name) + r"\(p\)\s*\{(.+?)\n\}\n",
            html, re.DOTALL,
        )
        assert fn, f"{fn_name} function missing"
        body = fn.group(1)
        assert "_renderProposalDetailHistory(p.history)" in body, (
            f"{fn_name} must call _renderProposalDetailHistory(p.history); "
            "calling renderProposalHistory would resolve to the page-level "
            "renderer and emit a literal 'undefined' between RISK and REFINE."
        )
        assert "renderProposalHistory(p.history)" not in body, (
            f"{fn_name} must NOT call renderProposalHistory(p.history) — "
            "JS hoisting binds that name to the page-level renderer that "
            "returns undefined when proposal-history-list isn't on the page."
        )


def test_no_duplicate_render_proposal_history_declarations():
    """Pin the absence of the historical duplicate. There must be
    exactly one ``function renderProposalHistory(`` declaration in the
    script — the page-level one at the bottom. Any second declaration
    with the same name would re-introduce the hoisting-shadow bug."""
    html = _html()
    matches = re.findall(r"^function renderProposalHistory\(", html, re.MULTILINE)
    assert len(matches) == 1, (
        f"Found {len(matches)} top-level renderProposalHistory declarations; "
        "expected exactly 1 (the page-level renderer). Duplicate "
        "declarations cause function hoisting to shadow earlier "
        "definitions and produce 'undefined' rendering bugs."
    )
