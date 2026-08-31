"""tests/test_dispatch_ui.py — Phase 3.2 client UI structural pins.

Slice 3 (2026-06-04) introduces a dispatch flow: proposals with
``dispatch_target`` set route the accept button through a confirmation
modal that posts to ``/api/arbiter/proposals/<id>/dispatch`` instead
of /act. These tests pin the load-bearing markup + JS identifiers so a
careless refactor of ``renderProposalCard`` can't silently break the
wiring.

Spec: ``internal/spec-take-this-on-evo-dispatch-2026-06-04.md`` §"Client-
side: UI changes".

Phase 3.1 (server endpoints + schema) shipped as PR #2074; this file
matches that contract. If the endpoint paths or the dispatch_state /
dispatch_target / dispatch_message proposal-shape change there, the
assertions below will need to follow.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_INDEX_HTML = _ADMIN_PKG / "evolve_admin" / "web" / "index.html"


_SELF_IMPROVEMENT_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "self-improvement.js"
def _read_html() -> str:
    return _INDEX_HTML.read_text() + "\n" + _SELF_IMPROVEMENT_JS.read_text()


# ── Button labels ───────────────────────────────────────────────────────────


def test_button_label_branch_for_evo():
    """When ``dispatch_target == 'evo'`` the accept-button label is
    'Have evo fix this'. The string is the operator-facing copy from
    the spec; matching on it pins the branch."""
    html = _read_html()
    assert "'Have evo fix this'" in html, (
        "Expected the 'Have evo fix this' label for the evo dispatch "
        "branch. Spec: internal/spec-take-this-on-evo-dispatch-2026-06-04.md"
    )


def test_button_label_branch_for_forge():
    """When ``dispatch_target == 'forge'`` the label is 'Have forge
    handle this'."""
    html = _read_html()
    assert "'Have forge handle this'" in html


def test_button_label_branch_for_bot_id():
    """Any other dispatch_target value (a bot id) gets a 'Send to
    {target}' label. Pin the template-literal shape that produces it."""
    html = _read_html()
    assert "`Send to ${dispatchTarget}`" in html


def test_button_label_still_take_this_on_when_no_dispatch_target():
    """Operator-only proposals (dispatch_target == null) keep today's
    'Take this on' label — this is the load-bearing fallback path."""
    html = _read_html()
    assert "'Take this on'" in html


# ── Dispatch confirmation modal ─────────────────────────────────────────────


def test_open_dispatch_confirm_modal_function_exists():
    """The confirmation-modal entry point. Function name is part of
    the public contract used by the card's onclick handlers."""
    html = _read_html()
    assert "function openDispatchConfirmModal(" in html


def test_dispatch_modal_posts_to_dispatch_endpoint():
    """The modal's Send button posts to ``/api/arbiter/proposals/<id>
    /dispatch`` (the Phase 3.1 endpoint). Pin the fetch URL template
    so a copy-paste of the /act endpoint can't silently mis-wire it."""
    html = _read_html()
    assert (
        "`/api/arbiter/proposals/${encodeURIComponent(id)}/dispatch`"
        in html
    )


def test_dispatch_modal_close_function_exists():
    """The modal needs a close hook for the Cancel button."""
    html = _read_html()
    assert "function closeDispatchConfirmModal(" in html


def test_card_button_wires_to_dispatch_modal_when_dispatch_target_set():
    """The card button's onclick must route to
    openDispatchConfirmModal when dispatch_target is set, and to
    arbiterAct otherwise. Pin the conditional shape."""
    html = _read_html()
    assert "openDispatchConfirmModal('${escHtml(p.id)}')" in html
    # Both branches present.
    assert "arbiterAct('${escHtml(p.id)}')" in html


# ── Dispatched-state badge + cancel ─────────────────────────────────────────


def test_dispatched_state_branch_exists():
    """A dedicated dispatched branch in renderProposalCard."""
    html = _read_html()
    assert "const dispatched = p.status === 'dispatched'" in html


def test_dispatched_badge_rendered_on_card():
    """The ⚙ badge identifies dispatched proposals at top of the card."""
    html = _read_html()
    # The badge format is "⚙ Dispatched to {target} · {ago(dispatched_at)}"
    # — pin the leading glyph + label so visual continuity stays intact.
    assert "⚙ Dispatched to" in html


def test_cancel_dispatch_button_renders_on_dispatched_proposals():
    """Dispatched proposals show only a 'Cancel dispatch' button — the
    regular Accept / Snooze / Dismiss buttons are suppressed."""
    html = _read_html()
    assert "Cancel dispatch" in html
    assert "arbiterCancelDispatch('${escHtml(p.id)}')" in html


def test_cancel_dispatch_function_posts_to_cancel_endpoint():
    """arbiterCancelDispatch posts to the Phase 3.1 /dispatch/cancel
    endpoint."""
    html = _read_html()
    assert "function arbiterCancelDispatch(" in html
    assert (
        "`/api/arbiter/proposals/${encodeURIComponent(id)}/dispatch/cancel`"
        in html
    )


# ── Failed-result UI (Retry path) ───────────────────────────────────────────


def test_dispatch_failed_branch_renders_retry_button():
    """When the dispatched target reported failure, the card shows a
    Retry button that re-dispatches (calls /dispatch again) instead of
    the regular Retry that calls /retry. Pin both: the branch flag and
    the retry handler name."""
    html = _read_html()
    assert "dispatchFailed" in html
    assert "arbiterRetryDispatch('${escHtml(p.id)}')" in html


def test_retry_dispatch_function_posts_to_dispatch_endpoint():
    """arbiterRetryDispatch re-fires the dispatch by posting to the
    same /dispatch endpoint with an empty body — matches the spec's
    'calls dispatch again with same body' wording."""
    html = _read_html()
    assert "function arbiterRetryDispatch(" in html
    # Find the function body and check it posts to /dispatch (not /retry).
    fn_idx = html.find("function arbiterRetryDispatch(")
    assert fn_idx > 0
    fn_body = html[fn_idx:fn_idx + 800]
    assert (
        "`/api/arbiter/proposals/${encodeURIComponent(id)}/dispatch`"
        in fn_body
    ), "arbiterRetryDispatch must post to /dispatch, not /retry"


# ── Detail-modal dispatch info block ────────────────────────────────────────


def test_detail_modal_dispatch_block_helper_exists():
    """The detail drawer renders dispatch state (verbatim message,
    badge, result) via a dedicated helper so a refactor of the drawer
    body order doesn't accidentally drop it."""
    html = _read_html()
    assert "function _renderProposalDispatchState(" in html
    # And it's actually wired into the render output.
    assert "_renderProposalDispatchState(p)" in html
