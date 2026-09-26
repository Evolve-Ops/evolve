"""tests/test_alerts_paired_proposal_action.py — pin the paired-row
behavior on the Alerts page.

Background: when a Signal has motivated Proposals (via the canonical
Proposal.motivating_signals link + Signal.motivated_proposals backref
maintained by arbiter.store.write_proposal), the Alerts row should
surface the action affordance INLINE rather than forcing the operator
to context-switch to the Recommendations tab. The 2026-06-04 review
pointed at this as the obvious fix for "alerts and proposals describe
the same observation but live on different surfaces."

Two contracts pinned here:

  1. The render function _alSignalRow looks at
     ``sig.motivated_proposals_view`` (hydrated server-side) and
     branches on whether there's exactly one actionable proposal,
     multiple, or none.

  2. The inline Act button calls _alPairedAct which POSTs to
     /api/arbiter/proposals/<id>/act — the same endpoint the Home and
     Recommendations surfaces use, so the action semantics stay
     consistent across surfaces.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_ALERTS_EXTENDED_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "alerts-extended.js"
_HOME_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "home.js"
_ALERTS_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "static" / "js" / "pages" / "alerts.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _ALERTS_JS.read_text(encoding="utf-8") + "\n" + _HOME_JS.read_text(encoding="utf-8") + "\n" + _ALERTS_EXTENDED_JS.read_text(encoding="utf-8")


def test_paired_act_helper_exists():
    """_alPairedAct must be defined — the inline button references it
    via onclick. Missing helper would throw a ReferenceError when the
    operator clicks the linked Act button."""
    html = _html()
    assert "async function _alPairedAct(propId, btn)" in html, (
        "_alPairedAct helper missing — inline Act buttons would throw"
    )


def test_paired_act_posts_to_arbiter_proposals_endpoint():
    """The inline Act path MUST use the same endpoint as Home /
    Recommendations (/api/arbiter/proposals/<id>/act) so applier
    semantics are consistent across surfaces."""
    html = _html()
    m = re.search(
        r"async function _alPairedAct\(propId, btn\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert m, "_alPairedAct body not found"
    body = m.group(1)
    assert "/api/arbiter/proposals/" in body, (
        "_alPairedAct must POST to /api/arbiter/proposals/<id>/act — "
        "any other endpoint diverges the applier path from Home/Recs"
    )
    assert "'/act'" in body or '"/act"' in body or "/act`" in body, (
        "Action suffix must be 'act' to mirror _homeProposalAct"
    )


def test_paired_act_refreshes_lane_on_success():
    """Acting on a paired proposal should refresh the Alerts lane so
    the now-resolving Signal disappears (or, if it remains firing, the
    operator sees the persistence). Without this the operator clicks
    Act, sees nothing change, and assumes the action failed."""
    html = _html()
    m = re.search(
        r"async function _alPairedAct\(propId, btn\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert m, "_alPairedAct body not found"
    body = m.group(1)
    assert "_alRefreshActive" in body, (
        "_alPairedAct must call _alRefreshActive() on success"
    )


def test_signal_row_uses_motivated_proposals_view():
    """The render function _alSignalRow must consume the server-hydrated
    motivated_proposals_view (id+title+status+kind), not the bare id
    list. Without the title + status fields, the inline Act button
    can't render an actionable label or branch on archived vs pending."""
    html = _html()
    assert "sig.motivated_proposals_view" in html, (
        "_alSignalRow must read sig.motivated_proposals_view so the "
        "inline Act button can show the proposal title and filter by "
        "actionable status"
    )


def test_signal_row_branches_actionable_vs_archived():
    """The render must distinguish:
       - exactly one actionable proposal → inline Act button
       - multiple actionable → count badge
       - all terminal → muted count badge (audit trail only)
    The 'actionable' status set must include the pending-side
    lifecycle states (draft, pending, approved_*, dispatched)."""
    html = _html()
    assert "_ACTIONABLE_PROP_STATUSES" in html, (
        "Render must define an actionable-status set so terminal "
        "proposals don't surface a stale Act button"
    )
    # Required pending-side statuses
    m = re.search(
        r"_ACTIONABLE_PROP_STATUSES\s*=\s*new Set\(\[(.+?)\]\)",
        html, re.DOTALL,
    )
    assert m, "_ACTIONABLE_PROP_STATUSES initializer not found"
    states = m.group(1)
    for required in ("pending", "draft", "approved_auto", "approved_human", "dispatched"):
        assert f"'{required}'" in states or f'"{required}"' in states, (
            f"Actionable status set must include '{required}' — "
            f"otherwise the Act button is hidden for a proposal that's "
            f"actually still actionable"
        )


def test_inline_act_button_carries_proposal_title():
    """The inline Act button label should include the proposal title
    (truncated) so the operator can see WHAT they're about to do
    without expanding the row — 'Act: Remove exec scripts/foo.py'
    rather than just 'Act'."""
    html = _html()
    # The render path interpolates the proposal title into the button
    # label via `Act: ${titleJs}` (template literal). Pin the shape so
    # a refactor that strips the label trips this test.
    assert "Act: ${titleJs" in html, (
        "Inline Act button must include the proposal title in its "
        "label so the operator sees what they're acting on"
    )
