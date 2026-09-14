"""Tests for the dossier's synthesis layer — the first sentences it speaks.

What is pinned here, in the order the brief asked for it:

  1. each of the four v1 modules synthesizes from a fixture EDITION PAIR —
     the right headline and the right trend;
  2. a single-edition pod yields null trends that SAY they are null, rather
     than a direction invented from one data point;
  3. the readability gate reds on a jargon-seeded string — the failing case
     is what proves the gate has teeth, so it is asserted directly against
     the tool CI runs, not against a re-implementation of it;
  4. an absent producer yields a "we cannot show this yet" module carrying
     the reason — never a zero.

And two laws that only a test can hold down, because nothing about the code
shape prevents breaking them: trends read EDITIONS and never re-derive
history from producers, and every sentence that reaches an operator comes
from the gated registry.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from dossier import headlines as hl, readability, store
from dossier import modules as mod
from dossier.edition import build_edition
from dossier.window import resolve_timezone, window_for
from dossier_edition import main, run_edition

TZ = "America/Los_Angeles"
NOW_MIDWEEK = datetime(2026, 8, 27, 17, 0, tzinfo=timezone.utc)   # in 2026-W35
NOW_MONDAY = datetime(2026, 8, 31, 11, 10, tzinfo=timezone.utc)   # W35 complete

REPO_ROOT = Path(__file__).resolve().parents[3]


# ── edition fixtures (the synthesis layer is pure over these) ────────────────

FIRE_LAST_DATE = "2026-08-30"
FIRE_DATES = [
    (date(2026, 8, 30) - timedelta(days=i)).isoformat()
    for i in range(27, -1, -1)
]


def _fires(runs: dict[str, list[str]] | None,
           cron_only: tuple[str, ...] = ()) -> dict | None:
    """A ``fires`` block in the shape ``sources.collect_fires`` writes.

    ``runs`` maps app id to the dates a schedule fired. Coverage opens at
    each app's first run, exactly as the collector computes it, so a test
    can state days-ran and days-missed by writing dates rather than counts.
    """
    if runs is None:
        return None
    # Cadence and the due-date grid come from the collector's own helpers,
    # so a fixture edition cannot drift into a shape the writer never emits.
    from dossier.sources import _expected_dates, _observed_cadence

    apps = {}
    for app_id, dates in runs.items():
        ran = sorted(dates)
        cadence = _observed_cadence(ran)
        expected = _expected_dates(ran, FIRE_DATES, cadence)
        missed = [d for d in expected if d not in set(ran)]
        apps[app_id] = {
            "bots": ["bot_a"],
            "installed_by_evolve": app_id in cron_only or bool(cron_only),
            "runs_by_date": {d: 1 for d in ran},
            "first_run_date": ran[0], "last_run_date": ran[-1],
            "days_ran": len(ran), "cadence_days": cadence,
            "days_covered": len(expected) if expected else None,
            "days_missed": len(missed) if expected else None,
            "expected_dates": expected or None,
            "missed_dates": missed if expected else None,
            "runs_total": len(ran),
        }
    return {
        "source": "annotations (scheduled attribution) + app-cron-map",
        "grade": "scheduled",
        "window": {"days": 28, "first_date": FIRE_DATES[0],
                   "last_date": FIRE_LAST_DATE, "dates": FIRE_DATES,
                   "note": "UTC-dated annotation files"},
        "apps": apps,
        "apps_without_history": list(cron_only),
        "apps_installed_on_a_schedule": {a: ["bot_a"] for a in cron_only},
        "bots_without_annotations": [],
        "annotation_reader_available": True,
    }


def _edition(
    edition_id: str,
    *,
    apps: dict | None = None,
    untied: int | None = 0,
    open_signals: int | None = None,
    snoozed: int | None = None,
    opened: int | None = None,
    spend: float | None = None,
    people: list[str] | None = None,
    fires: dict[str, list[str]] | None = None,
    cron_only: tuple[str, ...] = (),
) -> dict:
    """A hand-built edition carrying only what the modules read.

    Hand-built rather than generated so a test can state the EXACT prior-week
    number a trend must find. ``None`` for a block means its producer was
    absent — the tri-state case each module has to handle.
    """
    per_app = None
    if apps is not None:
        per_app = {
            "source": "usage_by_app",
            "source_windows": {"as_of_date_by_bot": {"bot_a": "2026-08-27"}},
            "apps": {
                app_id: {"d7": {"turns": turns, "cost_estimated": 0.1,
                                "inferred_turns": 0},
                         "d30": {"turns": turns, "cost_estimated": 0.1,
                                 "inferred_turns": 0},
                         "bots": ["bot_a"], "last_seen_ts": None}
                for app_id, turns in apps.items()
            },
            "coverage": {
                # ``unattributed_turns_share`` is computed the way
                # ``sources._with_shares`` computes it — the number the
                # near-zero-coverage headline turns on, so a fixture that
                # hard-coded it null could never reach that branch.
                "d7": {"attributed_turns": sum(apps.values()),
                       "unattributed_turns": untied,
                       "app_turns_total": sum(apps.values()) + (untied or 0),
                       "unattributed_turns_share": (
                           round((untied or 0)
                                 / (sum(apps.values()) + (untied or 0)), 4)
                           if (sum(apps.values()) + (untied or 0)) else None)},
                "d30": {},
            },
            "coverage_by_bot": {},
            "bots_without_rollup": [],
        }

    signals = None
    if open_signals is not None:
        signals = {
            "source": "signals_store",
            "active": {"window": "point_in_time",
                       "total": open_signals + (snoozed or 0),
                       "by_state": {"firing": open_signals,
                                    "snoozed": snoozed or 0},
                       "by_severity": {"info": 0, "warn": open_signals,
                                       "alert": 0},
                       "by_producer": {"audit": open_signals},
                       "by_bot": {}},
            "transitions": (None if opened is None else
                            {"window": "edition_week", "total": opened,
                             "by_kind": {"opened": opened}, "by_producer": {}}),
        }

    costs = None
    if spend is not None:
        costs = {"source": "cost_rollup", "window": "edition_week",
                 "total_usd": spend, "event_count": 3, "days_in_window": 7,
                 "bots_with_data": 1, "by_model": {"claude-opus-5": {}},
                 "by_bot": {}}

    users = {"available": False, "producer": "usage_by_user", "window": "d7",
             "by_app": None, "by_person": None, "requesters": None,
             "requesters_withheld": None,
             "note": "no bot on the pod has written a per-person rollup"}
    if people is not None:
        users = {
            "available": True, "producer": "usage_by_user", "window": "d7",
            "by_person": {p: {"turns": 4 + i, "cost_estimated": 0.05,
                              "bots": ["bot_a"]}
                          for i, p in enumerate(people)},
            "by_app": {"morning_brief": {
                p: {"turns": 4, "cost_estimated": 0.05} for p in people}},
            "requesters": list(people), "requesters_withheld": 0, "note": "",
        }

    return {
        "schema_version": 1,
        "edition_id": edition_id,
        "computed_at": "2026-08-27T17:00:00Z",
        "sealed": True,
        "window": {"edition_id": edition_id, "timezone": TZ,
                   "first_date": "2026-08-24", "last_date": "2026-08-30",
                   "complete": True},
        "pod": {"roster": {"members": 2}},
        "per_bot": {},
        "per_app": per_app,
        "users": users,
        "drafts": None,
        "drift": None,
        "signals": signals,
        "fires": _fires(fires, cron_only),
        "costs": costs,
    }


#: A schedule that fired on every one of the window's 28 days but two.
PERFECT_RUN = FIRE_DATES
TWO_MISSES = [d for d in FIRE_DATES if d not in ("2026-08-16", "2026-08-25")]


def _full(edition_id: str, **kw) -> dict:
    """An edition where every one of the four modules has a source."""
    base = {"apps": {"morning_brief": 20}, "untied": 3, "open_signals": 5,
            "opened": 2, "spend": 1.0, "people": ["slack:U1", "slack:U2"],
            "fires": {"morning_brief": TWO_MISSES}}
    base.update(kw)
    return _edition(edition_id, **base)


def _module_ids(shared: Path) -> list[str]:
    """Module-set ids on disk. Local to the tests on purpose: the store
    exports no public lister while nothing in production reads module sets
    (the repo's no-uncalled-function gate, honoured rather than exempted)."""
    d = store.modules_dir(shared)
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def _by_id(payload: dict) -> dict[str, dict]:
    return {m["module_id"]: m for m in payload["modules"]}


def _modules(edition: dict, priors: list[dict] | None = None) -> dict[str, dict]:
    return _by_id(mod.build_modules(edition, priors or [], now=NOW_MIDWEEK))


# ── 1. each module synthesizes from an edition pair ──────────────────────────

def test_every_module_speaks_and_trends_against_the_previous_edition():
    prior = _full("2026-W34")
    now = _full("2026-W35", apps={"morning_brief": 40}, open_signals=3,
                spend=2.5, people=["slack:U1", "slack:U2", "slack:U3"],
                fires={"morning_brief": PERFECT_RUN})
    got = _modules(now, [prior])

    assert set(got) == {"apps_leaderboard", "reliability_history",
                        "cost_trajectory", "users_activity"}

    apps = got["apps_leaderboard"]
    assert apps["values"]["top"][0] == {
        "app_id": "morning_brief", "name": "Morning Brief", "requests": 40}
    assert apps["trend"]["previous"] == 20
    assert apps["trend"]["direction"] == "up"
    assert apps["trend"]["compared_with"] == "2026-W34"
    assert apps["headline"] == (
        "Morning Brief was the only app people used this week, 40 times. "
        "That is more than the week before, when it was 20."
    )

    rel = got["reliability_history"]
    assert rel["values"]["times_ran"] == 28
    assert rel["values"]["times_missed"] == 0
    assert rel["trend"]["direction"] == "down"     # two misses last week, none now
    assert rel["headline"] == (
        "Morning Brief ran every day for the last 28 days. "
        "That is less than the week before, when it was 2."
    )

    cost = got["cost_trajectory"]
    assert cost["values"]["spend_this_week"] == 2.5
    assert cost["trend"] == {
        "metric": "spend_this_week", "available": True, "current": 2.5,
        "previous": 1.0, "compared_with": "2026-W34", "delta": 1.5,
        "percent_change": 1.5, "direction": "up", "editions_looked_back": 1,
        "reason": None,
    }
    assert cost["headline"] == (
        "The pod spent about $2.50 this week. "
        "That is more than the week before, when it was $1.00."
    )

    users = got["users_activity"]
    assert users["values"]["people"] == 3
    assert users["headline"] == (
        "3 people used the pod this week. "
        "That is more than the week before, when it was 2."
    )


def test_a_small_wobble_reads_as_about_the_same():
    """Every week claiming a direction teaches its reader to stop looking."""
    prior = _full("2026-W34", spend=2.00)
    now = _full("2026-W35", spend=2.04)  # +2%, inside the flat band
    cost = _modules(now, [prior])["cost_trajectory"]
    assert cost["trend"]["direction"] == "flat"
    assert cost["trend"]["available"] is True     # measured, just not moving
    assert cost["headline"].endswith("That is about the same as the week before.")


def test_singular_and_plural_are_both_english():
    one = _modules(_full("2026-W35", people=["slack:U1"]))
    assert one["users_activity"]["headline"].startswith(
        "One person used the pod this week.")

    zero = _modules(_full("2026-W35", people=[]))
    assert zero["users_activity"]["headline"].startswith(
        "Nobody used the pod this week.")


def test_a_leaderboard_built_on_a_minority_of_the_work_says_so():
    """The live 2026-W35 shape: one app at 7 requests, 967 of them untied.

    Ranking the app without the caveat is the "lying by omission" the
    coverage counters exist to prevent — so the caveat is in the SENTENCE,
    not only in a detail block nobody reads.
    """
    apps = _modules(_full("2026-W35", apps={"security-cve-scan": 7},
                          untied=12))["apps_leaderboard"]
    assert apps["headline"].startswith(
        "Security Cve Scan led the apps this week with 7 requests, but most "
        "of the pod's work was not tied to any app.")
    assert apps["values"]["requests_not_tied_to_an_app"] == 12


# ── reliability speaks about scheduled runs, and about nothing else ─────────

def test_reliability_reports_fire_history_and_names_the_misses():
    """Design §4.1 / D-T11, and the defect the live page shipped with.

    "Ran N of the last M days, with the misses named" — the sentence the
    design asked for, over the data path that produces it.
    """
    rel = _modules(_full("2026-W35"))["reliability_history"]
    assert rel["headline"].startswith(
        "Morning Brief ran on 26 of the last 28 days, and missed 2.")
    assert rel["values"]["times_ran"] == 26
    assert rel["values"]["times_missed"] == 2
    strip = rel["values"]["strip"]
    assert len(strip["days"]) == 28
    assert strip["missed_dates"] == ["2026-08-16", "2026-08-25"]
    assert {d["state"] for d in strip["days"]} == {"ran", "missed"}


def test_reliability_never_substitutes_a_signal_count_for_a_run_history():
    """THE DEFECT THIS BRIEF CAME TO FIX.

    The shipped card read "93 things need attention" — a true number about a
    different question, standing under a title that promised scheduled-run
    history. A substitute metric wearing a module's name is worse than an
    empty card, so the module must not so much as glance at the signal store.
    """
    # A pod drowning in alerts and with a perfectly reliable schedule.
    rel = _modules(_full("2026-W35", open_signals=93, snoozed=78,
                         fires={"morning_brief": PERFECT_RUN}))
    rel = rel["reliability_history"]
    assert "93" not in rel["headline"]
    assert "attention" not in rel["headline"]
    assert rel["headline"].startswith("Morning Brief ran every day")
    assert "needs_attention_now" not in (rel["values"] or {})

    # …and the reverse: no alerts at all, one schedule that ran daily for a
    # week and then died. The old module called this pod healthy.
    dead = _modules(_edition("2026-W35", apps={}, open_signals=0,
                             fires={"morning_brief": FIRE_DATES[:7]}))
    assert dead["reliability_history"]["values"]["times_missed"] == 21
    assert dead["reliability_history"]["headline"].startswith(
        "Morning Brief ran on 7 of the last 28 days, and missed 21.")


def test_a_weekly_app_is_judged_weekly_not_daily():
    """A healthy weekly app measured daily is six misses in seven, forever.

    The cadence is read off the app's own runs — nothing on a real pod
    declares one — so a Monday app is due on Mondays and the six quiet days
    between are grey, not amber.
    """
    mondays = [d for d in FIRE_DATES if date.fromisoformat(d).weekday() == 0]
    rel = _modules(_edition("2026-W35",
                            fires={"weekly_recap": mondays}))["reliability_history"]
    assert rel["values"]["times_missed"] == 0
    assert rel["headline"].startswith(
        "Weekly Recap ran every time it was due over the last 28 days.")
    states = [d["state"] for d in rel["values"]["strip"]["days"]]
    assert states.count("ran") == len(mondays)
    assert states.count("missed") == 0
    assert states.count("off") == 28 - len(mondays)


def test_a_weekly_app_that_skipped_a_week_says_so_in_its_own_units():
    mondays = [d for d in FIRE_DATES if date.fromisoformat(d).weekday() == 0]
    rel = _modules(_edition("2026-W35", fires={"weekly_recap": mondays[:-1]},
                            ))["reliability_history"]
    assert rel["headline"].startswith(
        "Weekly Recap ran 3 of the 4 times it was due, and missed 1.")


def test_too_few_runs_to_read_a_rhythm_claims_no_misses_at_all():
    """A guessed schedule is how a healthy app gets a failing grade."""
    rel = _modules(_edition("2026-W35",
                            fires={"new_app": ["2026-08-28", "2026-08-30"]},
                            ))["reliability_history"]
    assert rel["headline"].startswith(
        "New App has run 2 times in the last 28 days.")
    assert rel["values"]["times_missed"] is None
    assert rel["values"]["strip"]["missed_dates"] == []


def test_an_app_with_a_schedule_and_no_runs_is_a_to_do_not_a_verdict():
    """Explain and remediate: the row says the MEASURING is missing, and
    the card carries the door to the fix."""
    rel = _modules(_edition("2026-W35", fires={},
                            cron_only=("weekly_recap",)))["reliability_history"]
    assert rel["measurable"] is True
    assert rel["headline"].startswith(
        "Nothing here has recorded a scheduled app running yet.")
    assert rel["values"]["apps_with_no_record"] == 1
    # Nulls, not zeros: nothing here knows whether it ran.
    assert rel["values"]["times_ran"] is None
    assert rel["values"]["times_missed"] is None
    assert rel["remediation"]["page"] == "maintenance"
    # NOT the same sentence the Apps card uses. Two cards on one screen can
    # both have a gap, and one line printed twice is the repetition this
    # page was just cured of.
    apps = _modules(_full("2026-W35", apps={"a": 1}, untied=999,
                          cron_only=("weekly_recap",)))
    assert (apps["reliability_history"]["remediation"]["note"]
            != apps["apps_leaderboard"]["remediation"]["note"])
    # The surface is named; a shell command never is.
    assert "sudo" not in json.dumps(rel)


def test_a_pod_that_simply_runs_nothing_on_a_timer_is_not_told_to_fix_it():
    """No cron, no fires — nothing is broken, so nothing is offered."""
    rel = _modules(_edition("2026-W35", fires={}))["reliability_history"]
    assert rel["remediation"] is None


def test_untied_requests_are_not_reported_as_no_usage():
    """The live 2026-W35 shape: zero ranked apps over a busy week.

    "The pod handled no app requests" would be the exact lie the coverage
    counters exist to prevent. Everything untied is also the near-zero case,
    so the headline names the repair — and the count it is talking about
    stays on the card's face rather than in the sentence.
    """
    apps = _modules(_full("2026-W35", apps={}, untied=147))["apps_leaderboard"]
    assert apps["measurable"] is True
    assert apps["values"]["apps_used"] == 0
    assert apps["values"]["requests_not_tied_to_an_app"] == 147
    assert "no app requests" not in apps["headline"]
    assert apps["headline"].startswith("Most of the pod's work was not tied")
    assert apps["remediation"]["page"] == "maintenance"


def test_a_busy_week_with_no_coverage_counter_still_says_what_it_saw():
    """Tri-state one level down: the rollup is present, its coverage SHARE is
    not. Without a share there is nothing to compare against the near-zero
    bar, so the card reports what it can see instead of claiming a diagnosis.
    """
    edition = _full("2026-W35", apps={}, untied=147)
    edition["per_app"]["coverage"]["d7"]["unattributed_turns_share"] = None
    apps = _modules(edition)["apps_leaderboard"]
    assert apps["headline"].startswith(
        "The pod handled 147 requests this week that were not tied to an app.")
    # Still offered the repair: no app ranked and a busy week is the same
    # gap, whether or not the producer wrote the ratio.
    assert apps["remediation"]["page"] == "maintenance"


def test_near_zero_credit_makes_the_repair_the_headline():
    """The live pod's shape, and what the operator should read on it.

    At 99% of the week's work untied, the leaderboard is noise and the
    missing credit is the story — so the state clause becomes the repair,
    and the card carries the door to the surface that turns it on.
    """
    apps = _modules(_full("2026-W35", apps={"security-cve-scan": 7},
                          untied=967))["apps_leaderboard"]
    assert apps["headline"].startswith(
        "Most of the pod's work was not tied to an app, and one repair step "
        "can fix that.")
    assert apps["remediation"]["page"] == "maintenance"
    assert "Maintenance" in apps["remediation"]["note"]
    # The detail names the JOB, never a command line: a web page does not
    # teach an operator to paste privileged shell.
    assert apps["detail"]["remediation"]["job"] == "usage-by-app"
    assert "sudo" not in json.dumps(apps)


def test_a_healthy_leaderboard_offers_no_repair():
    apps = _modules(_full("2026-W35"))["apps_leaderboard"]
    assert apps["remediation"] is None
    assert apps["headline"].startswith("Morning Brief was the only app")


def test_sub_cent_spend_is_not_rounded_into_a_false_zero():
    cost = _modules(_full("2026-W35", spend=0.004))["cost_trajectory"]
    assert "$0.0040" in cost["headline"]
    assert cost["values"]["spend_this_week"] == 0.004


# ── 2. a single-edition pod: null trends, stated honestly ────────────────────

def test_a_first_edition_records_a_null_trend_on_every_module():
    got = _modules(_full("2026-W35"))
    for module in got.values():
        trend = module["trend"]
        assert trend["available"] is False
        assert trend["previous"] is None
        assert trend["direction"] is None
        assert trend["delta"] is None
        assert trend["percent_change"] is None
        assert trend["compared_with"] is None
        assert trend["reason"] == "this is the first week on record"


def test_the_first_week_is_said_once_and_not_on_every_card():
    """THE DEFECT: four cards, four copies of one sentence.

    "There is nothing to compare it with yet." used to be appended to every
    module's headline, so the very first page a new operator saw repeated
    one line down its whole length. The FACT is unchanged and still in the
    trend block; the page says it once, in the week bar, and each card gets
    a forward line of its own instead.
    """
    got = _modules(_full("2026-W35"))
    for module_id, module in got.items():
        assert "nothing to compare" not in module["headline"], module_id
        assert "first week" not in module["headline"], module_id
        # Each card still says something about where its trend will appear —
        # in its OWN words, so four cards are not four copies.
        assert module["forward_note"].startswith("Next week"), module_id
    notes = {m["forward_note"] for m in got.values()}
    assert len(notes) == len(got)     # no two cards say the same line

    # The page-level sentence exists, is gated, and is said exactly once.
    once = hl.say("page.first_week")
    assert "first week on record" in once
    assert sum(once in m["headline"] for m in got.values()) == 0


def test_a_second_week_speaks_its_trend_again():
    """The suppression is scoped to "no earlier week exists", nothing wider."""
    got = _modules(_full("2026-W35", spend=2.0), [_full("2026-W34", spend=1.0)])
    assert got["cost_trajectory"]["headline"].endswith(
        "That is more than the week before, when it was $1.00.")


def test_a_prior_edition_that_never_measured_it_is_not_a_comparison():
    """A gap in the spine reads as a gap, not as a comparison to nothing."""
    prior = _full("2026-W34", spend=None)      # cost rollup absent that week
    cost = _modules(_full("2026-W35"), [prior])["cost_trajectory"]
    assert cost["trend"]["available"] is False
    assert cost["trend"]["compared_with"] is None
    assert "no earlier week" in cost["trend"]["reason"]
    assert cost["headline"].endswith(
        "Earlier weeks have nothing we can compare it with.")
    # The module beside it still trends fine — one blind producer does not
    # blind the whole set.
    assert _modules(_full("2026-W35"), [prior])["users_activity"]["trend"][
        "available"] is True


def test_the_lookback_is_bounded_so_an_old_week_is_not_called_last_week():
    priors = [_full(f"2026-W{w}", spend=None) for w in (34, 33, 32, 31)]
    priors.append(_full("2026-W30", spend=9.0))
    cost = _modules(_full("2026-W35"), priors)["cost_trajectory"]
    assert cost["trend"]["available"] is False
    assert cost["trend"]["editions_looked_back"] == mod.MAX_TREND_LOOKBACK


def test_a_zero_previous_week_yields_a_direction_but_no_percentage():
    prior = _full("2026-W34", spend=0.0)
    cost = _modules(_full("2026-W35", spend=3.0), [prior])["cost_trajectory"]
    assert cost["trend"]["direction"] == "up"
    assert cost["trend"]["percent_change"] is None   # not "infinite growth"


# ── 3. the readability gate has teeth ────────────────────────────────────────

def _load_lint():
    """Import ``tools/readability-lint`` — the exact file CI runs."""
    path = REPO_ROOT / "tools" / "readability-lint"
    spec = importlib.util.spec_from_loader(
        "readability_lint_under_test",
        importlib.machinery.SourceFileLoader(
            "readability_lint_under_test", str(path)),
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_gate_reds_on_a_jargon_seeded_headline(capsys, monkeypatch):
    """The failing case. Without this, the gate could be green by vacuity."""
    lint = _load_lint()
    monkeypatch.setitem(
        lint.HEADLINES, "apps.seeded",
        "The signals producer emitted a null unattributed_turns rollup.",
    )
    assert lint.main([]) == 1
    out = capsys.readouterr().out
    assert "apps.seeded" in out
    for rule in ("jargon", "field_name"):
        assert rule in out
    # …and the same registry without the seed is clean, so the red above is
    # the seed and not a pre-existing failure.
    assert lint.main([]) == 1
    monkeypatch.undo()
    capsys.readouterr()
    assert _load_lint().main([]) == 0


@pytest.mark.parametrize("text,rule", [
    ("The rollup had no data.", "jargon"),
    ("Spend is reported in USD.", "acronym"),
    ("Check per_app.coverage for the rest.", "field_name"),
    ("The appId column is empty.", "field_name"),
    ("One. Two. Three.", "sentences"),
    ("Notwithstanding the aforementioned instrumentation deficiencies, the "
     "comprehensive longitudinal accumulation demonstrates deterioration.",
     "grade"),
])
def test_each_rule_catches_its_own_bug_class(text, rule):
    assert rule in {f.rule for f in readability.check(text)}


def test_the_gate_reds_on_a_headline_built_from_a_literal(tmp_path):
    """The bypass the structural rule exists to close."""
    lint = _load_lint()
    good = tmp_path / "good.py"
    good.write_text('x = {"headline": say("cost.spend", spend="$1")}\n')
    assert lint._literal_headlines(good) == []

    bad = tmp_path / "bad.py"
    bad.write_text('x = {"headline": "The rollup emitted a null."}\n')
    assert lint._literal_headlines(bad) == [(1, "The rollup emitted a null.")]
    # …and the synthesizer that actually ships has none.
    assert lint._literal_headlines(lint._SYNTHESIZER) == []


def test_every_registered_sentence_meets_the_bar():
    for key, text in hl.HEADLINES.items():
        limit = readability.MAX_SENTENCES if key in hl.STANDALONE else 1
        assert readability.check(text, max_sentences=limit) == [], key


def test_every_rendered_headline_meets_the_bar():
    """The templates pass; this pins that what they RENDER to passes too."""
    editions = [
        _full("2026-W35"),
        _full("2026-W35", apps={}, untied=0, open_signals=0, spend=0.0,
              people=[]),
        _full("2026-W35", apps={"morning_brief": 12, "stand_up": 3}),
        _edition("2026-W35"),  # every producer absent
    ]
    for edition in editions:
        for priors in ([], [_full("2026-W34")]):
            for module in mod.build_modules(edition, priors,
                                            now=NOW_MIDWEEK)["modules"]:
                text = module["headline"]
                assert readability.check(text) == [], text


def test_an_unregistered_sentence_is_a_loud_failure():
    """A KeyError, not a quiet un-gated string."""
    with pytest.raises(KeyError, match="no registered headline"):
        hl.say("cost.improvised", spend="$1.00")


def test_an_app_id_is_said_out_loud_rather_than_printed_raw():
    assert hl.humanize_id("morning_brief") == "Morning Brief"
    assert hl.humanize_id("stand-up") == "Stand Up"
    assert hl.humanize_id("PTO_tracker") == "PTO Tracker"
    assert hl.humanize_id("") == "That app"


# ── 4. an absent producer cannot measure — and says why ──────────────────────

def test_an_absent_producer_yields_a_cannot_measure_module():
    got = _modules(_edition("2026-W35"))   # nothing on the pod has written
    for module_id, source in (
        ("apps_leaderboard", "usage-by-app rollup"),
        ("reliability_history", "turn annotations"),
        ("cost_trajectory", "daily cost rollups"),
        ("users_activity", "usage_by_user"),
    ):
        module = got[module_id]
        assert module["measurable"] is False, module_id
        assert module["values"] is None, module_id
        assert module["headline"].startswith("We cannot show this yet.")
        assert module["detail"]["missing_source"] == source
        assert module["detail"]["why"]
        assert module["trend"]["available"] is False
        assert module["trend"]["current"] is None


def test_a_cannot_measure_module_invents_no_numbers():
    """The tri-state law, in its strong form: no zero anywhere in the module."""
    for module in mod.build_modules(_edition("2026-W35"), [],
                                    now=NOW_MIDWEEK)["modules"]:
        # ``editions_looked_back`` counts OUR OWN lookback, not a producer's
        # number — zero editions really were there to look at. Every other
        # leaf in the module must be null rather than a manufactured 0.
        trend = {k: v for k, v in module["trend"].items()
                 if k != "editions_looked_back"}
        assert _zero_paths({**module, "trend": trend}) == [], module["module_id"]


def test_users_fills_itself_in_the_day_its_producer_lands():
    """Today's live shape is 'cannot show'; the code for the other side exists."""
    assert _modules(_edition("2026-W35"))["users_activity"]["measurable"] is False
    landed = _modules(_full("2026-W35", people=["slack:U1"]))["users_activity"]
    assert landed["measurable"] is True
    assert landed["values"]["people"] == 1
    # Names stay out of the sentence; the ids live in detail on purpose.
    assert "slack" not in landed["headline"]
    assert landed["detail"]["requesters"] == ["slack:U1"]


def test_a_present_producer_with_nothing_to_report_is_not_the_same_thing():
    """Zero is a measurement; null is the absence of one. Both are spoken."""
    measured = _modules(_full("2026-W35", spend=0.0))["cost_trajectory"]
    unmeasured = _modules(_edition("2026-W35"))["cost_trajectory"]
    assert measured["measurable"] is True
    assert measured["values"]["spend_this_week"] == 0
    assert measured["headline"].startswith("The pod spent nothing this week.")
    assert unmeasured["measurable"] is False
    assert unmeasured["headline"].startswith("We cannot show this yet.")


def _zero_paths(node, path="") -> list[str]:
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _zero_paths(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _zero_paths(v, f"{path}[{i}]")]
    if isinstance(node, (int, float)) and not isinstance(node, bool) and node == 0:
        return [path]
    return []


# ── the v1 cut, declared ─────────────────────────────────────────────────────

def test_the_v1_cut_is_the_four_modules_the_operator_chose_in_order():
    assert [spec.module_id for spec, _ in mod.SPECS] == [
        "apps_leaderboard", "reliability_history", "cost_trajectory",
        "users_activity",
    ]


def test_every_trend_names_a_number_the_module_actually_reports():
    """A direction whose metric is not in ``values`` is unreadable.

    ``trend.metric`` exists so a reader never has to guess WHICH of several
    values a direction refers to — which only works if the name resolves.
    """
    for module in mod.build_modules(_full("2026-W35"), [_full("2026-W34")],
                                    now=NOW_MIDWEEK)["modules"]:
        metric = module["trend"]["metric"]
        assert metric in module["values"], module["module_id"]
        assert module["values"][metric] == module["trend"]["current"]


def test_no_v1_module_is_critical_and_that_is_a_decision():
    """Design §4a rule 2: mark which are critical — in v1, none.

    A pod with no apps, no alert history, no cost rollup, or no per-person
    attribution is an EARLY pod, not a broken one. Marking any of these
    critical would turn "young" into "alarming". The field still ships so
    the first genuinely critical module needs no schema change.
    """
    assert [spec.critical for spec, _ in mod.SPECS] == [False] * 4
    for module in mod.build_modules(_edition("2026-W35"), [],
                                    now=NOW_MIDWEEK)["modules"]:
        assert module["critical"] is False


def test_a_module_set_is_json_serialisable():
    payload = mod.build_modules(_full("2026-W35"), [_full("2026-W34")],
                                now=NOW_MIDWEEK)
    assert json.loads(json.dumps(payload)) == payload
    assert payload["based_on"]["editions_on_record"] == 2
    assert payload["based_on"]["edition_id"] == "2026-W35"


# ── the weekly run: two files, one clock ─────────────────────────────────────

@pytest.fixture
def pod(tmp_path: Path) -> tuple[Path, dict]:
    """A pod whose producers have written enough for all four modules."""
    shared = tmp_path / "shared"
    (shared / "metrics" / "bot_a").mkdir(parents=True)
    (shared / "bot_a").mkdir(parents=True)
    (shared / "signals" / "firing").mkdir(parents=True)
    (shared / "signals" / "log").mkdir(parents=True)
    net = {"sharedDir": str(shared), "members": ["bot_a"], "primary": "bot_a",
           "timezone": TZ}
    (shared / "network.json").write_text(json.dumps(net))

    (shared / "metrics" / "bot_a" / "cost-2026-08-24.json").write_text(json.dumps({
        "total_usd": 0.5, "input_tokens": 100, "output_tokens": 20,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "event_count": 3,
        "by_model": {"claude-opus-5": {"cost_usd": 0.5, "event_count": 3}},
    }))
    (shared / "bot_a" / "usage-by-app.json").write_text(json.dumps({
        "as_of_date": "2026-08-27",
        "coverage": {"d7": {"attributed_turns": 10, "unattributed_turns": 6,
                            "app_turns_total": 18},
                     "d30": {}},
        "apps": {"morning_brief": {
            "d7": {"total": {"turns": 10, "cost_estimated": 0.2},
                   "inferred": {"turns": 0}},
            "d30": {"total": {"turns": 40, "cost_estimated": 0.9},
                    "inferred": {"turns": 0}}}},
    }))
    (shared / "signals" / "firing" / "sig1.json").write_text(json.dumps({
        "id": "sig1", "producer": "audit", "severity": "warn",
        "state": "firing", "bot_id": "bot_a",
    }))
    return shared, net


def _build_here(shared: Path, net: dict, *, now=NOW_MIDWEEK) -> dict:
    tz = resolve_timezone(net)
    cal = now.astimezone(tz).isocalendar()
    return build_edition(shared, net, window_for(cal[0], cal[1], tz, now=now),
                         now=now, bot_home=lambda b: Path("/nonexistent") / b)


def test_the_weekly_run_writes_a_module_set_beside_the_edition(pod):
    shared, net = pod
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)

    assert store.iter_edition_ids(shared) == ["2026-W35"]
    assert _module_ids(shared) == ["2026-W35"]
    payload = store.load_modules(shared, "2026-W35")
    got = _by_id(payload)
    assert got["apps_leaderboard"]["headline"].startswith("Morning Brief")
    assert got["cost_trajectory"]["values"]["spend_this_week"] == 0.5
    # The fixture pod has a signal store and no annotations: reliability
    # must read the SECOND one, and say it cannot measure without it.
    assert got["reliability_history"]["measurable"] is False
    assert got["users_activity"]["measurable"] is False
    assert payload["based_on"]["edition_computed_at"] == "2026-08-27T17:00:00Z"


def test_the_module_file_is_operator_readable(pod):
    shared, net = pod
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    mode = store.modules_path(shared, "2026-W35").stat().st_mode & 0o777
    assert mode == store.EDITION_MODE == 0o644


def test_a_dry_run_writes_neither_file(pod):
    shared, net = pod
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True, dry_run=True)
    assert store.iter_edition_ids(shared) == []
    assert _module_ids(shared) == []


