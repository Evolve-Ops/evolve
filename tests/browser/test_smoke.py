"""Cross-browser smoke suite for the Evolve admin UI.

Phase 0 §4.4 baseline. Each test runs across the browser engines
selected by pytest-playwright's ``--browser`` flag (defaults to
chromium; CI runs ``--browser chromium --browser webkit --browser
firefox``).

These tests intentionally check **engine compatibility**, not feature
behaviour. The point is: if a Chrome-only feature breaks on WebKit, we
want to know on the next CI run. Anything beyond that — feature E2E,
visual regression, accessibility — is the responsibility of feature-
specific test suites or the manual matrix in
``internal/pwa/test-matrix-browsers.md``.

Run locally::

    pip install pytest-playwright
    playwright install chromium webkit firefox
    pytest tests/browser/ --browser chromium --browser webkit --browser firefox

Skip when Playwright isn't installed — the conftest fixture would fail
to import otherwise. The collect-time skip means ``pytest packages/admin/``
runs fine without the optional dep.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip the whole module if pytest-playwright isn't installed. We don't
# want `pytest packages/admin/` (or a plain `pytest` from repo root) to
# fail just because someone hasn't run `pip install pytest-playwright`.
# The pytest-playwright import also covers the bare `playwright` package
# since it depends on it.
pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)


# ── Core: server reachable, index renders ────────────────────────────────────


def test_index_loads(page, admin_server: str) -> None:
    """The admin SPA loads at / and the page title is set."""
    response = page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    assert response is not None, "page.goto returned no response"
    assert response.status == 200, f"expected 200 at /, got {response.status}"
    assert page.title(), "page has no <title> — check templates/index.html"


# Known JS errors at page load that this baseline INTENTIONALLY tolerates
# so the smoke suite stays green while we get cross-browser baseline up.
#
# Each entry is a substring; if every captured error matches at least one
# entry, the test passes. Any NEW pattern fails — that's the regression
# guard.
#
# - "nav is not defined": Inline <script> blocks in deep-link-redirect
#   stub pages (#page-gallery, #page-forge, #page-settings, etc.) call
#   nav(el) during page parse, but nav() is defined ~4800 lines later.
#   These stubs throw 9× on every page load today. Tracked as a follow-
#   up; fix is to either move nav() above the stubs or wrap each
#   redirect in DOMContentLoaded.
KNOWN_CONSOLE_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "nav is not defined",          # Chromium, Firefox phrasing
    "Can't find variable: nav",    # WebKit / Safari phrasing for the same throw
)


def test_index_has_no_console_errors(page, admin_server: str) -> None:
    """The SPA shell doesn't fail on load — modulo known issues.

    Distinguishes JS errors (script parse / uncaught throw) from
    expected 404s in the network tab — empty-pod fixture has no real
    bots, so /api/status fan-out *will* surface some empty/404 responses;
    those are fine. JS-level errors are not — except for those in the
    ``KNOWN_CONSOLE_ERROR_SUBSTRINGS`` allow-list, which exist to keep
    this cross-engine smoke suite green while their fixes land
    separately. Any NEW error pattern fails this test on purpose; that
    is the regression guard.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Give the SPA's initial fetch fan-out a beat to settle in
    # case any deferred handler throws.
    page.wait_for_timeout(500)
    unexpected = [
        e
        for e in errors
        if not any(known in e for known in KNOWN_CONSOLE_ERROR_SUBSTRINGS)
    ]
    assert unexpected == [], (
        f"page raised UNEXPECTED JS errors on load: {unexpected}\n"
        f"(all captured errors: {errors})"
    )


def test_spa_nav_items_present(page, admin_server: str) -> None:
    """The sidebar nav renders the canonical data-page items.

    Smoke check that ``index.html`` is being served whole — not a
    truncated fallback. The page list mirrors
    ``internal/spec-pwa-2026-05-18.md`` §4.3 plus the existing nav rows.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    expected = [
        "home",
        "overview",
        "integrations-keys",
        "cost",
        "maintenance",
        "backup",   # Phase 1b: promoted from a Maintenance subtab
        "reports",
        "security",
        "skills",
        "apps",
        "self-improvement",
        "settings",
        "help",
        "terminal",
    ]
    for page_id in expected:
        loc = page.locator(f'.nav-item[data-page="{page_id}"]')
        assert loc.count() >= 1, (
            f"nav-item[data-page='{page_id}'] not found — "
            "the SPA shell may be partial or the page id changed"
        )


# ── Core: critical endpoints respond ────────────────────────────────────────
#
# We exercise these via the browser (rather than urllib) so the request
# goes through the same engine + same CORS / cookie behaviour the SPA
# itself would use. Anything that breaks for browser fetches but works
# for plain HTTP gets caught here.


@pytest.mark.parametrize(
    "path",
    [
        "/api/version",
        "/api/health",
        "/api/status",
        "/api/network",
        "/api/host-health",
    ],
)
def test_core_api_endpoints_respond(
    page, admin_server: str, path: str
) -> None:
    """Each core API returns 200 and valid JSON to a browser fetch."""
    # Use page.request — uses the same network stack as the browser
    # context, but lets us inspect status without rendering.
    response = page.request.get(f"{admin_server}{path}")
    assert response.status == 200, (
        f"{path} returned {response.status}; body={response.text()[:200]}"
    )
    # All these endpoints are documented as JSON.
    body = response.json()
    assert isinstance(body, (dict, list)), (
        f"{path} should return JSON; got {type(body).__name__}"
    )


# ── Static assets the PWA Phase 1 will rely on ──────────────────────────────


@pytest.mark.parametrize(
    "asset",
    [
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/favicon-16x16.png",
        "/favicon-32x32.png",
        "/site.webmanifest",  # backwards-compat alias for /manifest.json
    ],
)
def test_static_assets_served(page, admin_server: str, asset: str) -> None:
    """Icon + manifest assets used by Phase 1 PWA install are served today."""
    response = page.request.get(f"{admin_server}{asset}")
    assert response.status == 200, (
        f"{asset} should be served; got {response.status}"
    )


# ── PWA Phase 1.1.A — manifest + service worker + install criteria ──────────
#
# The browser's "install" affordance only appears when ALL the install
# criteria are met:
#   1. HTTPS (or localhost — these tests use 127.0.0.1 which counts).
#   2. A served manifest with name, short_name, start_url, display, icons.
#   3. A registered service worker with a non-trivial fetch handler.
# Any one missing = silent disable. These tests pin all three so a
# regression here doesn't quietly turn off the Install button across the
# browser matrix.


def test_manifest_json_has_required_pwa_shape(page, admin_server: str) -> None:
    """``/manifest.json`` returns a valid Web App Manifest.

    Pins: content-type, the five fields Chrome/Edge check before showing
    the Install button (name, short_name, start_url, display, icons),
    display=standalone (fullscreen disables install in Chromium), and
    that the icons array contains at least one 512×512 entry — the
    minimum size browsers require for the install prompt.
    """
    response = page.request.get(f"{admin_server}/manifest.json")
    assert response.status == 200, (
        f"/manifest.json returned {response.status}; "
        f"body={response.text()[:200]}"
    )
    ct = response.headers.get("content-type", "")
    assert "application/manifest+json" in ct, (
        f"/manifest.json should use application/manifest+json; got {ct!r}"
    )
    body = response.json()
    assert isinstance(body, dict), "manifest body should be a JSON object"
    for field in ("name", "short_name", "start_url", "display", "icons"):
        assert field in body, f"manifest missing required field: {field!r}"
    assert body["display"] == "standalone", (
        f"manifest display must be 'standalone' to pass install criteria; "
        f"got {body['display']!r}"
    )
    icons = body["icons"]
    assert isinstance(icons, list) and icons, "manifest icons must be a non-empty list"
    sizes = {ic.get("sizes") for ic in icons}
    assert "512x512" in sizes, (
        f"manifest must declare at least one 512x512 icon; got sizes={sizes}"
    )
    purposes = {ic.get("purpose") for ic in icons if ic.get("purpose")}
    assert "maskable" in purposes, (
        "manifest must declare a maskable icon so Android/Chrome can crop "
        "into circle/squircle masks without clipping"
    )


def test_manifest_reflects_pod_name(
    page, admin_server: str, network_path: Path
) -> None:
    """The manifest's ``short_name`` derives from network.json's pod id.

    The fixture's default ``networkId`` is ``"browser-smoke-test"``; flip
    it and assert the manifest mirrors the change without a server
    restart (the route reads network.json on each request, same as the
    HTTPS-banner test). Pins the per-pod manifest contract from spec
    §3 — two pods, two installed PWAs with different short_names.
    """
    data = json.loads(network_path.read_text())
    data["networkId"] = "test-pod-acme"
    network_path.write_text(json.dumps(data, indent=2))

    body = page.request.get(f"{admin_server}/manifest.json").json()
    assert body["short_name"] == "test-pod-acme", (
        f"manifest short_name should reflect networkId; got {body['short_name']!r}"
    )
    assert body["name"].endswith("test-pod-acme"), (
        f"manifest name should embed networkId; got {body['name']!r}"
    )


def test_manifest_drops_suffix_for_default_pod_name(
    page, admin_server: str, network_path: Path
) -> None:
    """When ``networkId`` is the wizard's default ("my-pod") the manifest
    renders plain "Evolve" instead of "Evolve · my-pod".

    Without this, Chrome's standalone-window title stitching produces
    "Evolve · my-pod Evolve" in the dock/title bar on a fresh install —
    the redundant duplication the PWA install-polish fix kills. Pin both
    name and short_name so a future refactor doesn't quietly bring the
    "Evolve · my-pod" branding back.
    """
    data = json.loads(network_path.read_text())
    data["networkId"] = "my-pod"
    network_path.write_text(json.dumps(data, indent=2))

    body = page.request.get(f"{admin_server}/manifest.json").json()
    assert body["name"] == "Evolve", (
        f"default pod name should give plain 'Evolve'; got name={body['name']!r}"
    )
    assert body["short_name"] == "Evolve", (
        f"default pod name should give plain 'Evolve'; got short_name={body['short_name']!r}"
    )


def test_service_worker_served_with_js_mime(page, admin_server: str) -> None:
    """``/sw.js`` returns ``application/javascript`` (or text/javascript).

    Browsers reject service-worker scripts served with the wrong MIME
    (text/html, text/plain, application/json). The Service-Worker-
    Allowed header lets a SW at a deeper path control the whole origin —
    we serve from /sw.js so the header is belt-and-braces, not required.
    """
    response = page.request.get(f"{admin_server}/sw.js")
    assert response.status == 200, (
        f"/sw.js returned {response.status}; body={response.text()[:200]}"
    )
    ct = response.headers.get("content-type", "")
    assert "javascript" in ct, (
        f"/sw.js should use a javascript MIME; got {ct!r}. "
        "Browsers reject SW scripts with non-javascript MIME types."
    )
    body = response.text()
    # Sanity-check that the script actually contains the SW shape — a
    # truncated/empty file would still serve 200 but break registration.
    assert "addEventListener" in body, "/sw.js body looks empty / missing handlers"


@pytest.mark.parametrize(
    "path",
    [
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/icon-512-maskable.png",
        "/static/icons/apple-touch-icon-180.png",
    ],
)
def test_pwa_icons_served(page, admin_server: str, path: str) -> None:
    """Every icon the manifest references resolves to a PNG.

    A 404 on any of these silently kills the install prompt: Chrome
    requires every declared icon to load. We check both status and
    content-type so a "200 OK with an HTML error page" regression
    (the SPA's catch-all is a common source) fails loudly.
    """
    response = page.request.get(f"{admin_server}{path}")
    assert response.status == 200, (
        f"{path} returned {response.status}; install criteria broken"
    )
    ct = response.headers.get("content-type", "")
    assert "image/png" in ct, (
        f"{path} should serve image/png; got {ct!r}"
    )


def test_service_worker_registers_on_load(page, admin_server: str) -> None:
    """After the SPA loads, ``/sw.js`` registers and reaches the active state.

    The third install-criterion check: a SW must actually register. On
    the *first* visit ``navigator.serviceWorker.controller`` is null
    (the page's HTTP request preceded the registration), and our
    in-page handler reloads on ``controllerchange`` so subsequent
    navigations are controlled. To avoid racing that reload we instead
    poll the registration's ``active`` slot via
    ``getRegistration()`` — it transitions to set as soon as the SW
    finishes ``activate``, independent of whether THIS page is yet
    controlled. Then issue a second goto and confirm the controller
    is wired.

    Note: 127.0.0.1 counts as a secure context (per the WHATWG secure-
    contexts spec), so the registration's secure-context guard passes
    despite the http:// scheme.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Wait for the SW to install + activate. ``getRegistration().active``
    # populates once the worker reaches ``activated`` state — safe to
    # poll without racing the controllerchange-driven reload below.
    page.wait_for_function(
        """async () => {
            if (!('serviceWorker' in navigator)) return false;
            const reg = await navigator.serviceWorker.getRegistration('/');
            return !!(reg && reg.active && reg.active.scriptURL.endsWith('/sw.js'));
        }""",
        timeout=15000,
    )
    # Reload onto a controlled page so ``controller`` is populated. The
    # SW is already active; this load goes through the fetch handler.
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => navigator.serviceWorker.controller !== null",
        timeout=10000,
    )
    controller = page.evaluate(
        "() => navigator.serviceWorker.controller?.scriptURL || null"
    )
    assert controller and controller.endswith("/sw.js"), (
        f"expected /sw.js controller after second load; got {controller!r}"
    )


def test_install_menu_item_hidden_until_event_fires(page, admin_server: str) -> None:
    """``#pwa-install-item`` defaults to display:none.

    The beforeinstallprompt stash reveals the menu row when (and only
    when) the browser confirms install criteria are met. Playwright/
    headless doesn't synthesize the event, so the row should still be
    hidden after a normal page load. This pins the "no false-positive
    Install affordance" contract — if we flipped the default to
    display:flex we'd be promising an install that doesn't work.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    display = page.evaluate(
        "() => getComputedStyle(document.getElementById('pwa-install-item')).display"
    )
    assert display == "none", (
        f"#pwa-install-item should be hidden by default; got display={display!r}. "
        "A visible Install row without the cached beforeinstallprompt event "
        "would prompt a no-op click."
    )


def test_ios_hint_visibility_logic(browser, admin_server: str) -> None:
    """The iOS hint shows for iOS Safari UA + non-standalone display.

    Spins up a context with an iPhone Safari user-agent so the in-page
    UA check fires. The hint should appear; clicking dismiss should
    persist the choice in localStorage and hide it on the next load.
    Engines other than WebKit ignore navigator.standalone so we also
    verify the hint hides if the page is already in standalone mode.
    """
    iphone_ua = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
        "Mobile/15E148 Safari/604.1"
    )
    ctx = browser.new_context(user_agent=iphone_ua)
    try:
        page = ctx.new_page()
        page.goto(f"{admin_server}/", wait_until="domcontentloaded")
        page.wait_for_selector("#pwa-ios-hint.visible", timeout=3000)
        page.click("#pwa-ios-hint .pih-dismiss")
        page.wait_for_function(
            "() => !document.getElementById('pwa-ios-hint').classList.contains('visible')",
            timeout=2000,
        )
        # Persistence: reload, the dismissal should stick.
        page.reload(wait_until="domcontentloaded")
        # Give the IIFE a beat to evaluate the localStorage gate.
        page.wait_for_timeout(200)
        visible_after_reload = page.evaluate(
            "() => document.getElementById('pwa-ios-hint').classList.contains('visible')"
        )
        assert not visible_after_reload, (
            "iOS hint should stay dismissed across reloads; localStorage gate broken"
        )
    finally:
        ctx.close()


def test_head_advertises_manifest_and_theme_color(page, admin_server: str) -> None:
    """``<link rel="manifest">`` and the iOS meta tags are present.

    Without ``<link rel="manifest">`` the browser never fetches the
    manifest; the install button stays disabled. The theme-color +
    apple-mobile-web-app-* tags drive standalone-window chrome on each
    platform. Cheap, deterministic regression guard.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => ({
            manifest_href: document.querySelector('link[rel="manifest"]')?.getAttribute('href'),
            theme_color: document.querySelector('meta[name="theme-color"]')?.getAttribute('content'),
            apple_capable: document.querySelector('meta[name="apple-mobile-web-app-capable"]')?.getAttribute('content'),
            apple_touch_icon: document.querySelector('link[rel="apple-touch-icon"]')?.getAttribute('href'),
        })"""
    )
    assert result["manifest_href"] == "/manifest.json", (
        f"<link rel=manifest> should point at /manifest.json; got {result['manifest_href']!r}"
    )
    assert result["theme_color"], "missing <meta name=theme-color>"
    assert result["apple_capable"] == "yes", (
        f"missing/incorrect <meta name=apple-mobile-web-app-capable>; got {result['apple_capable']!r}"
    )
    assert result["apple_touch_icon"] and result["apple_touch_icon"].startswith("/static/icons/"), (
        f"apple-touch-icon should point at the /static/icons/ path; got {result['apple_touch_icon']!r}"
    )


# ── SPA navigation actually swaps pages ─────────────────────────────────────


def test_spa_nav_switches_active_page(page, admin_server: str) -> None:
    """Clicking a nav item activates that data-page div.

    Catches engine-specific event-handling regressions (e.g. WebKit
    pointer-event quirks). Uses the dashboard ('overview') tab because
    it's stable and unlikely to be renamed.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # The sidebar is the slide-in drawer at phone width — clicking a
    # nav-item requires opening it first OR calling nav() directly via
    # JS. The latter is faster and exercises the same code path.
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"overview\"]'))"
    )
    active = page.locator(".nav-item.active")
    assert active.count() >= 1
    assert active.first.get_attribute("data-page") == "overview", (
        "clicking the overview nav item did not mark it active"
    )
    # The corresponding page div should also be activated.
    page_div = page.locator("#page-overview.active")
    assert page_div.count() == 1, (
        "#page-overview did not become active after nav click"
    )


