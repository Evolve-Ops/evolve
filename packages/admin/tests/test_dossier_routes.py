"""Tests for the Pod Intelligence reads + the one write (routes_dossier).

    GET  /api/dossier/current
    GET  /api/dossier/editions/<week_id>
    GET  /api/dossier/profile
    POST /api/dossier/profile

Design: internal/design-pod-dossier-2026-08-24.md · brief:
internal/dispatch/done/pod-intelligence-shell.md.

WHAT THESE PIN, and why each is a thing a later edit could quietly break:

  * **No field name can reach the screen.** ``values`` is keyed by schema
    fields; only ``FACT_LABELS`` may promote one to the card's face, and an
    unmapped field is DROPPED. A test that only checked "the label is
    pretty" would pass on a title-cased field name, so the assertion is
    that unmapped fields are absent and that every label clears the same
    acronym / field-name / jargon rules ``tools/readability-lint`` applies
    to the headline registry beside it.
  * **Tri-state survives the reader.** A ``null`` value comes back as
    ``measured: false``, never as ``0``. The synthesis layer is careful
    about this; a reader that coerced it would undo that care one layer
    from the operator's eye.
  * **Trends read editions, never producers.** Each history point is the
    number a PAST module set recorded. The fixture's past weeks carry
    numbers that could not be re-derived from anything else on disk, so a
    reader that started recomputing would fail here.
  * **The profile round-trips, and is bounded.** It is the page's only
    write; the store's caps and the id filter are exercised through HTTP,
    where a client actually reaches them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import routes_dossier as rd  # noqa: E402
from evolve_admin.web.routes_dossier import register_dossier_routes  # noqa: E402


# ── fixture pod: three weeks on record ─────────────────────────────────────


def _module(
    module_id: str,
    *,
    title: str,
    headline: str,
    current,
    previous=None,
    values: dict | None = None,
    critical: bool = False,
    measurable: bool = True,
    detail: dict | None = None,
    forward_note: str | None = None,
    remediation: dict | None = None,
) -> dict:
    """One module, shaped exactly as ``dossier.modules.build_module`` writes it."""
    trend = {
        "metric": "metric",
        "current": current,
        "previous": previous,
        "compared_with": None,
        "delta": None,
        "percent_change": (
            None if previous in (None, 0) or current is None
            else round((current - previous) / abs(previous), 4)
        ),
        "direction": (
            None if previous is None or current is None
            else ("up" if current > previous else
                  "down" if current < previous else "flat")
        ),
        "editions_looked_back": 0 if previous is None else 1,
        "available": previous is not None,
        "reason": None if previous is not None else "this is the first week on record",
    }
    return {
        "module_id": module_id,
        "title": title,
        "critical": critical,
        "measurable": measurable,
        "headline": headline,
        "values": values,
        "trend": trend,
        "forward_note": forward_note or f"Next week this space shows {module_id}.",
        "remediation": remediation,
        "detail": detail or {"source": "fixture"},
    }


def _module_set(week_id: str, first: str, last: str, modules: list[dict],
                *, complete: bool = True) -> dict:
    return {
        "schema_version": 1,
        "edition_id": week_id,
        "computed_at": "2026-08-29T10:40:39Z",
        "based_on": {
            "edition_id": week_id,
            "edition_sealed": complete,
            "editions_on_record": 1,
            "window": {
                "edition_id": week_id,
                "first_date": first,
                "last_date": last,
                "complete": complete,
                "days": 7,
            },
        },
        "modules": modules,
    }


def _apps(current, previous=None, *, top: list[tuple[str, int]] | None = None,
          untied=967) -> dict:
    values = {
        "apps_used": len(top or []),
        "requests_total": current,
        "requests_not_tied_to_an_app": untied,
        "top": [{"app_id": a.lower().replace(" ", "-"), "name": a, "requests": n}
                for a, n in (top or [])],
        # Deliberately unmapped: proof that an unlabelled field is dropped
        # rather than shown with its schema name.
        "internal_debug_counter": 41,
    }
    return _module(
        "apps_leaderboard", title="Apps leaderboard",
        headline="Morning Brief was the busiest of 3 apps this week.",
        current=current, previous=previous, values=values,
    )


def _cost(current, previous=None) -> dict:
    return _module(
        "cost_trajectory", title="Cost trajectory",
        headline="The pod spent about $24.37 this week.",
        current=current, previous=previous,
        values={"spend_this_week": current, "spend_display": f"${current:,.2f}",
                "bots_that_spent": 8, "models_used": 4},
    )


def _users_absent() -> dict:
    return _module(
        "users_activity", title="Users activity",
        headline="We cannot show this yet. Nothing here records who is using the pod.",
        current=None, values=None, measurable=False,
        detail={"missing_source": "usage_by_user"},
    )


def _users(current: int, *, top: list[tuple[str, int]]) -> dict:
    return _module(
        "users_activity", title="Users activity",
        headline="2 people used the pod this week.",
        current=current,
        values={"people": current, "requests_total": sum(n for _, n in top),
                "apps_with_a_named_user": 1, "people_held_back": 0,
                "top": [{"person_id": pid, "name": pid, "requests": n}
                        for pid, n in top]},
    )


def _reliability(*, days_ran: int, days_missed: int,
                 states: list[str] | None = None) -> dict:
    """The fire-history module, in the shape synthesis writes it."""
    cells = states or (["ran"] * days_ran + ["missed"] * days_missed)
    days = [{"date": f"2026-08-{i + 1:02d}", "state": st, "runs": 1 if st == "ran" else 0}
            for i, st in enumerate(cells)]
    return _module(
        "reliability_history", title="Reliability history",
        headline=f"Morning Brief ran on {days_ran} of the last "
                 f"{days_ran + days_missed} days.",
        current=days_missed,
        values={"times_ran": days_ran, "times_missed": days_missed,
                "apps_on_a_schedule": 2, "apps_with_no_record": 1,
                "top": [{"app_id": "morning-brief", "name": "Morning Brief",
                         "requests": days_ran},
                        {"app_id": "recap", "name": "Recap", "requests": 3}],
                "strip": {"app_id": "morning-brief", "name": "Morning Brief",
                          "days": days,
                          "missed_dates": [d["date"] for d in days
                                           if d["state"] == "missed"]}},
    )


def _cost_with_split(current, previous=None) -> dict:
    module = _cost(current, previous)
    module["values"]["by_bot_spend"] = [
        {"bot_id": "atlas", "name": "Atlas", "spend": 12.0,
         "spend_display": "$12.00"},
        {"bot_id": "team_bot_a", "name": "Team Bot A", "spend": 7.4,
         "spend_display": "$7.40"},
    ]
    return module


def _security_critical() -> dict:
    """A module the house marks critical. None of the four v1 modules is, so
    the fixture supplies one — the no-filter-bubble rule needs a subject."""
    return _module(
        "security_posture", title="Security posture",
        headline="One thing on the pod needs a closer look.",
        current=1, critical=True,
        values={"needs_attention_now": 1},
    )


@pytest.fixture()
def pod(tmp_path: Path):
    shared = tmp_path / "evolve"
    modules_dir = shared / "dossier" / "modules"
    modules_dir.mkdir(parents=True)

    weeks = [
        ("2026-W33", "2026-08-10", "2026-08-16",
         [_apps(64, top=[("Morning Brief", 22), ("Journal", 16)]), _cost(26.10),
          _users_absent(), _security_critical()]),
        ("2026-W34", "2026-08-17", "2026-08-23",
         [_apps(79, 64, top=[("Morning Brief", 29), ("Journal", 17)]),
          _cost(22.05, 26.10), _users_absent(), _security_critical()]),
        ("2026-W35", "2026-08-24", "2026-08-30",
         [_apps(93, 79, top=[("Morning Brief", 34), ("Journal", 22)]),
          _cost_with_split(19.40, 22.05),
          _reliability(days_ran=26, days_missed=2),
          _users(2, top=[("slack:U1", 41), ("slack:U2", 23)]),
          _security_critical()]),
    ]
    for week_id, first, last, mods in weeks:
        complete = week_id != "2026-W35"
        (modules_dir / f"{week_id}.json").write_text(
            json.dumps(_module_set(week_id, first, last, mods, complete=complete))
        )

    network = shared / "network.json"
    network.write_text(json.dumps({"sharedDir": str(shared)}))

    app = Flask(__name__)
    register_dossier_routes(app, network)
    app.testing = True
    return {"client": app.test_client(), "shared": shared, "network": network}


@pytest.fixture()
def empty_pod(tmp_path: Path):
    """A pod whose weekly writer has not run yet."""
    shared = tmp_path / "evolve"
    shared.mkdir(parents=True)
    network = shared / "network.json"
    network.write_text(json.dumps({"sharedDir": str(shared)}))
    app = Flask(__name__)
    register_dossier_routes(app, network)
    app.testing = True
    return {"client": app.test_client(), "shared": shared}


def _get(pod, path: str, expect: int = 200) -> dict:
    res = pod["client"].get(path)
    assert res.status_code == expect, res.data
    return res.get_json()


def _module_by_id(body: dict, module_id: str) -> dict:
    for m in body["modules"]:
        if m["module_id"] == module_id:
            return m
    raise AssertionError(f"{module_id} not in {[m['module_id'] for m in body['modules']]}")


# ── /api/dossier/current ───────────────────────────────────────────────────


def test_current_returns_the_newest_week_and_its_neighbours(pod):
    body = _get(pod, "/api/dossier/current")
    assert body["ok"] is True and body["available"] is True
    assert body["week"]["id"] == "2026-W35"
    assert body["week"]["is_current"] is True
    # The operator's word for a week is a span of dates, not an id.
    assert body["week"]["label"] == "Aug 24 – Aug 30"
    # The still-open week says so — a partial week must never read as a quiet one.
    assert body["week"]["week_finished"] is False
    assert [w["id"] for w in body["weeks"]] == ["2026-W35", "2026-W34", "2026-W33"]
    assert body["weeks_on_record"] == 3


def test_a_pod_with_no_weeks_says_so_rather_than_rendering_empty_cards(empty_pod):
    body = _get(empty_pod, "/api/dossier/current")
    assert body["ok"] is True
    assert body["available"] is False
    assert body["modules"] == []


def test_headline_is_passed_through_verbatim(pod):
    """The page never composes a sentence; it shows the one synthesis wrote."""
    apps = _module_by_id(_get(pod, "/api/dossier/current"), "apps_leaderboard")
    assert apps["headline"] == (
        "Morning Brief was the busiest of 3 apps this week."
    )


# ── facts: the only path from a schema field to the screen ─────────────────


def test_facts_carry_plain_labels_and_drop_unmapped_fields(pod):
    apps = _module_by_id(_get(pod, "/api/dossier/current"), "apps_leaderboard")
    labels = [f["label"] for f in apps["facts"]]
    assert labels == ["Requests to apps", "Apps people used",
                      "Requests not tied to an app"]
    blob = json.dumps(apps["facts"])
    # The field this module carries that has no label must not appear at all —
    # not raw, and not prettied into "Internal Debug Counter".
    assert "internal_debug_counter" not in blob
    assert "Internal Debug Counter" not in blob


def test_a_null_value_reads_as_not_measured_never_zero(pod):
    """Tri-state survives the reader (the whole point of the synthesis nulls)."""
    module = rd._module_view(
        {}, _module("apps_leaderboard", title="Apps", headline="h", current=None,
                    values={"requests_total": None, "apps_used": 0}), [],
    )
    facts = {f["label"]: f for f in module["facts"]}
    assert facts["Requests to apps"]["measured"] is False
    assert facts["Requests to apps"]["value"] is None
    # A real zero is still a real zero — the two are different facts.
    assert facts["Apps people used"] == {
        "label": "Apps people used", "value": "0", "measured": True,
    }


def test_money_is_spelled_the_way_the_headline_spells_it(pod):
    cost = _module_by_id(_get(pod, "/api/dossier/current"), "cost_trajectory")
    facts = {f["label"]: f["value"] for f in cost["facts"]}
    assert facts["Spent this week"] == "$19.40"
    # ...and the COUNTS on a money module are still counts. A module-wide
    # money rule rendered "8 bots" as "$8.00" on the live pod — a false
    # statement about the pod's spend, not a formatting slip.
    assert facts["Bots that spent"] == "8"
    assert facts["Kinds of model used"] == "4"
    assert [p["value_display"] for p in cost["history"]] == [
        "$26.10", "$22.05", "$19.40",
    ]


def test_each_fact_kind_spells_its_value_its_own_way():
    """The three kinds, exercised directly.

    ``MONEY`` has no entry in ``FACT_LABELS`` today — cost's one amount
    arrives pre-formatted from the synthesis — so without this the branch
    would ship unexercised, and the next module that needs a raw money field
    would be the one to discover it broken.
    """
    assert rd._display(rd.COUNT, 1234) == "1,234"
    assert rd._display(rd.MONEY, 24.3695) == "$24.37"
    assert rd._display(rd.MONEY, 0.004) == "$0.0040"   # sub-cent stays true
    assert rd._display(rd.TEXT, "$24.37") == "$24.37"
    assert rd._display(rd.COUNT, None) is None
    # A boolean is not a count. Rendering True as "1" would invent a number.
    assert rd._display(rd.COUNT, True) is None


def test_fact_labels_meet_the_tenth_grader_bar():
    """Every word this layer adds is scored by the gate's own rules.

    ``tools/readability-lint`` walks ``dossier.headlines``; these labels are
    noun phrases in the same register that live one package away, so the
    rules are applied here instead of leaving them ungated.
    """
    sys.path.insert(0, str(_ADMIN_DIR.parent / "analyzer"))
    from dossier import readability  # noqa: PLC0415

    strings = [label for rows in rd.FACT_LABELS.values() for _, label, _k in rows]
    strings += [rd.TREND_NO_MATCH, rd.TREND_FLAT]
    strings += list(rd.STRIP_WORDS.values())
    strings += [text for _state, text in rd.STRIP_LEGEND]
    strings += [unit for _f, _v, _k, unit in rd.BAR_SPECS.values()]
    offenders = {}
    for text in strings:
        bad = [f for f in readability.check(text)
               if f.rule in ("acronym", "field_name", "jargon")]
        if bad:
            offenders[text] = [f"{f.rule}: {f.detail}" for f in bad]
    assert not offenders, offenders


# ── trends: read from earlier weeks, never recomputed ──────────────────────


def test_history_is_lifted_from_earlier_module_sets(pod):
    apps = _module_by_id(_get(pod, "/api/dossier/current"), "apps_leaderboard")
    assert [p["week"] for p in apps["history"]] == [
        "2026-W33", "2026-W34", "2026-W35",
    ]
    assert [p["value"] for p in apps["history"]] == [64, 79, 93]
    assert [p["label"] for p in apps["history"]] == ["Aug 10", "Aug 17", "Aug 24"]
    assert apps["history_note"] is None


def test_a_first_week_says_the_line_arrives_next_week(tmp_path: Path):
    shared = tmp_path / "evolve"
    (shared / "dossier" / "modules").mkdir(parents=True)
    apps_module = _apps(93)
    (shared / "dossier" / "modules" / "2026-W35.json").write_text(json.dumps(
        _module_set("2026-W35", "2026-08-24", "2026-08-30", [apps_module])
    ))
    network = shared / "network.json"
    network.write_text(json.dumps({"sharedDir": str(shared)}))
    app = Flask(__name__)
    register_dossier_routes(app, network)
    app.testing = True
    body = app.test_client().get("/api/dossier/current").get_json()
    apps = body["modules"][0]
    assert len(apps["history"]) == 1
    # The card's forward line is the MODULE's own, from the gated registry —
    # not one sentence this layer writes for every card alike.
    assert apps["history_note"] == apps_module["forward_note"]
    # No chip: a pod's first week is said once, in the week bar.
    assert apps["trend_chip"] == {"text": None, "direction": None}
    assert body["week"]["first_week_note"]
    assert "first week on record" in body["week"]["first_week_note"]


def test_the_first_week_sentence_appears_exactly_once_on_the_page(tmp_path: Path):
    """THE DEDUP THIS BRIEF EXISTS TO PIN.

    The shipped page said "There is nothing to compare it with yet." on
    every card. Whatever the page says about a pod having no history, it
    says ONCE — anywhere else in the payload is a repeat an operator reads
    as noise.
    """
    shared = tmp_path / "evolve"
    (shared / "dossier" / "modules").mkdir(parents=True)
    (shared / "dossier" / "modules" / "2026-W35.json").write_text(json.dumps(
        _module_set("2026-W35", "2026-08-24", "2026-08-30", [
            _apps(93, top=[("Morning Brief", 34), ("Journal", 22)]),
            _cost_with_split(19.40),
            _reliability(days_ran=26, days_missed=2),
            _users(2, top=[("slack:U1", 41), ("slack:U2", 23)]),
        ])
    ))
    network = shared / "network.json"
    network.write_text(json.dumps({"sharedDir": str(shared)}))
    app = Flask(__name__)
    register_dossier_routes(app, network)
    app.testing = True
    body = app.test_client().get("/api/dossier/current").get_json()

    note = body["week"]["first_week_note"]
    assert note
    # Only the OPERATOR-FACING strings are checked: ``trend.reason`` is a
    # machine field a card never renders, and gating it would be pinning the
    # payload's vocabulary rather than the page's voice.
    spoken = json.dumps([
        {"headline": m["headline"], "history_note": m["history_note"],
         "chip": (m["trend_chip"] or {}).get("text"),
         "fix": (m["remediation"] or {}).get("note")}
        for m in body["modules"]
    ])
    assert note not in spoken
    for phrase in ("first week on record", "nothing to compare it with"):
        assert phrase not in spoken, phrase
    # And every card still points forward, in four different sentences.
    notes = [m["history_note"] for m in body["modules"]]
    assert all(notes) and len(set(notes)) == len(notes)


def test_a_settled_pod_is_not_told_it_is_new(pod):
    """The note is scoped to a pod with ONE week — nothing wider."""
    body = _get(pod, "/api/dossier/current")
    assert body["week"]["first_week_note"] is None


def test_an_unmeasurable_module_gets_no_series_and_no_bars(pod):
    # The earlier weeks are the ones whose per-person rollup had not landed.
    users = _module_by_id(
        _get(pod, "/api/dossier/editions/2026-W33"), "users_activity")
    assert users["measurable"] is False
    assert users["history"] == []
    assert users["bars"] is None
    assert users["facts"] == []


def test_trend_chip_states_direction_without_a_verdict(pod):
    cost = _module_by_id(_get(pod, "/api/dossier/current"), "cost_trajectory")
    assert cost["trend_chip"] == {"text": "down 12%", "direction": "down"}
    # No tone/severity field: nothing in the dossier declares whether "down"
    # is good for a given metric, so the reader never claims it does.
    assert set(cost["trend_chip"]) == {"text", "direction"}


# ── bars: a one-bar bar chart is a number with extra ink ───────────────────


def test_bars_render_only_with_two_or_more_rows(pod):
    apps = _module_by_id(_get(pod, "/api/dossier/current"), "apps_leaderboard")
    assert [r["label"] for r in apps["bars"]["rows"]] == ["Morning Brief", "Journal"]
    lone = rd._module_view({}, _apps(7, top=[("Security Scan", 7)]), [])
    assert lone["bars"] is None, "a single ranked app is a fact, not a chart"


def test_every_current_week_module_draws_something_without_any_history():
    """THE DEFECT: a page whose only pictures needed two weeks of history.

    Apps, users and cost each get bars from this week's own numbers, and
    reliability gets its 28-day strip. On a pod installed this morning that
    is four cards with four pictures — where the shipped page had none.
    """
    modules = [_apps(93, top=[("Morning Brief", 34), ("Journal", 22)]),
               _cost_with_split(19.40),
               _reliability(days_ran=26, days_missed=2),
               _users(2, top=[("slack:U1", 41), ("slack:U2", 23)])]
    views = [rd._module_view({}, m, []) for m in modules]
    assert [bool(v["bars"] or v["strip"]) for v in views] == [True] * 4


def test_a_money_split_is_spelled_as_money_and_a_count_as_a_count():
    """One shared rule would render "3 requests" as "$3.00" — not a rounding
    slip, a false statement about the pod's spend."""
    cost = rd._module_view({}, _cost_with_split(19.40), [])
    assert [r["value_display"] for r in cost["bars"]["rows"]] == ["$12.00", "$7.40"]
    apps = rd._module_view(
        {}, _apps(93, top=[("Morning Brief", 34), ("Journal", 22)]), [])
    assert [r["value_display"] for r in apps["bars"]["rows"]] == ["34", "22"]