def test_a_trend_reads_the_earlier_edition_not_the_producers_behind_it(pod):
    """Law 2, and the one that cannot be recovered if it is ever broken.

    The earlier edition is written with a spend of $4.00 while NO cost rollup
    for that week exists anywhere on disk. A synthesis that re-derived
    history would find nothing and report no trend; one that reads editions
    finds $4.00. Producers roll over — that is the whole reason the spine
    exists.
    """
    shared, net = pod
    store.write_edition(shared, _full("2026-W34", spend=4.0))
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)

    cost = _by_id(store.load_modules(shared, "2026-W35"))["cost_trajectory"]
    assert cost["trend"]["previous"] == 4.0
    assert cost["trend"]["compared_with"] == "2026-W34"
    assert cost["trend"]["direction"] == "down"      # 0.50 against 4.00
    assert not list((shared / "metrics" / "bot_a").glob("cost-2026-08-1*.json"))


def test_a_module_set_is_re_said_over_a_sealed_week_without_force(pod, capsys,
                                                                  monkeypatch):
    """Rule 3: the measurement is immutable, the wording is not."""
    shared, net = pod
    monkeypatch.setattr("dossier_edition.datetime", _FrozenDatetime(NOW_MONDAY))
    assert main(["--network", str(shared / "network.json")]) == 0
    edition_bytes = store.edition_path(shared, "2026-W35").read_bytes()
    capsys.readouterr()

    # A second ordinary run refuses to rewrite the sealed measurement…
    assert main(["--network", str(shared / "network.json")]) == 0
    assert "already recorded, not rewritten" in capsys.readouterr().out

    # …and --modules-only re-says the same week, edition untouched.
    assert main(["--network", str(shared / "network.json"),
                 "--week", "2026-W35", "--modules-only"]) == 0
    out = capsys.readouterr().out
    assert "re-said" in out and "Morning Brief" in out
    assert store.edition_path(shared, "2026-W35").read_bytes() == edition_bytes
    assert store.load_modules(shared, "2026-W35") is not None