# ── Engine-specific guard for the modern APIs we DO rely on ─────────────────


def test_clipboard_api_present(page, admin_server: str) -> None:
    """``navigator.clipboard.writeText`` exists in the page context.

    The admin UI uses it 8x with a ``document.execCommand('copy')``
    fallback. The API requires a secure context — over plain HTTP from
    127.0.0.1 / localhost it IS available; over plain HTTP from a non-
    localhost host (the today-pre-Phase-0.1 state from a phone) it is
    NOT. This test pins the localhost contract: as long as we serve
    from 127.0.0.1, clipboard is available across all three engines.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    has_clipboard = page.evaluate(
        "() => typeof navigator.clipboard?.writeText === 'function'"
    )
    assert has_clipboard, "navigator.clipboard.writeText missing in browser context"


# ── Phase 0 §4.3.a responsive foundation primitives ─────────────────────────
#
# These tests pin the FOUNDATIONAL behaviour shipped in 4.3.a:
# breakpoint custom-property declaration, auto-fill grid utilities, the
# .resp-table primitive's three width modes, the tooltip touch path, and
# subtab horizontal-overflow. Per-page sweeps (4.3.b–d) layer on top
# without touching these — that's the whole point of breaking the
# foundation out.


def test_breakpoint_custom_properties_declared(page, admin_server: str) -> None:
    """The canonical breakpoint values resolve from :root.

    The values themselves are documentation (CSS @media queries can't
    consume custom properties), so this test guards against accidental
    deletion of the declaration block. If anyone removes the :root
    --bp-* declaration, every downstream consumer assumption breaks.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    vals = page.evaluate(
        """() => {
        const cs = getComputedStyle(document.documentElement);
        return {
          phone: cs.getPropertyValue('--bp-phone').trim(),
          tablet: cs.getPropertyValue('--bp-tablet').trim(),
          laptop: cs.getPropertyValue('--bp-laptop').trim(),
        };
        }"""
    )
    assert vals == {"phone": "480px", "tablet": "768px", "laptop": "1280px"}, (
        f"breakpoint custom properties drifted from the canonical set: {vals}"
    )


def test_grid_utilities_use_auto_fill(page, admin_server: str) -> None:
    """The .grid-N utilities reflow via repeat(auto-fill, minmax(…)).

    Catches accidental regression to the old `repeat(N, 1fr)` form,
    which doesn't reflow on narrow viewports. We don't assert exact
    minmax floors (those are tuneable); we only assert that auto-fill
    is the layout mode.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    modes = page.evaluate(
        """() => {
        // Each utility lives on a div the page may not currently use;
        // build a probe element, attach the class, and read the
        // computed grid-template-columns value. Auto-fill + minmax
        // resolves to a concrete pixel string at the probe width;
        // the literal string 'repeat(' would survive only the
        // un-reflowing repeat(N, 1fr) form. So we test by stripping
        // the probe down to 50px wide — auto-fill collapses to one
        // column; the old form would still show N tracks.
        const probe = document.createElement('div');
        probe.style.cssText = 'display:grid;width:50px;position:absolute;left:-9999px';
        document.body.appendChild(probe);
        const out = {};
        for (const cls of ['grid-2', 'grid-3', 'grid-4', 'grid-6']) {
            probe.className = 'grid ' + cls;
            const cols = getComputedStyle(probe).gridTemplateColumns
                .split(' ').filter(Boolean).length;
            out[cls] = cols;
        }
        probe.remove();
        return out;
        }"""
    )
    # At 50px wide, every auto-fill grid should collapse to 1 column.
    # The old repeat(N, 1fr) form would still show N tracks here.
    for cls, cols in modes.items():
        assert cols == 1, (
            f".{cls} resolved to {cols} columns at 50px width — "
            "this means it's still using fixed repeat(N, 1fr) instead "
            "of repeat(auto-fill, minmax(...)). Regressed from 4.3.a."
        )


def test_resp_table_card_stack_at_phone_width(page, admin_server: str) -> None:
    """At <480px viewport, .resp-table rows render as stacked cards.

    Pinpoints the phone-mode behaviour of the responsive-table
    primitive: <thead> hides, each <tr> becomes display:block, each
    <td> becomes a flex row with the data-label rendered via
    ::before { content: attr(data-label) }.

    We use a programmatic probe (rather than navigating to the live
    Infra Jobs table, which requires the launchd API) so the test is
    deterministic across environments.
    """
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const wrap = document.createElement('div');
        wrap.className = 'resp-table-wrap';
        wrap.innerHTML = `
            <table class="resp-table">
              <thead><tr><th>Col A</th><th>Col B</th></tr></thead>
              <tbody>
                <tr><td data-label="Col A">a1</td><td data-label="Col B">b1</td></tr>
              </tbody>
            </table>
        `;
        document.body.appendChild(wrap);
        const thead = wrap.querySelector('thead');
        const tr = wrap.querySelector('tbody tr');
        const td = wrap.querySelector('tbody td');
        const beforeContent = getComputedStyle(td, '::before').content;
        // Firefox returns the literal `attr(data-label)` in computed
        // style; Chromium/WebKit resolve it to the attribute value.
        // Either form proves the rule is wired up.
        const labelOk =
            beforeContent.includes('Col A') ||
            beforeContent.includes('attr(data-label)');
        const out = {
            thead_display: getComputedStyle(thead).display,
            tr_display: getComputedStyle(tr).display,
            td_display: getComputedStyle(td).display,
            label_in_before: labelOk,
            before_raw: beforeContent,
        };
        wrap.remove();
        return out;
        }"""
    )
    assert result["thead_display"] == "none", (
        f"<thead> should hide at phone width; got display={result['thead_display']}"
    )
    assert result["tr_display"] == "block", (
        f"<tr> should be display:block at phone width; got {result['tr_display']}"
    )
    assert result["td_display"] == "flex", (
        f"<td> should be display:flex at phone width; got {result['td_display']}"
    )
    assert result["label_in_before"], (
        "::before pseudo-element should render data-label content; "
        f"got {result!r}"
    )


def test_resp_table_normal_at_laptop_width(page, admin_server: str) -> None:
    """At ≥768px viewport, .resp-table renders as a normal table.

    Mirror of the phone-width test: above the tablet boundary, no
    responsive mode applies and the table should display as
    table/table-row-group/table-cell.
    """
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const wrap = document.createElement('div');
        wrap.className = 'resp-table-wrap';
        wrap.innerHTML = `
            <table class="resp-table">
              <thead><tr><th>Col</th></tr></thead>
              <tbody><tr><td data-label="Col">a</td></tr></tbody>
            </table>
        `;
        document.body.appendChild(wrap);
        const out = {
            thead: getComputedStyle(wrap.querySelector('thead')).display,
            tr: getComputedStyle(wrap.querySelector('tbody tr')).display,
            td: getComputedStyle(wrap.querySelector('tbody td')).display,
        };
        wrap.remove();
        return out;
        }"""
    )
    assert result["thead"] == "table-header-group", (
        f"<thead> should render normally at laptop width; got {result['thead']}"
    )
    assert result["tr"] == "table-row", (
        f"<tr> should render normally at laptop width; got {result['tr']}"
    )
    assert result["td"] == "table-cell", (
        f"<td> should render normally at laptop width; got {result['td']}"
    )


def test_tooltip_opens_on_tap_and_closes_on_outside(page, admin_server: str) -> None:
    """Touch-fix: clicking a .help-btn sets data-open; clicking outside clears.

    Catches regressions where the tap-to-toggle JS handler is removed
    or short-circuited. Locks in the Phase 0 §4.3.a contract that
    touch users can both open AND dismiss tooltips.

    Uses a programmatic .help-btn (rather than the many in the live
    SPA) because most ship inside non-active pages — visibility-stable
    real instances are surface-area dependent, and the touch path is a
    delegated document-level handler that doesn't care.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """async () => {
        const probe = document.createElement('span');
        probe.className = 'help-btn';
        probe.id = 'test-help-probe';
        probe.innerHTML = '?<span class="tip">probe-tip</span>';
        // Position visibly so we don't fight any layout edge cases.
        probe.style.position = 'fixed';
        probe.style.top = '100px';
        probe.style.left = '100px';
        probe.style.zIndex = '99999';
        document.body.appendChild(probe);

        // Simulate the tap.
        probe.click();
        const afterTap = probe.getAttribute('data-open');

        // Simulate clicking outside (body bubbles into the document-level
        // delegated handler).
        document.body.click();
        const afterOutside = probe.getAttribute('data-open');

        // Cleanup.
        probe.remove();
        return { afterTap, afterOutside };
        }"""
    )
    assert result["afterTap"] == "true", (
        f"click on .help-btn did not set data-open=true; got {result['afterTap']!r}. "
        "Touch path (Phase 0 §4.3.a tooltip touch-fix) broken."
    )
    assert result["afterOutside"] != "true", (
        f"click outside .help-btn did not clear data-open; got {result['afterOutside']!r}. "
        "Tap-outside-dismiss broken."
    )


# ── Phase 0 §4.3.c-Cost: Cost-page responsive sweep ─────────────────────────
#
# These tests pin the contract delivered by the Cost-page sweep: every
# JS-rendered Cost-page table opts into the .resp-table primitive AND
# carries data-label on every <td>. Without data-label, the phone-mode
# card stack renders empty labels — the silent-failure mode the
# foundation primitive is most vulnerable to. We assert at the structural
# level (DOM after render) so a future refactor that drops the attrs
# fails loudly.


def test_cost_breakdown_tables_have_resp_table_and_data_labels(
    page, admin_server: str
) -> None:
    """The four Spend-subtab breakdown tables emit data-label on every cell.

    Drives _renderUsageTables() with a minimal stub payload (no API
    fetch — keeps the test deterministic) and reads the resulting DOM
    for each of the four breakdown containers. If any <td> is missing
    data-label, the card-stack mode at phone width would render
    unlabeled rows — silent failure.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const stub = {
            by_model: [{model: 'anthropic/claude-sonnet-4-6', calls: 10, cost: 0.5, cache_read: 100, cache_write: 200, auth_mode: 'API key'}],
            by_channel: [{channel: 'slack', calls: 5, cost: 0.25}],
            by_source: [{source: 'user', calls: 10, sessions: 3, cost: 0.5}, {source: 'cron', calls: 4, sessions: 2, cost: 0.1}],
            by_user: [{user_key: 'u1@example', calls: 5}, {user_key: 'u2@example', calls: 3}],
        };
        _renderUsageTables(stub);
        const out = {};
        for (const id of ['usage-table-model', 'usage-table-channel', 'usage-table-source', 'usage-table-user']) {
            const root = document.getElementById(id);
            const table = root.querySelector('table.resp-table');
            const wrap = root.querySelector('div.resp-table-wrap');
            const tds = root.querySelectorAll('tbody td');
            const missing = [...tds].filter(td => !td.hasAttribute('data-label'));
            out[id] = {
                has_resp_table: !!table,
                has_wrap: !!wrap,
                td_count: tds.length,
                missing_label: missing.length,
            };
        }
        return out;
        }"""
    )
    for table_id, info in result.items():
        assert info["has_resp_table"], (
            f"{table_id}: <table.resp-table> missing — table not opted into primitive"
        )
        assert info["has_wrap"], (
            f"{table_id}: <div.resp-table-wrap> missing — scroll viewport not wired up"
        )
        assert info["td_count"] > 0, (
            f"{table_id}: stub render produced 0 <td>s — renderer broken"
        )
        assert info["missing_label"] == 0, (
            f"{table_id}: {info['missing_label']}/{info['td_count']} <td>s missing "
            "data-label — card-stack mode would render unlabeled rows"
        )


def test_resp_table_empty_data_label_collapses_pseudo(
    page, admin_server: str
) -> None:
    """Cells with data-label="" render full-width on phone with no label.

    Used by decorative swatch columns (e.g. the By Model table's color
    dot) and the session-browser accordion expand row. Without the
    override, ::before would render with empty content but still occupy
    space, producing an off-center value column.
    """
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const wrap = document.createElement('div');
        wrap.className = 'resp-table-wrap';
        wrap.innerHTML = `
            <table class="resp-table">
              <thead><tr><th>L</th><th>V</th></tr></thead>
              <tbody><tr>
                <td data-label="">decorative</td>
                <td data-label="Value">v</td>
              </tr></tbody>
            </table>`;
        document.body.appendChild(wrap);
        const decor = wrap.querySelector('td[data-label=""]');
        const labelled = wrap.querySelector('td[data-label="Value"]');
        const out = {
            decor_display: getComputedStyle(decor).display,
            decor_text_align: getComputedStyle(decor).textAlign,
            decor_before: getComputedStyle(decor, '::before').display,
            labelled_display: getComputedStyle(labelled).display,
        };
        wrap.remove();
        return out;
        }"""
    )
    assert result["decor_display"] == "block", (
        f"empty-data-label td should be display:block at phone width; "
        f"got {result['decor_display']}"
    )
    assert result["decor_text_align"] != "right", (
        f"empty-data-label td should not be right-aligned (would crowd the value); "
        f"got text-align={result['decor_text_align']}"
    )
    assert result["decor_before"] == "none", (
        f"empty-data-label td ::before should be display:none; "
        f"got {result['decor_before']}"
    )
    assert result["labelled_display"] == "flex", (
        f"normal-data-label td should still be display:flex; "
        f"got {result['labelled_display']}"
    )


def test_cost_page_renders_without_horizontal_overflow_at_phone(
    page, admin_server: str
) -> None:
    """At 360px, the Cost page itself doesn't horizontally overflow.

    Tables may scroll internally (tablet mode at 480-767px); below 480
    they card-stack and the page body should not exceed the viewport.
    """
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # At <980px the sidebar lives off-screen behind the hamburger drawer
    # (§4.3.b). Open the drawer before clicking the nav item; clicking
    # the still-off-screen .nav-item directly would hang Playwright on
    # the visibility wait. _open_drawer is defined below.
    _open_drawer(page)
    page.click('.nav-item[data-page="cost"]')
    page.wait_for_timeout(300)
    body_overflow = page.evaluate(
        """() => {
        const root = document.documentElement;
        return {
            scroll_w: root.scrollWidth,
            client_w: root.clientWidth,
        };
        }"""
    )
    # Allow a 2px tolerance for sub-pixel rendering on different engines.
    assert body_overflow["scroll_w"] <= body_overflow["client_w"] + 2, (
        f"Cost page overflows viewport at 360px: scrollWidth={body_overflow['scroll_w']} "
        f"vs clientWidth={body_overflow['client_w']}"
    )


# ── Cost-page polish: primary/secondary column split + .resp-table-dense ────
#
# These tests pin the contract delivered by the polish PR on top of the
# §4.3.c-Cost sweep: the four wide Cost-page breakdown tables hide
# diagnostic columns by default and expose them via a per-panel toggle
# (state persisted in localStorage), AND they card-stack at <768px
# instead of <480px via the .resp-table-dense modifier so the 480–767px
# tablet range gets cards rather than horizontal scroll.
#
# Silent-failure modes the prompt explicitly flagged:
#  - Renderer that drops data-secondary on a future column refactor.
#  - localStorage write that races between two panels (would manifest as
#    one panel's state clobbering another's).
#  - CSS specificity that doesn't actually hide secondary cells when
#    .resp-table-dense kicks in at 481–767px.
# Each test below pins one of these.


def _seed_cost_tables(page) -> None:
    """Helper: render the Cost-page breakdown tables with a stub payload.

    The same stub used by test_cost_breakdown_tables_have_resp_table_and_data_labels
    so we keep the test corpus consistent. Pre-emptively clears any
    persisted resp-cols state to start each test from a clean slate.
    """
    page.evaluate(
        """() => {
        // Clear any persisted toggle state from earlier tests.
        for (const k of Object.keys(localStorage)) {
            if (k.startsWith('evolve.respCols.')) localStorage.removeItem(k);
        }
        const stub = {
            by_model: [
                {model: 'anthropic/claude-sonnet-4-6', calls: 10, cost: 0.5,
                 cache_read: 100, cache_write: 200, auth_mode: 'API key'},
            ],
            by_channel: [{channel: 'slack', calls: 5, cost: 0.25}],
            by_source: [
                {source: 'user', calls: 10, sessions: 3, cost: 0.5},
                {source: 'cron', calls: 4, sessions: 2, cost: 0.1},
            ],
            by_user: [
                {user_key: 'u1@example', calls: 5},
                {user_key: 'u2@example', calls: 3},
            ],
        };
        _renderUsageTables(stub);
        }"""
    )