def test_a_person_is_named_by_the_roster_at_read_time(monkeypatch):
    """One resolver, and it runs on the READ side.

    The spine stores the stable ``platform:senderId`` key; the name is looked
    up when the chart is drawn, by the same cache-only resolver the Usage
    page uses. A name stored in the spine would leave two names in five
    years of weekly records the first time someone changes theirs.
    """
    from evolve_admin import roster_resolver

    monkeypatch.setattr(
        roster_resolver, "resolve_display_name",
        lambda network, bot_id, channel, ext_id, shared: (
            ("Maya R.", "resolved_names") if ext_id == "U1" else (None, "none")
        ),
    )
    module = _users(2, top=[("slack:U1", 41), ("slack:U2", 23)])
    for row in module["values"]["top"]:
        row["bots"] = ["bot_a"]
    view = rd._module_view({}, module, [], rd._name_resolver({}, Path("/nope")))
    assert [r["label"] for r in view["bars"]["rows"]] == [
        "Maya R.", "Slack user · U2",
    ]


def test_an_unresolvable_person_never_shows_a_raw_rollup_key(monkeypatch):
    from evolve_admin import roster_resolver

    monkeypatch.setattr(roster_resolver, "resolve_display_name",
                        lambda *a, **k: (None, "allowlist_only"))
    module = _users(2, top=[("slack:U9XYZ", 41), ("slack:U2", 23)])
    view = rd._module_view({}, module, [], rd._name_resolver({}, Path("/nope")))
    labels = [r["label"] for r in view["bars"]["rows"]]
    assert all("slack:" not in label for label in labels), labels


