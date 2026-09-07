"""Browser smoke for the Pod Intelligence page (design-pod-dossier D-T5/T9).

The claims this page makes are DOM facts, not payload facts, so they are
checked here against a real browser rather than inferred from
``packages/admin/tests/test_dossier_routes.py`` (which owns the payload):

  * the grid renders one card per module, headline first;
  * rating, reordering and turning a card down PERSIST — the reorder
    survives a full reload, which is the assertion that separates "the page
    remembers" from "the page repainted";
  * **no filter bubble** — a module the house marks ``critical`` renders even
    when the operator has turned it down (design §4a rule 2). This is the one
    behaviour a well-meaning refactor is most likely to "simplify" away,
    because every other module obeys the hidden list;
  * no field name reaches the card's face (D-T8), the expanded technical
    layer excepted — that is where the depth is supposed to live;
  * both themes paint the card surface, with no JS errors.

WHY THE WEEKS ARE WRITTEN TO DISK RATHER THAN STUBBED ON THE WIRE. The
harness boots a real admin server against a throwaway ``sharedDir``, and the
dossier reads plain JSON files out of it — so seeding three weeks there
exercises the REAL routes, the real store, and the real renderer end to end.
There is nothing to mock: the writer's output is a file, and a file is
exactly what a test can produce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)

from playwright.sync_api import expect  # noqa: E402  (after importorskip)


# Shared with test_theme_and_pages.py — the SPA's known boot-order noise.
_BASELINE_PAGEERROR_SUBSTRINGS: tuple[str, ...] = (
    "nav is not defined",
    "Can't find variable: nav",
)


# ── fixture weeks ──────────────────────────────────────────────────────────


def _trend(current, previous):
    return {
        "metric": "metric", "current": current, "previous": previous,
        "compared_with": None, "delta": None,
        "percent_change": (None if previous in (None, 0) or current is None
                           else round((current - previous) / abs(previous), 4)),
        "direction": (None if previous is None or current is None else
                      ("up" if current > previous else
                       "down" if current < previous else "flat")),
        "editions_looked_back": 0 if previous is None else 1,
        "available": previous is not None,
        "reason": None if previous is not None else "this is the first week on record",
    }


def _apps(total, previous=None):
    return {
        "module_id": "apps_leaderboard", "title": "Apps leaderboard",
        "critical": False, "measurable": True,
        "headline": f"Morning Brief was the busiest of 3 apps this week, used {total} times.",
        "values": {
            "apps_used": 3, "requests_total": total,
            "requests_not_tied_to_an_app": 12,
            "top": [{"app_id": "morning-brief", "name": "Morning Brief", "requests": 34},
                    {"app_id": "journal", "name": "Journal", "requests": 22}],
        },
        "trend": _trend(total, previous),
        "forward_note": "Next week this space shows which way app use is moving.",
        "remediation": {"note": "You can turn this on from the Maintenance page.",
                        "page": "maintenance"},
        "detail": {"source": "usage_by_app", "field": "per_app.apps.<app_id>.d7.turns"},
    }


def _reliability(missed, previous=None):
    """The fire-history module: a 28-day strip and one row per timed app.

    Written here in the shape ``dossier.modules`` writes it, because the
    strip is the one picture on this page that has no chart library behind
    it — it is 28 spans and a legend, and only a browser can say whether it
    laid out.
    """
    states = ["off"] * 3 + ["ran"] * 12 + ["missed"] + ["ran"] * 11 + ["missed"]
    return {
        "module_id": "reliability_history", "title": "Reliability history",
        "critical": False, "measurable": True,
        "headline": f"Morning Brief ran on 23 of the last 25 days, and missed {missed}.",
        "values": {
            "times_ran": 23, "times_missed": missed,
            "apps_on_a_schedule": 2, "apps_with_no_record": 0,
            "top": [{"app_id": "morning-brief", "name": "Morning Brief", "requests": 23},
                    {"app_id": "weekly-recap", "name": "Weekly Recap", "requests": 4}],
            "strip": {
                "app_id": "morning-brief", "name": "Morning Brief",
                "cadence_days": 1,
                "days": [{"date": f"2026-08-{i + 3:02d}", "state": st,
                          "runs": 1 if st == "ran" else 0}
                         for i, st in enumerate(states)],
                "missed_dates": ["2026-08-18", "2026-08-30"],
            },
        },
        "trend": _trend(missed, previous),
        "forward_note": "Next week this space shows whether misses are going up or down.",
        "remediation": None,
        "detail": {"source": "annotations", "field": "fires.apps.<app_id>.runs_by_date"},
    }


def _cost(total, previous=None):
    return {
        "module_id": "cost_trajectory", "title": "Cost trajectory",
        "critical": False, "measurable": True,
        "headline": f"The pod spent about ${total:,.2f} this week.",
        "values": {"spend_this_week": total, "spend_display": f"${total:,.2f}",
                   "bots_that_spent": 8, "models_used": 4,
                   "by_bot_spend": [
                       {"bot_id": "atlas", "name": "Atlas",
                        "spend": round(total * 0.61, 2),
                        "spend_display": f"${total * 0.61:,.2f}"},
                       {"bot_id": "team-bot-a", "name": "Team Bot A",
                        "spend": round(total * 0.39, 2),
                        "spend_display": f"${total * 0.39:,.2f}"}]},
        "trend": _trend(total, previous),
        "forward_note": "Next week this space shows which way spend is moving.",
        "remediation": None,
        "detail": {"source": "cost_rollup", "field": "costs.total_usd"},
    }


def _users(people, previous=None):
    return {
        "module_id": "users_activity", "title": "Users activity",
        "critical": False, "measurable": True,
        "headline": f"{people} people used the pod this week.",
        "values": {"people": people, "requests_total": 88,
                   "apps_with_a_named_user": 2, "people_held_back": 0,
                   "top": [{"person_id": "slack:U1", "name": "Maya R.",
                            "requests": 41, "bots": ["atlas"]},
                           {"person_id": "slack:U2", "name": "Jordan",
                            "requests": 23, "bots": ["atlas"]}]},
        "trend": _trend(people, previous),
        "forward_note": "Next week this space shows who is doing more and who is doing less.",
        "remediation": None,
        "detail": {"source": "usage_by_user", "field": "users.by_person.<person>.turns"},
    }


def _users_absent():
    return {
        "module_id": "users_activity", "title": "Users activity",
        "critical": False, "measurable": False,
        "headline": "We cannot show this yet. Nothing here records who is using the pod.",
        "values": None, "trend": _trend(None, None),
        "forward_note": "Next week this space shows who is doing more and who is doing less.",
        "remediation": None,
        "detail": {"missing_source": "usage_by_user"},
    }


def _security(count, previous=None):
    """The critical module. None of the four v1 modules is critical, so the
    no-filter-bubble rule needs a subject the fixture supplies itself."""
    return {
        "module_id": "security_posture", "title": "Security posture",
        "critical": True, "measurable": True,
        "headline": "One thing on the pod needs a closer look.",
        "values": {"needs_attention_now": count},
        "trend": _trend(count, previous),
        # No ``forward_note``: a module the reader has no registered forward
        # line for must degrade to no line at all, not to a made-up one.
        "detail": {"source": "signals_store"},
    }


def _week(week_id, first, last, modules, *, complete=True):
    return {
        "schema_version": 1, "edition_id": week_id,
        "computed_at": "2026-08-29T10:40:39Z",
        "based_on": {
            "edition_id": week_id, "edition_sealed": complete,
            "editions_on_record": 1,
            "window": {"edition_id": week_id, "first_date": first,
                       "last_date": last, "complete": complete, "days": 7},
        },
        "modules": modules,
    }


@pytest.fixture(scope="session")
def dossier_weeks(shared_dir: Path) -> None:
    """Three weeks on record, written where the running server reads them."""
    modules = shared_dir / "dossier" / "modules"
    modules.mkdir(parents=True, exist_ok=True)
    weeks = [
        ("2026-W33", "2026-08-10", "2026-08-16",
         [_apps(64), _cost(26.10), _users_absent()], True),
        ("2026-W34", "2026-08-17", "2026-08-23",
         [_apps(79, 64), _cost(22.05, 26.10), _users_absent()], True),
        # The critical module appears only in the newest week — which also
        # gives the page a card whose series is one point long, so the
        # young-pod sentence is exercised beside the trend lines.
        ("2026-W35", "2026-08-24", "2026-08-30",
         [_apps(93, 79), _reliability(2), _cost(19.40, 22.05), _users(4),
          _security(1)], False),
    ]
    for week_id, first, last, mods, complete in weeks:
        (modules / f"{week_id}.json").write_text(
            json.dumps(_week(week_id, first, last, mods, complete=complete))
        )


@pytest.fixture(autouse=True)
def _clean_profile(shared_dir: Path, dossier_weeks) -> None:
    """Each test starts with an operator who has arranged nothing."""
    profile = shared_dir / "dossier" / "profile.json"
    if profile.exists():
        profile.unlink()


# ── navigation helper ──────────────────────────────────────────────────────


def _open_intelligence(page, admin_server: str, *, errors: list | None = None):
    # Keep the service worker out of it — see test_apps_shell.py's note: a
    # worker that claims the page mid-test changes what is fetched and when.
    page.add_init_script(
        "if (navigator.serviceWorker && navigator.serviceWorker.register) {"
        "  navigator.serviceWorker.register = () => new Promise(() => {});"
        "}"
    )
    if errors is not None:
        page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # The SPA's boot fires a second nav(); clicking inside that window lands
    # on an element that has just gone display:none. #last-refresh gets its
    # stamp on the line before _restoreNav(), so it is the boot's own
    # completion signal rather than a sleep.
    page.wait_for_function(
        "() => (document.getElementById('last-refresh')?.textContent || '')"
        ".startsWith('As of')"
    )
    page.evaluate(
        "() => window.nav(document.querySelector"
        "('.nav-item[data-page=\"intelligence\"]'))"
    )
    page.wait_for_selector("#page-intelligence .pi-module")
    return page.locator("#page-intelligence")


def _card_ids(page) -> list[str]:
    return page.eval_on_selector_all(
        "#pi-grid .pi-module", "els => els.map(e => e.dataset.module)"
    )


def _unexpected(errors: list[str]) -> list[str]:
    return [e for e in errors
            if not any(s in e for s in _BASELINE_PAGEERROR_SUBSTRINGS)]


# ── the grid ───────────────────────────────────────────────────────────────


def test_the_page_renders_a_card_per_module_from_the_current_week(
    page, admin_server: str
) -> None:
    _open_intelligence(page, admin_server)
    assert _card_ids(page) == [
        "apps_leaderboard", "reliability_history", "cost_trajectory",
        "users_activity", "security_posture",
    ]
    expect(page.locator("#pi-grid .pi-module").first.locator(".pi-headline")).to_contain_text(
        "Morning Brief was the busiest"
    )


def test_the_week_chooser_lists_the_weeks_on_record(page, admin_server: str) -> None:
    _open_intelligence(page, admin_server)
    labels = page.eval_on_selector_all(
        "#pi-week option", "els => els.map(e => e.textContent.trim())"
    )
    assert labels[0].startswith("Aug 24 – Aug 30")
    assert "this week" in labels[0]
    assert len(labels) == 3
    # The word "edition" is ours, not the reader's (dossier.readability's
    # JARGON set). The operator sees weeks.
    assert "edition" not in page.locator("#pi-weekbar").inner_text().lower()


def test_switching_to_an_earlier_week_repaints_the_grid(page, admin_server: str) -> None:
    _open_intelligence(page, admin_server)
    page.select_option("#pi-week", "2026-W34")
    page.wait_for_function(
        "() => !document.querySelector('#pi-grid .pi-module[data-module=\"security_posture\"]')"
    )
    # W34 predates the critical module, so its card is legitimately absent.
    assert "security_posture" not in _card_ids(page)
    expect(page.locator("#pi-weekbar")).to_contain_text("Looking back")


def test_a_young_card_says_what_will_stand_where_its_line_goes(
    page, admin_server: str
) -> None:
    """One point of history is not a trend — so the card points forward.

    In the module's OWN words, not one sentence repeated down the grid: the
    page used to append "there is nothing to compare it with yet" to all
    four headlines, and a new operator's whole first screen said it four
    times.
    """
    _open_intelligence(page, admin_server)
    card = page.locator("#pi-grid .pi-module[data-module='users_activity']")
    expect(card.locator(".pi-viz-absent")).to_contain_text(
        "Next week this space shows who is doing more"
    )
    # ...and a card WITH history draws instead of explaining.
    cost = page.locator("#pi-grid .pi-module[data-module='cost_trajectory']")
    assert cost.locator(".pi-spark svg").count() == 1
    assert cost.locator(".pi-viz-absent").count() == 0

    # A module this server knows no forward line for gets NO line rather than
    # an invented one.
    security = page.locator("#pi-grid .pi-module[data-module='security_posture']")
    assert security.locator(".pi-viz-absent").count() == 0


def test_no_two_cards_say_the_same_forward_line(page, admin_server: str) -> None:
    _open_intelligence(page, admin_server)
    lines = page.eval_on_selector_all(
        "#pi-grid .pi-viz-absent", "els => els.map(e => e.innerText.trim())"
    )
    assert lines, "no card pointed forward at all"
    assert len(set(lines)) == len(lines), lines


def test_an_unmeasurable_card_shows_its_sentence_and_no_chart(
    page, admin_server: str
) -> None:
    """The earlier weeks are the ones whose per-person rollup had not landed."""
    page_root = _open_intelligence(page, admin_server)
    page.select_option("#pi-week", "2026-W34")
    page.wait_for_function(
        "() => (document.querySelector('#pi-weekbar')?.innerText || '')"
        ".includes('Looking back')"
    )
    card = page_root.locator(".pi-module[data-module='users_activity']")
    expect(card.locator(".pi-headline")).to_contain_text("We cannot show this yet")
    assert card.locator(".pi-viz").count() == 0
    assert card.locator(".pi-facts").count() == 0


# ── this week's own pictures (they need no history at all) ─────────────────


def test_every_card_draws_something_from_this_week_alone(
    page, admin_server: str
) -> None:
    """The defect this replaced: a page that drew nothing until week two.

    The bars and the day strip come out of the current week's own numbers,
    so a pod installed this morning still gets pictures.
    """
    _open_intelligence(page, admin_server)
    for module_id in ("apps_leaderboard", "cost_trajectory", "users_activity"):
        card = page.locator(f"#pi-grid .pi-module[data-module='{module_id}']")
        assert card.locator(".pi-bar-row").count() >= 2, module_id
    strip = page.locator(
        "#pi-grid .pi-module[data-module='reliability_history'] .pi-daystrip"
    )
    assert strip.locator(".pi-day").count() == 28


def test_the_day_strip_paints_three_states_and_names_its_misses(
    page, admin_server: str
) -> None:
    """A miss an operator can only find by hovering is not a named miss."""
    _open_intelligence(page, admin_server)
    card = page.locator("#pi-grid .pi-module[data-module='reliability_history']")
    # Scoped to the STRIP: the legend's swatches wear the same state classes
    # on purpose, so that a legend can never drift out of step with the
    # colours it explains.
    strip = card.locator(".pi-daystrip")
    assert strip.locator(".pi-day-ran").count() == 23
    assert strip.locator(".pi-day-missed").count() == 2
    assert strip.locator(".pi-day-off").count() == 3
    expect(card.locator(".pi-strip-legend")).to_contain_text("missed Aug 18, Aug 30")
    # Every cell carries its own words, for a reader who cannot see colour.
    first = strip.locator(".pi-day").first
    assert "Aug 3" in (first.get_attribute("aria-label") or "")


def test_a_money_split_is_spelled_as_money_and_a_count_as_a_count(
    page, admin_server: str
) -> None:
    _open_intelligence(page, admin_server)
    values = page.eval_on_selector_all(
        "#pi-grid .pi-module[data-module='cost_trajectory'] .pi-bar-val",
        "els => els.map(e => e.textContent.trim())",
    )
    assert values and all(v.startswith("$") for v in values), values
    counts = page.eval_on_selector_all(
        "#pi-grid .pi-module[data-module='apps_leaderboard'] .pi-bar-val",
        "els => els.map(e => e.textContent.trim())",
    )
    assert counts == ["34", "22"]


def test_a_card_that_names_a_gap_offers_the_door_to_the_fix(
    page, admin_server: str
) -> None:
    """Explain AND remediate — and never by printing a shell command."""
    _open_intelligence(page, admin_server)
    fix = page.locator(
        "#pi-grid .pi-module[data-module='apps_leaderboard'] .pi-remediation"
    )
    expect(fix).to_contain_text("Maintenance page")
    assert "sudo" not in fix.inner_text().lower()
    fix.get_by_role("button", name="Open Maintenance").click()
    page.wait_for_function(
        "() => document.getElementById('page-maintenance')"
        "?.classList.contains('active')"
    )


# ── the arrangement persists ───────────────────────────────────────────────


def test_rating_a_card_persists_across_a_reload(page, admin_server: str) -> None:
    _open_intelligence(page, admin_server)
    card = page.locator("#pi-grid .pi-module[data-module='cost_trajectory']")
    card.get_by_role("button", name="Useful").click()
    expect(card.get_by_role("button", name="Useful")).to_have_attribute(
        "aria-pressed", "true"
    )
    _open_intelligence(page, admin_server)
    again = page.locator("#pi-grid .pi-module[data-module='cost_trajectory']")
    expect(again.get_by_role("button", name="Useful")).to_have_attribute(
        "aria-pressed", "true"
    )


def test_reordering_survives_a_reload(page, admin_server: str) -> None:
    """The assertion that separates "remembers" from "just repainted"."""
    _open_intelligence(page, admin_server)
    before = _card_ids(page)
    page.locator(
        "#pi-grid .pi-module[data-module='cost_trajectory'] [aria-label='Move up']"
    ).click()
    moved = _card_ids(page)
    assert moved.index("cost_trajectory") == before.index("cost_trajectory") - 1
    assert moved != before

    _open_intelligence(page, admin_server)
    assert _card_ids(page) == moved, "the order did not come back from the server"


def test_turning_a_card_down_moves_it_to_the_tray_and_sticks(
    page, admin_server: str
) -> None:
    _open_intelligence(page, admin_server)
    page.locator(
        "#pi-grid .pi-module[data-module='users_activity']"
    ).get_by_role("button", name="Hide").click()
    page.wait_for_function(
        "() => !document.querySelector"
        "('#pi-grid .pi-module[data-module=\"users_activity\"]')"
    )
    expect(page.locator("#pi-tray")).to_contain_text("Users activity")

    _open_intelligence(page, admin_server)
    assert "users_activity" not in _card_ids(page)
    expect(page.locator("#pi-tray")).to_contain_text("Users activity")

    page.locator("#pi-tray").get_by_role("button", name="Show").click()
    page.wait_for_selector("#pi-grid .pi-module[data-module='users_activity']")


# ── no filter bubble (design §4a rule 2) ───────────────────────────────────


def test_a_critical_card_keeps_showing_when_it_is_turned_down(
    page, admin_server: str
) -> None:
    """Hide means collapse and de-emphasize — never silence.

    The operator least interested in the security card is the one who most
    needs it, so a critical module renders regardless of the hidden list.
    """
    _open_intelligence(page, admin_server)
    card = page.locator("#pi-grid .pi-module[data-module='security_posture']")
    card.get_by_role("button", name="Hide").click()
    page.wait_for_selector("#pi-grid .pi-module.pi-module-muted")

    still = page.locator("#pi-grid .pi-module[data-module='security_posture']")
    assert still.count() == 1, "a critical module was silenced by hiding it"
    expect(still).to_have_class(__import__("re").compile(r"pi-module-muted"))
    # Its headline survives the muting: turned down, not turned off.
    expect(still.locator(".pi-headline")).to_contain_text("needs a closer look")
    # And it is NOT parked in the tray — the tray implies "not on the page".
    assert "Security posture" not in page.locator("#pi-tray").inner_text()

    # The preference is genuinely stored — this is not the page ignoring the
    # click, it is the page obeying a higher rule.
    _open_intelligence(page, admin_server)
    reloaded = page.locator("#pi-grid .pi-module[data-module='security_posture']")
    expect(reloaded).to_have_class(__import__("re").compile(r"pi-module-muted"))


# ── the 10th-grader bar, as a DOM fact ─────────────────────────────────────


def test_no_field_name_reaches_a_cards_face(page, admin_server: str) -> None:
    """Schema words belong in the expanded detail, and nowhere else.

    The check strips ``.pi-tech`` before looking: D-T8 puts the technical
    depth there on purpose, so the assertion is about the card's FACE.
    """
    _open_intelligence(page, admin_server)
    faces = page.evaluate(
        """() => Array.from(document.querySelectorAll('#pi-grid .pi-module'))
             .map(card => {
               const clone = card.cloneNode(true);
               clone.querySelectorAll('.pi-tech').forEach(n => n.remove());
               return clone.innerText;
             })"""
    )
    text = "\n".join(faces) + "\n" + page.locator("#pi-weekbar").inner_text()
    import re as _re
    offenders = sorted(set(_re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", text)))
    assert not offenders, f"field names on a card face: {offenders}"
    # Two of our own words, specifically. "edition" is the page's name for a
    # week; "unattributed" is what the schema calls work with no app.
    for ours in ("edition", "unattributed", "rollup", "producer"):
        assert ours not in text.lower(), f"{ours!r} is our word, not the reader's"


def test_the_technical_layer_is_where_the_field_names_live(
    page, admin_server: str
) -> None:
    """The complement of the test above — depth exists, it is just folded."""
    _open_intelligence(page, admin_server)
    card = page.locator("#pi-grid .pi-module[data-module='apps_leaderboard']")
    card.locator(".pi-tech summary").click()
    expect(card.locator(".pi-tech-json")).to_contain_text("per_app.apps")


# ── both themes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_page_renders_in_both_themes_without_console_errors(
    page, admin_server: str, theme: str
) -> None:
    errors: list[str] = []
    failed: list[str] = []
    page.on("requestfailed", lambda r: failed.append(r.url))
    page.add_init_script(
        f"() => localStorage.setItem('evolve_theme', '{theme}')"
    )
    _open_intelligence(page, admin_server, errors=errors)
    page.evaluate(
        f"() => document.documentElement.setAttribute('data-theme', '{theme}')"
    )
    # A card must paint an explicit surface in either theme — a transparent
    # card is the silent light-theme regression this suite exists to catch.
    bg = page.evaluate(
        "() => getComputedStyle(document.querySelector"
        "('#pi-grid .pi-module')).backgroundColor"
    )
    assert bg not in ("rgba(0, 0, 0, 0)", "transparent"), (
        f"the module card has no background in the {theme} theme"
    )
    assert not _unexpected(errors), _unexpected(errors)
    # Console 404s are pre-existing shell noise on a bot-less harness pod
    # (the same reason test_theme_and_pages.py watches pageerror rather than
    # console); what must not fail is anything this page asked for.
    assert not [u for u in failed if "/api/dossier/" in u], failed
    page.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")


def test_card_and_page_backgrounds_differ_between_themes(
    page, admin_server: str
) -> None:
    _open_intelligence(page, admin_server)
    read = ("() => getComputedStyle(document.querySelector"
            "('#pi-grid .pi-module')).backgroundColor")
    page.evaluate("() => document.documentElement.setAttribute('data-theme','dark')")
    dark = page.evaluate(read)
    page.evaluate("() => document.documentElement.setAttribute('data-theme','light')")
    light = page.evaluate(read)
    assert dark != light, (
        f"the module card looks identical in both themes ({dark}) — it is "
        "using a literal color instead of var(--bg2)"
    )
    page.evaluate("() => document.documentElement.setAttribute('data-theme','dark')")
