# Cross-browser smoke tests

Phase 0 §4.4 baseline for [spec-pwa-2026-05-18.md](../../docs/spec-pwa-2026-05-18.md).

A small Playwright suite that drives a real browser (Chromium, WebKit,
Firefox) against a locally-spun-up admin server, to catch the obvious
class of regression where a feature works in Chrome and breaks in
Safari or Firefox.

**Out of scope:** feature E2E, accessibility audit, visual regression.
This suite is the cross-engine watchdog — the per-feature test plans
live with the features they cover. Manual cross-device coverage (real
iOS, iPadOS, Android devices) is the operator-facing checklist in
[`docs/pwa/test-matrix-browsers.md`](../../docs/pwa/test-matrix-browsers.md).

## Install

```bash
pip install pytest-playwright
playwright install chromium webkit firefox
```

The first command pulls Playwright's Python bindings. The second
downloads the browser binaries (~300MB on first run; cached after).

## Run

```bash
# default — chromium only (fastest)
pytest tests/browser/

# all three engines
pytest tests/browser/ --browser chromium --browser webkit --browser firefox

# headed (useful for debugging)
pytest tests/browser/ --browser chromium --headed --slowmo 500
```

The conftest fixture spins up the admin server in a subprocess against
a throwaway `network.json` and tears it down at session end. The fixture
is `session`-scoped so the cost is paid once per test run, not per test.

## What's covered

A dozen-ish tests, all engine-parametric:

- `test_index_loads` — the SPA shell is served with status 200.
- `test_index_has_no_console_errors` — no JS-level errors on load.
- `test_spa_nav_items_present` — the canonical `data-page` items are in the DOM.
- `test_core_api_endpoints_respond` — `/api/version`, `/api/health`, `/api/status`, `/api/network`, `/api/host-health` all return 200 JSON.
- `test_static_assets_served` — favicons, apple-touch-icon, site.webmanifest all served (Phase 1 PWA install will need these).
- `test_spa_nav_switches_active_page` — clicking a nav item updates `.active` on both the nav item and the matching page div.
- `test_clipboard_api_present` — `navigator.clipboard.writeText` is available across all engines (localhost is a secure context).

## What's NOT covered

- Anything Phase 1+ adds (manifest content beyond "is served", service-worker registration, install UX, push). Those tests land with their features.
- Anything dependent on real bots being present (e.g. per-bot detail pages, bot-config flows). The fixture network.json is empty by design.
- Touch / mobile-viewport behaviour. Playwright can emulate these but engine quirks on real iOS Safari don't reproduce in Playwright WebKit on Linux. See the manual matrix.

## Why subprocess and not Flask `test_client`

`test_client` is fast but in-process — Playwright is a real browser
talking real HTTP, so we need a real server socket. Subprocess is the
right shape and matches how the admin server is actually launched
(`python -m evolve_admin.web.run`).