def test_a_resolver_that_throws_costs_a_name_not_the_page(monkeypatch):
    from evolve_admin import roster_resolver

    def boom(*_a, **_k):
        raise RuntimeError("directory unreachable")

    monkeypatch.setattr(roster_resolver, "resolve_display_name", boom)
    module = _users(2, top=[("slack:U1", 41), ("slack:U2", 23)])
    view = rd._module_view({}, module, [], rd._name_resolver({}, Path("/nope")))
    assert len(view["bars"]["rows"]) == 2


# ── the day strip: a habit, with its misses named ──────────────────────────


def test_the_day_strip_carries_one_readable_cell_per_day(pod):
    rel = _module_by_id(_get(pod, "/api/dossier/current"), "reliability_history")
    strip = rel["strip"]
    assert len(strip["cells"]) == 28
    assert strip["label"] == "Morning Brief"
    # Every cell says its own words: no value on this page is reachable only
    # by hovering a coloured square.
    assert strip["cells"][0]["tip"] == "Aug 1 · ran"
    assert strip["cells"][-1]["tip"] == "Aug 28 · no run"
    assert {c["state"] for c in strip["cells"]} == {"ran", "missed"}


def test_the_misses_are_named_in_text_not_only_in_colour(pod):
    rel = _module_by_id(_get(pod, "/api/dossier/current"), "reliability_history")
    assert rel["strip"]["missed_label"] == "missed Aug 27, Aug 28"