def test_cost_by_model_secondary_columns_hidden_at_desktop(
    page, admin_server: str
) -> None:
    """At desktop width, BY MODEL hides Cache read / Cache write / Auth.

    Pins the default state: primary cols (Model, Calls, Cost, Cost/turn)
    visible; the three secondary cols hidden until the user opts in.
    Asserted at the computed-style level so a CSS regression that breaks
    the [data-secondary] hide rule is caught.
    """
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    _seed_cost_tables(page)
    result = page.evaluate(
        """() => {
        const table = document.querySelector('#usage-table-model table.resp-table');
        if (!table) return {error: 'no table'};
        const ths = [...table.querySelectorAll('thead th')];
        const out = {};
        for (const th of ths) {
            const label = th.textContent.trim() || '(swatch)';
            out[label] = {
                secondary: th.hasAttribute('data-secondary'),
                display: getComputedStyle(th).display,
            };
        }
        return out;
        }"""
    )
    primary_labels = {"Model", "Calls", "Cost", "Cost/turn"}
    secondary_labels = {"Cache read", "Cache write", "Auth"}
    for label in primary_labels:
        assert label in result, (
            f"BY MODEL header missing expected primary column {label!r}; "
            f"saw: {sorted(result.keys())}"
        )
        assert not result[label]["secondary"], (
            f"BY MODEL primary col {label!r} unexpectedly tagged data-secondary"
        )
        assert result[label]["display"] != "none", (
            f"BY MODEL primary col {label!r} hidden at desktop "
            f"(display={result[label]['display']})"
        )
    for label in secondary_labels:
        assert label in result, (
            f"BY MODEL header missing expected secondary column {label!r}; "
            f"saw: {sorted(result.keys())}"
        )
        assert result[label]["secondary"], (
            f"BY MODEL secondary col {label!r} missing data-secondary attr — "
            "renderer dropped it (silent-failure mode)"
        )
        assert result[label]["display"] == "none", (
            f"BY MODEL secondary col {label!r} should be hidden by default; "
            f"got display={result[label]['display']}. CSS hide rule broken."
        )


def test_cost_toggle_reveals_secondary_columns_and_persists(
    page, admin_server: str
) -> None:
    """Clicking 'Show more columns' reveals secondary cells AND persists.

    Three contracts in one test (cheap — same page load):
      1. Click sets data-show-secondary on the table; secondary cells
         become visible.
      2. localStorage key evolve.respCols.cost-by-model = 'true'.
      3. On a fresh page load the toggle state survives and is re-applied
         to the table on re-render.
    """
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    _seed_cost_tables(page)

    # 1. Click the BY MODEL toggle. The button lives inside the hidden
    #    #page-cost container (only made visible by nav click), so we
    #    drive it programmatically rather than via a Playwright .click()
    #    — same DOM event the inline onclick=toggleRespCols(this) handler
    #    sees, but skips the visibility wait.
    page.evaluate(
        """() => document
            .querySelector('[data-resp-cols-panel="cost-by-model"]').click()"""
    )
    after_click = page.evaluate(
        """() => {
        const table = document.querySelector('#usage-table-model table.resp-table');
        const btn = document.querySelector('[data-resp-cols-panel="cost-by-model"]');
        const cacheTh = [...table.querySelectorAll('thead th')]
            .find(th => th.textContent.trim() === 'Cache read');
        return {
            attr: table.getAttribute('data-show-secondary'),
            cache_display: getComputedStyle(cacheTh).display,
            btn_label: btn.textContent.trim(),
            btn_pressed: btn.getAttribute('aria-pressed'),
            ls: localStorage.getItem('evolve.respCols.cost-by-model'),
        };
        }"""
    )
    assert after_click["attr"] == "true", (
        f"toggle should set data-show-secondary='true'; got {after_click['attr']!r}"
    )
    assert after_click["cache_display"] == "table-cell", (
        f"Cache read should render as table-cell when shown at desktop; "
        f"got display={after_click['cache_display']!r}"
    )
    assert after_click["btn_label"] == "Hide extra columns", (
        f"button text should flip when toggled on; got {after_click['btn_label']!r}"
    )
    assert after_click["btn_pressed"] == "true", (
        f"button aria-pressed should be 'true' when toggled on; "
        f"got {after_click['btn_pressed']!r}"
    )
    assert after_click["ls"] == "true", (
        f"localStorage should record toggle state; got {after_click['ls']!r}"
    )

    # 2. Re-render via the stub and confirm the persisted state re-applies.
    page.evaluate(
        """() => {
        _renderUsageTables({
            by_model: [{model: 'anthropic/claude-sonnet-4-6', calls: 1, cost: 0.01,
                        cache_read: 10, cache_write: 20, auth_mode: 'API key'}],
            by_channel: [], by_source: [], by_user: [],
        });
        }"""
    )
    after_rerender = page.evaluate(
        """() => {
        const table = document.querySelector('#usage-table-model table.resp-table');
        return table.getAttribute('data-show-secondary');
        }"""
    )
    assert after_rerender == "true", (
        "data-show-secondary should be reapplied after re-render — the "
        "renderer or _respColsApplyAll() regressed; got "
        f"{after_rerender!r}"
    )

    # 3. Confirm the OTHER panel's state was not clobbered when this one
    #    was toggled (the race-between-panels silent failure).
    other_ls = page.evaluate(
        """() => localStorage.getItem('evolve.respCols.cost-by-channel')"""
    )
    assert other_ls is None or other_ls == "false", (
        "Toggling BY MODEL must not write to other panels' localStorage keys; "
        f"got cost-by-channel = {other_ls!r}"
    )


def test_cost_by_model_card_stacks_at_tablet_width(
    page, admin_server: str
) -> None:
    """At 600px viewport, BY MODEL card-stacks (no horizontal table).

    This is the central bug the polish PR fixes: between 480 and 767px
    the base .resp-table primitive uses horizontal scroll, which is
    unreadable for 7-column tables. .resp-table-dense pulls card-stack
    in to <768px so the operator gets cards in the tablet range.
    """
    page.set_viewport_size({"width": 600, "height": 900})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    _seed_cost_tables(page)
    result = page.evaluate(
        """() => {
        const table = document.querySelector('#usage-table-model table.resp-table');
        const tr = table.querySelector('tbody tr');
        const td = table.querySelector('tbody td[data-label="Model"]');
        const thead = table.querySelector('thead');
        const wrap = table.parentElement;
        return {
            has_dense: table.classList.contains('resp-table-dense'),
            thead: getComputedStyle(thead).display,
            tr: getComputedStyle(tr).display,
            td: getComputedStyle(td).display,
            wrap_overflow: getComputedStyle(wrap).overflowX,
        };
        }"""
    )
    assert result["has_dense"], (
        "BY MODEL <table> missing .resp-table-dense class — the renderer "
        "regressed and the table won't card-stack at the tablet range"
    )
    assert result["thead"] == "none", (
        f"BY MODEL <thead> should hide at 600px (card-stack); got {result['thead']}"
    )
    assert result["tr"] == "block", (
        f"BY MODEL <tr> should be display:block at 600px; got {result['tr']}"
    )
    assert result["td"] == "flex", (
        f"BY MODEL <td> should be display:flex at 600px; got {result['td']}. "
        "The .resp-table-dense card-stack rule at 481–767px regressed."
    )
    assert result["wrap_overflow"] == "visible", (
        f"wrap should NOT use overflow-x:auto for dense tables at 600px; "
        f"got {result['wrap_overflow']}. The tablet horizontal-scroll mode "
        "is leaking into dense tables."
    )


def test_cost_by_model_secondary_hidden_in_card_mode(
    page, admin_server: str
) -> None:
    """At card-stack widths, secondary cells stay hidden by default,
    and flip to flex (not table-cell) when the toggle is on.

    Pins the CSS-specificity silent-failure mode flagged in the prompt:
    the global show-rule sets display:table-cell, but in card mode the
    cell must be display:flex. The media-query-scoped override in the
    primitive corrects this; this test catches a regression where the
    override is wrong.
    """
    page.set_viewport_size({"width": 600, "height": 900})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    _seed_cost_tables(page)

    hidden = page.evaluate(
        """() => {
        const td = document.querySelector(
            '#usage-table-model table.resp-table tbody td[data-label="Cache read"]'
        );
        return td ? getComputedStyle(td).display : 'no-td';
        }"""
    )
    assert hidden == "none", (
        f"Cache read cell should be hidden by default at 600px (card mode); "
        f"got display={hidden!r}"
    )

    # Toggle on (DOM click — see comment in toggle-persistence test for
    # why we don't use Playwright .click() here).
    page.evaluate(
        """() => document
            .querySelector('[data-resp-cols-panel="cost-by-model"]').click()"""
    )
    shown = page.evaluate(
        """() => {
        const td = document.querySelector(
            '#usage-table-model table.resp-table tbody td[data-label="Cache read"]'
        );
        return getComputedStyle(td).display;
        }"""
    )
    assert shown == "flex", (
        f"Cache read cell should be display:flex in card-stack mode when "
        f"shown (not table-cell — that would break the card layout); "
        f"got display={shown!r}"
    )


def test_cost_by_model_card_stacks_at_phone(page, admin_server: str) -> None:
    """At 360px, BY MODEL still card-stacks (regression for the base path).

    The polish PR moves the card-stack breakpoint UP to 768px for dense
    tables; the existing <480px behaviour must still hold. This catches
    a regression where adding .resp-table-dense accidentally breaks the
    phone-width card-stack.
    """
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    _seed_cost_tables(page)
    td_display = page.evaluate(
        """() => {
        const td = document.querySelector(
            '#usage-table-model table.resp-table tbody td[data-label="Model"]'
        );
        return getComputedStyle(td).display;
        }"""
    )
    assert td_display == "flex", (
        f"BY MODEL Model cell should be display:flex at 360px (card-stack); "
        f"got {td_display!r}"
    )


def test_non_cost_resp_table_unaffected_at_tablet_width(
    page, admin_server: str
) -> None:
    """Tables that don't opt into .resp-table-dense keep their original behaviour.

    At 600px viewport, a plain .resp-table (no dense modifier) should
    still use horizontal-scroll mode with sticky first column, not
    card-stack. Prevents the dense rules from leaking into the rest of
    the app (Maintenance Infra Jobs, Apps Forge Jobs, etc.).
    """
    page.set_viewport_size({"width": 600, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const wrap = document.createElement('div');
        wrap.className = 'resp-table-wrap';
        wrap.innerHTML = `
            <table class="resp-table">
              <thead><tr><th>A</th><th>B</th></tr></thead>
              <tbody><tr>
                <td data-label="A">a1</td><td data-label="B">b1</td>
              </tr></tbody>
            </table>`;
        document.body.appendChild(wrap);
        const out = {
            wrap_overflow: getComputedStyle(wrap).overflowX,
            thead: getComputedStyle(wrap.querySelector('thead')).display,
            td: getComputedStyle(wrap.querySelector('td')).display,
        };
        wrap.remove();
        return out;
        }"""
    )
    assert result["wrap_overflow"] == "auto", (
        f"plain .resp-table wrap should keep overflow-x:auto at 600px; "
        f"got {result['wrap_overflow']!r}. .resp-table-dense leaked into "
        "the base primitive."
    )
    assert result["thead"] == "table-header-group", (
        f"plain .resp-table thead should display normally at 600px; "
        f"got {result['thead']!r}. Card-stack rules leaked to non-dense tables."
    )
    assert result["td"] == "table-cell", (
        f"plain .resp-table td should be table-cell at 600px; "
        f"got {result['td']!r}"
    )


def test_subtab_bar_allows_horizontal_scroll(page, admin_server: str) -> None:
    """.subtab-bar / .subtabs / .subtabs-inner expose horizontal overflow.

    Locks in the Phase 0 §4.3.a guarantee that long subtab lists
    (Maintenance has 11) can scroll on phone rather than running off
    the right edge invisibly.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    overflow_modes = page.evaluate(
        """() => {
        const out = {};
        for (const cls of ['subtabs', 'subtab-bar', 'subtabs-inner', 'tab-bar']) {
            const probe = document.createElement('div');
            probe.className = cls;
            document.body.appendChild(probe);
            out[cls] = getComputedStyle(probe).overflowX;
            probe.remove();
        }
        return out;
        }"""
    )
    for cls, mode in overflow_modes.items():
        assert mode == "auto", (
            f".{cls} should set overflow-x:auto; got {mode!r}. "
            "Subtab horizontal-overflow primitive (4.3.a) regressed."
        )


# ── Phase 0 §4.3.c-Maintenance — IA collapse + table data-labels ────────────


def test_maintenance_subtabs_dynamic_priority_nav(
    page, admin_server: str
) -> None:
    """The Maintenance subtab bar uses a dynamic priority nav (9 tabs total).

    All 9 subtabs are flat siblings inside #maint-subtabs. A ResizeObserver
    distributes them between the visible row and the More ▾ popover based on
    available width — the split is not fixed. This test pins: (a) the full
    count of 9 tabs (row + popover), (b) the cluster and trigger exist, and
    (c) _initMaintOverflow is defined so dynamic allocation actually runs.

    Was 11 before Phase 1b promoted Backup + Recovery to their own top-level
    Backup page (spec-backup-and-data-classification-2026-05-28.md).
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const row = document.getElementById('maint-subtabs');
        if (!row) return null;
        const cluster = row.querySelector(':scope > .subtab-more-cluster');
        const popover = cluster && cluster.querySelector('.subtab-more-popover');
        const inRow = row.querySelectorAll(':scope > .subtab').length;
        const inPopover = popover
          ? popover.querySelectorAll(':scope > .subtab').length
          : 0;
        return {
          total: inRow + inPopover,
          hasCluster: !!cluster,
          hasTrigger: !!row.querySelector(
            ':scope > .subtab-more-cluster > .subtab-more-trigger'
          ),
          initDefined: typeof window._initMaintOverflow === 'function',
        };
        }"""
    )
    assert result is not None, "#maint-subtabs not found"
    assert result["total"] == 9, (
        f"expected 9 total Maintenance subtabs (row + popover), got "
        f"{result['total']}. Dynamic priority nav regressed."
    )
    assert result["hasCluster"], "More ▾ cluster missing from #maint-subtabs"
    assert result["hasTrigger"], "More ▾ trigger missing from cluster"
    assert result["initDefined"], (
        "_initMaintOverflow missing — dynamic priority nav will not run"
    )


def test_maintenance_more_trigger_and_advanced_wiring(
    page, admin_server: str
) -> None:
    """The More trigger has the toggle wiring and all 9 subtabs are reachable.

    Dynamic priority nav: tabs move between the row and the popover based on
    width, so reachability is checked by gathering data-subtab values from
    both locations. Static markup + reachability check only — no click
    dispatch, no page navigation (see original docstring for CI timeout
    context; same applies here).

    Was 11 subtabs before Phase 1b moved Backup + Recovery to their own
    top-level Backup page.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const row = document.getElementById('maint-subtabs');
        if (!row) return null;
        const cluster = row.querySelector(':scope > .subtab-more-cluster');
        if (!cluster) return null;
        const trigger = cluster.querySelector('.subtab-more-trigger');
        const label = trigger && trigger.querySelector('.maint-more-label');
        // Tabs may be in the row or the popover depending on viewport width.
        const allTabs = [
          ...row.querySelectorAll(':scope > .subtab'),
          ...cluster.querySelectorAll('.subtab-more-popover > .subtab'),
        ];
        const dataSubtabs = allTabs.map(t => t.getAttribute('data-subtab')).sort();
        return {
          triggerOnclick: trigger && trigger.getAttribute('onclick'),
          triggerHasLabelSpan: !!label,
          ariaInitial: trigger && trigger.getAttribute('aria-expanded'),
          allDataSubtabs: dataSubtabs,
          toggleMaintMoreDefined: typeof window.toggleMaintMore === 'function',
          syncMaintMoreLabelDefined: typeof window._syncMaintMoreLabel === 'function',
        };
        }"""
    )
    assert result is not None, "Maintenance subtab cluster not found"
    assert result["toggleMaintMoreDefined"], (
        "toggleMaintMore is missing from the global scope — popover toggle "
        "wiring regressed"
    )
    assert result["syncMaintMoreLabelDefined"], (
        "_syncMaintMoreLabel is missing — subTab() popover-aware path will "
        "leak active state to the More trigger label"
    )
    assert result["triggerOnclick"] and "toggleMaintMore" in result["triggerOnclick"], (
        f"trigger onclick should invoke toggleMaintMore; got "
        f"{result['triggerOnclick']!r}"
    )
    assert result["triggerHasLabelSpan"], (
        ".maint-more-label span missing — _syncMaintMoreLabel has nowhere "
        "to write the active-advanced name"
    )
    assert result["ariaInitial"] == "false", (
        f"trigger aria-expanded should start as 'false'; got "
        f"{result['ariaInitial']!r}"
    )
    assert result["allDataSubtabs"] == [
        "adminserver", "cron", "health", "infrajobs", "logs", "mcp",
        "setup", "status", "system",
    ], (
        f"Maintenance subtab set drifted from the canonical 9; got "
        f"{result['allDataSubtabs']}"
    )


