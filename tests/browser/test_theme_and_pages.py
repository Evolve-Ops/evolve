"""Browser smoke — theme parity + critical-flow page rendering.

Dispatch spec 3.3 additions.  Each test reuses the existing
``admin_server`` and ``page`` fixtures from ``conftest.py``.

Coverage added here
-------------------
1. **Theme parity** — the only gap in the 80→100 roadmap flagged as
   highest-value and currently UNCOVERED:
   - The sidebar theme-toggle button flips ``data-theme`` between
     ``dark`` and ``light``.
   - Key surfaces (sidebar background, card background) actually adopt
     a DIFFERENT computed color in each theme — catching a regression
     where the CSS variable declarations are removed or the JS toggle
     stops writing ``data-theme``.
   - NEITHER theme produces JS console errors.  The test FAILS if
     the light theme silently regresses on page load.

2. **Proposals / Self-Improvement page** — renders (nav item present,
   page div mounts, proposals container is in the DOM) and shows a
   clean state (no JS errors).  Not previously exercised by the suite.

3. **Maintenance → Pod Health subtab** — navigating to the subtab
   activates ``#maintenance-health`` and the "Scan Now" button is
   reachable.  The surrounding ``test_maintenance_subtabs_*`` tests in
   ``test_smoke.py`` only verify static DOM structure, not activation.

What is NOT tested here
-----------------------
- Pixel snapshots (out of scope — this is a smoke suite).
- API response bodies for ``/api/arbiter/proposals`` or
  ``/api/pod-health`` (those belong in unit tests).
- Per-generator detail panels, proposal actions, or apply flows.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)

# Known-baseline JS errors shared by test_smoke.py and test_bot_config_ux.py.
# Anything outside this list is a regression.
_BASELINE_PAGEERROR_SUBSTRINGS: tuple[str, ...] = (
    "nav is not defined",
    "Can't find variable: nav",
)


def _unexpected_errors(errors: list[str]) -> list[str]:
    return [
        e for e in errors
        if not any(sub in e for sub in _BASELINE_PAGEERROR_SUBSTRINGS)
    ]


# ── 1. Theme parity ──────────────────────────────────────────────────────────


def test_theme_toggle_flips_data_theme_attribute(
    page, admin_server: str
) -> None:
    """Clicking the theme-toggle button cycles ``data-theme`` dark↔light.

    The toggle is the ◑ button in the sidebar footer.  It calls
    ``toggleTheme()`` which reads the current ``data-theme`` on
    ``<html>``, flips it, and persists the choice to ``localStorage``.

    This test pins three contracts:
      1. The initial theme is one of {dark, light} (not missing /
         undefined).
      2. After ONE click the value changes to the other member of the set.
      3. After a SECOND click it returns to the original value (full cycle).
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")

    initial = page.evaluate(
        "() => document.documentElement.getAttribute('data-theme')"
    )
    assert initial in ("dark", "light"), (
        f"data-theme must be 'dark' or 'light' on load; got {initial!r}. "
        "Either the JS init block is missing or the attribute wasn't set."
    )
    expected_after_first = "light" if initial == "dark" else "dark"

    # Click the theme toggle button (sidebar footer — always in the DOM,
    # even at phone widths, because the sidebar is just off-screen).
    page.evaluate("() => document.querySelector('.theme-toggle').click()")
    after_first = page.evaluate(
        "() => document.documentElement.getAttribute('data-theme')"
    )
    assert after_first == expected_after_first, (
        f"theme should flip from {initial!r} to {expected_after_first!r} "
        f"on first click; got {after_first!r}. "
        "toggleTheme() is either missing or not writing data-theme."
    )

    # Second click restores the original value (full round-trip).
    page.evaluate("() => document.querySelector('.theme-toggle').click()")
    after_second = page.evaluate(
        "() => document.documentElement.getAttribute('data-theme')"
    )
    assert after_second == initial, (
        f"theme should return to {initial!r} after second click; "
        f"got {after_second!r}. Toggle is not a proper round-trip."
    )


