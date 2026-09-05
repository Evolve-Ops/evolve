"""Browser smoke for the pod-first Apps surface (AL-1.8a).

internal/build-AL-1.8a-apps-shell.md §4. The page-level claims — pod-first
landing, the four lifecycle subtabs, no bot tab bar, honest placeholders
where data does not exist — are DOM facts, so they are checked here rather
than inferred from the route tests in
``packages/admin/tests/test_apps_pod_routes.py``.

WHY THE READS ARE STUBBED. The smoke harness boots the admin server
against a throwaway network.json with no bots, and a bot's manifests live
in that bot's own home directory — there is no pod to point it at inside
CI. Stubbing the four ``/api/apps*`` reads is therefore not a way to dodge
the real thing: it is the only way to render a populated table at all, and
it makes the RENDER the thing under test (does a null cost become "not
measured"? does a row open detail?) while the route tests own the payload.
The stubs mirror the real payload shape field for field; a drift between
them shows up as a smoke failure the moment the renderer reads a key the
routes stopped sending. ``test_apps_reads_answer_with_the_documented_shape``
keeps the real routes honest, against the real server, with no stub.

AND WHY IN THE PAGE, NOT ON THE WIRE. The obvious mechanism — ``page.route``
— made this file flaky on the CI runners: a different subtab's table would
fail to appear about once per fifteen tests, always in firefox, never
reproducibly. Interception sits on the browser's network path, and these
tests share one admin-server subprocess whose slower boot requests occupy
firefox's per-host connection slots; a stubbed read then waits behind them.
Replacing the global ``api()`` helper the page modules already call takes
the network out of the loop entirely: the fixture is returned in-process,
so no connection pool, no service worker, and no interception ordering can
sit between the click and the render. Unstubbed paths (``/api/forge/jobs``
under Activity) fall through to the real helper, so nothing else changes.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)

from playwright.sync_api import expect  # noqa: E402  (after importorskip)


# ── Stub payloads — the exact shape routes_apps returns ─────────────────────

def _member(app_id: str, bot: str, *, cost, measured: bool, summary: str) -> dict:
    """One half of the grouped Morning Brief — the row it would be alone.

    ALPHA-3a: the pod stores a separate id per bot for an app each bot
    discovered for itself, and the grouped row carries these so the surface
    can split without a re-fetch.
    """
    return {
        "app_id": app_id, "name": "Morning Brief",
        "purpose": "Sends a short summary of the day each morning.",
        "kind": "scheduled", "audience": "everyone",
        "spec_version": 2026051910, "spec_source": "derived",
        "provenance": {"origin": "discovered", "at": "2026-05-19T08:00:00Z",
                       "from_bot": bot},
        "defined_since": "2026-05-19T08:00:00Z", "app_kind": "application",
        "status": "active", "bots_total": 1,
        "usage_measured_bots": 1 if measured else 0,
        "cost_7d": cost, "turns_7d": 6 if measured else None,
        "last_run": ({"state": "seen", "ts": "2026-08-19T09:00:00Z"} if measured
                     else {"state": "cant_measure", "ts": None}),
        "grouped": False, "grouped_app_ids": [app_id], "group_basis": None,
        "bots": [{"bot_id": bot, "manifest_stem": "morning-brief",
                  "spec_version": 2026051910, "status": "active",
                  "app_kind": "application", "config_summary": summary,
                  "usage_measured": measured,
                  "turns_7d": 6 if measured else None, "cost_7d": cost,
                  "grade_breakdown": ({"scheduled": {"turns": 6, "cost_estimated": 0.42}}
                                      if measured else {}),
                  "last_run": ({"state": "seen", "ts": "2026-08-19T09:00:00Z"}
                               if measured else {"state": "cant_measure", "ts": None})}],
    }


APPS_PAYLOAD = {
    "ok": True,
    "count": 2,
    "grouped": True,
    "bots": ["team-bot-a", "team-bot-c"],
    "usage_measured_bots": ["team-bot-a"],
    "usage_unmeasured_bots": ["team-bot-c"],
    "apps": [
        {
            "app_id": "morning-brief",
            "name": "Morning Brief",
            "purpose": "Sends a short summary of the day each morning.",
            "kind": "scheduled",
            "audience": "everyone",
            "spec_version": 2026051910,
            "spec_source": "derived",
            "provenance": {"origin": "discovered", "at": "2026-05-19T08:00:00Z",
                           "from_bot": "team-bot-a"},
            "defined_since": "2026-05-19T08:00:00Z",
            "app_kind": "application",
            "status": "active",
            "bots_total": 2,
            "usage_measured_bots": 1,
            "cost_7d": 0.42,
            "turns_7d": 6,
            "last_run": {"state": "seen", "ts": "2026-08-19T09:00:00Z"},
            # Each bot discovered this app for itself, so the pod holds two
            # ids for it; ALPHA-3a claims they are one app and sends one row.
            "grouped": True,
            "grouped_app_ids": ["morning-brief", "p-049bf7ab"],
            "group_basis": "name_and_files",
            "members": [
                _member("morning-brief", "team-bot-a", cost=0.42, measured=True,
                        summary="1 schedule · 2 files"),
                _member("p-049bf7ab", "team-bot-c", cost=None, measured=False,
                        summary="2 files"),
            ],
            "bots": [
                {"bot_id": "team-bot-a", "manifest_stem": "morning-brief",
                 "spec_version": 2026051910, "status": "active",
                 "app_kind": "application", "config_summary": "1 schedule · 2 files",
                 "usage_measured": True, "turns_7d": 6, "cost_7d": 0.42,
                 "grade_breakdown": {"scheduled": {"turns": 6, "cost_estimated": 0.42},
                                     "inferred": {"turns": 3, "cost_estimated": 0.01}},
                 "last_run": {"state": "seen", "ts": "2026-08-19T09:00:00Z"}},
                {"bot_id": "team-bot-c", "manifest_stem": "morning-brief",
                 "spec_version": 2026051910, "status": "active",
                 "app_kind": "application", "config_summary": "2 files",
                 "usage_measured": False, "turns_7d": None, "cost_7d": None,
                 "grade_breakdown": {},
                 "last_run": {"state": "cant_measure", "ts": None}},
            ],
        },
        {
            "app_id": "note-filer",
            "name": "Note Filer",
            "purpose": "Files meeting notes into the right folder.",
            "kind": "on_request",
            "audience": "everyone",
            "spec_version": 1,
            "spec_source": "derived",
            "provenance": {"origin": "authored", "at": ""},
            "defined_since": None,
            "app_kind": "application",
            "status": "active",
            "bots_total": 1,
            "usage_measured_bots": 0,
            "cost_7d": None,
            "turns_7d": None,
            "last_run": {"state": "cant_measure", "ts": None},
            "grouped": False,
            "grouped_app_ids": ["note-filer"],
            "group_basis": None,
            "bots": [
                {"bot_id": "team-bot-c", "manifest_stem": "note-filer",
                 "spec_version": 1, "status": "active", "app_kind": "application",
                 "config_summary": "no schedules or files recorded",
                 "usage_measured": False, "turns_7d": None, "cost_7d": None,
                 "grade_breakdown": {},
                 "last_run": {"state": "cant_measure", "ts": None}},
            ],
        },
    ],
}

DETAIL_PAYLOAD = dict(
    {k: v for k, v in APPS_PAYLOAD["apps"][0].items() if k != "members"},
    ok=True,
    definition_states={"team-bot-a": "defined", "team-bot-c": "defined"},
    # Empty, not ["evolve"]: the pod's own service account is never offered
    # as somewhere to install an app (ALPHA-3a / audit P4).
    bots_without=[],
    signals=[],
)

# The same app with the claim withdrawn (``?grouped=0``) — one bot, and
# team-bot-c honestly absent from THIS record.
DETAIL_SPLIT_PAYLOAD = dict(
    APPS_PAYLOAD["apps"][0]["members"][0],
    ok=True,
    definition_states={"team-bot-a": "defined"},
    bots_without=["team-bot-c"],
    signals=[],
)

DISCOVERED_PAYLOAD = {
    "ok": True,
    "count": 1,
    "drafts": [{
        "bot_id": "team-bot-a",
        "manifest_stem": "receipt-sorter",
        "draft_id": "draft-abc123",
        "name": "Receipt Sorter",
        "purpose": "Sorts receipts into monthly folders.",
        "evidence": ["files", "cron"],
        "app_kind": "application",
        "created_at": "2026-08-01T00:00:00Z",
        "readiness": None,
        "offer": None,
    }],
}

ACTIVITY_PAYLOAD = {
    "ok": True,
    "count": 1,
    "total": 1,
    "truncated": False,
    "entries": [{
        "kind": "authoring", "ts": "2026-08-19T10:00:00Z",
        "app_id": "morning-brief", "bot_id": "team-bot-a",
        "outcome": "complete", "detail": "3 tests passed", "job_id": "j-0001",
    }],
}


def _stub_apps_api(page) -> None:
    """Swap the SPA's global ``api()`` for one that answers the four reads.

    Falls through to the original helper for every other path, so the
    Activity tab's real ``/api/forge/jobs`` call still exercises the server.
    An unknown ``/api/apps/<id>`` returns the same 404-shaped body the route
    does, which is what the detail view's not-found path renders from.

    The full path INCLUDING its query is tried first, so ``?grouped=0`` gets
    its own fixture: ALPHA-3a's split path is a different answer from the
    server, not a client-side filter, and stubbing it to the grouped body
    would let a broken split pass.
    """
    page.evaluate(
        """(fixtures) => {
             const orig = window.api;
             window.api = async (method, path, body, extra) => {
               const full = String(path);
               const p = full.split('?')[0];
               for (const key of [full, p]) {
                 if (Object.prototype.hasOwnProperty.call(fixtures, key)) {
                   return JSON.parse(JSON.stringify(fixtures[key]));
                 }
               }
               if (p.startsWith('/api/apps/')) {
                 return { ok: false, error: 'app not found' };
               }
               return orig(method, path, body, extra);
             };
           }""",
        {
            "/api/apps": APPS_PAYLOAD,
            "/api/apps/discovered": DISCOVERED_PAYLOAD,
            "/api/apps/activity": ACTIVITY_PAYLOAD,
            "/api/apps/morning-brief": DETAIL_PAYLOAD,
            "/api/apps/morning-brief?grouped=0": DETAIL_SPLIT_PAYLOAD,
        },
    )


def _open_apps(page, admin_server: str, *, stub: bool = True):
    # Keep the SPA's service worker out of these tests. sw.js calls
    # ``skipWaiting()`` + ``clients.claim()``, so it takes control of the
    # already-open page a beat after load and every later request runs
    # through it — which changes both what the page fetches and when. The
    # reads under test are stubbed in-process (see _stub_apps_api), but the
    # unstubbed ones still go over the wire, and a claiming SW mid-test is
    # one more source of "the table never appeared". The context-level
    # ``service_workers="block"`` option is Chromium-only, so this blocks
    # registration from an init script instead and covers all three engines.
    page.add_init_script(
        "if (navigator.serviceWorker && navigator.serviceWorker.register) {"
        "  navigator.serviceWorker.register = () => new Promise(() => {});"
        "}"
    )
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    if stub:
        _stub_apps_api(page)
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


def _open_app(page, name: str):
    """Open an app's detail by clicking its NAME, not the row's centre.

    ``resp-table`` stacks its cells at this viewport, so a row's centre
    point lands inside the *Bots* cell — and every bot chip there calls
    ``stopPropagation()`` on purpose, because clicking a chip filters the
    list instead of opening the app. Whether the centre lands on a chip or
    on the padding beside one comes down to a few pixels of line-height
    rounding, so a centre click passes on chromium and silently does
    nothing on firefox and webkit. Clicking the app name is both what an
    operator does and stable across all three.
    """
    page.locator("#apps-list-body tbody tr", has_text=name).locator(
        "td[data-label='App'] div"
    ).first.click()


# ── The shell ───────────────────────────────────────────────────────────────


def test_apps_page_opens_pod_first_with_lifecycle_subtabs(page, admin_server: str) -> None:
    """Apps / Discovered / Gallery / Activity, and Apps is what you land on."""
    _open_apps(page, admin_server)
    labels = page.locator("#page-apps > .subtabs > .subtab").all_text_contents()
    assert [t.strip() for t in labels] == [
        "Apps", "Discovered", "Gallery", "Activity",
    ], f"unexpected Apps subtabs: {labels}"
    assert page.locator("#apps-apps.active").count() == 1, (
        "the pod-first Apps subtab is not the landing view"
    )


def test_old_bot_tab_bar_is_gone(page, admin_server: str) -> None:
    """Pod-first means the per-bot tab strip no longer exists (design §3)."""
    _open_apps(page, admin_server)
    assert page.locator("#cap-bot-tabs").count() == 0, (
        "the per-bot tab bar is still in the DOM"
    )
    assert page.locator("#new-cap-form").count() == 0, (
        "the inline New Application form survived — D-U4 retired it"
    )
    for retired in ("#apps-installed", "#apps-create", "#apps-forge-jobs"):
        assert page.locator(retired).count() == 0, f"{retired} still exists"


def test_bot_filter_is_a_select_defaulting_to_all_bots(page, admin_server: str) -> None:
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    options = page.locator("#apps-filter-bot option").all_text_contents()
    assert options[0].strip() == "All bots", f"first option is {options[0]!r}"
    assert page.locator("#apps-filter-bot").input_value() == "", (
        "the bot filter does not default to the pod view"
    )


# ── The Apps table ──────────────────────────────────────────────────────────


def test_one_row_per_app_across_bots(page, admin_server: str) -> None:
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    rows = page.locator("#apps-list-body tbody tr")
    # expect(), not a bare count(): the table is repainted by one innerHTML
    # assignment, and a count() taken mid-swap reads zero rows. That is a
    # flake, not a finding — the retrying assertion still fails for real if
    # the row count is genuinely wrong.
    expect(rows).to_have_count(2)
    first = rows.nth(0)
    assert "Morning Brief" in first.inner_text()
    # Both bots appear as chips on the single row.
    assert "team-bot-a" in first.inner_text() and "team-bot-c" in first.inner_text()


def test_unmeasured_cost_renders_as_not_measured_never_zero(page, admin_server: str) -> None:
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    note_filer = page.locator("#apps-list-body tbody tr", has_text="Note Filer")
    text = note_filer.inner_text()
    assert "not measured" in text, f"expected an honest placeholder, got: {text}"
    assert "$0.00" not in text, "an unmeasured app is showing a fabricated $0.00"


def test_absent_last_run_renders_cant_measure(page, admin_server: str) -> None:
    """Tri-state honesty: nothing on the pod can answer, so say so — and never
    claim the app failed to run (that verdict needs AL-2.1)."""
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    text = page.locator("#apps-list-body tbody tr", has_text="Note Filer").inner_text()
    assert "can't measure" in text, f"expected the tri-state placeholder, got: {text}"
    assert "did not run" not in text and "didn't run" not in text


def test_bot_filter_narrows_the_rows(page, admin_server: str) -> None:
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    page.select_option("#apps-filter-bot", "team-bot-a")
    rows = page.locator("#apps-list-body tbody tr")
    expect(rows).to_have_count(1)   # see test_one_row_per_app_across_bots
    assert "Morning Brief" in rows.nth(0).inner_text()


# ── One app on N bots (ALPHA-3a / audit B3) ─────────────────────────────────


def test_a_grouped_row_says_so_in_words_and_offers_a_way_out(
    page, admin_server: str,
) -> None:
    """The claim is visible, plain, and reversible — never a silent merge."""
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    row = page.locator("#apps-list-body tbody tr", has_text="Morning Brief")
    text = row.inner_text()
    assert "look like the same app" in text, (
        f"the grouped row does not state the claim: {text}"
    )
    expect(row.locator("text=Show separately")).to_be_visible()


def test_show_separately_splits_the_row_and_groups_back(
    page, admin_server: str,
) -> None:
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    rows = page.locator("#apps-list-body tbody tr")
    expect(rows).to_have_count(2)
    page.locator("#apps-list-body tbody tr", has_text="Morning Brief").locator(
        "text=Show separately").click()
    # One row per stored record now — three rows, not two.
    expect(rows).to_have_count(3)
    briefs = page.locator("#apps-list-body tbody tr", has_text="Morning Brief")
    expect(briefs).to_have_count(2)
    assert "Shown on its own" in briefs.nth(0).inner_text()
    # And back again: the split lives in the page, not on disk.
    briefs.nth(0).locator("text=Show together again").click()
    expect(rows).to_have_count(2)


def test_an_ungrouped_row_carries_no_grouping_language(
    page, admin_server: str,
) -> None:
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    text = page.locator("#apps-list-body tbody tr", has_text="Note Filer").inner_text()
    assert "look like the same app" not in text
    assert "Show separately" not in text


def test_detail_agrees_with_the_list_and_can_be_split(
    page, admin_server: str,
) -> None:
    """B3's evidence was a DETAIL page denying a bot that had the app."""
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    _open_app(page, "Morning Brief")
    page.wait_for_selector("#apps-detail-view table")
    detail = page.locator("#apps-detail-view")
    assert "look like the same app" in detail.inner_text()
    # Both bots are in the bots × facts table, and none is called absent.
    assert "Every bot on the pod has this app" in detail.inner_text()
    detail.locator("text=Show them separately").click()
    page.wait_for_selector("#apps-detail-view >> text=Show every copy together")
    split = detail.inner_text()
    assert "team-bot-c" in split, "the split view must still name the bot it lacks"
    assert "Not on" in split
    # A genuinely absent bot is where the disabled "Install to…" belongs.
    expect(page.locator("#apps-detail-view button[disabled]").first).to_be_visible()