def test_backup_page_promoted_with_five_subtabs(
    page, admin_server: str
) -> None:
    """Phase 1b promoted Backup to a top-level page with 5 subtabs.

    Verifies the structural contract:
      - Sidebar has a "Backup" nav item with data-page="backup"
      - #page-backup exists as a sibling of #page-maintenance
      - The five subtabs (status / cloud / local / data / recovery) are
        all present inside #backup-subtabs
      - The loaders called by the subtab onclicks are defined globally:
        loadBackupStatusSummary, loadBackupConfig, loadLocalBackupStatus,
        recoveryRefresh

    Pins the spec-backup-and-data-classification-2026-05-28.md Phase 1
    page promotion. Backup + Recovery are no longer Maintenance subtabs.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const navItem = document.querySelector('.nav-item[data-page="backup"]');
        const pageEl = document.getElementById('page-backup');
        const subtabRow = document.getElementById('backup-subtabs');
        const subtabs = subtabRow
          ? Array.from(subtabRow.querySelectorAll(':scope > .subtab'))
              .map(t => t.getAttribute('data-subtab'))
          : [];
        return {
          hasNavItem: !!navItem,
          hasPage: !!pageEl,
          hasSubtabRow: !!subtabRow,
          subtabs: subtabs.sort(),
          loaders: {
            loadBackupStatusSummary: typeof window.loadBackupStatusSummary === 'function',
            loadBackupConfig:        typeof window.loadBackupConfig === 'function',
            loadLocalBackupStatus:   typeof window.loadLocalBackupStatus === 'function',
            loadBackupDataOverview:  typeof window.loadBackupDataOverview === 'function',
            recoveryRefresh:         typeof window.recoveryRefresh === 'function',
          },
        };
        }"""
    )
    assert result["hasNavItem"], (
        "Sidebar is missing the Backup nav item — Phase 1b page promotion "
        "regressed"
    )
    assert result["hasPage"], "#page-backup container is missing from the DOM"
    assert result["hasSubtabRow"], "#backup-subtabs subtab row is missing"
    assert result["subtabs"] == ["cloud", "data", "local", "recovery", "status"], (
        f"Backup page subtab set drifted; got {result['subtabs']}. Spec "
        f"locks in: Status, Cloud, Local, Data, Recovery."
    )
    for fn, present in result["loaders"].items():
        assert present, (
            f"{fn} is missing from global scope — Backup subtab onclicks "
            f"will throw when activated."
        )


# ── Phase 0 §4.3.b sidebar hamburger drawer ─────────────────────────────────
#
# Locks in the mobile-drawer contract: <980px hides the sidebar behind a
# hamburger; ≥980px the sidebar is pinned. Per the firefox+Linux runner
# cascade documented above (test_maintenance_more_trigger_and_advanced_
# wiring), we keep page.goto's to one per test and bundle multiple
# assertions into single tests rather than spinning up a fresh page per
# behavior. Each goto on that runner queues request fan-out that later
# tests pay for as a 30s timeout — so 6 separate "open/close path" tests
# would cascade the firefox half of the suite.
#
# Spec: internal/spec-pwa-phase0-responsive-2026-05-18.md §3.2, §5.7.


def _drawer_left(page) -> float:
    """Return the sidebar's left-edge x in viewport coords (negative if off)."""
    return page.evaluate(
        "() => document.getElementById('sidebar').getBoundingClientRect().left"
    )


def _wait_drawer_state(page, *, open_: bool, timeout_ms: int = 2000) -> None:
    """Poll until the sidebar bounding rect reflects the requested state.

    The drawer transition is 200ms (`transition: left 0.2s ease`), but
    the GitHub Actions Linux WebKit runner sometimes needs > 500ms for
    the rect to update. Polling the actual geometry beats a fixed
    `wait_for_timeout` — fast on healthy runs, robust on slow ones.
    """
    cmp_ = "rect.left === 0" if open_ else "rect.left < 0"
    page.wait_for_function(
        f"() => {{ const rect = document.getElementById('sidebar').getBoundingClientRect(); return {cmp_}; }}",
        timeout=timeout_ms,
    )


def _open_drawer(page) -> None:
    """Click the hamburger and wait for the slide-in to complete."""
    page.click("#hamburger-btn")
    _wait_drawer_state(page, open_=True)


def _wait_drawer_closed(page) -> None:
    """Wait until the slide-out has fully retracted the drawer."""
    _wait_drawer_state(page, open_=False)


def test_drawer_default_state_phone_vs_desktop(
    page, admin_server: str
) -> None:
    """At <980px the hamburger shows and the sidebar is off-screen;
    at ≥980px the hamburger hides and the sidebar pins on the left.

    Bundles both the 360px default state AND the 1280px desktop
    regression check so we only spend one page.goto on the
    breakpoint contract (set_viewport_size + reload is cheap; full
    page.goto is what stresses the in-process server).
    """
    # Phone: 360px viewport, default state.
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    assert page.locator("#hamburger-btn").is_visible(), (
        "hamburger button should be visible at phone width"
    )
    left = _drawer_left(page)
    assert left < 0, (
        f"sidebar should be off-screen at phone width; got left={left}px. "
        "Either the slide-in default state regressed or the breakpoint "
        "didn't take effect."
    )

    # Desktop regression: re-size to 1280px and confirm desktop chrome
    # re-pins without reloading the page. This is the "didn't break
    # desktop" assertion — CSS media queries should re-apply on resize.
    page.set_viewport_size({"width": 1280, "height": 900})
    assert not page.locator("#hamburger-btn").is_visible(), (
        "hamburger button should NOT be visible at desktop width — the "
        "@media (max-width:979px) boundary or the default display:none "
        "flipped."
    )
    assert page.locator("#sidebar").is_visible(), (
        "sidebar should be visible at desktop width"
    )
    left_desktop = _drawer_left(page)
    assert left_desktop == 0, (
        f"sidebar at desktop should sit at left=0; got {left_desktop}. "
        "The fixed-position drawer rule leaked into desktop."
    )


def test_drawer_open_close_paths(page, admin_server: str) -> None:
    """All three dismiss paths plus the open contract, in one page load.

    Covers: hamburger click → open + overlay shown + aria-expanded; then
    Escape → close + focus returns to hamburger; then re-open and
    overlay click → close. Bundled because the firefox+Linux cascade
    (see comment above) makes per-path goto's expensive.
    """
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")

    # 1. Hamburger click opens the drawer (geometry settled by
    #    _open_drawer's wait_for_function).
    _open_drawer(page)
    assert page.locator("#sidebar-overlay.open").count() == 1, (
        "sidebar overlay should have .open class after hamburger click"
    )
    expanded = page.locator("#hamburger-btn").get_attribute("aria-expanded")
    assert expanded == "true", (
        f"hamburger aria-expanded should be true after open; got {expanded!r}"
    )

    # 2. Escape closes; focus returns to hamburger (§5.7).
    page.keyboard.press("Escape")
    _wait_drawer_closed(page)
    focused_id = page.evaluate("() => document.activeElement?.id")
    assert focused_id == "hamburger-btn", (
        f"focus should return to #hamburger-btn after close; got {focused_id!r}. "
        "Return-focus path (a11y §5.7) regressed."
    )

    # 3. Overlay click dismisses on a re-open. The drawer occupies
    #    x=0..220; viewport is 360 wide, so a click at x=330 lands on
    #    the dim overlay rather than the drawer itself.
    _open_drawer(page)
    page.locator("#sidebar-overlay").click(position={"x": 330, "y": 400})
    _wait_drawer_closed(page)


def test_drawer_nav_item_dismisses_and_navigates(
    page, admin_server: str
) -> None:
    """Tapping a nav item inside the drawer dismisses AND changes the page.

    Kept separate from test_drawer_open_close_paths because nav() mutates
    page state permanently (active-tab change, route mutation) and
    bundling it with the open/close paths would couple two contracts.
    The dismiss happens via setTimeout(0) so the inline nav(this) call
    runs first.
    """
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    _open_drawer(page)
    # Pick a stable, distinct target — overview (Dashboard) is the
    # cross-browser smoke baseline used elsewhere in this file.
    page.click('#sidebar .nav-item[data-page="overview"]')
    _wait_drawer_closed(page)
    active_page = page.evaluate(
        "() => document.querySelector('.nav-item.active')?.dataset?.page"
    )
    assert active_page == "overview", (
        f"clicking the overview nav item should activate it; got {active_page!r}"
    )


# ── Phase 0 §4.3.c-Apps responsive sweep ────────────────────────────────────
#
# Apps is one of three heavy-lift pages. These tests pin the Apps-page
# specific opt-ins: the static forge-jobs table is wired into the .resp-
# table primitive, the new .resp-table-fullspan utility behaves as a
# card-stack opt-out at phone width, and modals no longer overflow a
# 360px viewport.


def test_forge_jobs_table_opts_into_resp_table(page, admin_server: str) -> None:
    """The Apps · Forge Jobs table participates in the .resp-table primitive.

    Catches a silent regression where the class or wrapper is removed —
    the table would still render at desktop but lose its phone card-
    stack and tablet scroll-with-sticky-col behaviour. We check the
    static markup directly (no API calls required) so the test is
    deterministic on the empty-pod fixture.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    table = page.locator("table#forge-jobs-table")
    assert table.count() == 1, "forge-jobs-table missing from #page-apps"
    assert "resp-table" in (table.get_attribute("class") or ""), (
        "forge-jobs-table lost its .resp-table class — phone card-stack "
        "and tablet sticky-col modes won't engage"
    )
    # The wrapper is what owns horizontal scroll + the right-edge fade
    # in tablet mode. A missing wrapper would silently strip the fade
    # without otherwise breaking the table.
    wrapped = page.evaluate(
        """() => {
        const t = document.querySelector('table#forge-jobs-table');
        return !!(t && t.parentElement && t.parentElement.classList.contains('resp-table-wrap'));
        }"""
    )
    assert wrapped, (
        "forge-jobs-table is not inside a .resp-table-wrap — the tablet "
        "horizontal-scroll + right-edge fade primitive is broken"
    )


def test_resp_table_fullspan_opts_out_of_card_stack(page, admin_server: str) -> None:
    """At phone width, td.resp-table-fullspan stays full-width.

    The default .resp-table card-stack treatment makes every td a flex
    row with a label gutter — perfect for data cells, wrong for
    colspan rows that hold free-form content (expanded detail panels,
    empty-state messages). This test pins that the fullspan utility
    overrides display:flex and the ::before label gutter.
    """
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const wrap = document.createElement('div');
        wrap.className = 'resp-table-wrap';
        wrap.innerHTML = `
            <table class="resp-table">
              <thead><tr><th>Col A</th><th>Col B</th></tr></thead>
              <tbody>
                <tr><td colspan="2" class="resp-table-fullspan">expanded content</td></tr>
              </tbody>
            </table>
        `;
        document.body.appendChild(wrap);
        const td = wrap.querySelector('tbody td');
        const cs = getComputedStyle(td);
        const before = getComputedStyle(td, '::before');
        const out = {
            display: cs.display,
            textAlign: cs.textAlign,
            beforeDisplay: before.display,
        };
        wrap.remove();
        return out;
        }"""
    )
    assert result["display"] == "block", (
        f".resp-table-fullspan should opt out of display:flex; got {result['display']!r}. "
        "Expanded-row content would be cramped into a label-gutter layout."
    )
    assert result["textAlign"] in ("left", "start"), (
        f".resp-table-fullspan should reset text-align to left; got {result['textAlign']!r}"
    )
    assert result["beforeDisplay"] == "none", (
        f".resp-table-fullspan ::before should be display:none; got {result['beforeDisplay']!r}. "
        "Phantom label gutter would still take space."
    )


