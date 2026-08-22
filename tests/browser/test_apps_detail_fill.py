"""Browser smoke for AL-1.8b's detail fill (docs/build-AL-1.8b-detail-fill.md §4).

The Files panel, the Uses panel, the Discovered drawer and the Discovered
bot filter are all RENDER claims — "does a missing file say missing", "does a
credential show its name and nothing else", "does clicking a draft row open a
drawer with its evidence and the same two actions the row had". Those are DOM
facts and belong here; the payload shapes are pinned by
``packages/admin/tests/test_apps_detail_fill.py`` against the real routes.

The reads are stubbed in-process for the reason
``test_apps_shell.py``'s docstring records at length: the smoke harness boots
the admin server against a throwaway network.json with no bots, so there is no
pod to render from, and ``page.route`` interception made these files flaky on
the CI runners. Swapping the SPA's own ``api()`` helper takes the network out
of the loop entirely. The stubs mirror the route payloads field for field.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)

from playwright.sync_api import expect  # noqa: E402  (after importorskip)

BOT_A = "team-bot-a"
BOT_C = "team-bot-c"

APPS_PAYLOAD = {
    "ok": True, "count": 1,
    "bots": [BOT_A, BOT_C],
    "usage_measured_bots": [BOT_A],
    "usage_unmeasured_bots": [BOT_C],
    "apps": [{
        "app_id": "morning-brief", "name": "Morning Brief",
        "purpose": "Sends a short summary of the day each morning.",
        "kind": "scheduled", "audience": "everyone",
        "spec_version": 2026051910, "spec_source": "derived",
        "provenance": {"origin": "authored", "at": ""},
        "defined_since": None, "app_kind": "application", "status": "active",
        "bots_total": 2, "usage_measured_bots": 1,
        "cost_7d": None, "turns_7d": None,
        "last_run": {"state": "cant_measure", "ts": None},
        "bots": [
            {"bot_id": BOT_A, "manifest_stem": "morning-brief",
             "spec_version": 2026051910, "status": "active",
             "app_kind": "application", "config_summary": "1 schedule · 3 files",
             "usage_measured": False, "turns_7d": None, "cost_7d": None,
             "grade_breakdown": {},
             "last_run": {"state": "cant_measure", "ts": None}},
            {"bot_id": BOT_C, "manifest_stem": "morning-brief",
             "spec_version": 2026051910, "status": "active",
             "app_kind": "application", "config_summary": "3 files",
             "usage_measured": False, "turns_7d": None, "cost_7d": None,
             "grade_breakdown": {},
             "last_run": {"state": "cant_measure", "ts": None}},
        ],
    }],
}

DETAIL_PAYLOAD = dict(
    APPS_PAYLOAD["apps"][0],
    ok=True,
    definition_states={BOT_A: "defined", BOT_C: "defined"},
    bots_without=[],
    signals=[],
    requires={
        "skills": ["calendar-read"],
        "tools": ["calendar.list"],
        "integrations": ["google-calendar"],
        "secrets": ["GOOGLE_OAUTH_TOKEN"],
    },
    exclusive_tools=["brief.compose"],
    requires_declared=5,
    package={
        "sha_kind": "realized", "sha_kind_bot": BOT_A,
        "declared": 3, "hashed": 2,
        "files": [
            {"path": "apps/brief/run.py", "role": "vital_to_blueprint",
             "sha256": "a" * 64, "sha_kind": "realized",
             "bots": {BOT_A: {"state": "ok", "realized_sha": "a" * 64},
                      BOT_C: {"state": "ok", "realized_sha": "a" * 64}}},
            {"path": "apps/brief/helper.py", "role": "",
             "sha256": "b" * 64, "sha_kind": "realized",
             "bots": {BOT_A: {"state": "ok", "realized_sha": "b" * 64},
                      BOT_C: {"state": "missing",
                              "note": "declared but not on disk"}}},
            {"path": "apps/brief/local.py", "role": "", "sha256": "",
             "sha_kind": None,
             "bots": {BOT_A: {"state": "missing",
                              "note": "declared but not on disk"},
                      BOT_C: {"state": "cant_measure", "realized_sha": "c" * 64,
                              "note": "the app has no recorded digest to "
                                      "compare against"}}},
        ],
    },
)

DISCOVERED_PAYLOAD = {
    "ok": True, "count": 3, "bots": [BOT_A, BOT_C], "offers_readable": True,
    "drafts": [
        {"bot_id": BOT_A, "manifest_stem": "receipt-sorter",
         "draft_id": "d-receipts", "name": "Receipt Sorter",
         "purpose": "Sorts receipts into monthly folders.",
         "evidence": ["files", "cron"], "app_kind": "application",
         "created_at": "2026-08-01T00:00:00Z",
         "readiness": {"score": 82, "band": "ready", "dimensions": [],
                       "dimensions_measured": 1, "dimensions_total": 3,
                       "eligible_to_offer": True, "offer_threshold": 75,
                       "emerging_threshold": 35, "version": 2},
         "offer": {"state": "not_offered", "by": None, "at": None,
                   "until": None, "outcome": None, "to": None}},
        {"bot_id": BOT_A, "manifest_stem": "photo-tidy",
         "draft_id": None, "name": "Photo Tidy",
         "purpose": "", "evidence": ["memory"], "app_kind": "application",
         "created_at": None, "readiness": None,
         "offer": {"state": "snoozed", "by": None,
                   "at": "2026-08-20T09:00:00Z",
                   "until": "2036-09-01T09:00:00Z", "outcome": None,
                   "to": "bot_primary_user"}},
        {"bot_id": BOT_C, "manifest_stem": "standup-nudge",
         "draft_id": None, "name": "Standup Nudge",
         "purpose": "", "evidence": [], "app_kind": "application",
         "created_at": None, "readiness": None,
         "offer": {"state": "never", "by": "the primary user", "at": None,
                   "until": None, "outcome": None, "to": None}},
    ],
}

DRAFT_PAYLOAD = {
    "ok": True, "bot_id": BOT_A, "manifest_stem": "receipt-sorter",
    "draft_id": "d-receipts", "name": "Receipt Sorter",
    "purpose": "Sorts receipts into monthly folders.",
    "description": "Files receipts into monthly folders when asked.",
    "app_kind": "application", "created_at": "2026-08-01T00:00:00Z",
    "evidence": {
        "kinds": ["files", "cron", "memory"],
        "conversation_only": False,
        "files": ["workspace/receipts/inbox.md"], "files_total": 1,
        "schedules": [{"when": "0 9 * * *", "what": "receipts.py", "where": ""}],
        "schedules_total": 1,
        "memory": {"path": "HEARTBEAT.MD", "sections": ["## Receipts"]},
        "conversation": {"label": "sort my receipts", "days_seen": 6,
                         "window_days": 10, "occurrences": 9,
                         "center_hour": 9, "first_day": "2026-08-05",
                         "last_day": "2026-08-19",
                         "primary_requester": "the primary user"},
    },
    "readiness": DISCOVERED_PAYLOAD["drafts"][0]["readiness"],
    "offer": DISCOVERED_PAYLOAD["drafts"][0]["offer"],
}

_FIXTURES = {
    "/api/apps": APPS_PAYLOAD,
    "/api/apps/discovered": DISCOVERED_PAYLOAD,
    "/api/apps/morning-brief": DETAIL_PAYLOAD,
    "/api/apps/discovered/d-receipts": DRAFT_PAYLOAD,
}


def _open_apps(page, admin_server: str):
    """Land on Apps with the four reads answered in-process. See the docstring."""
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
               window.__calls.push(method + ' ' + p);
               if (Object.prototype.hasOwnProperty.call(fixtures, p)) {
                 return JSON.parse(JSON.stringify(fixtures[p]));
               }
               if (method === 'POST') return { ok: true };
               if (p.startsWith('/api/apps/')) {
                 return { ok: false, error: 'not found' };
               }
               return orig(method, path, body, extra);
             };
           }""",
        _FIXTURES,
    )
    # WAIT FOR THE SPA'S OWN BOOT TO FINISH BEFORE TOUCHING ANYTHING.
    # ``load()`` awaits eight fetches and *then* calls ``_restoreNav()``,
    # which fires a second ``nav()``. ``nav()`` removes ``.active`` from
    # every ``.page`` before re-adding it, so a click dispatched inside that
    # window lands on an element that has just become ``display:none`` — the
    # click is silently swallowed, no error raised, and the subtab never
    # switches. Measured on firefox: the inline ``onclick`` never ran,
    # ``subTab`` was never called, and the test failed waiting for a table
    # nothing had asked for. Chromium's faster event loop mostly closed the
    # window, which is exactly the cross-engine gap this suite exists for.
    #
    # ``#last-refresh`` gets its "As of …" stamp on the line immediately
    # before ``_restoreNav()``, so this is the boot's own completion signal
    # — not a sleep, and not a retry loop that would mask a real failure.
    page.wait_for_function(
        "() => (document.getElementById('last-refresh')?.textContent || '')"
        ".startsWith('As of')"
    )
    page.evaluate(
        "() => window.nav(document.querySelector('.nav-item[data-page=\"apps\"]'))"
    )
    return page.locator("#page-apps")