# ── Detail ──────────────────────────────────────────────────────────────────


def test_row_opens_detail_with_bots_by_facts(page, admin_server: str) -> None:
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    _open_app(page, "Morning Brief")
    page.wait_for_selector("#apps-detail-view table")
    detail = page.locator("#apps-detail-view").inner_text()
    assert "Morning Brief" in detail
    assert "On this pod" in detail
    # One row per bot, each with its own facts.
    assert "1 schedule · 2 files" in detail
    # The bot with no rollup says so rather than showing a zero.
    assert "not measured" in detail
    assert "can't measure" in detail
    # Every bot HAS this app here — and the pod's own service account is not
    # counted as a bot that is missing it (ALPHA-3a / audit P4). The disabled
    # "Install to…" is exercised where a bot really is absent:
    # test_detail_agrees_with_the_list_and_can_be_split.
    assert "Every bot on the pod has this app" in detail


def test_detail_offers_the_raw_instance_behind_a_single_click(
    page, admin_server: str,
) -> None:
    """The 90-field manifest modal survives — as "advanced", not as the view."""
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    _open_app(page, "Morning Brief")
    page.wait_for_selector("#apps-detail-view table")
    raw = page.locator("#apps-detail-view button", has_text="Open raw instance")
    expect(raw).to_have_count(2)   # one per install