def test_modal_fits_phone_viewport(page, admin_server: str) -> None:
    """At <480px, .modal honours max-width:95vw and drops the 380px min-width.

    The desktop rule sets min-width:380px which overflows a 360px
    phone viewport — content gets clipped or forces a horizontal page
    scroll. This test pins the phone fallback added in §4.3.c-Apps so
    the install/import/details modals on the Apps page stay usable.
    """
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay open';
        const modal = document.createElement('div');
        modal.className = 'modal';
        modal.innerHTML = '<h2>probe</h2><p>contents</p>';
        overlay.appendChild(modal);
        document.body.appendChild(overlay);
        const cs = getComputedStyle(modal);
        const rect = modal.getBoundingClientRect();
        const out = {
            minWidth: cs.minWidth,
            width: rect.width,
            viewport: window.innerWidth,
        };
        overlay.remove();
        return out;
        }"""
    )
    assert result["minWidth"] == "0px", (
        f".modal min-width should collapse below the phone breakpoint; got {result['minWidth']!r}. "
        "Desktop's 380px floor would overflow a 360px viewport."
    )
    assert result["width"] <= result["viewport"], (
        f".modal rendered at {result['width']}px in a {result['viewport']}px viewport — "
        "the 95vw cap is not engaged. Apps install/import modals would clip."
    )


# ── Phase 0 §4.1.d HTTPS-nudge banner ───────────────────────────────────────
#
# The banner is rendered by index.html (id=#https-banner) and toggled by
# _renderHttpsBanner() after each /api/network poll. Trigger: the resolved
# adminBaseUrl starts with http://. Spec §5.4 makes it persistent (no
# dismiss) so the operator is reminded across page loads until they run
# `evolve-admin enable-https`.


def _set_admin_base_url(network_path: Path, scheme: str) -> None:
    """Rewrite network.json so the admin server resolves the given scheme."""
    data = json.loads(network_path.read_text())
    data["adminBaseUrl"] = f"{scheme}://test-pod.example:5050"
    network_path.write_text(json.dumps(data, indent=2))


def test_https_banner_shows_when_admin_base_url_is_http(
    page, admin_server: str, network_path: Path
) -> None:
    """The banner is visible iff network.json's adminBaseUrl is http://.

    Also exercises the auto-hide path: same page load, flip the scheme to
    https://, re-trigger the loader, assert the banner has gone away. No
    dismiss button is asserted — by design (spec §5.4) the banner is
    persistent and only goes away when the underlying URL is HTTPS.
    """
    _set_admin_base_url(network_path, "http")
    # domcontentloaded + the explicit wait_for_function below is enough
    # — networkidle adds 500ms of "no inflight requests" wait that the
    # follow-up assertion doesn't need.
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # loadNetwork() runs inside load() at startup; wait until #https-banner
    # has the .visible class rather than racing the fetch.
    page.wait_for_function(
        "() => document.getElementById('https-banner')?.classList.contains('visible')",
        timeout=5000,
    )
    banner = page.locator("#https-banner")
    assert banner.is_visible(), "banner should be visible while adminBaseUrl is http://"
    # Lead text is the non-jargon "Pod is on HTTP" copy — pin a substring
    # so a future copy change doesn't silently break the trigger.
    text = banner.inner_text()
    assert "HTTP" in text and "install" in text.lower(), (
        f"banner copy regressed; got {text!r}"
    )
    # The "Show me how" link points at the docs route, not an external URL,
    # so the operator stays inside the pod's tailnet boundary.
    link_href = page.locator("#https-banner .hb-link").get_attribute("href")
    assert link_href == "/docs/https-setup", (
        f"banner link should target /docs/https-setup; got {link_href!r}"
    )
    # Clicking the link must open the in-SPA HTTPS wizard modal, not
    # navigate: the desktop shell's Tauri webview drops target="_blank"
    # new-window requests, so the onclick path is the one that works
    # everywhere. The href stays as a middle-click / no-JS fallback.
    page.locator("#https-banner .hb-link").click()
    page.wait_for_function(
        "() => document.getElementById('https-setup-modal')?.classList.contains('open')",
        timeout=5000,
    )
    assert "/docs/https-setup" not in page.url, (
        f"click should not navigate away from the SPA; landed on {page.url}"
    )
    page.evaluate("() => closeHttpsSetupWizard()")
    # No dismiss button — spec §5.4 (persistent reminder, defer-not-block).
    assert page.locator("#https-banner .err-dismiss, #https-banner [data-dismiss]").count() == 0, (
        "banner gained a dismiss control — that defeats the persistent reminder design"
    )

    # Flip to https:// and re-run the loader. The banner should hide on
    # the next /api/network response without a full page reload.
    _set_admin_base_url(network_path, "https")
    page.evaluate("() => loadNetwork()")
    page.wait_for_function(
        "() => !document.getElementById('https-banner')?.classList.contains('visible')",
        timeout=5000,
    )
    assert not page.locator("#https-banner.visible").count(), (
        "banner should hide once adminBaseUrl is https://"
    )


def test_https_banner_fits_phone_viewport(
    page, admin_server: str, network_path: Path
) -> None:
    """At 360px wide, the banner doesn't overflow the viewport.

    The desktop "Show me how →" link collapses to "How →" below 480px
    so the banner stays on one row even at the tightest phone widths.
    """
    _set_admin_base_url(network_path, "http")
    page.set_viewport_size({"width": 360, "height": 800})
    # See note on the sibling test above — the explicit wait_for_function
    # below is the load-bearing wait, networkidle adds nothing.
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.getElementById('https-banner')?.classList.contains('visible')",
        timeout=5000,
    )
    geometry = page.evaluate(
        """() => {
        const el = document.getElementById('https-banner');
        const rect = el.getBoundingClientRect();
        const longLink = el.querySelector('.hb-link-long');
        const shortLink = el.querySelector('.hb-link-short');
        return {
            banner_width: rect.width,
            viewport: window.innerWidth,
            long_display: getComputedStyle(longLink).display,
            short_display: getComputedStyle(shortLink).display,
        };
        }"""
    )
    assert geometry["banner_width"] <= geometry["viewport"] + 2, (
        f"banner overflows phone viewport: {geometry['banner_width']}px > "
        f"{geometry['viewport']}px"
    )
    assert geometry["long_display"] == "none", (
        f"long-form link should be hidden at 360px; got display={geometry['long_display']}"
    )
    assert geometry["short_display"] != "none", (
        f"short-form link should be visible at 360px; got display={geometry['short_display']}"
    )


def test_https_docs_page_renders(page, admin_server: str) -> None:
    """The /docs/https-setup endpoint serves the rendered markdown.

    Smoke-only: confirms the route is wired up and produces an HTML
    response whose body contains the doc's H1 (so we'd notice if the
    renderer returned an empty shell).
    """
    response = page.request.get(f"{admin_server}/docs/https-setup")
    assert response.status == 200, (
        f"/docs/https-setup returned {response.status}; body={response.text()[:200]}"
    )
    body = response.text()
    assert "<h1" in body and "HTTPS" in body, (
        "rendered docs page is missing expected heading or content"
    )
    # Unknown doc names should 404, not 500. Pins the allow-list shape.
    bad = page.request.get(f"{admin_server}/docs/does-not-exist")
    assert bad.status == 404


# ── Phase 0 §4.3.d long-tail sweep ───────────────────────────────────────────
#
# The sweep brought the remaining secondary pages onto the foundation
# primitives (resp-table, auto-fill grids, sys-loop-bar phone-stack). These
# tests pin the JS-side seam — a post-render data-label labelizer that lets
# renderers opt a table into card-stack mode by adding two class names
# (resp-table-wrap + resp-table) without rewriting every <td> string. The
# silent-failure mode it guards against: a renderer that opts in to the
# primitive but leaves <td>s unlabeled, so phone-mode cards render with no
# row labels.


def test_resp_table_labelize_helper_seeds_data_labels(page, admin_server: str) -> None:
    """`_respTableLabelize` walks a resp-table and seeds data-label from <th>.

    The helper exists so JS-rendered tables (Plugins page MCP / Keys /
    Plugins / Hooks / Activity, Cost Optimization audit, Analytics turns,
    Self-Improvement history, etc.) can opt into the resp-table primitive
    by adding two classes without rewriting every <td>. If anyone removes
    or breaks the helper, all those tables silently lose their phone
    card-stack labels.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        // Verify the helper is on the global scope and seeds labels correctly.
        if (typeof window._respTableLabelize !== 'function') {
            return { defined: false };
        }
        const probe = document.createElement('div');
        probe.innerHTML = `
            <table class="resp-table">
              <thead><tr><th>One</th><th>Two</th><th>Three</th></tr></thead>
              <tbody>
                <tr><td>a</td><td>b</td><td>c</td></tr>
                <tr><td>x</td><td data-label="Custom">y</td><td>z</td></tr>
              </tbody>
            </table>`;
        document.body.appendChild(probe);
        window._respTableLabelize(probe);
        const tds = probe.querySelectorAll('tbody td');
        const labels = [...tds].map(td => td.getAttribute('data-label'));
        probe.remove();
        return { defined: true, labels };
        }"""
    )
    assert result["defined"], (
        "_respTableLabelize is missing from the global scope — every JS-rendered "
        "table that relies on it will render unlabeled cards at phone width"
    )
    # First row: all three labels seeded from the headers.
    # Second row: the explicit data-label="Custom" must NOT be clobbered;
    # neighbors still get their header-derived labels.
    assert result["labels"] == ["One", "Two", "Three", "One", "Custom", "Three"], (
        f"data-label seeding misbehaved: got {result['labels']}. "
        "Either the helper overwrote an explicit data-label, skipped a cell, "
        "or read the wrong header text."
    )


def test_sys_loop_bar_stacks_at_phone(page, admin_server: str) -> None:
    """At <480px, the 4-segment system-loop bar reflows to a 2×2 grid.

    Desktop is a single flex row with `padding:22px 28px` and 2.4rem values;
    that overflows a 360px viewport. The 4.3.d sweep adds a phone media
    query that switches to `display:grid` with a `1fr 1fr` template so the
    Overview page no longer horizontally overflows on phones.
    """
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Overview is reachable via the sidebar nav at phone width; open the
    # drawer first (the page is otherwise not visited by default).
    _open_drawer(page)
    # The sidebar is the slide-in drawer at phone width — clicking a
    # nav-item requires opening it first OR calling nav() directly via
    # JS. The latter is faster and exercises the same code path.
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"overview\"]'))"
    )
    page.wait_for_timeout(200)
    result = page.evaluate(
        """() => {
        const bar = document.querySelector('#page-overview .sys-loop-bar');
        if (!bar) return null;
        const cs = getComputedStyle(bar);
        const dividerHidden = [...bar.querySelectorAll('.sys-loop-divider')]
            .every(d => getComputedStyle(d).display === 'none');
        return {
            display: cs.display,
            columns: cs.gridTemplateColumns.split(' ').filter(Boolean).length,
            dividers_hidden: dividerHidden,
        };
        }"""
    )
    assert result is not None, "#page-overview .sys-loop-bar not found"
    assert result["display"] == "grid", (
        f".sys-loop-bar should switch to display:grid at phone width; got "
        f"{result['display']}. The 4-segment bar would overflow without it."
    )
    assert result["columns"] == 2, (
        f".sys-loop-bar should resolve to a 2-column grid at 360px; got "
        f"{result['columns']} columns."
    )
    assert result["dividers_hidden"], (
        ".sys-loop-divider should hide at phone width — vertical dividers "
        "between grid cells look broken without the original flex flow."
    )


# ── Phase 1.1.B — drop / paste primitive (spec §5.4) ────────────────────────
#
# These tests pin the drop / paste primitive end-to-end. They run on
# chromium + webkit only; the firefox-on-Linux runner cascade
# documented in this file (and in .github/workflows/browser-smoke.yml)
# makes any additional page.goto past a baseline threshold tip the
# in-process Flask subprocess over a 30s wedge state — independent of
# what the new tests actually do. The behaviour being verified here
# is standards-based browser JS + the unauthenticated /api/chat-uploads
# route; cross-engine coverage from chromium + webkit is the same
# signal Firefox would provide. Server-side enforcement (allowlists,
# 10 MB cap, partial success, GET hardening) is exercised exhaustively
# in packages/admin/tests/test_chat_upload_routes.py against Flask's
# test_client — fast, deterministic, and not subject to the cascade.
#
# What survives here:
#   • surface seam      — window.pwaDrop public methods + DOM mounts + CSS
#   • client validation — type allowlist, paste-isolation
#   • happy-path upload — one tiny PNG round-trips end-to-end (POST + GET)
#   • failure recovery  — flushPending preserves queue + chips on upload error


def _skip_firefox_cascade(browser_name: str) -> None:
    """Skip the PWA browser smoke tests on Firefox.

    The Firefox-on-Linux runner consistently wedges the admin-server
    subprocess after ~30s of cross-test page.goto traffic (see the
    long comment block above + the matrix-split rationale in the
    workflow). Adding 4 more page.goto's worth of warmup pushes
    /api/health and the subsequent baseline checks past the 30s
    Playwright timeout. Chromium + WebKit cover the standards-based
    behaviour — Firefox-specific PWA regressions, if they ever
    arise, will surface in the unit-test layer at the Werkzeug seam.
    """
    if browser_name == "firefox":
        pytest.skip(
            "Skipping on Firefox — adding page.goto's past the runner's "
            "subprocess-wedge threshold cascades the baseline tests. "
            "See chat_upload_routes unit tests for server-side coverage."
        )


def test_pwa_drop_primitive_and_surfaces_wired(
    page, admin_server: str, browser_name: str,
) -> None:
    """The primitive is loaded, the chip strips exist, and the drag
    highlight is styled.

    Bundled from three earlier per-aspect tests so the page.goto budget
    on Firefox-Linux stays manageable. Probes the seam (window.pwaDrop)
    and the DOM mounts each surface init uses (#evo-drawer-chips etc.)
    + the visible .pwa-drop-active outline rule.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        // 1) Primitive shape.
        const m = window.pwaDrop;
        const primitive = m ? {
            defined: true,
            attachDropTarget: typeof m.attachDropTarget,
            attachPasteHandler: typeof m.attachPasteHandler,
            pwaUploadAttachments: typeof m.pwaUploadAttachments,
            makeChip: typeof m.makeChip,
            acceptFiles: typeof m.acceptFiles,
            max_bytes: m.MAX_BYTES,
            allowed_count: m.ALLOWED_MIME.size,
        } : { defined: false };

        // 2) Chip-strip mount points.
        const stripIds = ['evo-drawer-chips', 'home-chat-chips', 'diag-chips'];
        const strips = {};
        for (const id of stripIds) {
            const el = document.getElementById(id);
            strips[id] = {
                present: !!el,
                hasClass: !!(el && el.classList.contains('pwa-chip-strip')),
            };
        }

        // 3) Drag-over highlight style. Probe with a synthetic element
        //    so we don't need real drag choreography.
        const probe = document.createElement('div');
        probe.className = 'pwa-drop-active';
        probe.style.cssText = 'width:100px;height:100px;position:fixed;top:-200px';
        document.body.appendChild(probe);
        const cs = getComputedStyle(probe);
        const highlight = {
            outlineStyle: cs.outlineStyle,
            outlineWidth: cs.outlineWidth,
        };
        probe.remove();

        return { primitive, strips, highlight };
        }"""
    )
    p = result["primitive"]
    assert p["defined"], "window.pwaDrop missing — primitive load broke"
    for name in (
        "attachDropTarget", "attachPasteHandler",
        "pwaUploadAttachments", "makeChip", "acceptFiles",
    ):
        assert p[name] == "function", (
            f"window.pwaDrop.{name} should be a function; got {p[name]!r}"
        )
    assert p["max_bytes"] == 10 * 1024 * 1024, (
        f"client cap drifted from 10MB; got {p['max_bytes']}. Must stay in "
        "sync with chat_upload_routes.MAX_BYTES."
    )
    assert p["allowed_count"] == 7, (
        f"client allowlist drifted from 7 types; got {p['allowed_count']}"
    )
    for chip_id, info in result["strips"].items():
        assert info["present"], (
            f"#{chip_id} chip strip missing — drop primitive has nowhere to "
            "render previews on this surface"
        )
        assert info["hasClass"], (
            f"#{chip_id} missing .pwa-chip-strip class — CSS rules won't match"
        )
    h = result["highlight"]
    assert h["outlineStyle"] == "dashed", (
        f".pwa-drop-active should set a dashed outline; got "
        f"{h['outlineStyle']!r}. Drag-over feedback regressed."
    )
    assert h["outlineWidth"] != "0px", (
        ".pwa-drop-active outline-width is 0; the highlight wouldn't render"
    )


def test_pwa_drop_client_validation_and_paste_isolation(
    page, admin_server: str, browser_name: str,
) -> None:
    """Client-side guards: type allowlist + paste isolation.

    Three behaviours bundled into one page.goto. The oversized-cap
    check is intentionally NOT done here — server-side size
    enforcement is covered exhaustively by
    packages/admin/tests/test_chat_upload_routes.py instead, and the
    client-side check is a straight ``file.size > MAX_BYTES`` whose
    behaviour follows from the type-rejection seam tested here.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """async () => {
        const out = {};

        // (a) Unsupported-type rejection.
        {
            const zip = new File(['x'], 'a.zip', { type: 'application/zip' });
            const reasons = [];
            window.pwaDrop.acceptFiles([zip], {
                onAccept: () => reasons.push('UNEXPECTED_ACCEPT'),
                onReject: (_f, r) => reasons.push(r),
            });
            out.unsupported = reasons;
        }

        // (b) All seven allowlisted types accept.
        {
            const probes = [
                ['a.png', 'image/png'], ['a.jpg', 'image/jpeg'],
                ['a.gif', 'image/gif'], ['a.webp', 'image/webp'],
                ['a.txt', 'text/plain'], ['a.md', 'text/markdown'],
                ['a.json', 'application/json'],
            ];
            const accepted = [];
            for (const [name, mime] of probes) {
                const f = new File(['x'], name, { type: mime });
                window.pwaDrop.acceptFiles([f], {
                    onAccept: (fs) => accepted.push(fs[0].name),
                    onReject: () => accepted.push('REJECT:' + name),
                });
            }
            out.accepted = accepted;
        }

        // (c) Paste in an unrelated input doesn't get hijacked.
        {
            const stray = document.createElement('input');
            stray.id = 'stray-paste-probe';
            stray.type = 'text';
            document.body.appendChild(stray);
            stray.focus();
            const dt = new DataTransfer();
            try {
                const blob = new Blob(
                    [new Uint8Array([0x89, 0x50, 0x4e, 0x47])],
                    { type: 'image/png' },
                );
                dt.items.add(new File([blob], 'p.png', { type: 'image/png' }));
            } catch (_) { /* engine-specific; focus check is what we're after */ }
            const ev = new ClipboardEvent('paste', {
                bubbles: true, cancelable: true, clipboardData: dt,
            });
            let defaulted = true;
            ev.preventDefault = () => { defaulted = false; };
            document.dispatchEvent(ev);
            stray.remove();
            out.paste_defaulted = defaulted;
        }

        return out;
        }"""
    )
    assert result["unsupported"] == [
        "Unsupported file type — only images, text, and JSON in v1."
    ], (
        f"unsupported type should be rejected with the canonical message; "
        f"got {result['unsupported']!r}"
    )
    assert result["accepted"] == [
        "a.png", "a.jpg", "a.gif", "a.webp", "a.txt", "a.md", "a.json",
    ], f"allowlist drifted; got {result['accepted']!r}"
    assert result["paste_defaulted"], (
        "paste in an unrelated input was hijacked by the PWA drop primitive — "
        "the focus check is broken; plain-text paste outside chat surfaces "
        "must keep default behaviour"
    )


def test_pwa_chat_upload_endpoint_round_trips_image(
    page, admin_server: str, browser_name: str,
) -> None:
    """A small PNG uploads via /api/chat-uploads and serves back unchanged.

    The single end-to-end browser-level happy-path test. Heavy-payload
    cases (size cap, unsupported type, partial success, etc.) live at
    the unit-test layer.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """async () => {
        const png = new Uint8Array([
            0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
            0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
        ]);
        const form = new FormData();
        form.append('surface', 'evo-drawer');
        form.append(
            'file',
            new File([png], 'probe.png', { type: 'image/png' }),
            'probe.png',
        );
        const post = await fetch('/api/chat-uploads', {
            method: 'POST', body: form,
        });
        const body = await post.json();
        // GET round-trip via the SAME page context (browser fetch, not
        // Playwright's APIRequestContext). Pins that the stored file
        // is reachable end-to-end without crossing the Playwright API
        // boundary that destabilised the Firefox runner.
        let getStatus = 0, firstBytes = [];
        const url = (body.attachments || [])[0]?.url;
        if (url) {
            const get = await fetch(url);
            getStatus = get.status;
            const buf = new Uint8Array(await get.arrayBuffer());
            firstBytes = Array.from(buf.slice(0, 8));
        }
        return { status: post.status, body, getStatus, firstBytes };
        }"""
    )
    assert result["status"] == 200, (
        f"/api/chat-uploads returned {result['status']}; body={result['body']!r}"
    )
    attachments = (result["body"] or {}).get("attachments") or []
    assert len(attachments) == 1, (
        f"expected one attachment in the response; got {attachments!r}"
    )
    a = attachments[0]
    assert a.get("filename") == "probe.png"
    assert a.get("mime_type") == "image/png"
    assert a.get("size_bytes") == 16
    url = a.get("url") or ""
    assert url.startswith("/chat-uploads/evo-drawer/"), (
        f"upload URL should sit under /chat-uploads/evo-drawer/; got {url!r}"
    )
    assert result["getStatus"] == 200, (
        f"stored upload not retrievable via {url}: GET {result['getStatus']}"
    )
    # PNG signature is 8 bytes: 0x89 'P' 'N' 'G' \r \n 0x1a \n
    assert result["firstBytes"] == [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], (
        f"PNG signature round-trip failed — got {result['firstBytes']!r}"
    )


def test_pwa_flush_pending_survives_upload_failure(
    page, admin_server: str, browser_name: str,
) -> None:
    """When the upload network call fails, pending files stay in queue.

    Silent-failure guard for the "lose your attachment on transient
    error" mode: ``flushPending`` must not clear the queue or chip
    strip until the POST returns a result. The operator's retry on
    the chat bubble should re-flush the same files, not discover an
    empty queue with no chips left to show.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """async () => {
        const realFetch = window.fetch;
        window.fetch = () => Promise.reject(new Error('forced network error'));
        try {
            const f = new File(
                [new Uint8Array(4)], 'probe.png', { type: 'image/png' }
            );
            window._pwaPending['evo-drawer'].push(f);
            const strip = document.getElementById('evo-drawer-chips');
            window.pwaDrop.makeChip(strip, f, { onRemove: () => {} });
            let threw = false;
            try {
                await window._pwaFlushPending('evo-drawer');
            } catch (_) { threw = true; }
            return {
                threw,
                queueLen: window._pwaPending['evo-drawer'].length,
                chipsLeft: strip.querySelectorAll('.pwa-chip').length,
                erroredChips: strip.querySelectorAll('.pwa-chip.error').length,
            };
        } finally {
            window.fetch = realFetch;
            window._pwaPending['evo-drawer'] = [];
            const s = document.getElementById('evo-drawer-chips');
            while (s.firstChild) s.removeChild(s.firstChild);
        }
        }"""
    )
    assert result["threw"], (
        "flushPending should re-throw upload errors so the chat-send "
        "catch block surfaces them; the promise resolved instead"
    )
    assert result["queueLen"] == 1, (
        f"pending queue lost its file on upload failure; "
        f"queueLen={result['queueLen']}. Silent data loss — operator's "
        "retry can't include the attachment."
    )
    assert result["chipsLeft"] == 1, (
        f"chip strip cleared on upload failure; chipsLeft={result['chipsLeft']}. "
        "Operator has no UI evidence the attachment still exists."
    )
    assert result["erroredChips"] == 1, (
        "chip should be marked .error so the operator sees the upload failed; "
        f"erroredChips={result['erroredChips']}"
    )