def test_modules_only_refuses_a_week_that_was_never_measured(pod, capsys):
    shared, net = pod
    assert main(["--network", str(shared / "network.json"),
                 "--week", "2026-W30", "--modules-only"]) == 2
    assert "never measured" in capsys.readouterr().err
    assert _module_ids(shared) == []


def test_the_report_says_every_module_out_loud(pod, capsys, monkeypatch):
    """The operator-facing surface: including the ones that cannot be shown."""
    shared, net = pod
    monkeypatch.setattr("dossier_edition.datetime", _FrozenDatetime(NOW_MIDWEEK))
    assert main(["--network", str(shared / "network.json"), "--now",
                 "--report"]) == 0
    out = capsys.readouterr().out
    for title in ("Apps leaderboard", "Reliability history", "Cost trajectory",
                  "Users activity"):
        assert title in out
    assert "We cannot show this yet." in out       # users, on today's pods


def test_an_in_week_rerun_is_idempotent_for_the_module_set_too(pod):
    shared, net = pod
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    first = store.modules_path(shared, "2026-W35").read_text()
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    assert store.modules_path(shared, "2026-W35").read_text() == first


def test_the_prior_lookup_never_reads_the_week_it_is_writing(pod):
    shared, net = pod
    for eid in ("2026-W33", "2026-W34", "2026-W35"):
        store.write_edition(shared, _full(eid))
    priors = store.load_prior_editions(shared, "2026-W35", limit=4)
    assert [p["edition_id"] for p in priors] == ["2026-W34", "2026-W33"]