def test_a_clean_run_has_no_miss_line_to_read(pod):
    view = rd._module_view({}, _reliability(days_ran=28, days_missed=0), [])
    assert view["strip"]["missed_label"] is None


def test_a_module_with_no_strip_is_not_given_an_empty_one(pod):
    apps = _module_by_id(_get(pod, "/api/dossier/current"), "apps_leaderboard")
    assert apps["strip"] is None


# ── explain and remediate ──────────────────────────────────────────────────


def test_a_card_that_names_a_gap_carries_the_door_to_the_fix():
    module = _apps(7, top=[("Security Scan", 7)])
    module["remediation"] = {
        "note": "You can turn this on from the Maintenance page.",
        "page": "maintenance",
    }
    view = rd._module_view({}, module, [])
    assert view["remediation"] == {
        "note": "You can turn this on from the Maintenance page.",
        "page": "maintenance",
    }


def test_a_remediation_without_a_sentence_is_dropped_rather_than_half_drawn():
    module = _apps(7, top=[("Security Scan", 7)])
    module["remediation"] = {"page": "maintenance"}
    assert rd._module_view({}, module, [])["remediation"] is None


# ── the critical flag is carried, never acted on here ──────────────────────


def test_critical_is_passed_through_untouched(pod):
    module = _module_by_id(_get(pod, "/api/dossier/current"), "security_posture")
    assert module["critical"] is True