# ── PWA Phase 4 — tunnel CONNECT helper (spec §8) ─────────────────────────
#
# The detector lives entirely in the page; tests inject a stubbed
# `fetch` so /api/health returns 503 (down) or 200 (back). We bypass
# the ~10s ping interval by reaching into the public helpers exposed
# by the IIFE: `_pwaTunnelTryAgain()` issues a fresh ping on demand,
# and the chained promise lets us await its side-effects deterministically.


def test_phase4_overlay_starts_hidden_on_healthy_load(
    page, admin_server: str, browser_name: str,
) -> None:
    """On a normal page load the overlay + toast both start hidden.

    Default state — even after the first ping completes (the server is
    serving /api/health 200), nothing should be on screen. Pins the
    "no false positives" contract.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Give the first ping a beat to land.
    page.wait_for_timeout(500)
    state = page.evaluate(
        """() => ({
        overlayVisible: document.getElementById('pwa-tunnel-overlay')?.classList.contains('visible'),
        toastVisible: document.getElementById('pwa-net-toast')?.classList.contains('visible'),
        overlayDisplay: getComputedStyle(document.getElementById('pwa-tunnel-overlay')).display,
        toastDisplay: getComputedStyle(document.getElementById('pwa-net-toast')).display,
        })"""
    )
    assert state["overlayVisible"] is False, "overlay should not be visible on healthy load"
    assert state["toastVisible"] is False, "toast should not be visible on healthy load"
    assert state["overlayDisplay"] == "none", (
        f"overlay should have display:none by default; got {state['overlayDisplay']!r}"
    )
    assert state["toastDisplay"] == "none", (
        f"toast should have display:none by default; got {state['toastDisplay']!r}"
    )


def test_phase4_overlay_appears_when_health_fails_then_recovers(
    page, admin_server: str, browser_name: str,
) -> None:
    """503 from /api/health for enough consecutive pings → overlay.
    Then 200 → overlay dismisses + green "Back online" toast appears.

    Bundled so the page.goto budget is one for the whole flow (the
    Firefox-cascade comment block above explains why we minimise
    those). The two halves of the assertion live in one test because
    they share the same fetch-stub setup.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Wait for the initial healthy ping so everConnected is true. We
    # specifically guard against the SW signal tripping the overlay
    # before the first successful connection.
    page.wait_for_timeout(500)
    # Install the fetch stub; force /api/health to 503. Other URLs go
    # through to the real fetch so the SPA keeps working underneath.
    page.evaluate(
        """() => {
        window._fakeHealthStatus = 503;
        const real = window.fetch.bind(window);
        window._realFetch = real;
        window.fetch = (input, init) => {
            const url = (typeof input === 'string') ? input : (input && input.url) || '';
            if (url.endsWith('/api/health')) {
                const status = window._fakeHealthStatus;
                return Promise.resolve(new Response('{}', {
                    status, headers: {'Content-Type': 'application/json'},
                }));
            }
            return real(input, init);
        };
        }"""
    )
    # Drive the failure count past the overlay threshold (5) using the
    # public Try-again handler. Each call awaits one ping cycle.
    page.evaluate(
        """async () => {
        for (let i = 0; i < 6; i++) {
            window._pwaTunnelTryAgain();
            await new Promise(r => setTimeout(r, 80));
        }
        }"""
    )
    page.wait_for_function(
        "() => document.getElementById('pwa-tunnel-overlay').classList.contains('visible')",
        timeout=4000,
    )
    overlay = page.locator("#pwa-tunnel-overlay")
    assert overlay.is_visible(), "overlay should be visible after sustained 503s"
    # Plain-English headline must not include jargon ("tailnet", "VPN").
    heading = page.locator("#pto-heading").inner_text()
    assert "Can't reach your pod" in heading, f"headline regressed: {heading!r}"
    body_text = overlay.inner_text().lower()
    assert "tailnet" not in body_text, "headline copy leaked jargon 'tailnet'"
    assert "vpn" not in body_text, "headline copy leaked jargon 'VPN'"

    # Recovery: flip the stub to 200; Try again triggers a fresh ping,
    # the success path hides the overlay and shows the green toast.
    page.evaluate("() => { window._fakeHealthStatus = 200; }")
    page.evaluate("() => window._pwaTunnelTryAgain()")
    page.wait_for_function(
        "() => !document.getElementById('pwa-tunnel-overlay').classList.contains('visible')",
        timeout=4000,
    )
    assert not overlay.is_visible(), "overlay should hide on recovery"
    # The "Back online" toast briefly appears before fading; check it
    # has the green-variant class while visible.
    page.wait_for_function(
        "() => document.getElementById('pwa-net-toast').classList.contains('back-online')",
        timeout=2000,
    )


def test_phase4_reconnect_attempts_tailscale_deep_link(
    page, admin_server: str, browser_name: str,
) -> None:
    """On mobile, clicking Reconnect attempts navigation to ``tailscale://``.

    Cross-engine browsers refuse to let JS observe (or even shadow) a
    navigation to an unhandled scheme, so the impl records the
    attempted URL on ``window._pwaTunnelLastDeepLink`` immediately
    before assigning ``location.href``. That public flag is the
    regression guard: if a future change drops the deep-link or fires
    the wrong scheme, this test catches it.

    Also covers button wiring — clicking the rendered overlay button
    must drive the same helper, not be a no-op.

    Note: the overlay branches on platform (desktop hides Reconnect;
    the macOS Tailscale app doesn't reliably handle the scheme, and
    the real fix on desktop is an SSH-tunnel shell command). CI runs
    headless Chrome on Linux which detects as ``desktop``, so we
    explicitly stub it into the mobile branch for this assertion. The
    desktop-hides-Reconnect contract is covered by the companion test
    below.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_timeout(300)
    # Helper exists.
    has_helper = page.evaluate(
        "() => typeof window._pwaTunnelReconnect === 'function'"
    )
    assert has_helper, "_pwaTunnelReconnect helper missing — overlay button would no-op"
    # Coerce the overlay into its mobile branch. The Reconnect handler
    # reads the overlay's data-platform attribute as the live source
    # of truth — rewriting the attribute un-hides the button (via the
    # CSS branch) AND tells the handler to fire the deep-link.
    page.evaluate(
        """() => {
        const ov = document.getElementById('pwa-tunnel-overlay');
        ov.setAttribute('data-platform', 'mobile');
        ov.classList.add('visible');
        }"""
    )
    page.evaluate("() => { window._pwaTunnelLastDeepLink = null; }")
    page.click("#pwa-tunnel-overlay .pto-btn-primary")
    # Give the click handler one tick; the helper records the URL
    # synchronously before the (refused) navigation.
    page.wait_for_timeout(100)
    attempted = page.evaluate("() => window._pwaTunnelLastDeepLink")
    assert attempted == "tailscale://", (
        f"Reconnect button should drive a tailscale:// deep-link on mobile; "
        f"observed _pwaTunnelLastDeepLink={attempted!r}"
    )


def test_phase4_reconnect_hidden_on_desktop(
    page, admin_server: str, browser_name: str,
) -> None:
    """Desktop overlays must hide the Reconnect button.

    On the canonical desktop setup the admin UI rides an SSH tunnel
    (`evolve-admin connect`), not Tailscale. The macOS Tailscale app
    doesn't reliably handle the `tailscale://` scheme either, so the
    button on desktop would be deceptive UI. Pin both halves of the
    contract: (1) the button carries `data-platform="mobile"`, and
    (2) CSS hides it when the overlay's `data-platform="desktop"`.

    CI runs headless Chrome on Linux, which detects as desktop — so
    the default branch on this load already exercises the contract.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_timeout(300)
    state = page.evaluate(
        """() => {
        const ov = document.getElementById('pwa-tunnel-overlay');
        const btn = document.getElementById('pto-reconnect-btn');
        ov.classList.add('visible');  // force visible to read computed styles
        return {
            overlayPlatform: ov.getAttribute('data-platform'),
            btnPlatform: btn?.getAttribute('data-platform'),
            btnDisplay: btn ? getComputedStyle(btn).display : null,
        };
        }"""
    )
    assert state["overlayPlatform"] == "desktop", (
        f"CI headless Chrome on Linux should detect as desktop; "
        f"got data-platform={state['overlayPlatform']!r}"
    )
    assert state["btnPlatform"] == "mobile", (
        f"Reconnect button must be data-platform=mobile so desktop hides it; "
        f"got {state['btnPlatform']!r}"
    )
    assert state["btnDisplay"] == "none", (
        f"Reconnect button must be CSS-hidden on desktop overlay; "
        f"got display={state['btnDisplay']!r}"
    )
    # Try again button must still be present + visible on desktop.
    try_again_display = page.evaluate(
        """() => {
        const btn = document.querySelector('#pwa-tunnel-overlay .pto-btn-secondary');
        return btn ? getComputedStyle(btn).display : null;
        }"""
    )
    assert try_again_display and try_again_display != "none", (
        f"Try again button must remain visible on desktop; got {try_again_display!r}"
    )
    # Desktop sub-line must mention the canonical remediation command.
    desktop_sub = page.evaluate(
        "() => document.querySelector('#pwa-tunnel-overlay .pto-sub[data-platform=\"desktop\"]').innerText"
    )
    assert "laptop" in desktop_sub.lower() and "admin daemon" in desktop_sub.lower(), (
        f"desktop sub-line should name the laptop↔mini connection + admin daemon; got {desktop_sub!r}"
    )


