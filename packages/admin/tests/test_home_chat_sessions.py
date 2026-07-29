"""tests/test_home_chat_sessions.py — Chat page multi-session model.

The Chat page now supports multiple independent conversation threads
("sessions") with a session strip above the thread for switching.
Each session has its own title (auto-slugged from the first user
message), turn list, and trim archive.

The JS lives inline in ``evolve_admin/web/index.html``. We don't have
a JS test runner in this codebase, so these tests assert structural
invariants on the source: the right constants exist, the session
strip is in the right place in the DOM, the migration function
references the legacy key, etc. Mirrors the pattern in
``test_evo_page_prompts.py`` and ``test_evo_drawer.py`` (where they
exist).

What these tests CAN'T cover:
  * Session-switching JS behavior at runtime (no JS sandbox)
  * Cross-tab localStorage races (browser-only)
  * Visual layout regressions (no DOM in pytest)

What they DO catch:
  * Removed / renamed session functions
  * Storage-key drift
  * Slug-title cap regressions
  * Migration path silently broken
  * Session bar removed from DOM
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
_HOME_JS = _WEB/ "static" / "js" / "pages" / "home.js"
_BASE_CSS = _WEB / "static" / "css" / "base.css"


@pytest.fixture(scope="module")
def html() -> str:
    # base.css holds the shell's CSS after the Phase-1 source split;
    # home.js holds the chat-session JS after Phase 3m. Concat all
    # three so the existing string-shape assertions stay valid without
    # per-test knowledge of the layout.
    return (
        _INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _BASE_CSS.read_text(encoding="utf-8")
        + "\n"
        + _HOME_JS.read_text(encoding="utf-8")
    )


# ── Constants + storage keys ─────────────────────────────────────────────────


def test_sessions_storage_keys_defined(html: str):
    """Both storage keys must exist as JS string literals — drift
    here would silently desynchronize page reloads."""
    assert "'evolve_home_sessions'" in html, (
        "HOME_SESSIONS_KEY 'evolve_home_sessions' not declared in index.html"
    )
    assert "'evolve_home_active_session'" in html, (
        "HOME_ACTIVE_SESSION_KEY 'evolve_home_active_session' not declared"
    )


def test_legacy_chat_key_still_referenced(html: str):
    """The legacy single-thread blob ``evolve_home_chat`` must remain
    referenced — the migration helper reads from it on first load to
    move pre-sessions history into a session titled 'Resumed
    conversation' so operators don't lose their prior chats. Deleting
    this reference would silently strand the legacy blob."""
    assert "'evolve_home_chat'" in html, (
        "HOME_CHAT_KEY 'evolve_home_chat' reference dropped — migration "
        "path won't recover pre-sessions history"
    )


def test_session_max_turns_constant(html: str):
    """Per-session turn cap must be declared so the counter chip and
    trim logic agree on the number."""
    m = re.search(r"const\s+HOME_SESSION_MAX_TURNS\s*=\s*(\d+)\s*;", html)
    assert m, "HOME_SESSION_MAX_TURNS constant not found"
    assert int(m.group(1)) > 0, "HOME_SESSION_MAX_TURNS must be positive"
    assert int(m.group(1)) <= 200, (
        "HOME_SESSION_MAX_TURNS unreasonably high; per-session storage "
        "budget should stay bounded"
    )


def test_session_title_cap_reasonable(html: str):
    """Title-slug cap must exist and stay small enough to fit a chip."""
    m = re.search(r"const\s+HOME_SESSION_TITLE_MAX\s*=\s*(\d+)\s*;", html)
    assert m, "HOME_SESSION_TITLE_MAX constant not found"
    cap = int(m.group(1))
    assert 20 <= cap <= 80, (
        f"HOME_SESSION_TITLE_MAX={cap} is outside the sensible 20-80 "
        "range for chip rendering"
    )


# ── DOM structure ────────────────────────────────────────────────────────────


def test_session_bar_present_in_chat_page(html: str):
    """The session strip div must exist with the expected id so the
    JS render target is wired."""
    assert 'id="home-session-bar"' in html, (
        "home-session-bar div missing — session strip won't render"
    )


def test_session_bar_above_chat_thread(html: str):
    """The session strip must sit ABOVE the chat thread in the DOM.
    Reverse order would either render below the thread (visually wrong)
    or never render (if the thread overflows above it)."""
    bar_pos = html.find('id="home-session-bar"')
    thread_pos = html.find('id="home-thread"')
    assert bar_pos > 0, "session bar missing"
    assert thread_pos > 0, "home-thread missing"
    assert bar_pos < thread_pos, (
        "home-session-bar must come BEFORE home-thread in the DOM "
        "(strip above thread). Found bar at offset "
        f"{bar_pos}, thread at {thread_pos}."
    )


def test_session_bar_css_classes_defined(html: str):
    """The CSS rules referenced by _homeSessionStripRender must exist
    in the stylesheet — otherwise the chips render as unstyled text."""
    for cls in (
        ".home-session-bar",
        ".home-session-new",
        ".home-session-chips",
        ".home-session-chip",
        ".home-session-chip.active",
        ".home-session-chip-title",
        ".home-session-chip-close",
        ".home-session-counter",
    ):
        assert cls in html, f"missing CSS class declaration: {cls}"


# ── Function presence ────────────────────────────────────────────────────────


@pytest.mark.parametrize("fn_name", [
    # Session storage primitives
    "_homeSessionsLoad",
    "_homeSessionsSave",
    "_homeGenSessionId",
    "_homeNewSessionRecord",
    "_homeActiveSessionId",
    "_homeSetActiveSessionId",
    "_homeMigrateOrInit",
    "_homeGetActiveSession",
    "_homeUpdateActiveSession",
    # Title generation
    "_homeSlugTitleFromMessage",
    "_homeMaybeAutoTitle",
    # Strip render + actions
    "_homeSessionStripRender",
    "_homeSessionStripSyncCounter",
    "_homeSessionNew",
    "_homeSessionSwitch",
    "_homeSessionDelete",
    # Public chat helpers must still exist (session-aware now)
    "_homeChatLoad",
    "_homeChatSave",
    "_homeChatAppend",
    "_homeChatRestore",
    "_homeChatClear",  # back-compat alias — now creates a new session
])
def test_session_function_defined(html: str, fn_name: str):
    """Each session helper must be defined. Catches accidental renames /
    deletes — at least one onclick=... binding in the HTML or JS
    elsewhere depends on the exact function name."""
    pat = re.compile(rf"function\s+{re.escape(fn_name)}\s*\(", re.MULTILINE)
    assert pat.search(html), (
        f"function {fn_name}(...) not defined in index.html"
    )


# ── Wiring checks ────────────────────────────────────────────────────────────


def test_migration_function_reads_legacy_key(html: str):
    """``_homeMigrateOrInit`` must reference ``HOME_CHAT_KEY`` so the
    legacy single-thread blob actually gets migrated. If a refactor
    drops the reference, pre-sessions operators lose their history."""
    # Find the function body
    m = re.search(
        r"function\s+_homeMigrateOrInit\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeMigrateOrInit body"
    body = m.group(1)
    assert "HOME_CHAT_KEY" in body, (
        "_homeMigrateOrInit no longer references HOME_CHAT_KEY — "
        "legacy single-thread blob will not be migrated. Operators "
        "upgrading from pre-sessions will lose their prior chat."
    )


def test_chat_clear_redirects_to_new_session(html: str):
    """The legacy ``_homeChatClear`` function should now create a new
    session (rather than wiping the active one). Console-fired calls
    + any leftover UI references should do something sensible."""
    m = re.search(
        r"function\s+_homeChatClear\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatClear body"
    body = m.group(1)
    # New-session redirect is the documented behavior — body should
    # delegate to _homeSessionNew. (Body might be the call itself or a
    # one-liner.)
    assert "_homeSessionNew" in body, (
        "_homeChatClear should redirect to _homeSessionNew so console "
        "calls don't silently wipe history. Body was:\n" + body
    )


def test_session_new_caps_at_max(html: str):
    """``_homeSessionNew`` should consult HOME_SESSIONS_MAX so the
    sessions list doesn't grow unbounded."""
    m = re.search(
        r"function\s+_homeSessionNew\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeSessionNew body"
    body = m.group(1)
    assert "HOME_SESSIONS_MAX" in body, (
        "_homeSessionNew must reference HOME_SESSIONS_MAX for soft cap "
        "eviction; otherwise the session list grows without bound."
    )


def test_chat_send_path_unchanged_signature(html: str):
    """``_homeChatSend`` is the production chat-send path. It must
    still exist with the same name (called from onclick + Enter
    handler). Belt-and-braces guard against refactors that rename
    it during the sessions rewire."""
    pat = re.compile(r"async\s+function\s+_homeChatSend\s*\(", re.MULTILINE)
    assert pat.search(html), (
        "_homeChatSend missing or renamed — chat input won't submit"
    )


# ── Behavioral smoke check (regex-based, modest scope) ──────────────────────


def test_slug_title_cap_enforced_in_function_body(html: str):
    """``_homeSlugTitleFromMessage`` body must reference
    HOME_SESSION_TITLE_MAX (the cap constant). Silently bypassing the
    cap would produce overlong chips."""
    m = re.search(
        r"function\s+_homeSlugTitleFromMessage\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeSlugTitleFromMessage body"
    body = m.group(1)
    assert "HOME_SESSION_TITLE_MAX" in body, (
        "_homeSlugTitleFromMessage must enforce HOME_SESSION_TITLE_MAX; "
        "otherwise long first-messages produce overlong chip titles"
    )


# ── In-flight session-switch routing guards ──────────────────────────────────
# A 2026-05-20 operator transcript surfaced an ugly bug: typing a message
# in Session A, then switching to Session B before the response landed,
# caused the response to be appended to Session B (whichever was active
# at callback time). The fix captures the originating sid at send time
# and routes both the user message AND the evo reply to THAT session
# via ``_homeChatAppendToSession``.


def test_session_explicit_append_helper_defined(html: str):
    """The session-explicit append helper must exist. Without it,
    ``_homeChatSend`` falls back to active-session routing, which is
    the bug we just fixed."""
    pat = re.compile(
        r"function\s+_homeChatAppendToSession\s*\(\s*targetSid", re.MULTILINE,
    )
    assert pat.search(html), (
        "_homeChatAppendToSession(targetSid, msg) not defined. The chat-"
        "send path relies on this to pin replies to the originating "
        "session — without it, an in-flight thread switch routes the "
        "reply to the wrong session."
    )


def test_chat_send_captures_sending_sid_at_start(html: str):
    """``_homeChatSend`` must capture the active sid in a local
    variable BEFORE any await. Recapturing later (inside the try)
    would re-read after the operator has switched, defeating the
    purpose of the fix."""
    # Pull the function body.
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    body = m.group(1)
    # The capture should appear early in the body (before the first
    # await). We check that ``sendingSid = _homeActiveSessionId()``
    # appears, AND that it appears before ``await api(``.
    cap_pos = body.find("sendingSid = _homeActiveSessionId()")
    await_pos = body.find("await api(")
    assert cap_pos >= 0, (
        "_homeChatSend doesn't capture the active sid into a local "
        "``sendingSid`` variable. Without that pin, an in-flight "
        "switch routes the response to the wrong session."
    )
    assert 0 <= cap_pos < await_pos, (
        "_homeChatSend's sid capture happens after ``await api(...)``. "
        "It MUST happen before — once we await, the active session can "
        "change underneath us."
    )


def test_chat_send_uses_session_explicit_append(html: str):
    """The success + error append paths in ``_homeChatSend`` must
    route through ``_homeChatAppendToSession(sendingSid, ...)``, not
    the active-session ``_homeChatAppend``. If a refactor re-introduces
    the bare append, the in-flight-switch bug returns."""
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    body = m.group(1)
    # At least 3 calls to AppendToSession(sendingSid, ...): user msg,
    # evo response, error path.
    n_explicit = len(re.findall(
        r"_homeChatAppendToSession\(\s*sendingSid", body,
    ))
    assert n_explicit >= 3, (
        f"expected ≥3 _homeChatAppendToSession(sendingSid, ...) calls "
        f"in _homeChatSend (user msg + response + error path); found "
        f"{n_explicit}. Did someone re-introduce _homeChatAppend(...) "
        f"on the chat-send path?"
    )
    # And NO bare _homeChatAppend on this path.
    bare = re.findall(r"\b_homeChatAppend\(", body)
    assert not bare, (
        f"_homeChatSend still calls _homeChatAppend(...) directly "
        f"({len(bare)} occurrences). That helper routes via the "
        f"active session — for the in-flight-switch fix to hold, "
        f"this path must use _homeChatAppendToSession(sendingSid, ...) "
        f"only."
    )


def test_chat_send_builds_history_from_sending_session(html: str):
    """The history sent to the server must come from the SENDING
    session's stored turns, not from ``_homeChatLoad()`` (which
    reads the currently-active session). Otherwise an in-flight
    switch would send the wrong session's history to the model."""
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    body = m.group(1)
    # Must reference the sending session's stored turns explicitly.
    assert "sendingTurns" in body or "sendingSessions[sendingSid]" in body, (
        "_homeChatSend builds history via _homeChatLoad() — which reads "
        "the currently-active session. For the in-flight-switch fix, "
        "history must come from sendingSessions[sendingSid].turns "
        "instead. Did the build-history code revert to _homeChatLoad()?"
    )


def test_pending_bubble_only_renders_if_session_still_active(html: str):
    """The pending '…thinking…' placeholder should only be added to
    the DOM if the operator hasn't switched away yet. Otherwise the
    placeholder lands in the wrong session's DOM and stays there
    until ``_homeChatRestore`` next runs."""
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    body = m.group(1)
    # The pending render is guarded by an active-vs-sending check.
    assert "_homeActiveSessionId() === sendingSid" in body, (
        "the pending '…thinking…' bubble render isn't guarded by an "
        "active-session check. Without the guard, switching threads "
        "right after send paints the placeholder into the wrong "
        "thread."
    )


def test_append_to_session_silently_drops_if_session_deleted(html: str):
    """If the session was deleted while a send was in flight (e.g.
    operator hits Discard on the chip), the response must be
    swallowed silently rather than recreating the deleted session
    or surfacing in another. The helper checks for the session and
    returns early when missing."""
    m = re.search(
        r"function\s+_homeChatAppendToSession\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatAppendToSession body"
    body = m.group(1)
    # An explicit early-return when sessions[targetSid] is falsy.
    assert "if (!session)" in body or "if (!sessions[targetSid])" in body, (
        "_homeChatAppendToSession doesn't guard against a deleted "
        "target session. An in-flight response to a deleted session "
        "could fall through and corrupt unrelated state."
    )


def test_append_to_session_renders_only_when_target_is_active(html: str):
    """When the helper appends to a NON-active session, it must NOT
    render a bubble — that bubble would land in whichever session's
    DOM is currently displayed, which is the original bug."""
    m = re.search(
        r"function\s+_homeChatAppendToSession\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatAppendToSession body"
    body = m.group(1)
    assert "_homeActiveSessionId() === targetSid" in body, (
        "_homeChatAppendToSession's render path isn't gated on the "
        "target being the active session. An in-flight reply to a "
        "non-active session would paint a bubble into the wrong DOM."
    )


# ── Sprint 1: pending timeout + retry + quota handling ──────────────────────


def test_home_chat_pending_timeout_constant_defined(html: str):
    """The pending-bubble timeout must be declared so a stalled POST
    eventually flips into a retry bubble. Without the constant the
    placeholder sits forever."""
    m = re.search(
        r"const\s+HOME_CHAT_PENDING_TIMEOUT_MS\s*=\s*([0-9_]+)", html,
    )
    assert m, "HOME_CHAT_PENDING_TIMEOUT_MS not declared"
    val = int(m.group(1).replace("_", ""))
    # 10s–5min — anything outside is suspect (10s would clip normal
    # replies; 5min defeats the point).
    assert 10_000 <= val <= 300_000, (
        f"HOME_CHAT_PENDING_TIMEOUT_MS={val} outside the sensible 10s–5min "
        "window; verify whether you really meant that"
    )


def test_home_chat_send_starts_and_clears_pending_timeout(html: str):
    """_homeChatSend must set a stalled-bubble timeout AND clear it
    on both success + catch paths. Forgetting to clear leaks a stray
    error bubble after the real reply lands."""
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    body = m.group(1)
    assert "setTimeout(" in body and "HOME_CHAT_PENDING_TIMEOUT_MS" in body, (
        "_homeChatSend doesn't start a pending-bubble timeout. A stalled "
        "send would leave '…thinking…' visible forever."
    )
    # clearTimeout must appear at least twice — once on success path,
    # once on catch — so neither leaks the timeout.
    n_clear = body.count("clearTimeout(pendingTimeoutId)")
    assert n_clear >= 2, (
        f"_homeChatSend calls clearTimeout(pendingTimeoutId) only {n_clear} "
        f"times; expected ≥2 (success path + catch path). A missed "
        f"clear would leak a stray error bubble after the real reply."
    )


def test_home_chat_send_attaches_retry_text_on_error(html: str):
    """Error bubbles must carry the original message under ``retry_text``
    so the renderer can show a ↻ Retry button. Without it, the operator
    has to retype the message after every glitch."""
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    body = m.group(1)
    # Should appear at least twice — once on the timeout path, once
    # on the catch path.
    n_retry = body.count("retry_text: text")
    assert n_retry >= 2, (
        f"_homeChatSend attaches retry_text only {n_retry} times; "
        f"expected ≥2 (timeout path + catch path). Operator can't "
        f"recover from glitches without this."
    )


def test_home_chat_render_bubble_renders_retry_button(html: str):
    """The renderer must surface retry_text as a ↻ Retry button on
    error bubbles. Otherwise the field is ignored and the affordance
    is invisible."""
    m = re.search(
        r"function\s+_homeChatRenderBubble\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatRenderBubble body"
    body = m.group(1)
    assert "retry_text" in body, (
        "_homeChatRenderBubble doesn't reference retry_text. The retry "
        "button won't render even when the field is set."
    )
    assert "_homeChatRetrySend" in body, (
        "_homeChatRenderBubble doesn't wire onclick to _homeChatRetrySend. "
        "The button would render but do nothing."
    )


def test_home_chat_retry_send_defined(html: str):
    """The retry-send helper must exist as a callable JS function."""
    pat = re.compile(r"function\s+_homeChatRetrySend\s*\(", re.MULTILINE)
    assert pat.search(html), "_homeChatRetrySend() not defined"


# ── Long-turn progress (#1454) ───────────────────────────────────────────────


def test_home_chat_slow_indicator_constant_defined(html: str):
    """The slow-indicator threshold must exist and sit well below the
    hard ceiling — otherwise the operator never sees a "still working"
    update before the timeout fires."""
    m = re.search(
        r"const\s+HOME_CHAT_SLOW_INDICATOR_MS\s*=\s*([0-9_]+)", html,
    )
    assert m, "HOME_CHAT_SLOW_INDICATOR_MS not declared"
    slow = int(m.group(1).replace("_", ""))
    m2 = re.search(
        r"const\s+HOME_CHAT_PENDING_TIMEOUT_MS\s*=\s*([0-9_]+)", html,
    )
    assert m2, "HOME_CHAT_PENDING_TIMEOUT_MS not declared"
    hard = int(m2.group(1).replace("_", ""))
    assert 2_000 <= slow <= 30_000, (
        f"HOME_CHAT_SLOW_INDICATOR_MS={slow} outside the sensible 2s–30s "
        "window (too low spams, too high never shows)"
    )
    assert slow < hard, (
        "HOME_CHAT_SLOW_INDICATOR_MS must be smaller than the hard timeout"
    )


def test_home_chat_send_aborts_on_hard_timeout(html: str):
    """``_homeChatSend`` must wire an AbortController to the fetch so
    the hard ceiling actually cancels the request (not just hides the
    UI). Without this, a stalled mobile-Safari fetch can dangle past
    the timeout."""
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    body = m.group(1)
    assert "AbortController" in body, (
        "_homeChatSend doesn't create an AbortController; the hard "
        "timeout would only flip the UI, leaving the fetch hanging."
    )
    assert "abortCtl.abort" in body, (
        "_homeChatSend's timeout handler doesn't call abort(); the "
        "fetch keeps running past the ceiling."
    )
    assert "signal: abortCtl.signal" in body, (
        "_homeChatSend's fetch isn't passed the AbortController's "
        "signal; abort() would have nothing to act on."
    )


def test_home_chat_uses_shared_pending_indicator(html: str):
    """The home composer must call the shared ``_evoChatPendingIndicator``
    helper so the operator sees a live elapsed-time update on long
    turns. Same helper as the per-page evo drawer."""
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    assert "_evoChatPendingIndicator(" in m.group(1), (
        "_homeChatSend doesn't start the shared pending-indicator; "
        "the operator gets no feedback during long multi-tool turns."
    )


def test_shared_pending_indicator_defined(html: str):
    """The shared ``_evoChatPendingIndicator`` helper must exist — both
    the home composer and the drawer use it for the live elapsed-time
    update on the pending bubble."""
    pat = re.compile(
        r"function\s+_evoChatPendingIndicator\s*\(", re.MULTILINE,
    )
    assert pat.search(html), (
        "_evoChatPendingIndicator() helper not defined; the still-"
        "thinking elapsed-time updater is missing."
    )


def test_home_sessions_save_handles_quota_exceeded(html: str):
    """_homeSessionsSave must catch QuotaExceededError and evict
    oldest sessions to recover. The naive empty-catch implementation
    would silently drop the write and lose data."""
    m = re.search(
        r"function\s+_homeSessionsSave\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeSessionsSave body"
    body = m.group(1)
    assert "QuotaExceededError" in body, (
        "_homeSessionsSave doesn't detect QuotaExceededError. A pasted "
        "tool output that pushes the budget over the 5-10MB browser "
        "quota would silently fail (current code: empty catch)."
    )
    # Must NOT evict the active session — that would destroy the
    # in-progress conversation.
    assert "HOME_ACTIVE_SESSION_KEY" in body, (
        "_homeSessionsSave's eviction loop doesn't protect the active "
        "session. Evicting the operator's current conversation would "
        "be worse than the write failing."
    )


# ── #1367 follow-up: unconditional session_id in POST body ──────────────────
# A 2026-05-19 transcript surfaced evo "forgetting" earlier turns after
# an idle gap — diagnosed in
# docs/diagnosis-evo-session-memory-loss-2026-05-20.md as session-id
# fragmentation. When the Chat-page client omitted ``session_id`` from
# the POST body (legacy session records lacking ``oc_session_id``),
# the server minted a fresh ``admin-ui-anon-<uuid>`` per request —
# splitting one operator-perceived thread across many OC sessions.
# Fix Part 1: always include ``session_id`` in the body, computed
# from the session record's stored id when present and from
# ``_homeBuildOcSessionId(sendingSid)`` otherwise.


def test_chat_send_postbody_always_includes_session_id(html: str):
    """``_homeChatSend``'s POST body must include ``session_id``
    UNCONDITIONALLY. The previous code wrapped it in
    ``if (ocSessionId) postBody.session_id = ocSessionId`` — and
    when ``sendingSession.oc_session_id`` was falsy (legacy session
    records, migration corner cases), the field was omitted and the
    server minted a fresh anon UUID per request. Defense: always
    send the field, with the local session-id as the fallback shape."""
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    body = m.group(1)
    # The conditional inclusion is the BUG. It must be gone.
    assert "if (ocSessionId) postBody.session_id" not in body, (
        "_homeChatSend still gates session_id on ocSessionId being "
        "truthy. That's the #1367 session-fragmentation bug — when "
        "the session record lacks oc_session_id, the field is omitted "
        "and the server mints a fresh anon UUID per request."
    )
    # session_id must be a key in the postBody literal — so it ships
    # on every POST regardless of state.
    assert re.search(
        r"const\s+postBody\s*=\s*\{[^}]*session_id\s*:", body,
    ), (
        "_homeChatSend's postBody literal doesn't include session_id "
        "as an unconditional field. Without that, a missing "
        "oc_session_id reintroduces the #1367 fragmentation bug."
    )


def test_chat_send_falls_back_to_built_oc_session_id(html: str):
    """When the session record lacks ``oc_session_id`` (legacy /
    migrated records), the chat-send path must synthesize a STABLE
    id from the local session id via ``_homeBuildOcSessionId``.
    Without this, the fallback would be ``null``, the server would
    receive no session_id, and the anon UUID-per-request bug returns."""
    m = re.search(
        r"async\s+function\s+_homeChatSend\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeChatSend body"
    body = m.group(1)
    # The ocSessionId resolver must reference the build helper.
    assert "_homeBuildOcSessionId(sendingSid)" in body, (
        "_homeChatSend's oc_session_id resolution doesn't fall back "
        "through _homeBuildOcSessionId(sendingSid). A session record "
        "without oc_session_id would resolve to null and the server "
        "would mint a fresh anon UUID per request (#1367 bug)."
    )