# ── earlier weeks ──────────────────────────────────────────────────────────


def test_an_earlier_week_renders_in_the_same_shape(pod):
    body = _get(pod, "/api/dossier/editions/2026-W34")
    assert body["week"]["id"] == "2026-W34"
    assert body["week"]["is_current"] is False
    apps = _module_by_id(body, "apps_leaderboard")
    # Its series stops at its own week — a past week must not show the future.
    assert [p["week"] for p in apps["history"]] == ["2026-W33", "2026-W34"]


@pytest.mark.parametrize("bad", ["banana", "2026-W99", "2026-W5", "20260-W01"])
def test_a_malformed_week_id_is_rejected_before_it_touches_disk(pod, bad):
    """The id is parsed by the store's own validator, not string-matched.

    ``2026-W99`` is the interesting one: it is well-SHAPED and still not a
    week, so a route that only pattern-matched would go to disk with it.
    """
    body = _get(pod, f"/api/dossier/editions/{bad}", expect=400)
    assert body["ok"] is False


def test_a_traversal_shaped_week_id_never_reaches_the_handler(pod):
    """``<week_id>`` is a single path segment, so an escaped slash 404s at
    the router rather than arriving as a relative path."""
    res = pod["client"].get("/api/dossier/editions/..%2F..%2Fetc%2Fpasswd")
    assert res.status_code == 404