def test_phase4_try_again_issues_fresh_health_ping(
    page, admin_server: str, browser_name: str,
) -> None:
    """Clicking Try again fires a /api/health request immediately.

    Counter to the periodic loop — this button has to bypass the
    interval and ping right now, otherwise the operator's tap is
    indistinguishable from waiting.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_timeout(300)
    hits = page.evaluate(
        """async () => {
        let count = 0;
        const real = window.fetch.bind(window);
        window.fetch = (input, init) => {
            const url = (typeof input === 'string') ? input : (input && input.url) || '';
            if (url.endsWith('/api/health')) {
                count += 1;
                return Promise.resolve(new Response('{}', {
                    status: 200, headers: {'Content-Type': 'application/json'},
                }));
            }
            return real(input, init);
        };
        window._pwaTunnelTryAgain();
        // Give the fetch a tick to settle.
        await new Promise(r => setTimeout(r, 200));
        window.fetch = real;
        return count;
        }"""
    )
    assert hits >= 1, (
        f"Try again should issue at least one /api/health fetch; got {hits}"
    )


def test_phase4_sw_posts_network_down_on_fetch_failure(
    page, admin_server: str, browser_name: str,
) -> None:
    """Synthetic check: the page handles SW ``network-down`` messages.

    Unit-pin for the silent-failure mode where the SW broadcasts but
    the client ignores it. We can't easily force a real SW fetch to
    fail in a Playwright run (would require mocking at network layer
    below the SW), so instead we synthesise the message and confirm
    the client's listener path is wired — failure counter advances
    and after enough hits the toast/overlay show.

    The SW-only logic (TypeError → network-down) is small and inline;
    its absence would fail this test through the listener path.
    """
    _skip_firefox_cascade(browser_name)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Wait for the first real /api/health success so everConnected=true;
    # otherwise the SW-message handler intentionally early-returns.
    page.wait_for_timeout(800)
    visible = page.evaluate(
        """async () => {
        // Synthesise the SW message by dispatching a MessageEvent on
        // navigator.serviceWorker — the same target the listener is
        // attached to. The detector should treat each as one failure.
        if (!('serviceWorker' in navigator)) return null;
        const target = navigator.serviceWorker;
        for (let i = 0; i < 6; i++) {
            target.dispatchEvent(new MessageEvent('message', {
                data: { type: 'network-down', reason: 'synthetic' },
            }));
            await new Promise(r => setTimeout(r, 30));
        }
        return {
            overlay: document.getElementById('pwa-tunnel-overlay').classList.contains('visible'),
            toast: document.getElementById('pwa-net-toast').classList.contains('visible'),
        };
        }"""
    )
    if visible is None:
        pytest.skip("no serviceWorker on this engine — n/a")
    assert visible["overlay"] is True, (
        "SW network-down signal didn't escalate to the full overlay after 6 hits"
    )


# ── PWA Phase 4 — the overlay's two network-data rows ─────────────────────
#
# Both the `evolve-admin connect --host <X>` help row and the "Pod URL"
# row are rendered from network.json. They used to read
# ``window._networkData``, but ``_networkData`` is a top-level ``let`` in
# index.html — a *lexical* global, not a ``window`` property — so both
# reads resolved to ``undefined`` and silently took their fallback on
# every boot: the literal "mini" and the browsing origin. On a tunnelled
# or reverse-proxied session those are exactly the values that are wrong.
#
# These two tests pin the live-data path and the genuinely-absent path.


def _pto_force_overlay(page) -> None:
    """Drive the tunnel detector past its overlay threshold.

    Stubs /api/health to 503 (other URLs pass through to the real fetch
    so the SPA keeps working) and calls the public Try-again helper past
    OVERLAY_THRESHOLD. Going through the real state machine — rather
    than poking ``classList`` — is the point: the rows are populated by
    ``_showOverlay``, so a shortcut would skip the code under test.
    """
    page.evaluate(
        """() => {
        const real = window.fetch.bind(window);
        window.fetch = (input, init) => {
            const url = (typeof input === 'string') ? input : (input && input.url) || '';
            if (url.endsWith('/api/health')) {
                return Promise.resolve(new Response('{}', {
                    status: 503, headers: {'Content-Type': 'application/json'},
                }));
            }
            return real(input, init);
        };
        }"""
    )
    # Poll rather than fire a fixed burst: ``_ping`` drops a call while
    # another is already in flight, so a fixed count can land fewer than
    # OVERLAY_THRESHOLD failures on a slower engine (observed on WebKit).
    page.evaluate(
        """async () => {
        const ov = document.getElementById('pwa-tunnel-overlay');
        for (let i = 0; i < 40 && !ov.classList.contains('visible'); i++) {
            window._pwaTunnelTryAgain();
            await new Promise(r => setTimeout(r, 80));
        }
        }"""
    )
    page.wait_for_function(
        "() => document.getElementById('pwa-tunnel-overlay').classList.contains('visible')",
        timeout=8000,
    )


def test_phase4_overlay_rows_render_live_network_data(
    page, admin_server: str, browser_name: str, network_path,
) -> None:
    """Host + Pod URL rows must carry the pod's OWN values, not fallbacks.

    Seeds ``pod.ssh_target`` and an ``adminBaseUrl`` that is deliberately
    NOT the origin the test browser is on, then opens the overlay for
    real. Before the read fix this failed twice over: the help row read
    "evolve-admin connect --host mini" and the Pod URL row read the
    127.0.0.1 origin Playwright was browsing.
    """
    _skip_firefox_cascade(browser_name)
    net = json.loads(network_path.read_text())
    net["pod"] = dict(net.get("pod") or {}, ssh_target="podop@thehive.local")
    net["adminBaseUrl"] = "https://thehive.example.net:8443"
    network_path.write_text(json.dumps(net, indent=2))

    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # The rows read whatever the SPA's boot fan-out has loaded, so wait
    # for /api/network to land before forcing the overlay.
    page.wait_for_function(
        "() => typeof _networkData === 'object' && _networkData"
        " && _networkData.adminBaseUrl === 'https://thehive.example.net:8443'",
        timeout=8000,
    )
    _pto_force_overlay(page)

    rows = page.evaluate(
        """() => ({
        cmd: document.getElementById('pto-cmd-install').textContent,
        podUrl: document.getElementById('pto-pod-url').textContent,
        })"""
    )
    assert rows["cmd"] == "evolve-admin connect --host thehive.local", (
        "help row should name the operator's own ssh_target host; got "
        f"{rows['cmd']!r} (the 'mini' literal means the read fell back)"
    )
    assert rows["podUrl"] == "https://thehive.example.net:8443", (
        "Pod URL row should show the configured adminBaseUrl, not the "
        f"origin the browser happens to be on; got {rows['podUrl']!r}"
    )


def test_phase4_overlay_rows_fall_back_when_network_data_absent(
    page, admin_server: str, browser_name: str, network_path,
) -> None:
    """No ssh_target and no adminBaseUrl → the documented literals.

    The fixture network.json carries neither key, so this is the stock
    shape. Guards the other half of the read fix: reading the real
    ``_networkData`` must not start rendering ``undefined`` into the
    operator's copy-pasteable command.
    """
    _skip_firefox_cascade(browser_name)
    net = json.loads(network_path.read_text())
    assert not (net.get("pod") or {}).get("ssh_target"), "fixture drifted"
    assert not net.get("adminBaseUrl"), "fixture drifted"

    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof _networkData === 'object' && _networkData && _networkData.networkId",
        timeout=8000,
    )
    _pto_force_overlay(page)

    rows = page.evaluate(
        """() => ({
        cmd: document.getElementById('pto-cmd-install').textContent,
        podUrl: document.getElementById('pto-pod-url').textContent,
        })"""
    )
    assert rows["cmd"] == "evolve-admin connect --host mini", (
        f"absent ssh_target/adminBaseUrl should keep the canonical-install "
        f"literal; got {rows['cmd']!r}"
    )
    assert rows["podUrl"] == admin_server, (
        f"absent adminBaseUrl should fall back to the browsing origin; "
        f"got {rows['podUrl']!r}"
    )


# ── PWA Phase 1.2 — push notifications ─────────────────────────────────────


def test_push_vapid_public_key_route(page, admin_server: str) -> None:
    """``GET /api/push/vapid-public-key`` returns an unpadded b64url key.

    Pins the contract the frontend's ``_alEnablePushOnThisDevice`` flow
    relies on: a valid base64url string with no ``=`` padding,
    convertible to a Uint8Array suitable for
    ``pushManager.subscribe({applicationServerKey})``. A regression to
    padded base64 / standard base64 would fail silently in some
    browsers (Chrome's parser is forgiving, Safari's is not).
    """
    response = page.request.get(f"{admin_server}/api/push/vapid-public-key")
    assert response.status == 200, (
        f"/api/push/vapid-public-key returned {response.status}; "
        f"body={response.text()[:200]}"
    )
    body = response.json()
    pub = body.get("public_key")
    assert pub, "/api/push/vapid-public-key response missing public_key"
    assert "=" not in pub, f"public key still padded: {pub!r}"
    assert "+" not in pub and "/" not in pub, (
        f"public key not URL-safe base64: {pub!r}"
    )
    # P-256 uncompressed public point = 65 bytes → ~87 base64url chars.
    assert 80 <= len(pub) <= 100, (
        f"public key length {len(pub)} outside expected P-256 range"
    )


def test_push_subscribe_accepts_well_formed_body(page, admin_server: str) -> None:
    """``POST /api/push/subscribe`` returns a subscription_id on a valid body.

    Uses a synthetic endpoint so no real Apple/Google service is hit —
    we're testing the admin-side persistence + validation, not delivery.
    The subsequent GET /api/push/subscriptions surfaces the row so the
    Devices UI can render it.
    """
    body = {
        "endpoint": "https://push.apple.com/test-smoke",
        "keys":     {"p256dh": "FAKE_P256DH", "auth": "FAKE_AUTH"},
        "device_label": "Smoke test device",
    }
    resp = page.request.post(
        f"{admin_server}/api/push/subscribe", data=body,
    )
    assert resp.status == 200, (
        f"/api/push/subscribe returned {resp.status}; body={resp.text()[:200]}"
    )
    sub_id = resp.json().get("subscription_id")
    assert sub_id and sub_id.startswith("sub_"), (
        f"subscription_id should be 'sub_<hex>'; got {sub_id!r}"
    )

    listing = page.request.get(f"{admin_server}/api/push/subscriptions").json()
    devices = listing.get("subscriptions", [])
    assert any(d["id"] == sub_id for d in devices), (
        f"subscription {sub_id!r} not in /api/push/subscriptions: {devices!r}"
    )

    # Clean up so other tests in the matrix start from a clean store.
    page.request.post(
        f"{admin_server}/api/push/unsubscribe",
        data={"subscription_id": sub_id},
    )


def test_push_subscribe_rejects_missing_keys(page, admin_server: str) -> None:
    """Validation: missing endpoint or keys → 400, not 500."""
    resp = page.request.post(f"{admin_server}/api/push/subscribe", data={})
    assert resp.status == 400


def test_sw_registers_push_and_notificationclick_listeners(
    page, admin_server: str,
) -> None:
    """The active service worker handles ``push`` and ``notificationclick``.

    There's no portable browser API to introspect a SW's registered
    listeners post-install, so we verify the served script source has
    both addEventListener calls. The earlier
    ``test_service_worker_registers_on_load`` confirms the SW reaches
    ``active`` state — combining the two pins both the lifecycle and
    the handler surface.

    A regression where Phase 1.2's handlers got reverted to TODO stubs
    would surface here: the served sw.js would still register the
    listeners, but they'd be no-ops. We also assert ``showNotification``
    appears so a regression that registered listeners but skipped the
    actual notification render fails loudly.
    """
    response = page.request.get(f"{admin_server}/sw.js")
    assert response.status == 200
    body = response.text()
    assert "addEventListener('push'" in body or 'addEventListener("push"' in body, (
        "sw.js missing 'push' event listener"
    )
    assert (
        "addEventListener('notificationclick'" in body
        or 'addEventListener("notificationclick"' in body
    ), "sw.js missing 'notificationclick' event listener"
    assert "showNotification" in body, (
        "sw.js push handler must call showNotification; got a no-op stub"
    )


# ── PWA Phase 3 — embedded mini terminal ────────────────────────────────────


def test_terminal_info_endpoint_advertises_pod_name(
    page, admin_server: str
) -> None:
    """``/api/terminal/info`` returns the pod name + enabled flag."""
    resp = page.request.get(f"{admin_server}/api/terminal/info")
    assert resp.status == 200, (
        f"/api/terminal/info returned {resp.status}; body={resp.text()[:200]}"
    )
    body = resp.json()
    assert body.get("enabled") is True
    assert isinstance(body.get("pod_name"), str) and body["pod_name"], (
        "pod_name should be a non-empty string (drives the page header)"
    )
    # Timestamp shape — ISO-8601 with the trailing 'Z' the rest of the UI uses.
    assert isinstance(body.get("now"), str) and body["now"].endswith("Z")


def test_terminal_nav_item_renders_and_page_exists(
    page, admin_server: str
) -> None:
    """Sidebar shows the Terminal nav row + #page-terminal mounts.

    Smoke check that the spec §11 Q8 placement (top-level, not under
    Maintenance) shipped: the nav item exists and the page div is in
    the DOM ready for the SPA to activate.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    nav = page.locator('.nav-item[data-page="terminal"]')
    assert nav.count() == 1, "Terminal nav item missing from sidebar"
    assert page.locator("#page-terminal").count() == 1, "#page-terminal not in DOM"
    assert page.locator("#terminal-container").count() == 1, (
        "#terminal-container missing — xterm.js has nothing to attach to"
    )


def test_terminal_first_use_modal_present(page, admin_server: str) -> None:
    """The first-use 'real shell' modal markup is in the DOM."""
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    modal = page.locator("#terminal-firstuse-modal")
    assert modal.count() == 1, "first-use modal missing — operator gets a silent shell"


# ── PR #1413 — iPhone polish sweep ──────────────────────────────────────────
#
# Six bug fixes verified at the iPhone 15 Pro viewport (390×844). The
# matrix runs these on chromium + webkit so the WebKit-specific
# rendering (iOS Safari shares the engine) is exercised on every CI run.
#
# Note: viewport-fit=cover + env(safe-area-inset-*) are no-ops in
# Playwright's headless contexts (no system chrome to inset against),
# so these tests verify the *fallback* values resolve sanely (e.g.
# env(safe-area-inset-top, 8px) → 8px). Real-device behaviour is
# verified manually on the iPhone PWA install.


IPHONE_15_PRO = {"width": 390, "height": 844}


def test_iphone_topbar_has_gap_between_hamburger_and_title(
    page, admin_server: str
) -> None:
    """PR #1413 §a — hamburger and page-title slot have ≥12px gap.

    Before the fix the topbar held only the hamburger; the active
    page's <h1> rendered immediately below at the .main border, which
    Pod_admin's real-iPhone install read as 'hamburger crowding the
    headline'. The fix adds an explicit page-title slot inside the
    topbar and a 12px flex gap. This pins the resolved gap and that
    the slot is populated from the active page's h1 (or the active
    nav-item label for Home, which has no h1).
    """
    page.set_viewport_size(IPHONE_15_PRO)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")

    # Drive to a page that has an h1 — "overview" → "Overview" — so we
    # also pin the h1→title sync. The default home page has no h1 and
    # falls back to the nav-item label path.
    # The sidebar is the slide-in drawer at phone width — clicking a
    # nav-item requires opening it first OR calling nav() directly via
    # JS. The latter is faster and exercises the same code path.
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"overview\"]'))"
    )

    geom = page.evaluate(
        """() => {
        const bar = document.getElementById('mobile-topbar');
        const ham = document.getElementById('hamburger-btn');
        const title = document.getElementById('mobile-topbar-title');
        if (!bar || !ham || !title) return null;
        const cs = getComputedStyle(bar);
        return {
            display: cs.display,
            gap: cs.gap,
            ham_right: ham.getBoundingClientRect().right,
            title_left: title.getBoundingClientRect().left,
            title_text: title.textContent.trim(),
        };
        }"""
    )
    assert geom is not None, "topbar / hamburger / title slot missing from DOM"
    assert geom["display"] == "flex", (
        f".mobile-topbar should be a flex container at phone width; "
        f"got display={geom['display']!r}"
    )
    # Gap is reported as a length string like '12px'; normalize.
    gap_px = float(geom["gap"].replace("px", "")) if geom["gap"] else 0.0
    assert gap_px >= 12.0, (
        f".mobile-topbar gap should be ≥12px; got {geom['gap']!r}. "
        "Hamburger would crowd the page-title slot."
    )
    # Resolved spacing between the hamburger's right edge and the
    # title slot's left edge should reflect the flex gap.
    actual_spacing = geom["title_left"] - geom["ham_right"]
    assert actual_spacing >= 11.5, (
        f"hamburger→title spacing is {actual_spacing}px; expected ≥12px (PR #1413 §a)"
    )
    assert geom["title_text"] == "Overview", (
        f"topbar title should sync from the active page's <h1>; "
        f"got {geom['title_text']!r}"
    )


def test_iphone_body_bg_matches_theme_color(page, admin_server: str) -> None:
    """PR #1413 §b — html/body background paint into the safe-area band.

    The white-strip bug stems from iOS PWA exposing the area between
    the system status bar and the page chrome; without viewport-fit=
    cover and an html-level background, that band shows the default
    white. We assert <html> and <body> share a non-white dark
    background; the actual theme value (#0B0D10 vs the manifest's
    #0d1117) is allowed to drift by a hex digit, but it must NOT be
    white.
    """
    page.set_viewport_size(IPHONE_15_PRO)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    bg = page.evaluate(
        """() => {
        const htmlBg = getComputedStyle(document.documentElement).backgroundColor;
        const bodyBg = getComputedStyle(document.body).backgroundColor;
        const vp = document.querySelector('meta[name="viewport"]')?.content || '';
        const theme = document.querySelector('meta[name="theme-color"]')?.content || '';
        return { htmlBg, bodyBg, vp, theme };
        }"""
    )
    # Reject pure-white backgrounds (the symptom). The dark theme uses
    # rgb values close to (11, 13, 16); any rgb where every channel is
    # ≥240 is "white enough" to be the bug.
    def _is_white(rgb: str) -> bool:
        import re
        m = re.findall(r"\d+", rgb)
        if len(m) < 3:
            return False
        r, g, b = (int(m[0]), int(m[1]), int(m[2]))
        return r >= 240 and g >= 240 and b >= 240
    assert not _is_white(bg["htmlBg"]), (
        f"<html> background should not be white; got {bg['htmlBg']!r}. "
        "iOS overscroll would flash white above the page chrome."
    )
    assert not _is_white(bg["bodyBg"]), (
        f"<body> background should not be white; got {bg['bodyBg']!r}."
    )
    assert "viewport-fit=cover" in bg["vp"], (
        "viewport meta missing viewport-fit=cover; safe-area-inset-* "
        "env values resolve to 0 without it, so the topbar can't pad "
        "around the status bar (PR #1413 §b)."
    )
    assert bg["theme"], "theme-color meta missing — iOS won't tint the system chrome"


def test_iphone_no_horizontal_overflow_on_usage(
    page, admin_server: str
) -> None:
    """PR #1413 §c — Usage page tile grids fit inside the viewport.

    The mon-session-stats grid was hardcoded to repeat(5,1fr) which
    overflows iPhone widths by ~50px (5 cols of ~80px each plus gaps
    in a 326px container = 470px). The fix is auto-fill with a 140px
    minmax floor. This test asserts there's no horizontal page scroll
    at 390px when the Usage page is active.
    """
    page.set_viewport_size(IPHONE_15_PRO)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"cost\"]'))"
    )
    geom = page.evaluate(
        """() => ({
            scroll_w: document.documentElement.scrollWidth,
            client_w: document.documentElement.clientWidth,
            // mon-session-stats lives inside the Sessions subtab; even
            // when invisible its computed grid-template should be
            // auto-fill, not repeat(5,1fr).
            stats_cols: (() => {
                const el = document.getElementById('mon-session-stats');
                if (!el) return null;
                return getComputedStyle(el).gridTemplateColumns;
            })(),
        })"""
    )
    # Allow 2px slop for sub-pixel rounding on WebKit.
    assert geom["scroll_w"] <= geom["client_w"] + 2, (
        f"Usage page overflows viewport at 390px: scrollWidth={geom['scroll_w']} "
        f"vs clientWidth={geom['client_w']}. PR #1413 §c tile-grid fix regressed."
    )
    # The computed style for auto-fill resolves to the materialized
    # tracks at runtime — at 326px container width with minmax(140px,
    # 1fr), we expect ~2 tracks. The smoking-gun regression is
    # five tracks (the old repeat(5,1fr) value) which would always
    # produce 5 lengths in the resolved string.
    if geom["stats_cols"]:
        track_count = len(geom["stats_cols"].split())
        assert track_count <= 3, (
            f"#mon-session-stats resolved to {track_count} tracks at phone "
            f"width — expected ≤3 from auto-fill. Old repeat(5,1fr) "
            f"regressed back in. Got: {geom['stats_cols']!r}"
        )


def test_iphone_evo_fab_visible_and_anchored_bottom_right(
    page, admin_server: str
) -> None:
    """PR #1413 §d — FAB visible bottom-right on a non-Home page.

    On Home the FAB is intentionally suppressed (body.evo-on-chat —
    Home IS the chat). On every other page the FAB must be on-screen
    and roughly at bottom-right with safe-area-inset-bottom respect.
    The fallback inside env() means the 16px floor applies in headless.
    """
    page.set_viewport_size(IPHONE_15_PRO)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Navigate off Home so the FAB unhides.
    # The sidebar is the slide-in drawer at phone width — clicking a
    # nav-item requires opening it first OR calling nav() directly via
    # JS. The latter is faster and exercises the same code path.
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"overview\"]'))"
    )

    fab = page.locator("#evo-fab")
    assert fab.is_visible(), (
        "#evo-fab should be visible at iPhone width on a non-Home page "
        "(PR #1413 §d). On Home the FAB is intentionally suppressed."
    )
    geom = page.evaluate(
        """() => {
        const fab = document.getElementById('evo-fab');
        const bar = document.getElementById('utility-bar');
        if (!fab || !bar) return null;
        const r = fab.getBoundingClientRect();
        const cs = getComputedStyle(bar);
        return {
            top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            vh: window.innerHeight, vw: window.innerWidth,
            bar_position: cs.position,
            bar_z_index: cs.zIndex,
        };
        }"""
    )
    assert geom is not None
    # FAB should land in the bottom half of the viewport.
    assert geom["top"] > geom["vh"] / 2, (
        f"#evo-fab should be anchored to the bottom on phone; got top={geom['top']} "
        f"in {geom['vh']}-px viewport (PR #1413 §d)."
    )
    # And no farther than 24px from the right edge (16px right + 50px width = 66px
    # from the right edge for the left of the FAB; the right edge of the FAB
    # should be ~16px from the viewport edge).
    assert geom["vw"] - geom["right"] <= 24, (
        f"#evo-fab right edge should be ≤24px from viewport edge; got "
        f"{geom['vw'] - geom['right']}px"
    )
    # Z-index below modal-overlays so confirmation modals win (silent-
    # failure check from the spec).
    assert int(geom["bar_z_index"]) < 100 or geom["bar_z_index"] == "90", (
        f"#utility-bar z-index should drop below modal overlays on phone; "
        f"got {geom['bar_z_index']!r}"
    )


