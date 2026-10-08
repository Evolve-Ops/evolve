"""The records surface at 390 px, in a real browser — light and dark.

Module: ``evolve_admin/web/app_records_routes.py``. Brief:
``records-surface-from-the-schema`` item 7 ("the 390 px layout smoke").

The pages are standalone (they link ``/static/css/base.css`` and reuse its
``resp-table`` primitive), so the smoke serves the real stylesheet beside the
real routes and asks Chromium three things at phone width: the table has
collapsed to the label/value card stack, nothing overflows the viewport, and
both themes render legible text (text is not the background colour).

Skipped when Playwright/Chromium is not installed — ``tests/browser`` is the
cross-engine home for that dependency and CI's ``browser-smoke`` provisions it.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from flask import Flask, send_from_directory  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from evolve_admin import app_ledger  # noqa: E402
from evolve_admin import app_spec_schema as ass  # noqa: E402
from evolve_admin import app_store  # noqa: E402
from evolve_admin.applications.app_spec import AppSpec  # noqa: E402
from evolve_admin.applications.app_spec_store import write_spec  # noqa: E402
from evolve_admin.web.app_records_routes import register_app_records_routes  # noqa: E402

_CSS = _ADMIN_DIR / "evolve_admin" / "web" / "static" / "css"
APP, BOT = "collection-tracker", "bot-a"
STORE = {"tables": {
    "games": {
        "description": "Games you own, want, or have passed on.",
        "columns": {"id": "text", "title": "text", "year": "int", "owned": "bool"},
        "key": ["id"], "tile": "spend_by_month",
        "rollups": {"current_status": {"rollup": "status", "map": {"acquired": "owned"}},
                    "spend_by_month": {"rollup": "sum", "period": "month", "kinds": ["acquired"]}},
    },
    "events": {"ledger": True, "of": "games", "kinds": ["acquired", "sold"]},
}}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("records-ui")
    shared = tmp / "evolve"
    shared.mkdir()
    (tmp / "network.json").write_text(json.dumps({"sharedDir": str(shared)}))
    write_spec(AppSpec.from_dict({"app_id": APP, "name": "Collection Tracker", "purpose": "p",
                                  "store": STORE}), shared)
    app_store.ensure_store(shared, APP, BOT, ass.validate_store(STORE))
    for i, title in enumerate(("Alpha Quest", "Beta Harbor", "Gamma Ridge")):
        app_store.records_put(shared, APP, "games",
                              {"id": f"g{i}", "title": title, "year": 2000 + i, "owned": True},
                              caller=app_store.PLATFORM, instance=BOT)
    app_ledger.ledger_append(shared, APP, {"thing_id": "g0", "kind": "acquired",
                                           "at": "2026-09-01", "by": "platform", "amount": 20,
                                           "counterparty": "A shop"},
                             caller=app_store.PLATFORM, instance=BOT)
    flask_app = Flask(__name__)
    register_app_records_routes(flask_app, tmp / "network.json")

    @flask_app.get("/static/css/<name>")
    def css(name):
        return send_from_directory(_CSS, name, mimetype="text/css")

    srv = make_server("127.0.0.1", 0, flask_app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browsers not installed here
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _open(browser, theme: str):
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    ctx.add_init_script(f"try{{localStorage.setItem('evolve-theme','{theme}')}}catch(e){{}}")
    return ctx, ctx.new_page()


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("path", [
    f"/apps/{APP}/records/games",
    f"/apps/{APP}/records/games/g0",
    f"/apps/{APP}/records/events",
])
def test_phone_layout_is_a_card_stack_with_no_overflow(server, browser, theme, path):
    ctx, page = _open(browser, theme)
    try:
        page.goto(server + path)
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == theme
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        if page.query_selector("table.resp-table"):
            # <thead> hidden and rows stacked: the primitive's <480px card mode.
            assert page.evaluate("getComputedStyle(document.querySelector('.resp-table thead')).display") == "none"
            assert page.evaluate("getComputedStyle(document.querySelector('.resp-table tbody tr')).display") == "block"
            first = page.query_selector(".resp-table tbody td")
            assert page.evaluate("el => getComputedStyle(el, '::before').content", first) != "none"
        colours = page.evaluate("""() => {
            const cs = getComputedStyle(document.querySelector('h1'));
            return [cs.color, getComputedStyle(document.body).backgroundColor];
        }""")
        assert colours[0] != colours[1]
    finally:
        ctx.close()


def test_screenshots_for_the_pr(server, browser):
    """Writes the light/dark pair when ``RECORDS_SURFACE_SHOTS`` names a directory."""
    out = os.environ.get("RECORDS_SURFACE_SHOTS")
    if not out:
        pytest.skip("set RECORDS_SURFACE_SHOTS=<dir> to write the screenshots")
    Path(out).mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        ctx, page = _open(browser, theme)
        try:
            page.goto(server + f"/apps/{APP}/records/games")
            page.screenshot(path=str(Path(out) / f"records-list-390-{theme}.png"), full_page=True)
            page.goto(server + f"/apps/{APP}/records/games/g0")
            page.screenshot(path=str(Path(out) / f"records-detail-390-{theme}.png"), full_page=True)
        finally:
            ctx.close()