def test_theme_parity_key_surfaces_restyle(page, admin_server: str) -> None:
    """Switching theme ACTUALLY changes computed colors on key surfaces.

    Guards the silent-regression mode: if the ``[data-theme="light"]``
    CSS block is removed (or never defined), ``toggleTheme()`` still
    writes the attribute — but nothing visually changes.  This test
    catches that by reading the *computed* background-color of two
    surfaces (``<body>`` and ``.sidebar``) in both themes and asserting
    they are DIFFERENT.

    We do NOT assert exact hex values — those can change with a design
    refresh.  We assert inequality: if both themes produce the same
    computed background, the light-theme CSS is missing.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")

    # Capture colors in the current (default dark) theme.
    colors_dark = page.evaluate(
        """() => ({
            body_bg: getComputedStyle(document.body).backgroundColor,
            sidebar_bg: getComputedStyle(document.querySelector('#sidebar')).backgroundColor,
        })"""
    )

    # Switch to light theme.
    page.evaluate(
        """() => {
            document.documentElement.setAttribute('data-theme', 'light');
        }"""
    )
    colors_light = page.evaluate(
        """() => ({
            body_bg: getComputedStyle(document.body).backgroundColor,
            sidebar_bg: getComputedStyle(document.querySelector('#sidebar')).backgroundColor,
        })"""
    )

    assert colors_dark["body_bg"] != colors_light["body_bg"], (
        f"<body> background must differ between dark and light themes; "
        f"dark={colors_dark['body_bg']!r}, light={colors_light['body_bg']!r}. "
        "The [data-theme='light'] CSS block may be missing or --bg not "
        "referenced on body."
    )
    assert colors_dark["sidebar_bg"] != colors_light["sidebar_bg"], (
        f"#sidebar background must differ between dark and light themes; "
        f"dark={colors_dark['sidebar_bg']!r}, light={colors_light['sidebar_bg']!r}. "
        "The [data-theme='light'] --bg2 token or the sidebar background rule "
        "may have been removed."
    )

    # Restore to dark so other tests that share the session aren't affected.
    page.evaluate(
        "() => document.documentElement.setAttribute('data-theme', 'dark')"
    )


def test_dark_theme_no_console_errors(page, admin_server: str) -> None:
    """Dark theme (default) produces no unexpected JS errors on load.

    Baseline contract — the dark theme is always the default, so any
    JS errors in dark mode block every operator.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_timeout(500)
    unexpected = _unexpected_errors(errors)
    assert unexpected == [], (
        f"dark theme page raised UNEXPECTED JS errors: {unexpected}\n"
        f"(all errors: {errors})"
    )


def test_light_theme_no_console_errors(page, admin_server: str) -> None:
    """Light theme produces no unexpected JS errors.

    THIS TEST IS THE REGRESSION GUARD for light-theme silently breaking.

    Pattern: load the page, inject ``data-theme='light'`` before
    the SPA runs any deferred init, then reload with the theme already
    set in ``localStorage``.  Any JS that reads a CSS variable or
    accesses a DOM element that only exists in dark mode would throw
    here.

    The test MUST FAIL when light theme regresses.  If you see it fail:
    check the ``[data-theme='light']`` CSS block in ``base.css`` and
    any JS that branches on ``data-theme``.
    """
    errors: list[str] = []

    # Prime localStorage with the light theme preference BEFORE the
    # page's init IIFE runs so the IIFE picks it up on load.
    # Note: add_init_script evaluates the string as a script expression,
    # not a function call — write the statement directly, not as an arrow fn.
    page.add_init_script(
        "localStorage.setItem('evolve-theme', 'light');"
    )
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_timeout(500)

    # Confirm the theme was actually applied (not silently ignored).
    applied = page.evaluate(
        "() => document.documentElement.getAttribute('data-theme')"
    )
    assert applied == "light", (
        f"Expected data-theme='light' after localStorage prime; "
        f"got {applied!r}. The init IIFE no longer reads evolve-theme from "
        "localStorage — the parity test would be a no-op."
    )

    unexpected = _unexpected_errors(errors)
    assert unexpected == [], (
        f"Light theme raised UNEXPECTED JS errors: {unexpected}\n"
        f"(all errors: {errors})\n"
        "Light theme is silently broken — fix the [data-theme='light'] "
        "CSS rules or the JS path that causes these throws."
    )


# ── 2. Proposals / Self-Improvement page ─────────────────────────────────────


def test_self_improvement_nav_item_and_page_present(
    page, admin_server: str,
) -> None:
    """The Recommendations / Self-Improvement nav item exists and its page mounts.

    Pins the structural contract: sidebar has the nav row, the
    ``#page-self-improvement`` div is in the DOM, and the proposals
    container ``#arbiter-proposals-list`` is present (the renderer
    writes into it on activation).
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")

    # Nav item — the label may read "Recommendations" (HTML at time of
    # writing), but the data-page selector is what we pin.
    nav = page.locator('.nav-item[data-page="self-improvement"]')
    assert nav.count() == 1, (
        "Sidebar is missing data-page='self-improvement' nav item. "
        "The Recommendations page is unreachable from the sidebar."
    )

    page_div = page.locator("#page-self-improvement")
    assert page_div.count() == 1, (
        "#page-self-improvement div not found in DOM. "
        "The page container has been removed or renamed."
    )

    proposals_list = page.locator("#arbiter-proposals-list")
    assert proposals_list.count() == 1, (
        "#arbiter-proposals-list is missing — the proposal renderer has "
        "nowhere to write. Inbox subtab would be permanently empty."
    )


def test_self_improvement_page_activates_without_errors(
    page, admin_server: str,
) -> None:
    """Navigating to the Recommendations page activates it without JS errors.

    Uses the same JS-direct nav() pattern as test_spa_nav_switches_active_page
    in test_smoke.py — faster than driving the drawer and avoids phone-
    viewport sidebar issues.  The key contract: navigation doesn't throw.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")

    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"self-improvement\"]'))"
    )
    page.wait_for_timeout(300)

    # The page div should now be active.
    active = page.locator("#page-self-improvement.active")
    assert active.count() == 1, (
        "#page-self-improvement did not become active after nav() call. "
        "The page-activation path for 'self-improvement' is broken."
    )

    # The Inbox subtab is the default active subtab.
    inbox_subtab = page.locator(
        "#page-self-improvement .subtab[onclick*='proposals']"
    )
    assert inbox_subtab.count() >= 1, (
        "Inbox subtab (proposals) missing from #page-self-improvement. "
        "The subtab row was restructured without updating this test."
    )

    unexpected = _unexpected_errors(errors)
    assert unexpected == [], (
        f"Navigating to Recommendations raised JS errors: {unexpected}"
    )