def test_detail_back_returns_to_the_pod_list(page, admin_server: str) -> None:
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    _open_app(page, "Morning Brief")
    page.wait_for_selector("#apps-detail-view table")
    page.locator("#apps-detail-view button", has_text="All apps").click()
    page.wait_for_selector("#apps-list-body table")
    assert page.locator("#apps-detail-view").is_hidden()


# ── Discovered ──────────────────────────────────────────────────────────────


def test_discovered_lists_drafts_with_explicit_not_yet_placeholders(
    page, admin_server: str,
) -> None:
    _open_apps(page, admin_server)
    page.locator("#page-apps .subtab[data-subtab='discovered']").click()
    page.wait_for_selector("#apps-discovered-body table")
    text = page.locator("#apps-discovered-body").inner_text()
    assert "Receipt Sorter" in text
    # Evidence derived from what exists.
    assert "Files" in text and "Schedule" in text
    # The two columns later chips fill, said out loud rather than left blank.
    assert "not yet scored" in text, "readiness placeholder missing (AL-1.6)"
    assert "not yet offered" in text, "offer placeholder missing (AL-1.7)"


# ── Activity ────────────────────────────────────────────────────────────────


def test_activity_shows_the_feed_and_keeps_the_forge_table(
    page, admin_server: str,
) -> None:
    _open_apps(page, admin_server)
    page.locator("#page-apps .subtab[data-subtab='activity']").click()
    page.wait_for_selector("#apps-activity-body table")
    assert "morning-brief" in page.locator("#apps-activity-body").inner_text()
    # Forge Jobs moved here rather than being dropped: the table and its
    # approval panel are still on the page.
    assert page.locator("#apps-activity #forge-jobs-table").count() == 1
    assert page.locator("#apps-activity #forge-approval-panel").count() == 1


