#!/usr/bin/env python3
"""shots — drive the real admin UI over a fixture pod and save screenshots.

Both themes, one file per (screen, theme).  The point is to look at the
surfaces a stranger meets rather than to assert on them: this is an audit
instrument, not a test.

Usage::

    python3 tests/fixtures/pod/shots.py --base-url http://127.0.0.1:5099 \\
        --out /tmp/shots --tag populated
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _goto_page(page, name: str) -> None:
    # Call the SPA's own nav() rather than clicking: the sidebar element
    # covers its own items at some widths, which Playwright refuses to click
    # through. nav() is what the click handler calls anyway.
    page.evaluate(
        "n => nav(document.querySelector(`.nav-item[data-page=\"${n}\"]`))", name
    )
    page.wait_for_timeout(2000)


def _set_theme(page, theme: str) -> None:
    page.evaluate(
        "t => { document.documentElement.setAttribute('data-theme', t);"
        " try { localStorage.setItem('theme', t); } catch (e) {} }",
        theme,
    )
    page.wait_for_timeout(400)


def capture(base_url: str, out: Path, tag: str, screens: list[str]) -> list[Path]:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for theme in ("dark", "light"):
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(base_url, wait_until="networkidle")
                _set_theme(page, theme)
                page.wait_for_timeout(1200)

                if "overview" in screens:
                    _goto_page(page, "overview")
                    path = out / f"{tag}-overview-{theme}.png"
                    page.screenshot(path=str(path), full_page=True)
                    saved.append(path)

                if "apps" in screens or "discovered" in screens or "detail" in screens:
                    _goto_page(page, "apps")

                if "apps" in screens:
                    path = out / f"{tag}-apps-{theme}.png"
                    page.screenshot(path=str(path), full_page=True)
                    saved.append(path)

                if "discovered" in screens:
                    page.evaluate(
                        "() => document.querySelector('#page-apps .subtab"
                        "[data-subtab=\"discovered\"]').click()"
                    )
                    page.wait_for_timeout(1800)
                    path = out / f"{tag}-discovered-{theme}.png"
                    page.screenshot(path=str(path), full_page=True)
                    saved.append(path)

                if "detail" in screens:
                    page.evaluate(
                        "() => document.querySelector('#page-apps .subtab"
                        "[data-subtab=\"apps\"]').click()"
                    )
                    page.wait_for_timeout(1500)
                    rows = page.query_selector_all("#apps-list-body tr.apps-row")
                    if rows:
                        page.evaluate("() => document.querySelector('#apps-list-body tr.apps-row').click()")
                        page.wait_for_timeout(1800)
                        path = out / f"{tag}-app-detail-{theme}.png"
                        page.screenshot(path=str(path), full_page=True)
                        saved.append(path)

                if "usage" in screens:
                    _goto_page(page, "cost")
                    path = out / f"{tag}-usage-{theme}.png"
                    page.screenshot(path=str(path), full_page=True)
                    saved.append(path)

                page.close()
        finally:
            browser.close()
    return saved


def main() -> None:
    ap = argparse.ArgumentParser(description="Screenshot the admin UI over a fixture pod")
    ap.add_argument("--base-url", default="http://127.0.0.1:5099")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", default="pod")
    ap.add_argument(
        "--screens",
        default="overview,apps,discovered,detail,usage",
        help="Comma-separated: overview, apps, discovered, detail, usage",
    )
    args = ap.parse_args()
    screens = [s.strip() for s in args.screens.split(",") if s.strip()]
    for path in capture(args.base_url, Path(args.out), args.tag, screens):
        print(path)


if __name__ == "__main__":
    main()