def _click_row(page, container: str, text: str) -> None:
    """Click a table row's first cell, re-resolving the element on each retry.

    Two engine-portability lessons in one helper, both learned from CI:

    * **A selector, not a captured locator.** These tables repaint with a
      single ``innerHTML`` assignment, so an element resolved a moment
      earlier can be detached by the time the click lands — the same repaint
      race 1.8a's suite documents on ``count()``. Handing ``page.click`` a
      selector makes Playwright re-resolve on every actionability retry;
      firefox hit the detached window where chromium did not.
    * **The first cell, not the row's centre.** A row's centre can land on a
      bot chip or on the actions cell, both of which stop propagation — so
      the click would be swallowed and the drawer would never open.
    """
    page.wait_for_selector(f"{container} tbody tr")
    page.click(f"{container} tbody tr:has-text('{text}') td:first-child")


def _open_detail(page, admin_server: str):
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    _click_row(page, "#apps-list-body", "Morning Brief")
    page.wait_for_selector("#apps-detail-view table")
    return page.locator("#apps-detail-view")


def _open_discovered(page, admin_server: str):
    _open_apps(page, admin_server)
    page.locator("#page-apps .subtab[data-subtab='discovered']").click()
    page.wait_for_selector("#apps-discovered-body table")


# ── Files panel (D-U6) ──────────────────────────────────────────────────────