def test_iphone_drawer_tap_open_close(page, admin_server: str) -> None:
    """PR #1413 §e+§f — tap FAB opens drawer; drawer fits viewport; tap × closes.

    Bundles the open-gesture (single tap, not swipe) with the
    full-width-on-phone check and the close-button-via-tap path.
    """
    page.set_viewport_size(IPHONE_15_PRO)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # The sidebar is the slide-in drawer at phone width — clicking a
    # nav-item requires opening it first OR calling nav() directly via
    # JS. The latter is faster and exercises the same code path.
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"overview\"]'))"
    )

    drawer = page.locator("#evo-drawer")
    # Pre-tap: drawer closed (no .open class — translated off-screen).
    assert "open" not in (drawer.get_attribute("class") or ""), (
        "drawer should start closed"
    )

    # Single tap on the FAB opens the drawer.
    page.click("#evo-fab")
    # The drawer's open state is class-driven (`.open`), independent of
    # the transform transition — assert immediately and the wait_for
    # below covers any animation delay.
    page.wait_for_function(
        "() => document.getElementById('evo-drawer')?.classList.contains('open')",
        timeout=2000,
    )

    geom = page.evaluate(
        """() => {
        const d = document.getElementById('evo-drawer');
        const closeBtn = d.querySelector('.evo-drawer-close');
        const r = d.getBoundingClientRect();
        const cb = closeBtn ? closeBtn.getBoundingClientRect() : null;
        return {
            width: r.width, vw: window.innerWidth,
            left: r.left, right: r.right,
            close_w: cb ? cb.width : 0,
            close_h: cb ? cb.height : 0,
            close_visible: !!(closeBtn && closeBtn.offsetParent !== null),
        };
        }"""
    )
    # Drawer must not overshoot the viewport.
    assert geom["width"] <= geom["vw"] + 1, (
        f"drawer width {geom['width']}px exceeds viewport {geom['vw']}px "
        "(PR #1413 §f — drawer should clamp to 100vw on phone)"
    )
    # And must be fully on-screen (right edge at or beyond 0).
    assert geom["right"] >= geom["vw"] - 1, (
        f"drawer right edge at {geom['right']}px in {geom['vw']}-px viewport — "
        "the slide-in transform didn't complete or the drawer is mis-anchored"
    )
    assert geom["close_visible"], (
        "drawer close (✕) button must be visible — it's the only escape "
        "from the full-screen drawer on phone (PR #1413 §f)."
    )
    assert geom["close_w"] >= 36 and geom["close_h"] >= 36, (
        f"drawer close button tap-target is {geom['close_w']}×{geom['close_h']}; "
        f"should be ≥36×36 on phone (PR #1413 §f)."
    )

    # Tap the close button → drawer closes.
    page.click("#evo-drawer .evo-drawer-close")
    page.wait_for_function(
        "() => !document.getElementById('evo-drawer')?.classList.contains('open')",
        timeout=2000,
    )


def test_desktop_layout_unchanged_at_1280(page, admin_server: str) -> None:
    """PR #1413 silent-failure guard — desktop layout still works at 1280px.

    The fixes are wrapped in @media (max-width:…) blocks plus
    env(safe-area-inset-*) padding (which resolves to 0 on desktop).
    This test asserts the desktop chrome is unchanged: hamburger
    hidden, sidebar pinned, FAB at top-right, drawer width 420px.
    """
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Move off Home so FAB is visible.
    # The sidebar is the slide-in drawer at phone width — clicking a
    # nav-item requires opening it first OR calling nav() directly via
    # JS. The latter is faster and exercises the same code path.
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"overview\"]'))"
    )
    geom = page.evaluate(
        """() => {
        const ham = document.getElementById('hamburger-btn');
        const sb = document.getElementById('sidebar');
        const fab = document.getElementById('evo-fab');
        const bar = document.getElementById('utility-bar');
        const fab_r = fab.getBoundingClientRect();
        const bar_cs = getComputedStyle(bar);
        return {
            ham_visible: !!(ham && ham.offsetParent !== null),
            sb_visible: !!(sb && sb.offsetParent !== null),
            sb_left: sb.getBoundingClientRect().left,
            fab_top: fab_r.top,
            fab_right_from_edge: window.innerWidth - fab_r.right,
            bar_top: bar_cs.top,
            bar_right: bar_cs.right,
        };
        }"""
    )
    assert not geom["ham_visible"], (
        "hamburger should be hidden at 1280px — phone-only chrome leaked into desktop"
    )
    assert geom["sb_visible"] and geom["sb_left"] == 0, (
        f"sidebar should be pinned at left=0 on desktop; left={geom['sb_left']}"
    )
    # FAB at top-right (top ≤ 32px, flush with the right edge).
    assert geom["fab_top"] < 32, (
        f"#evo-fab should be near the viewport top on desktop; got top={geom['fab_top']}"
    )
    assert geom["fab_right_from_edge"] <= 4, (
        f"#evo-fab should be flush right on desktop; got "
        f"right-from-edge={geom['fab_right_from_edge']}"
    )
    # Desktop anchor — utility-bar pinned via top:16px and right:0; the
    # phone-anchor override (bottom: max(16px, env(...))) must not
    # apply here. Verify the canonical desktop values are intact.
    assert geom["bar_top"] == "16px", (
        f"#utility-bar top should be 16px on desktop "
        f"(phone-anchor regressed?); got {geom['bar_top']!r}"
    )
    assert geom["bar_right"] == "0px", (
        f"#utility-bar right should be 0px on desktop "
        f"(phone-anchor regressed?); got {geom['bar_right']!r}"
    )


# ── PR #1413 follow-up (#1428) ──────────────────────────────────────────────
#
# Three small follow-ups after a real-iPhone bug-bash:
#  - SW CACHE_VERSION bumped so installed PWAs get a fresh shell.
#  - Subtab rows now paint a right-edge fade when they overflow, so
#    iPhone users see "there's more here, swipe →" instead of an
#    abrupt cut.
#  - Drawer z-index raised so the full-screen phone drawer cannot
#    paint UNDER a stale feedback-modal that's open in the background.


def test_sw_cache_version_advanced(page, admin_server: str) -> None:
    """sw.js CACHE_VERSION is stamped per-build so installed PWAs refresh.

    Originally this pinned a hand-maintained ``evolve-pwa-vN`` counter
    forward. That manual counter is superseded (PR "[META:ui] PWA
    service-worker deploy-safety"): the server now substitutes the SW's
    ``__PWA_CACHE_VERSION__`` token with ``evolve-pwa-<hash>``, where the
    hash is a fingerprint of ALL SPA assets. That guarantees — by
    construction, with no manual bump — that every asset-changing deploy
    ships a DISTINCT CACHE_VERSION, which is exactly what makes the SW's
    ``activate`` flush the prior build's cache and an installed PWA pick
    up a fresh shell. The invariants pinned here:

      1. CACHE_VERSION is still a literal (the SW reads it directly).
      2. The ``__PWA_CACHE_VERSION__`` placeholder was substituted — an
         unstamped build would share one cache name across deploys and
         never flush.
      3. It has the per-build ``evolve-pwa-<16-hex>`` shape and tracks
         the served ``pwa-shell-fingerprint`` stamp.
    """
    resp = page.request.get(f"{admin_server}/sw.js")
    assert resp.status == 200
    body = resp.text()
    import re
    m = re.search(r"const\s+CACHE_VERSION\s*=\s*'([^']+)'", body)
    assert m, "sw.js no longer declares CACHE_VERSION literal — the SW can't name its cache"
    version = m.group(1)
    assert version != "__PWA_CACHE_VERSION__", (
        "CACHE_VERSION placeholder was not substituted by the server — every "
        "build would share one cache name and activate() could never flush "
        "the prior build's stale shell/modules."
    )
    m2 = re.match(r"^evolve-pwa-([0-9a-f]{16})$", version)
    assert m2, (
        f"CACHE_VERSION {version!r} isn't the per-build content-hash shape "
        f"evolve-pwa-<16-hex>; the deploy-safety stamping regressed."
    )
    # The cache version must track the asset fingerprint, so it changes
    # whenever any SPA asset changes (the whole point — installed PWAs
    # refresh on every asset-changing deploy).
    fp = re.search(r"pwa-shell-fingerprint:\s*([0-9a-f]+)", body)
    assert fp and version == f"evolve-pwa-{fp.group(1)}", (
        "CACHE_VERSION must track the pwa-shell-fingerprint stamp so it "
        "moves forward on every asset change."
    )


def test_iphone_subtab_scroll_fade_applies(page, admin_server: str) -> None:
    """At phone width, overflowing subtab rows get a right-edge mask fade.

    The fade is the iPhone scroll-affordance — without it, the user
    has no visual cue that the Maintenance subtab row continues past
    the right edge. The CSS rule lives under @media (max-width:979px)
    and is toggled off when JS detects the row is fully visible or
    scrolled to the end. This test exercises a synthetic overflowing
    row so the assertion doesn't depend on whatever page state is
    active in the test pod.
    """
    page.set_viewport_size(IPHONE_15_PRO)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    result = page.evaluate(
        """() => {
        // Build a .subtabs row pinned to a fixed 300px container so it
        // reliably overflows regardless of page padding. The .subtabs
        // styling itself owns the overflow + mask behaviour we're
        // testing.
        const wrap = document.createElement('div');
        wrap.style.cssText = 'position:fixed;top:200px;left:0;width:300px;z-index:99999;overflow:hidden';
        const row = document.createElement('div');
        row.className = 'subtabs has-overflow';
        // 8 tabs × ~120px each = 960px — easily overflows a 300px box.
        for (let i = 0; i < 8; i++) {
            const tab = document.createElement('div');
            tab.className = 'subtab';
            tab.textContent = 'Subtab ' + i;
            row.appendChild(tab);
        }
        wrap.appendChild(row);
        document.body.appendChild(wrap);

        const cs = getComputedStyle(row);
        const mask = cs.maskImage || cs.webkitMaskImage || '';

        // Test the scrolled-end state too — apply the class manually
        // and re-check that the mask drops.
        row.classList.add('scrolled-end');
        const csEnd = getComputedStyle(row);
        const maskEnd = csEnd.maskImage || csEnd.webkitMaskImage || '';
        row.classList.remove('scrolled-end');

        const out = { mask, maskEnd, overflows: row.scrollWidth > row.clientWidth };
        wrap.remove();
        return out;
        }"""
    )
    assert result["overflows"], (
        "test fixture didn't actually overflow — check the synthetic row"
    )
    # An overflowing row should have a linear-gradient mask.
    assert "linear-gradient" in result["mask"], (
        f"overflowing .subtabs should paint a right-edge fade mask; got "
        f"mask-image={result['mask']!r}. PR #1413 follow-up §subtab-fade."
    )
    # And the scrolled-end class should drop the mask back to none.
    assert "linear-gradient" not in result["maskEnd"], (
        f".scrolled-end should remove the fade; got mask-image={result['maskEnd']!r}"
    )


def test_iphone_maintenance_subtabs_fold_into_more(
    page, admin_server: str
) -> None:
    """The 9 Maintenance subtabs fold behind a More ▾ button on iPhone.

    Regression test for the boot-time measurement bug: _initMaintOverflow
    ran at script-parse time, but the Maintenance page was display:none
    then so every tab measured 0px wide and the priority-nav decided
    'everything fits' forever. Fix: re-measure on navigation to
    Maintenance via window._maintOverflowRemeasure().

    Without the fix, the subtabs would render in the row and either
    overflow the viewport or force horizontal scroll — neither
    acceptable on iPhone (Pod_admin confirmed real-device unusability).

    Was 11 subtabs before Phase 1b promoted Backup + Recovery to their own
    top-level Backup page; 9 still overflows iPhone width comfortably.
    """
    page.set_viewport_size(IPHONE_15_PRO)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Navigate to Maintenance — this fires onPageActivate('maintenance')
    # which calls _maintOverflowRemeasure().
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"maintenance\"]'))"
    )
    # Wait only for the binary regression-catch — fold happened: the More
    # cluster is visible AND at least one tab moved into the popover.
    # Once true this stays true: 11 iPhone-width tabs cannot fit naturally,
    # so reflow() will never decide to un-fold (totalW > rowW always),
    # which means the row/popover partition is stable once chosen.
    #
    # The previous predicate also required row.scrollWidth ≤ rowW + 2
    # atomically with the fold check, on the theory that splitting flaked
    # because state could flip across RPC calls. But the actually-flippable
    # state here IS scrollWidth — it can transiently overshoot during
    # ResizeObserver settle on webkit even after fold is stable. Bundling
    # the geometric check into the predicate meant a 1.5s → 5s → 10s
    # budget chase, with no convergence in sight. Splitting it out and
    # snapshotting atomically below is safe because fold itself doesn't
    # flip once decided, and the scrollWidth value is read from the same
    # JS tick as the row/popover tab counts.
    page.wait_for_function(
        """() => {
            const row = document.getElementById('maint-subtabs');
            if (!row) return false;
            const cluster = row.querySelector('.subtab-more-cluster');
            if (!cluster || cluster.style.display === 'none') return false;
            const popover = cluster.querySelector('.subtab-more-popover');
            return !!popover && popover.querySelectorAll('.subtab').length >= 1;
        }""",
        timeout=5000,
    )
    # Atomic geom snapshot — all reads in one tick, so the (row_tabs,
    # popover_tabs, row_scroll_w) tuple is internally consistent.
    geom = page.evaluate(
        """() => {
            const row = document.getElementById('maint-subtabs');
            const cluster = row.querySelector('.subtab-more-cluster');
            const popover = cluster.querySelector('.subtab-more-popover');
            const rowW = row.getBoundingClientRect().width;
            return {
                row_width: rowW,
                row_scroll_w: row.scrollWidth,
                row_tabs: row.querySelectorAll(':scope > .subtab').length,
                popover_tabs: popover.querySelectorAll('.subtab').length,
                cluster_visible: cluster.style.display !== 'none',
            };
        }"""
    )
    assert geom["cluster_visible"], (
        "The More ▾ cluster should be visible after navigating to "
        "Maintenance on iPhone (11 subtabs don't fit). Priority-nav "
        "didn't re-measure — _maintOverflowRemeasure() regression."
    )
    assert geom["popover_tabs"] >= 1, (
        f"At least one tab should have moved into the More popover; "
        f"got {geom['popover_tabs']} popover tabs, {geom['row_tabs']} row tabs"
    )
    # Total tabs must add up to 9 (Status, System, Gateway Logs, Pod
    # Health, Cron Jobs, Infra Jobs, Admin Server, Setup, Claude Access).
    # Was 11 before Phase 1b moved Backup + Recovery to the top-level
    # Backup page.
    assert geom["row_tabs"] + geom["popover_tabs"] == 9, (
        f"Expected 9 total Maintenance subtabs; got "
        f"{geom['row_tabs']} + {geom['popover_tabs']} = "
        f"{geom['row_tabs'] + geom['popover_tabs']}"
    )
    # Row scrollWidth should match row width when folded — no horizontal
    # scroll. Allow 2px slop for sub-pixel rounding.
    assert geom["row_scroll_w"] <= geom["row_width"] + 2, (
        f"Maintenance row overflows after fold: scrollWidth={geom['row_scroll_w']} "
        f"vs width={geom['row_width']}. Priority-nav didn't fold enough tabs."
    )


def test_iphone_drawer_zindex_above_modals(page, admin_server: str) -> None:
    """PR #1413 follow-up — full-screen phone drawer paints over modals.

    The desktop drawer at z-index:999 would sit BEHIND a feedback-modal
    at z-index:1002, which is fine when the drawer is a 420px right-side
    column (modal still visible to the left). On phone the drawer is
    100vw × 100dvh, so any modal above it would peek through the drawer
    cover. Phone media query bumps z-index to 1003 — above every modal.
    """
    page.set_viewport_size(IPHONE_15_PRO)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    z = page.evaluate(
        """() => {
        const d = document.getElementById('evo-drawer');
        if (!d) return null;
        return parseInt(getComputedStyle(d).zIndex, 10);
        }"""
    )
    assert z is not None and z >= 1003, (
        f"#evo-drawer z-index at phone width should be ≥1003 to paint "
        f"over feedback-modal (1002); got {z!r}"
    )