def test_a_week_not_on_record_is_a_404(pod):
    body = _get(pod, "/api/dossier/editions/2026-W01", expect=404)
    assert body["ok"] is False


# ── the profile: the page's one write ──────────────────────────────────────


def test_profile_starts_empty_and_round_trips(pod):
    body = _get(pod, "/api/dossier/profile")
    assert body["profile"]["order"] == []
    assert body["profile"]["hidden"] == []
    assert body["profile"]["ratings"] == {}

    res = pod["client"].post("/api/dossier/profile", json={
        "order": ["cost_trajectory", "apps_leaderboard"],
        "hidden": ["users_activity"],
        "ratings": {"cost_trajectory": "useful"},
    })
    assert res.status_code == 200, res.data
    saved = res.get_json()["profile"]
    assert saved["order"] == ["cost_trajectory", "apps_leaderboard"]
    assert saved["updated_at"].endswith("Z")

    again = _get(pod, "/api/dossier/profile")["profile"]
    assert again["order"] == ["cost_trajectory", "apps_leaderboard"]
    assert again["hidden"] == ["users_activity"]
    assert again["ratings"] == {"cost_trajectory": "useful"}


def test_profile_write_lands_beside_the_weeks_it_arranges(pod):
    pod["client"].post("/api/dossier/profile", json={"order": ["cost_trajectory"]})
    on_disk = pod["shared"] / "dossier" / "profile.json"
    assert on_disk.is_file()
    assert json.loads(on_disk.read_text())["schema_version"] == 1
    # Operator-readable, like the editions beside it. Nothing in it is secret.
    assert oct(on_disk.stat().st_mode & 0o777) == "0o644"