def test_files_panel_lists_the_apps_files_with_a_column_per_bot(
    page, admin_server: str,
) -> None:
    detail = _open_detail(page, admin_server)
    text = detail.inner_text()
    assert "Files" in text
    for path in ("apps/brief/run.py", "apps/brief/helper.py", "apps/brief/local.py"):
        assert path in text, f"{path} missing from the Files panel"
    # The file is the row and each bot is a column — the whole point of D-U6.
    assert BOT_A in text and BOT_C in text


def test_a_missing_file_says_missing_on_the_bot_that_lost_it(
    page, admin_server: str,
) -> None:
    detail = _open_detail(page, admin_server)
    row = detail.locator("tr", has_text="apps/brief/helper.py")
    assert "missing" in row.inner_text(), row.inner_text()
    assert "ok" in row.inner_text(), "the bot that still has it must read ok"


def test_an_unhashed_file_says_not_hashed_and_never_ok(
    page, admin_server: str,
) -> None:
    """Tri-state: no recorded digest is not a match, and not a zero."""
    detail = _open_detail(page, admin_server)
    row = detail.locator("tr", has_text="apps/brief/local.py")
    text = row.inner_text()
    assert "not hashed" in text, text
    assert "can't measure" in text, text


def test_the_digest_column_says_which_digest_it_is_showing(
    page, admin_server: str,
) -> None:
    """AL-1.5c §9.2: the field has two carriers, so the header names one."""
    detail = _open_detail(page, admin_server)
    header = detail.locator("th", has_text="Digest").first.inner_text().lower()
    assert "copy" in header, f"the digest column is unlabelled: {header!r}"