# ── Cross-cutting ───────────────────────────────────────────────────────────


def test_new_app_button_opens_the_wizard(page, admin_server: str) -> None:
    _open_apps(page, admin_server)
    page.locator("#page-apps > .subtabs button", has_text="New app").click()
    # The wizard is a display:flex overlay (#create-app-wizard), not a
    # .modal-overlay.open — openCreateWizard() toggles the style directly.
    page.wait_for_selector("#create-app-wizard", state="visible", timeout=5000)


def test_no_manifest_field_names_reach_the_screen(page, admin_server: str) -> None:
    """design §7: the words are "defined" and "discovered", and an app is an
    app. A field name on screen is the surface leaking its storage."""
    _open_apps(page, admin_server)
    page.wait_for_selector("#apps-list-body table")
    for subtab in ("discovered", "activity"):
        page.locator(f"#page-apps .subtab[data-subtab='{subtab}']").click()
        page.wait_for_selector(f"#apps-{subtab}-body table")
    body = page.locator("#page-apps").inner_text()
    for jargon in ("definition_status", "manifest_stem", "app_kind", "spec_source",
                   # ALPHA-3a's own vocabulary, which is machinery and not a
                   # thing an operator has any use for.
                   "grouped_app_ids", "group_basis", "name_and_files",
                   "similarity", "Jaccard", "normalized"):
        assert jargon not in body, f"{jargon!r} leaked onto the Apps page"


def test_apps_reads_answer_with_the_documented_shape(page, admin_server: str) -> None:
    """The four routes are live on the real server (no stubs here)."""
    ctx = page.request
    listing = ctx.get(f"{admin_server}/api/apps")
    assert listing.status == 200
    body = listing.json()
    assert body["ok"] is True and isinstance(body["apps"], list)
    # An empty pod still reports which bots it could and could not measure —
    # a client must be able to say "not measured" without guessing.
    assert "usage_measured_bots" in body and "usage_unmeasured_bots" in body

    for path, key in (("/api/apps/discovered", "drafts"),
                      ("/api/apps/activity", "entries")):
        res = ctx.get(f"{admin_server}{path}")
        assert res.status == 200, f"{path} -> {res.status}"
        assert isinstance(res.json()[key], list)

    missing = ctx.get(f"{admin_server}/api/apps/definitely-not-an-app")
    assert missing.status == 404, "an unknown app must 404, not return an empty row"