def test_profile_carries_no_bot_or_person_data(pod):
    """The one write is operator-scoped: module ids, an order, and a thumb."""
    pod["client"].post("/api/dossier/profile", json={
        "order": ["apps_leaderboard"], "ratings": {"apps_leaderboard": "useful"},
    })
    stored = json.loads((pod["shared"] / "dossier" / "profile.json").read_text())
    assert set(stored) == {"schema_version", "order", "hidden", "ratings",
                           "updated_at"}


def test_a_hidden_critical_module_is_still_STORED(pod):
    """Design §4a rule 2 is a RENDER rule, deliberately not a storage one.

    The store keeps the operator's preference faithfully; the page is where
    a critical module refuses to disappear. Enforcing it in both places
    would let the two disagree.
    """
    res = pod["client"].post("/api/dossier/profile",
                             json={"hidden": ["security_posture"]})
    assert res.get_json()["profile"]["hidden"] == ["security_posture"]


def test_profile_drops_junk_rather_than_rejecting_the_whole_write(pod):
    res = pod["client"].post("/api/dossier/profile", json={
        "order": ["apps_leaderboard", "../../etc/passwd", 7, "apps_leaderboard"],
        "ratings": {"cost_trajectory": "brilliant", "apps_leaderboard": "useful"},
        "hidden": "not-a-list",
    })
    saved = res.get_json()["profile"]
    assert saved["order"] == ["apps_leaderboard"]      # deduped, filtered
    assert saved["ratings"] == {"apps_leaderboard": "useful"}
    assert saved["hidden"] == []


def test_profile_is_bounded(pod):
    res = pod["client"].post("/api/dossier/profile", json={
        "order": [f"module_{i}" for i in range(500)],
    })
    assert len(res.get_json()["profile"]["order"]) == 64


def test_a_non_object_body_is_a_400(pod):
    res = pod["client"].post("/api/dossier/profile", json=["apps_leaderboard"])
    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_an_oversized_body_is_refused_before_it_is_parsed(pod):
    res = pod["client"].post(
        "/api/dossier/profile",
        data=json.dumps({"order": ["x" * (rd.MAX_PROFILE_BODY + 100)]}),
        content_type="application/json",
    )
    assert res.status_code == 413


def test_an_unreadable_profile_degrades_to_showing_everything(pod):
    """A corrupt preference file must not hide cards."""
    profile = pod["shared"] / "dossier" / "profile.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("{not json")
    body = _get(pod, "/api/dossier/profile")
    assert body["profile"]["hidden"] == []
    assert body["profile"]["order"] == []