# ── Uses panel ──────────────────────────────────────────────────────────────


def test_uses_panel_shows_the_four_groups_and_marks_exclusive_tools(
    page, admin_server: str,
) -> None:
    detail = _open_detail(page, admin_server)
    text = detail.inner_text()
    assert "What this app uses" in text
    for label in ("Skills", "Tools", "Integrations", "Credentials"):
        assert label in text, f"the {label} group is missing"
    assert "calendar-read" in text and "google-calendar" in text
    assert "brief.compose · exclusively" in text


def test_credentials_show_names_only(page, admin_server: str) -> None:
    """A name is the whole of what this panel may know about a credential."""
    detail = _open_detail(page, admin_server)
    assert "GOOGLE_OAUTH_TOKEN" in detail.inner_text()
    # Nothing anywhere on the page fetched or rendered a value: the only
    # credential-shaped string present is the name itself.
    body = page.locator("#page-apps").inner_text()
    assert body.count("GOOGLE_OAUTH_TOKEN") == 1


# ── Discovered: bot filter + drawer (D-U7) ─────────────────────────────────


def test_discovered_has_a_bot_filter_defaulting_to_all_bots(
    page, admin_server: str,
) -> None:
    _open_discovered(page, admin_server)
    options = page.locator("#apps-discovered-filter-bot option").all_text_contents()
    assert options[0].strip() == "All bots", f"first option is {options[0]!r}"
    assert page.locator("#apps-discovered-filter-bot").input_value() == ""


def test_the_bot_filter_narrows_the_drafts(page, admin_server: str) -> None:
    _open_discovered(page, admin_server)
    expect(page.locator("#apps-discovered-body tbody tr")).to_have_count(3)
    page.select_option("#apps-discovered-filter-bot", BOT_C)
    rows = page.locator("#apps-discovered-body tbody tr")
    expect(rows).to_have_count(1)
    assert "Standup Nudge" in rows.nth(0).inner_text()


def test_clicking_a_draft_opens_the_drawer_with_its_real_evidence(
    page, admin_server: str,
) -> None:
    _open_discovered(page, admin_server)
    _click_row(page, "#apps-discovered-body", "Receipt Sorter")
    page.wait_for_selector("#apps-draft-drawer.open")
    drawer = page.locator("#apps-draft-drawer").inner_text()
    assert "Receipt Sorter" in drawer
    assert "workspace/receipts/inbox.md" in drawer, "the file evidence is missing"
    assert "0 9 * * *" in drawer, "the schedule evidence is missing"
    assert "HEARTBEAT.MD" in drawer, "the standing-instruction evidence is missing"
    # The recurrence arithmetic, as a reason rather than as numbers.
    assert "sort my receipts" in drawer
    assert "6 of the last 10 days" in drawer
    assert "the primary user" in drawer


def test_the_drawer_offers_the_same_two_actions_the_row_does(
    page, admin_server: str,
) -> None:
    """D-U7: the same actions, no new ones."""
    _open_discovered(page, admin_server)
    _click_row(page, "#apps-discovered-body", "Receipt Sorter")
    page.wait_for_selector("#apps-draft-drawer.open")
    actions = page.locator("#apps-draft-drawer-actions button")
    expect(actions).to_have_count(2)
    labels = [t.strip() for t in actions.all_text_contents()]
    assert labels == ["Promote", "Never"], labels


