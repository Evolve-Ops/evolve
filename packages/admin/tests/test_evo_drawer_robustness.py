"""tests/test_evo_drawer_robustness.py — guards for the right-column drawer.

The drawer's chat-send flow shares many failure modes with the Chat
page session strip (in-flight nav, stalled responses, OC session
memory carryover after clear). These tests pin the Sprint 1
robustness fixes against regression:

  * **In-flight nav routing.** The drawer paints reply bubbles into
    a SPECIFIC page's thread, not whichever page the operator is
    currently viewing. Same class of bug as the Chat page's in-flight
    session-switch (fixed in #1343).

  * **↺ rotates OC session.** When the operator clears the drawer
    thread, the next send goes to a FRESH OC session — otherwise OC
    remembers the cleared conversation and contradicts the
    operator's "I cleared this" mental model.

  * **Pending timeout + retry.** A stalled POST flips '…thinking…'
    into an error bubble with a ↻ Retry button, so the operator
    isn't stuck staring at a spinner.

Same regex-on-source pattern as the other drawer / chat-page guards.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_INDEX_HTML = _ADMIN_PKG / "evolve_admin" / "web" / "index.html"


_HOME_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "home.js"
_EVO_DRAWER_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "evo-drawer.js"


@pytest.fixture(scope="module")
def html() -> str:
    # In-flight nav routing tests span home.js (Phase 3m — chat-send
    # path) and evo-drawer.js (Phase 3n — drawer-send path). Concat
    # both.
    return (
        _INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _HOME_JS.read_text(encoding="utf-8")
        + "\n"
        + _EVO_DRAWER_JS.read_text(encoding="utf-8")
    )


# ── In-flight nav routing ────────────────────────────────────────────────────


def test_drawer_append_gates_render_on_current_page(html: str):
    """``_evoDrawerAppend(page_id, msg)`` must only paint to the DOM
    if ``page_id`` is the currently-shown page. Without this gate, a
    response that lands AFTER the operator navigated to a different
    page paints into the wrong drawer (same root cause as the Chat
    page's in-flight-switch bug)."""
    m = re.search(
        r"function\s+_evoDrawerAppend\s*\(\s*page_id[^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerAppend body"
    body = m.group(1)
    assert "page_id === _evoDrawerCurrentPage()" in body, (
        "_evoDrawerAppend's render path isn't gated on the target page "
        "being current. An in-flight nav would paint reply bubbles into "
        "the wrong drawer."
    )


def test_drawer_pending_render_gates_on_current_page(html: str):
    """``_evoDrawerSend`` must only paint the '…thinking…' placeholder
    if the operator is still on the sending page. Otherwise the
    placeholder lands in the wrong drawer the moment they navigate."""
    m = re.search(
        r"async\s+function\s+_evoDrawerSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerSend body"
    body = m.group(1)
    assert "page_id === _evoDrawerCurrentPage()" in body, (
        "_evoDrawerSend doesn't gate the pending '…thinking…' render "
        "on page_id matching the current drawer. A mid-flight nav "
        "would orphan the placeholder."
    )


# ── ↺ rotates OC session ─────────────────────────────────────────────────────


def test_drawer_oc_session_id_helper_defined(html: str):
    """The drawer must expose an oc_session_id resolver so clear ↺
    can rotate to a fresh session. Without this, OC remembers the
    cleared conversation."""
    pat = re.compile(r"function\s+_evoDrawerOcSessionId\s*\(", re.MULTILINE)
    assert pat.search(html), "_evoDrawerOcSessionId() not defined"


def test_drawer_rotate_helper_defined(html: str):
    """The salt-bump helper must exist so the next send resolves to
    a different OC session id after ↺."""
    pat = re.compile(r"function\s+_evoDrawerRotateOcSession\s*\(", re.MULTILINE)
    assert pat.search(html), "_evoDrawerRotateOcSession() not defined"


def test_drawer_clear_rotates_oc_session(html: str):
    """``_evoDrawerClearThread`` must call the rotate helper. Without
    it, OC's session memory carries over and the operator's '↺ clear'
    promise is a lie from the model's POV."""
    m = re.search(
        r"function\s+_evoDrawerClearThread\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerClearThread body"
    body = m.group(1)
    assert "_evoDrawerRotateOcSession" in body, (
        "_evoDrawerClearThread doesn't rotate the OC session. After ↺ "
        "the next send resolves to the SAME OC session id, so OC keeps "
        "remembering the cleared conversation."
    )


def test_drawer_send_includes_oc_session_id_in_body(html: str):
    """The drawer's POST body must include ``session_id`` so the route
    uses the salted id instead of falling back to derive(page_id).
    Otherwise the rotation is invisible to the server."""
    m = re.search(
        r"async\s+function\s+_evoDrawerSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerSend body"
    body = m.group(1)
    assert "_evoDrawerOcSessionId(page_id)" in body, (
        "_evoDrawerSend doesn't compute the page's OC session id. "
        "Without that the body has no session_id and the server falls "
        "back to derive(page_id), which doesn't see the salt bump."
    )
    assert "session_id" in body, (
        "_evoDrawerSend's POST body doesn't include session_id. The "
        "server can't see the salted OC session id."
    )


# ── Pending timeout + retry ──────────────────────────────────────────────────


def test_drawer_pending_timeout_constant_defined(html: str):
    """The drawer-side pending timeout constant must exist."""
    m = re.search(
        r"const\s+EVO_DRAWER_PENDING_TIMEOUT_MS\s*=\s*([0-9_]+)", html,
    )
    assert m, "EVO_DRAWER_PENDING_TIMEOUT_MS not declared"
    val = int(m.group(1).replace("_", ""))
    assert 10_000 <= val <= 300_000, (
        f"EVO_DRAWER_PENDING_TIMEOUT_MS={val} outside the sensible 10s–5min "
        "window"
    )


def test_drawer_send_starts_and_clears_pending_timeout(html: str):
    """``_evoDrawerSend`` must start a stalled-bubble timeout AND
    clear it on both success + catch paths."""
    m = re.search(
        r"async\s+function\s+_evoDrawerSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerSend body"
    body = m.group(1)
    assert "setTimeout(" in body and "EVO_DRAWER_PENDING_TIMEOUT_MS" in body, (
        "_evoDrawerSend doesn't start a pending-bubble timeout. A "
        "stalled send would leave '…thinking…' visible forever."
    )
    n_clear = body.count("clearTimeout(pendingTimeoutId)")
    assert n_clear >= 2, (
        f"_evoDrawerSend calls clearTimeout only {n_clear} times; "
        f"expected ≥2 (success + catch). A missed clear leaks a stray "
        f"error bubble after the real reply."
    )


def test_drawer_send_attaches_retry_text_on_error(html: str):
    """Drawer error bubbles must carry retry_text for the ↻ button."""
    m = re.search(
        r"async\s+function\s+_evoDrawerSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerSend body"
    body = m.group(1)
    n_retry = body.count("retry_text: text")
    assert n_retry >= 2, (
        f"_evoDrawerSend attaches retry_text only {n_retry} times; "
        f"expected ≥2 (timeout + catch paths). Operator can't recover "
        f"from glitches without this."
    )


def test_drawer_render_bubble_renders_retry_button(html: str):
    """Drawer renderer must surface retry_text as a ↻ Retry button."""
    m = re.search(
        r"function\s+_evoDrawerRenderBubble\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerRenderBubble body"
    body = m.group(1)
    assert "retry_text" in body, (
        "_evoDrawerRenderBubble doesn't reference retry_text; the "
        "retry button won't render."
    )
    assert "_evoDrawerRetrySend" in body, (
        "_evoDrawerRenderBubble's retry button doesn't wire onclick "
        "to _evoDrawerRetrySend; the button would do nothing."
    )


def test_drawer_retry_send_defined(html: str):
    """The drawer's retry-send helper must exist."""
    pat = re.compile(r"function\s+_evoDrawerRetrySend\s*\(", re.MULTILINE)
    assert pat.search(html), "_evoDrawerRetrySend() not defined"


# ── Long-turn progress (#1454) ───────────────────────────────────────────────


def test_drawer_slow_indicator_constant_defined(html: str):
    """The slow-indicator threshold must exist and sit well below the
    hard ceiling — otherwise the operator never sees a "still working"
    update before the timeout fires."""
    m = re.search(
        r"const\s+EVO_DRAWER_SLOW_INDICATOR_MS\s*=\s*([0-9_]+)", html,
    )
    assert m, "EVO_DRAWER_SLOW_INDICATOR_MS not declared"
    slow = int(m.group(1).replace("_", ""))
    m2 = re.search(
        r"const\s+EVO_DRAWER_PENDING_TIMEOUT_MS\s*=\s*([0-9_]+)", html,
    )
    assert m2, "EVO_DRAWER_PENDING_TIMEOUT_MS not declared"
    hard = int(m2.group(1).replace("_", ""))
    assert 2_000 <= slow <= 30_000, (
        f"EVO_DRAWER_SLOW_INDICATOR_MS={slow} outside the sensible 2s–30s "
        "window (too low spams, too high never shows)"
    )
    assert slow < hard, (
        "EVO_DRAWER_SLOW_INDICATOR_MS must be smaller than the hard timeout"
    )


def test_drawer_send_aborts_on_hard_timeout(html: str):
    """``_evoDrawerSend`` must wire an AbortController to the fetch so
    the hard ceiling actually cancels the request (not just hides the
    UI). Without this, a stalled mobile-Safari fetch can dangle past
    the timeout."""
    m = re.search(
        r"async\s+function\s+_evoDrawerSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerSend body"
    body = m.group(1)
    assert "AbortController" in body, (
        "_evoDrawerSend doesn't create an AbortController; the hard "
        "timeout would only flip the UI, leaving the fetch hanging."
    )
    assert "abortCtl.abort" in body, (
        "_evoDrawerSend's timeout handler doesn't call abort(); the "
        "fetch keeps running past the ceiling."
    )
    assert "signal: abortCtl.signal" in body, (
        "_evoDrawerSend's fetch isn't passed the AbortController's "
        "signal; abort() would have nothing to act on."
    )


def test_drawer_uses_shared_pending_indicator(html: str):
    """The drawer's send path must call the shared
    ``_evoChatPendingIndicator`` helper so the operator sees a live
    elapsed-time update on long turns."""
    m = re.search(
        r"async\s+function\s+_evoDrawerSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerSend body"
    assert "_evoChatPendingIndicator(" in m.group(1), (
        "_evoDrawerSend doesn't start the shared pending-indicator; "
        "the operator gets no feedback during long multi-tool turns."
    )


# ── Phase 1 surface-aware help-style: surface_type + proxy_warn ─────────────


def test_drawer_context_pack_includes_surface_type(html: str):
    """Phase 1 of the surface-aware help-style spec adds a viewport-
    derived ``surface_type`` to the drawer's page-context pack. The
    proxy gates CLI emission on the surface line in <session-context>
    — without this field, the model would default to laptop and emit
    CLI to mobile operators."""
    m = re.search(
        r"function\s+_evoDrawerContextPack\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerContextPack body"
    body = m.group(1)
    assert "surface_type" in body, (
        "_evoDrawerContextPack no longer includes surface_type. The "
        "surface-aware help-style proxy plumbing (Phase 1) depends "
        "on this field — without it, mobile operators may receive "
        "CLI recommendations they can't paste."
    )
    assert "_evoSurfaceType" in body or "matchMedia" in body, (
        "the surface_type value isn't being computed (no _evoSurfaceType "
        "call and no inline matchMedia). The field is null and the "
        "server falls back to its laptop default."
    )


def test_evo_surface_type_classifier_defined(html: str):
    """The two-tier viewport classifier must exist and use the
    `(max-width: 720px)` / `(pointer: coarse)` rules per spec §2.3.2."""
    pat = re.compile(r"function\s+_evoSurfaceType\s*\(", re.MULTILINE)
    assert pat.search(html), "_evoSurfaceType() not defined"
    # The classifier must check both viewport width and pointer:coarse
    # (touch primary). Either signal alone classifies as mobile.
    m = re.search(
        r"function\s+_evoSurfaceType\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoSurfaceType body"
    body = m.group(1)
    assert "max-width: 720px" in body, (
        "_evoSurfaceType no longer uses the 720px breakpoint from "
        "spec §2.3.2. Changing the breakpoint will mis-classify."
    )
    assert "pointer: coarse" in body, (
        "_evoSurfaceType no longer checks pointer:coarse. Without this, "
        "iPad-in-tablet-mode and similar touch-primary surfaces with "
        "wide viewports leak through as 'laptop'."
    )


def test_drawer_handles_proxy_warn_source(html: str):
    """The chat-drawer JS must branch on ``source: 'proxy_warn'``
    (yellow bubble for empty-reply-with-synthesized-confirmation)
    separately from ``proxy_error`` (red bubble for subprocess
    failure). Spec §8 Phase 1 + diagnosis-empty-reply-after-successful-
    tool-calls-2026-05-21.md Priority 2."""
    # Both render paths (drawer + home composer) must include the branch
    assert "proxy_warn" in html, (
        "the chat-drawer JS no longer references 'proxy_warn'. The "
        "empty-reply UX (yellow bubble with synthesized confirmation) "
        "regresses to a red error bubble — the failure the diagnosis "
        "calls out."
    )
    # The branch must set meta.warn (not meta.error)
    assert "meta.warn = true" in html, (
        "the proxy_warn branch doesn't set meta.warn=true; the bubble "
        "wouldn't pick up the yellow styling."
    )


def test_home_msg_warn_css_class_defined(html: str):
    """The .home-msg-warn CSS class must exist so the yellow bubble
    actually renders distinct from .home-msg-error (red)."""
    assert ".home-msg-warn" in html, (
        "the .home-msg-warn CSS class is missing. proxy_warn bubbles "
        "would render with default (evo) styling instead of the "
        "intended yellow accent."
    )