def test_module_sets_are_pruned_on_the_same_horizon_as_editions(pod):
    shared, net = pod
    for eid in ("2018-W01", "2026-W35"):
        store.write_edition(shared, _full(eid))
        store.write_modules(shared, mod.build_modules(_full(eid), [],
                                                      now=NOW_MIDWEEK))
    assert store.prune_modules(shared, keep_years=5) == ["2018-W01"]
    assert _module_ids(shared) == ["2026-W35"]


def test_write_modules_rejects_a_payload_without_a_valid_week(tmp_path):
    with pytest.raises(ValueError):
        store.write_modules(tmp_path, {"edition_id": "nope"})


def test_the_edition_itself_never_grows_a_modules_key(pod):
    """The two stores stay separate: a sealed measurement holds no wording."""
    shared, net = pod
    edition = run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    assert "modules" not in edition
    assert "modules" not in store.load_edition(shared, "2026-W35")


def test_a_module_set_survives_a_prior_edition_it_cannot_understand(pod):
    """A neighbour from another schema is not comparable — and not fatal."""
    shared, net = pod
    store.write_edition(shared, {"edition_id": "2026-W34", "schema_version": 99,
                                 "sealed": True})
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    cost = _by_id(store.load_modules(shared, "2026-W35"))["cost_trajectory"]
    assert cost["trend"]["available"] is False
    assert cost["measurable"] is True     # this week still speaks


def test_synthesis_is_pure_over_the_edition_and_the_clock(pod):
    shared, net = pod
    edition = _build_here(shared, net)
    a = mod.build_modules(edition, [], now=NOW_MIDWEEK)
    b = mod.build_modules(edition, [], now=NOW_MIDWEEK)
    assert a == b


class _FrozenDatetime(datetime):
    """A ``datetime`` whose ``now()`` is pinned — the CLI reads the wall clock."""

    _pinned: datetime

    def __new__(cls, pinned: datetime):  # type: ignore[override]
        obj = super().__new__(
            cls, pinned.year, pinned.month, pinned.day,
            pinned.hour, pinned.minute, tzinfo=pinned.tzinfo,
        )
        obj._pinned = pinned
        return obj

    def now(self, tz=None):  # type: ignore[override]
        return self._pinned if tz is None else self._pinned.astimezone(tz)