def test_promote_from_the_drawer_posts_to_the_existing_endpoint(
    page, admin_server: str,
) -> None:
    _open_discovered(page, admin_server)
    _click_row(page, "#apps-discovered-body", "Receipt Sorter")
    page.wait_for_selector("#apps-draft-drawer.open")
    page.locator("#apps-draft-drawer-actions button", has_text="Promote").click()
    # The house confirm modal, never a native confirm() (style-guide §9.6) —
    # native dialogs are silently suppressed in the desktop app, so a
    # destructive action gated behind one would be a no-op there.
    page.wait_for_selector(".confirm-modal-overlay.open")
    page.locator('.confirm-modal-overlay [data-cm="ok"]').click()
    page.wait_for_function(
        "() => window.__calls.some(c => c.includes('/definition/promote'))"
    )


def test_the_drawer_closes_on_escape(page, admin_server: str) -> None:
    _open_discovered(page, admin_server)
    _click_row(page, "#apps-discovered-body", "Receipt Sorter")
    page.wait_for_selector("#apps-draft-drawer.open")
    page.keyboard.press("Escape")
    page.wait_for_selector("#apps-draft-drawer.open", state="detached", timeout=5000)


# ── Cross-cutting ───────────────────────────────────────────────────────────


def test_readiness_and_offer_render_their_own_states(page, admin_server: str) -> None:
    _open_discovered(page, admin_server)
    text = page.locator("#apps-discovered-body").inner_text()
    assert "82" in text, "a scored draft must show its score"
    assert "from 1 of 3 measures" in text, (
        "a composite standing on one measured dimension must say so"
    )
    assert "not yet scored" in text, "an unscored draft must still say so"
    assert "never" in text, "the user's standing 'never' must be visible"
    # A snooze expiry is in the future; ago() would render it "just now", so
    # this cell must show an absolute time.
    assert "quiet until" in text, "a deferred offer must say when it comes back"
    assert "just now" not in text


def test_no_manifest_field_names_reach_the_new_panels(
    page, admin_server: str,
) -> None:
    """design §7 / plex test: the storage never shows through.

    ONE page load, then subtab clicks — deliberately not ``_open_detail``
    followed by ``_open_discovered``, which would ``goto`` twice inside a
    single test. That reloaded the SPA mid-test and timed out on firefox in
    CI (chromium was happy, which is exactly the cross-engine gap this suite
    exists to catch). Both surfaces are reachable without a reload, and
    1.8a's equivalent test walks the subtabs the same way.
    """
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    _click_row(page, "#apps-list-body", "Morning Brief")
    page.wait_for_selector("#apps-detail-view table")
    detail = page.locator("#apps-detail-view").inner_text()

    page.locator("#page-apps .subtab[data-subtab='discovered']").click()
    page.wait_for_selector("#apps-discovered-body table")
    _click_row(page, "#apps-discovered-body", "Receipt Sorter")
    page.wait_for_selector("#apps-draft-drawer.open")
    surface = detail + page.locator("#apps-draft-drawer").inner_text()
    for jargon in ("definition_status", "manifest_stem", "sha_kind",
                   "conversation_evidence", "do_not_offer", "package.files",
                   "exclusive_tools", "requires"):
        assert jargon not in surface, f"{jargon!r} leaked onto the Apps page"


def test_the_new_reads_answer_on_the_real_server(page, admin_server: str) -> None:
    """No stubs: the two AL-1.8b route shapes, live."""
    ctx = page.request
    missing = ctx.get(f"{admin_server}/api/apps/discovered/not-a-draft")
    assert missing.status == 404, "an unknown draft must 404, not return a blank row"
    listing = ctx.get(f"{admin_server}/api/apps/discovered")
    assert listing.status == 200
    body = listing.json()
    # The bot list the filter is built from, and the honesty flag the footer
    # reads, both have to be there on an empty pod too.
    assert isinstance(body["bots"], list)
    assert isinstance(body["offers_readable"], bool)
    assert json.dumps(body)  # serialisable, no surprises