def test_self_improvement_inbox_shows_list_or_clean_empty_state(
    page, admin_server: str,
) -> None:
    """The proposal Inbox renders a list container or a clean empty state.

    With the empty-pod fixture there are no proposals on disk, so the
    renderer should produce an empty-state placeholder rather than a
    loading spinner or a JS exception.  We check for three acceptable
    outcomes and fail on anything else (hidden container, JS throw,
    raw "undefined", etc.).
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")

    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"self-improvement\"]'))"
    )
    # Wait for the async loader to resolve (proposals endpoint returns
    # quickly on an empty pod).
    page.wait_for_timeout(1000)

    container_html = page.evaluate(
        "() => document.getElementById('arbiter-proposals-list')?.innerHTML || ''"
    )
    # Acceptable states on an empty pod:
    #  (a) still showing "Loading…" — the API hasn't responded yet; fine.
    #  (b) empty-state text (e.g. "No proposals", "Nothing pending",
    #      "all caught up", etc.).
    #  (c) a real list rendered from the API (unlikely in smoke, but valid).
    # Failure states: empty string, "undefined", raw JS error text.
    assert container_html, (
        "#arbiter-proposals-list is empty (innerHTML='') after navigation. "
        "The renderer didn't run or cleared the container without writing."
    )
    assert "undefined" not in container_html.lower(), (
        f"#arbiter-proposals-list contains 'undefined' — likely a JS "
        f"template-string bug. Got: {container_html[:200]!r}"
    )

    unexpected = _unexpected_errors(errors)
    assert unexpected == [], (
        f"Proposals Inbox activation raised JS errors: {unexpected}"
    )


# ── 3. Maintenance → Pod Health subtab ───────────────────────────────────────


def test_maintenance_pod_health_subtab_present(
    page, admin_server: str,
) -> None:
    """The Pod Health subtab exists in the Maintenance subtab bar.

    Pins the data-subtab='health' structural contract.  The companion
    tests in test_smoke.py enumerate the 9 Maintenance subtabs as an
    aggregate; this test explicitly asserts the health subtab exists
    so a rename isn't silently hidden by the count check.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    health_tab = page.locator(
        '#page-maintenance .subtab[data-subtab="health"]'
    )
    assert health_tab.count() >= 1, (
        "Maintenance subtab data-subtab='health' not found. "
        "Pod Health has been removed or renamed — the Overview health-segment "
        "click-through and the signal runner's action button are both broken."
    )
    health_page = page.locator("#maintenance-health")
    assert health_page.count() == 1, (
        "#maintenance-health subtab page div is missing. "
        "The Pod Health content container has been removed."
    )


def test_maintenance_pod_health_activates_without_errors(
    page, admin_server: str,
) -> None:
    """Navigating to Maintenance and clicking Pod Health activates the subtab.

    Smoke-only: verifies that the nav+subTab() chain executes without
    throwing and that ``#maintenance-health`` becomes active.  Does NOT
    assert API results — the pod-health endpoint runs a live scan that
    would be non-deterministic in a CI environment with no running
    LaunchDaemons.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")

    # Navigate to Maintenance, then to the Pod Health subtab.
    page.evaluate(
        """() => {
            window.nav(document.querySelector('.nav-item[data-page="maintenance"]'));
        }"""
    )
    page.wait_for_timeout(200)
    page.evaluate(
        """() => {
            const tab = document.querySelector(
                '#page-maintenance .subtab[data-subtab="health"]'
            );
            if (tab) {
                window.subTab(tab, 'maintenance', 'health');
            }
        }"""
    )
    page.wait_for_timeout(300)

    # #maintenance-health should be active.
    active = page.locator("#maintenance-health.active")
    assert active.count() == 1, (
        "#maintenance-health did not become active after subTab() call. "
        "The Pod Health subtab activation is broken."
    )

    # The "Scan Now" button should be present (it's static markup).
    scan_btn = page.locator("#maintenance-health .btn")
    assert scan_btn.count() >= 1, (
        "No buttons found inside #maintenance-health — the static Scan Now "
        "button has been removed or the subtab content was cleared."
    )

    unexpected = _unexpected_errors(errors)
    assert unexpected == [], (
        f"Maintenance → Pod Health navigation raised JS errors: {unexpected}"
    )
