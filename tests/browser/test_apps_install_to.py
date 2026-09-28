"""Browser smoke for AL-3.2's Install to… / Update to vN dialog.

Brief: ``internal/dispatch/done/al-3-2-install-to.md``. The engine and the
route contract are pinned by ``packages/admin/tests/test_al_3_2_install_to.py``
and ``…_surface.py``; what belongs HERE is the render claims — does the button
turn on only where an install can happen, does the dialog preview before it
writes, and does the update refuse to enable its apply button while local
changes are unconfirmed. Those are DOM facts.

The reads are stubbed in-process for the reason ``test_apps_shell.py``'s
docstring records at length: the smoke harness boots the admin server against
a throwaway network.json with no bots, so there is no pod to render from, and
``page.route`` interception made these files flaky on the CI runners. The
stubs mirror the route payloads field for field.

BOTH THEMES, on the dialog itself. The style guide has no CI gate for theme
parity, and a modal is exactly the surface where a hard-coded colour hides
until an operator toggles — so the dialog is opened in dark AND light and its
background is asserted to actually differ.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)

from playwright.sync_api import expect  # noqa: E402  (after importorskip)

# The shell's own long-standing load-time errors, shared with
# ``test_theme_and_pages`` / ``test_smoke`` — two inline ``<script>nav(…)``
# tags in index.html that run before ``nav`` is defined. Filtered by the same
# list, from the same source, so this file cannot quietly widen it.
_BASELINE_PAGEERROR_SUBSTRINGS: tuple[str, ...] = (
    "nav is not defined",
    "Can't find variable: nav",
)


def _unexpected_errors(errors: "list[str]") -> "list[str]":
    return [e for e in errors
            if not any(sub in e for sub in _BASELINE_PAGEERROR_SUBSTRINGS)]

BOT_A = "team-bot-a"
BOT_C = "team-bot-c"
APP = "morning-brief"

_BOT_ROW = {
    "bot_id": BOT_A, "manifest_stem": APP, "spec_version": 2026051910,
    "status": "active", "app_kind": "application",
    "config_summary": "1 schedule · 2 files", "usage_measured": False,
    "turns_7d": None, "cost_7d": None, "grade_breakdown": {},
    "last_run": {"state": "cant_measure", "ts": None},
}

_APP_ROW = {
    "app_id": APP, "name": "Morning Brief",
    "purpose": "Sends a short summary of the day each morning.",
    "kind": "scheduled", "audience": "everyone",
    "spec_version": 2026051920, "spec_source": "vnext",
    "provenance": {"origin": "authored", "at": ""},
    "defined_since": None, "app_kind": "application", "status": "active",
    "bots_total": 1, "usage_measured_bots": 0,
    "cost_7d": None, "turns_7d": None,
    "last_run": {"state": "cant_measure", "ts": None},
    "bots": [_BOT_ROW],
}

APPS_PAYLOAD = {
    "ok": True, "count": 1, "bots": [BOT_A, BOT_C],
    "usage_measured_bots": [], "usage_unmeasured_bots": [BOT_A, BOT_C],
    "apps": [_APP_ROW],
}

DETAIL_PAYLOAD = dict(
    _APP_ROW, ok=True,
    definition_states={BOT_A: "defined"},
    bots_without=[BOT_C],
    signals=[],
    requires=None, exclusive_tools=[], requires_declared=0,
    package={"files": [], "sha_kind": None, "sha_kind_bot": None,
             "declared": 0, "hashed": 0},
    install={"state": "pack", "pack": True, "pack_dir": "/shared/apps/packs/x",
             "pack_files": 2, "sources": [BOT_A], "reason": "", "detail": ""},
)

# The same app with nothing to install — the button must stay OFF and say why.
DETAIL_UNAVAILABLE = dict(
    DETAIL_PAYLOAD,
    install={"state": "unavailable", "pack": False, "pack_dir": "",
             "pack_files": 0, "sources": [],
             "reason": "no bot on this pod has this app with any files to copy",
             "detail": "no_pack: morning-brief has no files-pack"},
)

INSTALL_PREVIEW = {
    "ok": True, "error": "", "refused": False, "app_id": APP, "bot_id": BOT_C,
    "pack_dir": "/shared/apps/packs/x", "dry_run": True, "needs_snapshot": False,
    "planned": [
        {"path": "scripts/steady.py", "rel": "scripts/steady.py", "mode": "0644",
         "source_sha": "a" * 64, "predicted_sha": "a" * 64, "state": "create",
         "current_sha": "", "basis": "none", "note": ""},
        {"path": "scripts/brief.py", "rel": "scripts/brief.py", "mode": "0644",
         "source_sha": "b" * 64, "predicted_sha": "c" * 64, "state": "create",
         "current_sha": "", "basis": "none", "note": ""},
    ],
    "installed": [], "failed": [], "snapshot": {},
    "proof": {"files": [], "source_shas": [], "explained": False,
              "unexplained": []},
    "manifest_path": "", "spec_version": 2026051920,
}

UPDATE_PREVIEW = {
    "ok": True, "error": "", "refused": False, "app_id": APP, "bot_id": BOT_A,
    "pack_dir": "/shared/apps/packs/x", "dry_run": True,
    "from_version": 2026051910, "to_version": 2026051920,
    "adapted": True, "bases": ["recorded_install"],
    "plan": [
        {"path": "scripts/steady.py", "rel": "scripts/steady.py", "mode": "0644",
         "source_sha": "a" * 64, "predicted_sha": "a" * 64,
         "state": "unadapted", "current_sha": "d" * 64,
         "basis": "recorded_install", "note": ""},
        {"path": "scripts/brief.py", "rel": "scripts/brief.py", "mode": "0644",
         "source_sha": "b" * 64, "predicted_sha": "c" * 64,
         "state": "adapted", "current_sha": "e" * 64,
         "basis": "recorded_install",
         "note": "changed on this bot since it was installed"},
    ],
    "conflicts": [
        {"path": "scripts/brief.py", "rel": "scripts/brief.py", "mode": "0644",
         "source_sha": "b" * 64, "predicted_sha": "c" * 64,
         "state": "adapted", "current_sha": "e" * 64,
         "basis": "recorded_install",
         "note": "changed on this bot since it was installed"},
    ],
    "removed_upstream": ["docs/old.md"],
    "applied": [], "failed": [], "manifest_path": "",
    "proof": {"files": [], "source_shas": [], "explained": False,
              "unexplained": []},
}

_FIXTURES = {
    "/api/apps": APPS_PAYLOAD,
    "/api/apps/discovered": {"ok": True, "drafts": [], "scans": None,
                             "scan_summary": None},
    f"/api/apps/{APP}": DETAIL_PAYLOAD,
    f"/api/apps/{APP}/install": INSTALL_PREVIEW,
    f"/api/apps/{APP}/update": UPDATE_PREVIEW,
}


def _open_detail(page, admin_server: str, fixtures=None):
    """Land on the Apps detail view with every read answered in-process."""
    page.add_init_script(
        "if (navigator.serviceWorker && navigator.serviceWorker.register) {"
        "  navigator.serviceWorker.register = () => new Promise(() => {});"
        "}"
    )
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.evaluate(
        """(fixtures) => {
             const orig = window.api;
             window.__calls = [];
             window.api = async (method, path, body, extra) => {
               const p = String(path).split('?')[0];
               window.__calls.push(method + ' ' + p + ' ' + JSON.stringify(body || {}));
               if (Object.prototype.hasOwnProperty.call(fixtures, p)) {
                 return JSON.parse(JSON.stringify(fixtures[p]));
               }
               if (method === 'POST') return { ok: true };
               if (p.startsWith('/api/apps/')) return { ok: false, error: 'not found' };
               return orig(method, path, body, extra);
             };
           }""",
        fixtures if fixtures is not None else _FIXTURES,
    )
    # The SPA's own boot-complete signal — see test_apps_detail_fill.py for
    # why this is a wait_for_function and not a sleep.
    page.wait_for_function(
        "() => (document.getElementById('last-refresh')?.textContent || '')"
        ".startsWith('As of')"
    )
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"apps\"]'))"
    )
    page.wait_for_selector("#apps-list-body table")
    page.evaluate(f"() => window.appsShowDetail('{APP}')")
    page.wait_for_selector("#apps-detail-view table")
    return page.locator("#apps-detail-view")


def _install_button(detail):
    return detail.locator("button", has_text="Install to…")


# ── the button's three states ───────────────────────────────────────────────


def test_install_to_is_enabled_when_a_pack_exists(page, admin_server: str):
    detail = _open_detail(page, admin_server)
    button = _install_button(detail)
    expect(button).to_be_visible()
    expect(button).to_be_enabled()


def test_install_to_is_off_and_says_why_when_there_is_nothing_to_install(
    page, admin_server: str,
):
    """An app with no files must not offer an install it cannot perform."""
    fixtures = dict(_FIXTURES)
    fixtures[f"/api/apps/{APP}"] = DETAIL_UNAVAILABLE
    detail = _open_detail(page, admin_server, fixtures)
    button = _install_button(detail)
    expect(button).to_be_disabled()
    title = button.get_attribute("title") or ""
    assert "no bot on this pod has this app with any files" in title
    # The engine's error class must NOT reach the screen (design §7).
    assert "no_pack" not in title and "files-pack" not in title


# ── install: preview before write ───────────────────────────────────────────


def test_choosing_a_bot_previews_and_writes_nothing(page, admin_server: str):
    detail = _open_detail(page, admin_server)
    _install_button(detail).click()
    modal = page.locator("#app-install-modal")
    expect(modal).to_have_class("modal-overlay open")

    page.select_option("#app-install-target", BOT_C)
    page.wait_for_selector("#app-install-plan table")
    expect(modal).to_contain_text("scripts/brief.py")
    expect(modal).to_contain_text("2 files would be written")

    # The ONLY call so far is a dry run. A preview that wrote would be the
    # whole point of the preview lost.
    calls = page.evaluate("() => window.__calls")
    posts = [c for c in calls if c.startswith("POST /api/apps/")]
    assert len(posts) == 1, posts
    assert '"dry_run":true' in posts[0].replace(", ", ",")


def test_the_apply_button_sends_a_real_install(page, admin_server: str):
    detail = _open_detail(page, admin_server)
    _install_button(detail).click()
    page.select_option("#app-install-target", BOT_C)
    page.wait_for_selector("#app-install-plan table")
    page.click("#app-install-plan button.btn-primary")
    page.wait_for_function(
        "() => window.__calls.filter(c => c.startsWith('POST /api/apps/')).length >= 2"
    )
    posts = page.evaluate(
        "() => window.__calls.filter(c => c.startsWith('POST /api/apps/'))")
    assert '"dry_run":false' in posts[-1].replace(", ", ",")


def test_the_dialog_closes_on_escape(page, admin_server: str):
    detail = _open_detail(page, admin_server)
    _install_button(detail).click()
    modal = page.locator("#app-install-modal")
    expect(modal).to_have_class("modal-overlay open")
    page.keyboard.press("Escape")
    expect(modal).to_have_class("modal-overlay")


# ── update: the merge, and the tick that gates it ───────────────────────────


def test_update_to_vn_shows_only_where_the_bot_is_behind(page, admin_server: str):
    detail = _open_detail(page, admin_server)
    expect(detail.locator("button", has_text="Update to")).to_be_visible()


def test_an_update_with_local_changes_cannot_be_applied_untouched(
    page, admin_server: str,
):
    """The apply button starts DISABLED and the tick is what enables it.

    D-L3: an update is a merge. A dialog where the destructive path is one
    click away from the safe one is a dialog that flattens by accident.
    """
    detail = _open_detail(page, admin_server)
    detail.locator("button", has_text="Update to").click()
    page.wait_for_selector("#app-update-apply")
    apply_button = page.locator("#app-update-apply")
    expect(apply_button).to_be_disabled()
    expect(page.locator("#app-install-modal")).to_contain_text(
        "1 file was changed on this bot")
    # A file the new version drops is reported, not silently removed.
    expect(page.locator("#app-install-modal")).to_contain_text("docs/old.md")

    page.check("#app-update-confirm")
    expect(apply_button).to_be_enabled()


def test_a_confirmed_update_sends_the_confirmation(page, admin_server: str):
    detail = _open_detail(page, admin_server)
    detail.locator("button", has_text="Update to").click()
    page.wait_for_selector("#app-update-apply")
    page.check("#app-update-confirm")
    page.click("#app-update-apply")
    page.wait_for_function(
        "() => window.__calls.filter(c => c.indexOf('/update') >= 0).length >= 2"
    )
    posts = page.evaluate(
        "() => window.__calls.filter(c => c.indexOf('/update') >= 0)")
    last = posts[-1].replace(", ", ",")
    assert '"dry_run":false' in last and '"confirm_overwrite":true' in last


# ── both themes ─────────────────────────────────────────────────────────────


def test_the_dialog_renders_in_both_themes(page, admin_server: str):
    """Dark and light, on the dialog itself — and no console errors in either.

    There is no CI gate for theme parity, and a modal is exactly where a
    hard-coded colour hides until an operator toggles.
    """
    # ``pageerror`` (uncaught JS), not ``console`` — the same signal
    # ``test_theme_and_pages`` watches. Console errors on this harness also
    # carry the shell's own 404s for assets the throwaway server does not
    # ship, which say nothing about the dialog.
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    detail = _open_detail(page, admin_server)
    _install_button(detail).click()
    page.select_option("#app-install-target", BOT_C)
    page.wait_for_selector("#app-install-plan table")

    seen = {}
    for _ in range(2):
        theme = page.evaluate(
            "() => document.documentElement.getAttribute('data-theme')")
        seen[theme] = page.evaluate(
            "() => getComputedStyle(document.querySelector("
            "'#app-install-modal .modal')).backgroundColor"
        )
        expect(page.locator("#app-install-modal")).to_contain_text("scripts/brief.py")
        page.evaluate("() => window.toggleTheme()")

    assert set(seen) == {"dark", "light"}, seen
    assert seen["dark"] != seen["light"], (
        f"the dialog painted the same background in both themes ({seen}) — "
        f"a hard-coded colour, not a token"
    )
    assert _unexpected_errors(errors) == [], _unexpected_errors(errors)


def test_the_fixture_payloads_match_the_route_shape():
    """The stubs are only worth anything if they are the real shape.

    Cheap guard: every key the dialog reads must exist in the fixture, so a
    route that renames one fails here rather than rendering blank in a smoke
    test that stubs the rename away.
    """
    for key in ("state", "sources", "reason", "detail"):
        assert key in DETAIL_PAYLOAD["install"]
    for key in ("planned", "needs_snapshot", "bot_id"):
        assert key in INSTALL_PREVIEW
    for key in ("plan", "conflicts", "removed_upstream", "bases",
                "from_version", "to_version"):
        assert key in UPDATE_PREVIEW
    json.dumps(_FIXTURES)   # the stubs must be JSON-serialisable
