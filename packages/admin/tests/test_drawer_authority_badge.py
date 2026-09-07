"""tests/test_drawer_authority_badge.py — guards for the drawer authority badge.

Closes the discoverability gap where operators couldn't tell what
authority tier (Ask / Ask big / Auto) applied to the per-page evo
drawer. The Chat page's three-button selector writes a global
localStorage value (`evolve_home_tier`); the drawer reads the same
value via `_homeReadTier()` at send time. Before this badge, the only
way to inspect that value from a non-Chat page was through DevTools.

These tests pin the visibility-only invariants and the click-routes
behaviour so the wiring doesn't regress:

  * Badge span is present in the drawer header (with the class name
    and stable id we ship).
  * Click handler routes to the Chat page (so the operator lands at
    the three-button selector when they want to change tier).
  * Badge is NOT wrapped in a `<select>` or button-group — visibility
    only. Per-drawer override is deferred to a separate, larger
    design.
  * Chat-page selector carries a scope-clarification tooltip pointing
    out the setting applies to drawers too.

Same regex-on-source pattern as the other drawer / chat-page guards
(see test_evo_drawer_robustness.py).
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
    # Home tier change → drawer-badge invariant spans home.js (Phase 3m
    # — _homeSaveTier) + evo-drawer.js (Phase 3n — badge render). Concat
    # both so the existing string-shape assertions stay valid.
    return (
        _INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _HOME_JS.read_text(encoding="utf-8")
        + "\n"
        + _EVO_DRAWER_JS.read_text(encoding="utf-8")
    )


# ── Part 1: badge span in the drawer header ────────────────────────────────


def test_drawer_header_contains_authority_badge_span(html: str):
    """The drawer header must contain a span carrying the
    ``evo-drawer-authority-badge`` class with a stable id so JS can
    target it without DOM querying ambiguity. Without this, the badge
    never renders and operators are back to inspecting localStorage."""
    # Locate the drawer header block.
    m = re.search(
        r'<div id="evo-drawer-header">([\s\S]*?)</div>',
        html,
    )
    assert m, "could not locate #evo-drawer-header block"
    header = m.group(1)
    assert 'id="evo-drawer-authority-badge"' in header, (
        "drawer header is missing the authority badge span with stable id "
        "#evo-drawer-authority-badge. The badge is what surfaces the "
        "current Ask / Ask big / Auto tier on every non-Chat page."
    )
    assert "evo-drawer-authority-badge" in header, (
        "drawer header is missing the .evo-drawer-authority-badge class. "
        "CSS targets that class for the chip styling."
    )


def test_authority_badge_has_three_tier_labels(html: str):
    """The JS lookup table must map all three tier values to display
    labels — otherwise an operator on `auto-small` (the middle setting)
    sees an empty badge."""
    # The labels map lives next to the update function.
    m = re.search(
        r"_DRAWER_AUTHORITY_LABELS\s*=\s*\{([\s\S]*?)\}",
        html,
    )
    assert m, "could not locate _DRAWER_AUTHORITY_LABELS map"
    body = m.group(1)
    for tier in ("ask", "auto-small", "auto"):
        assert f"'{tier}'" in body, (
            f"_DRAWER_AUTHORITY_LABELS is missing the '{tier}' tier. "
            "All three Chat-page authority values must map to a label."
        )


# ── Part 2: click handler routes to Chat (visibility-only) ─────────────────


def test_authority_badge_click_navigates_to_home(html: str):
    """Click must route the operator to the Chat (`home`) page, where
    the three-button selector lives. The badge is visibility-only — it
    must not pop a select/menu inline."""
    m = re.search(
        r"function\s+_evoDrawerOpenAuthoritySelector\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerOpenAuthoritySelector body"
    body = m.group(1)
    assert 'data-page="home"' in body, (
        "_evoDrawerOpenAuthoritySelector doesn't look up the home nav "
        "item — click won't land the operator at the Chat page's "
        "authority selector."
    )
    assert "nav(home)" in body or "nav(home," in body, (
        "_evoDrawerOpenAuthoritySelector doesn't call nav(home) to swap "
        "pages. Without this, the click is a no-op."
    )


def test_authority_badge_has_onclick_handler(html: str):
    """The badge span must wire onclick to the navigation helper. A
    static span without a handler is dead weight."""
    m = re.search(
        r'id="evo-drawer-authority-badge"[^>]*>',
        html,
    )
    assert m, "could not locate the authority badge tag"
    tag = m.group(0)
    assert "_evoDrawerOpenAuthoritySelector" in tag, (
        "authority badge span has no onclick wired to "
        "_evoDrawerOpenAuthoritySelector. Click won't route to Chat."
    )


def test_authority_badge_is_not_a_selector(html: str):
    """Visibility-only invariant: the badge must NOT be wrapped in a
    `<select>` element or a button-group. Per-drawer authority change
    is explicitly deferred (Option C in the design doc); the badge is
    Option A — read-only surface."""
    # Pull the badge span and ~250 chars of surrounding HTML.
    m = re.search(
        r'(.{0,250})<span[^>]+id="evo-drawer-authority-badge"[^>]*>'
        r"([\s\S]{0,250})",
        html,
    )
    assert m, "could not locate authority badge with surrounding context"
    surround = m.group(1) + m.group(2)
    # No <select>, no role="group" wrapping the badge, no inner <button>s.
    assert "<select" not in surround.lower(), (
        "authority badge is wrapped in a <select>. Visibility-only "
        "invariant violated — the badge must NOT be a selector."
    )
    assert 'role="group"' not in surround, (
        "authority badge sits inside a role=\"group\" wrapper, which "
        "implies a button group. Per-drawer override is deferred — the "
        "badge must remain visibility-only."
    )
    # The badge itself is a <span>, not a <button> — guards against a
    # future refactor that swaps the element type and inadvertently
    # gives it button affordance.
    badge_tag_m = re.search(
        r'(<\w+)\s+[^>]*id="evo-drawer-authority-badge"',
        html,
    )
    assert badge_tag_m, "could not isolate authority badge element"
    assert badge_tag_m.group(1) == "<span", (
        "authority badge element is not a <span>. Switching to "
        "<button> or <select> would imply override affordance the "
        "badge intentionally lacks."
    )


# ── Part 3: live update wiring ─────────────────────────────────────────────


def test_home_save_tier_propagates_to_drawer_badge(html: str):
    """The Chat-page button-group click handler (`_homeSaveTier`) must
    propagate the change to the drawer badge. Without this, the badge
    only updates when the drawer is opened — flipping tier on the Chat
    page with the drawer already open would show stale text."""
    m = re.search(
        r"function\s+_homeSaveTier\s*\(\s*v\s*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _homeSaveTier body"
    body = m.group(1)
    assert "_updateDrawerAuthorityBadge" in body, (
        "_homeSaveTier doesn't call _updateDrawerAuthorityBadge. Tier "
        "flips on the Chat page won't propagate to the drawer badge."
    )


def test_drawer_open_initializes_authority_badge(html: str):
    """When the drawer opens, the badge must be (re)painted so it
    reflects whatever the operator last picked — even if the tier was
    changed in another tab via localStorage."""
    m = re.search(
        r"function\s+_evoDrawerOpen\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _evoDrawerOpen body"
    body = m.group(1)
    assert "_updateDrawerAuthorityBadge" in body, (
        "_evoDrawerOpen doesn't initialize the authority badge. Opening "
        "the drawer for the first time after a tier change in a sibling "
        "tab would show stale (or empty) text."
    )


# ── Part 4: Chat-page selector tooltip clarifies scope ─────────────────────


def test_chat_page_authority_selector_has_scope_tooltip(html: str):
    """The Chat-page three-button selector must carry a tooltip making
    it explicit that the setting applies to drawers too — not just the
    Chat page send. Before this clarification, that fact was learned
    by reading code."""
    # Find the authority-selector block (the `.home-tier-bar` div).
    m = re.search(
        r'<div class="home-tier-bar"[\s\S]{0,2000}?</div>\s*</div>',
        html,
    )
    assert m, "could not locate .home-tier-bar block on Chat page"
    block = m.group(0)
    # Accept either explicit "drawers" mention or the "all evo surfaces"
    # phrasing. Tooltip pattern uses the existing `title="..."` attribute
    # convention (matches the three buttons' per-tier titles).
    has_scope_hint = (
        "drawers" in block.lower() or "all evo surfaces" in block.lower()
    )
    assert has_scope_hint, (
        "Chat-page authority selector doesn't mention 'drawers' or "
        "'all evo surfaces' in any tooltip. Scope of the selector "
        "(applies to drawers too, not just Chat page) is not "
        "discoverable from the UI."
    )
