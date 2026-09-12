"""dossier.modules — the synthesis layer: what the numbers MEAN, in English.

The edition writer records measurements. This module turns four of them into
sentences an operator can read without knowing how any of it works. The v1
cut is the operator's (D-T11): **apps leaderboard, reliability history, cost
trajectory, users activity** — outcome-shaped units, because that is how the
market thinks about what a pod does, and a pod should be able to say the same
about itself.

A FIFTH LAW, learned from the first live page (2026-08-30). **A module says
what its name promises, or it says nothing.** Reliability shipped reporting
open alert counts because those were the numbers nearest to hand — a true
measurement of a different question, standing where scheduled-run history
was supposed to be, under a title that claimed otherwise. A substitute
metric wearing a module's name is worse than an empty card: the empty card
is honest about what the pod does not yet know.

Every module is::

    {module_id, title, critical, measurable,
     headline,        # <= 2 plain-English sentences, gate-checked
     values{...},     # the numbers the headline is speaking about
     trend{...},      # this edition vs earlier ones, null-honest
     forward_note,    # what stands where the trend line will go
     remediation,     # how to close the gap this card names, or null
     detail{...}}     # may be technical: field names, windows, caveats

FOUR LAWS, and each one is a thing that goes wrong if it is dropped.

1. **Tri-state.** A module whose source is absent says "we cannot show this
   yet" and names the reason. It never renders a zero. "Nobody used the pod"
   and "nothing here records who uses the pod" are different facts, and the
   second one is a to-do, not a result.

2. **Trends compare editions only.** A trend reads the number recorded in an
   EARLIER edition. It never re-derives history from today's producers —
   those files have rolled over, so re-deriving would silently answer a
   different question every week.

3. **Every sentence comes from the registry.** :mod:`dossier.headlines` is
   the only source of operator-facing words, so ``tools/readability-lint``
   can enumerate and score all of them. An f-string here would be prose the
   gate cannot see.

4. **Synthesis is a rendering, never a measurement.** Nothing here computes
   a number the edition does not already carry. That is what lets a module
   file be regenerated for a sealed week without breaking the seal: the
   measurement is in the edition, the wording is here.

CRITICAL MODULES (design §4a rule 2). ``critical: true`` marks a module whose
ABSENCE is itself a finding — one a reader must refuse to render a page
without. **None of the four v1 modules is critical, and that is stated
deliberately rather than left to a default.** Each one is a report on a
capability a pod may legitimately not have yet: a pod with no apps, no alert
history, no cost rollup, or no per-person attribution is an early pod, not a
broken one. Marking any of them critical would turn "young" into "alarming".
The flag exists now so the first genuinely critical module (a safety or
spend-cap surface) does not have to add a field to a shipped schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from dossier import headlines as hl
from dossier.window import iso_z

#: Bumped only for a breaking change to the on-disk shape of a module set.
SCHEMA_VERSION = 1

#: How far back a trend will look for something to compare against. A gap in
#: the spine (the pod was off, the producer was not installed) should show as
#: "nothing to compare with", not as a comparison to two months ago dressed
#: up as "the week before".
MAX_TREND_LOOKBACK = 4

#: Movement smaller than this reads as "about the same". Without a band,
#: every week's headline claims a direction, and a dashboard that always
#: points somewhere teaches its reader to stop looking.
FLAT_BAND = 0.05

#: At or above this share of work untied to any app, the leaderboard stops
#: being the story and the missing credit becomes it. Set at 90% rather than
#: 100% because a pod with three attributed requests out of a thousand has
#: the same problem as one with zero, and rounding it to "we have a
#: leaderboard" is how a broken measurement passes for a thin one.
NEAR_ZERO_COVERAGE = 0.9

#: How many rows a card's bar chart draws. Five is the mockup's anatomy: long
#: enough to show a shape, short enough that every bar keeps a readable
#: direct label rather than becoming a legend.
BAR_ROWS = 5


# ── module payload ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModuleSpec:
    """The static declaration of one module. See CRITICAL MODULES above."""

    module_id: str
    title: str
    #: The number the trend follows. Named in the payload so a reader never
    #: has to guess WHICH of several values a direction refers to.
    metric: str
    #: v1: every one of these is False, deliberately. See the module docstring.
    critical: bool = False


@dataclass
class _Draft:
    """One module's synthesis before the trend sentence is attached."""

    #: The rendered first sentence, or "" when the module cannot be shown.
    sentence: str = ""
    #: Registry key for the reason, set only when the source is absent.
    reason_key: str | None = None
    values: dict[str, Any] | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    #: The trended number, or None when there is nothing to trend.
    metric: float | int | None = None
    #: How this module says its metric out loud ("$0.75", "3").
    fmt: Callable[[float | int], str] = str
    #: The gap this card can tell its reader how to close, when it has one:
    #: ``{"note": <gated sentence>, "page": <admin surface>}``. A card that
    #: names a gap and stops there is the shape
    #: docs/principle-alerts-explain-and-remediate.md exists to forbid.
    remediation: dict[str, Any] | None = None

    @property
    def measurable(self) -> bool:
        return self.reason_key is None


# ── the four v1 modules ──────────────────────────────────────────────────────

def _apps(edition: dict[str, Any]) -> _Draft:
    """Which app the pod actually used, and how much.

    The counts come from each bot's app-usage rollup, whose windows are its
    OWN rolling 7 days as of its own run — not the edition's Monday-to-Sunday
    week. Comparing one edition's rolling-7 against the previous edition's
    rolling-7 is still a week-over-week comparison (the snapshots are a week
    apart); it is just not a calendar week, and ``detail`` says so rather
    than letting the headline imply otherwise.
    """
    per_app = edition.get("per_app")
    if per_app is None:
        return _Draft(reason_key="apps.reason_absent",
                      detail={"missing_source": "usage-by-app rollup",
                              "why": "no bot on the pod has written "
                                     "usage-by-app.json"})

    apps = per_app.get("apps") or {}
    ranked = sorted(
        ((str(k), int((v.get("d7") or {}).get("turns") or 0))
         for k, v in apps.items() if isinstance(v, dict)),
        key=lambda kv: (-kv[1], kv[0]),
    )
    used = [(app_id, turns) for app_id, turns in ranked if turns > 0]
    coverage = (per_app.get("coverage") or {}).get("d7") or {}
    untied = coverage.get("unattributed_turns")
    total_turns = sum(t for _, t in used)

    detail = {
        "source": per_app.get("source"),
        "window_note": "each bot's own rolling 7-day window, as of its own "
                       "rollup run — not the Monday-to-Sunday edition week",
        "as_of_date_by_bot": (per_app.get("source_windows") or {})
                             .get("as_of_date_by_bot"),
        "coverage_d7": coverage or None,
        "bots_without_rollup": per_app.get("bots_without_rollup"),
        "field": "per_app.apps.<app_id>.d7.turns",
    }
    values = {
        "apps_used": len(used),
        "requests_total": total_turns,
        "top": [{"app_id": a, "name": hl.humanize_id(a), "requests": t}
                for a, t in used[:BAR_ROWS]],
        # Tri-state one level down: the coverage counter may be absent even
        # when the rollup is present.
        "requests_not_tied_to_an_app": (
            int(untied) if isinstance(untied, (int, float)) else None
        ),
    }

    # More work untied than ranked means the leaderboard is a minority
    # report, and a headline that does not say so is the omission the
    # coverage counters exist to prevent. The live 2026-W35 pod is exactly
    # this shape: one app at 7 requests, 967 untied.
    mostly_untied = isinstance(untied, (int, float)) and untied > total_turns
    share = coverage.get("unattributed_turns_share")
    # Near-zero coverage is a DIFFERENT card from a lopsided leaderboard.
    # At 99% untied the ranking is noise and the missing credit is the whole
    # story, so the state clause becomes the repair rather than the ranking.
    starved = isinstance(share, (int, float)) and share >= NEAR_ZERO_COVERAGE

    remediation = None
    if starved or (not used and isinstance(untied, (int, float)) and untied > 0):
        remediation = {
            "note": hl.say("shared.fix_on_maintenance"),
            "page": "maintenance",
        }
        # The COMMAND is named in the detail as a job, never as a shell line:
        # a web page that prints a sudo invocation is teaching an operator to
        # paste privileged commands out of a browser.
        detail["remediation"] = {
            "turns_on": "which app each request belongs to",
            "job": "usage-by-app",
            "where": "Maintenance, under the pod's background jobs",
        }

    if starved:
        sentence = hl.say("apps.mostly_untied_remediation")
    elif used:
        top_id, top_turns = used[0]
        if mostly_untied:
            key = "apps.leader_but_mostly_untied"
        elif len(used) == 1:
            key = "apps.leader_only"
        else:
            key = "apps.leader_and_others"
        sentence = hl.say(key, app=hl.humanize_id(top_id),
                          turns=top_turns, count=len(used))
    elif isinstance(untied, (int, float)) and untied > 0:
        # The live 2026-W35 shape: no app ranked, but the pod was busy. A
        # bare "no apps were used" here would be the lie the coverage
        # counters exist to prevent.
        sentence = hl.say("apps.none_but_traffic", turns=int(untied))
    else:
        sentence = hl.say("apps.none_at_all")

    return _Draft(sentence=sentence, values=values, detail=detail,
                  metric=total_turns, fmt=lambda v: f"{int(v)}",
                  remediation=remediation)


def _reliability(edition: dict[str, Any]) -> _Draft:
    """Whether the pod's scheduled apps actually showed up, day by day.

    THE MODULE'S SUBJECT, and the correction it exists to make. This card
    used to report open alert counts — "93 things need attention" — which is
    a true number about a different question wearing this module's name.
    Design §4.1 and D-T11 ask for scheduled-fire history per app: "ran N of
    the last M days," with the misses named. That is what this is now.

    THE LEAD APP is the one with the longest run of recorded days, then the
    most days run, then its id — a stable order that does not swap the
    headline's subject on a tie. The other scheduled apps ride in ``values``
    and in the detail so the card reports a pod rather than an app.

    AN APP WITH A SCHEDULE AND NO RECORD gets a row saying so — never a zero
    and never silence. "Evolve installed a cron for this and nothing here has
    recorded it running" is a to-do about the measuring, not a verdict about
    the app, and the row says which.
    """
    block = edition.get("fires")
    if block is None:
        return _Draft(reason_key="reliability.reason_absent",
                      detail={"missing_source": "turn annotations",
                              "why": "{shared_dir}/annotations does not exist "
                                     "on this pod"})

    window = block.get("window") or {}
    apps = block.get("apps") or {}
    without = list(block.get("apps_without_history") or [])
    # ``apps`` is keyed by app id; carry the key onto the row so the ranking
    # and every downstream reader speak about the same identifier.
    ranked = [
        {**row, "app_id": app_id, "name": hl.humanize_id(app_id)}
        for app_id, row in sorted(apps.items()) if isinstance(row, dict)
    ]
    ranked.sort(key=lambda r: (-int(r.get("days_covered") or 0),
                               -int(r.get("days_ran") or 0), r["app_id"]))

    detail = {
        "source": block.get("source"),
        "counts": "only turns a schedule started; a person running the app by "
                  "hand is not a scheduled run",
        "window": window,
        "apps": {r["app_id"]: {k: r.get(k) for k in
                               ("bots", "cadence_days", "days_ran",
                                "days_covered", "days_missed", "missed_dates",
                                "runs_total", "first_run_date",
                                "last_run_date")}
                 for r in ranked},
        "cadence_note": "how often each app runs is read off its own history, "
                        "not declared anywhere — an app with fewer than three "
                        "runs has no rhythm yet and no miss count",
        "apps_with_a_schedule_and_no_record": without,
        "bots_without_annotations": block.get("bots_without_annotations"),
        "field": "fires.apps.<app_id>.runs_by_date",
    }

    # The remediation is only honest when Evolve KNOWS of a schedule it has
    # no record of. A pod that simply runs nothing on a timer is not broken
    # and is offered nothing.
    remediation = None
    if without:
        remediation = {"note": hl.say("reliability.fix_no_record"),
                       "page": "maintenance"}
        detail["remediation"] = {
            "turns_on": "which app each scheduled run belongs to",
            "job": "usage-by-app",
            "where": "Maintenance, under the pod's background jobs",
        }

    if not ranked:
        # Nulls, not zeros. An app with a timer and no record has not "run 0
        # times and missed 0" — nothing here knows either way, and a pair of
        # confident zeros is the exact tri-state violation this whole file is
        # built to prevent.
        return _Draft(
            sentence=hl.say("reliability.none_recorded"),
            values={
                "apps_on_a_schedule": len(without),
                "times_ran": None,
                "times_missed": None,
                "apps_with_no_record": len(without),
                "strip": None,
                "top": [],
            },
            detail=detail, metric=None, fmt=lambda v: f"{int(v)}",
            remediation=remediation,
        )

    lead = ranked[0]
    window_days = int((window or {}).get("days") or 0)
    days_ran = int(lead.get("days_ran") or 0)
    cadence = lead.get("cadence_days")
    due = lead.get("days_covered")
    missed = lead.get("days_missed")

    if not cadence or due is None or missed is None:
        # Not enough runs to read a rhythm: report what was seen and claim
        # nothing about misses.
        sentence = hl.say("reliability.fires_too_new", app=lead["name"],
                          runs=days_ran, days=window_days)
    elif cadence == 1:
        sentence = (
            hl.say("reliability.fires_missed", app=lead["name"], ran=days_ran,
                   days=int(due), missed=int(missed))
            if missed else
            hl.say("reliability.fires_perfect", app=lead["name"], days=int(due))
        )
    else:
        sentence = (
            hl.say("reliability.fires_missed_runs", app=lead["name"],
                   ran=days_ran, due=int(due), missed=int(missed))
            if missed else
            hl.say("reliability.fires_perfect_runs", app=lead["name"],
                   days=window_days)
        )

    values = {
        "apps_on_a_schedule": len(ranked) + len(without),
        "times_ran": days_ran,
        # Tri-state again: an app with no readable rhythm has no miss count,
        # and null is the only honest answer.
        "times_missed": None if missed is None else int(missed),
        "apps_with_no_record": len(without),
        # The day strip the card draws: one cell per day of the window for
        # the lead app. ``off`` is "not due, or before its first run" — an
        # app installed on Thursday did not miss Monday, and a weekly app
        # did not miss the six days between its runs.
        "strip": _fire_strip(lead, window),
        # Every scheduled app as a bar row, so a pod with five of them is not
        # reported as if it had one.
        "top": [{"app_id": r["app_id"], "name": r["name"],
                 "requests": int(r.get("days_ran") or 0)}
                for r in ranked[:BAR_ROWS]],
    }
    # A miss anywhere on the pod, not just on the lead app: the trended
    # number has to be about the pod, or a quiet week on the lead app would
    # read as a quiet week everywhere. Apps with no readable rhythm
    # contribute nothing rather than a guessed zero.
    missed_total = sum(int(r["days_missed"]) for r in ranked
                       if isinstance(r.get("days_missed"), int))
    return _Draft(sentence=sentence, values=values, detail=detail,
                  metric=missed_total, fmt=lambda v: f"{int(v)}",
                  remediation=remediation)


def _fire_strip(row: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    """One cell per day of the window, for one app.

    Three states and no fourth: ``ran`` (a schedule fired and did work),
    ``missed`` (a day this app was DUE and nothing was recorded), ``off``
    (not due — before its first run, or one of the six days a weekly app is
    quiet on purpose). Collapsing ``off`` into ``missed`` would greet every
    newly-installed app with three weeks of failure it was never present
    for, and paint a perfectly healthy weekly app almost entirely amber.
    """
    dates = list(window.get("dates") or [])
    runs = row.get("runs_by_date") or {}
    expected = set(row.get("expected_dates") or [])
    days = []
    for day in dates:
        if day in runs:
            state, runs_that_day = "ran", int(runs[day] or 0)
        elif day in expected:
            state, runs_that_day = "missed", 0
        else:
            state, runs_that_day = "off", 0
        days.append({"date": day, "state": state, "runs": runs_that_day})
    return {
        "app_id": row.get("app_id"),
        "name": row.get("name"),
        "cadence_days": row.get("cadence_days"),
        "days": days,
        "missed_dates": list(row.get("missed_dates") or []),
    }


def _cost(edition: dict[str, Any]) -> _Draft:
    """What the week cost, which way that is heading, and who spent it."""
    costs = edition.get("costs")
    if costs is None:
        return _Draft(reason_key="cost.reason_absent",
                      detail={"missing_source": "daily cost rollups",
                              "why": "no cost rollup exists for any bot in "
                                     "this week's seven days"})

    total = float(costs.get("total_usd") or 0.0)
    days = costs.get("days_in_window")
    # The per-bot split the card draws. A pod's spend is a sum of bots and
    # the operator's next question is always "which one" — a total with no
    # split makes them go find the Cost page to ask it.
    by_bot = []
    for bot_id, row in sorted((costs.get("by_bot") or {}).items()):
        if not isinstance(row, dict):
            continue   # tri-state: a bot with no rollup is null, not $0
        spend = round(float(row.get("total_usd") or 0.0), 6)
        by_bot.append({"bot_id": bot_id, "name": hl.humanize_id(bot_id),
                       "spend": spend, "spend_display": _money(spend)})
    by_bot.sort(key=lambda r: (-r["spend"], r["bot_id"]))

    values = {
        "spend_this_week": round(total, 4),
        "spend_display": _money(total),
        "bots_that_spent": costs.get("bots_with_data"),
        "models_used": len(costs.get("by_model") or {}),
        "by_bot_spend": by_bot[:BAR_ROWS],
    }
    detail = {
        "source": costs.get("source"),
        "window": costs.get("window"),
        "days_in_window": days,
        "by_model": costs.get("by_model"),
        "note": "a day with no rollup file contributes nothing rather than a "
                "zero, so a partly-instrumented week reads low",
        "field": "costs.total_usd",
    }
    key = "cost.nothing" if total <= 0 else "cost.spend"
    # Rounded the same as ``values["spend_this_week"]``: the metric IS that
    # value, and two spellings of one number in one payload is a reader's
    # first reason to distrust the rest of it.
    return _Draft(sentence=hl.say(key, spend=_money(total)), values=values,
                  detail=detail, metric=round(total, 4), fmt=_money)


def _users(edition: dict[str, Any]) -> _Draft:
    """How many people the pod served, and which of them asked for the most.

    Its producer is ``usage_by_user`` — the per-person rollup that landed
    2026-08-30. Before that this module read a ``users`` key inside the
    per-app rollup, a key that producer never writes, so the card reported
    "nothing here records who is using the pod" on pods that had a record.

    THE HEADLINE COUNTS; IT DOES NOT NAME. Person ids ride in ``values`` for
    the chart on the OPERATOR's page — which design D-T10 scopes to the
    operator alone — and never enter a sentence. The per-bot weekly digest
    is a projection of this same object and must read the count, never the
    chart: a bot's own person may not see other people's activity.

    WITHHELD IS NOT ABSENT. The producer's do-not-track gate can hold a
    person back, and it says so. A gate that withheld everyone yields "we
    cannot show this yet" with that reason — never "nobody used the pod".
    """
    block = edition.get("users") or {}
    if block.get("available") is not True:
        withheld = block.get("requesters_withheld")
        key = ("users.withheld" if isinstance(withheld, int) and withheld
               else "users.reason_absent")
        return _Draft(reason_key=key,
                      detail={"missing_source": block.get("producer")
                                                or "usage_by_user",
                              "why": block.get("note")
                                     or "no per-person rollup on this pod",
                              "people_held_back": withheld,
                              "bots_without_rollup":
                                  block.get("bots_without_rollup")})

    by_person = block.get("by_person") or {}
    by_app = block.get("by_app") or {}
    people = list(block.get("requesters") or [])
    requests = sum(int((row or {}).get("turns") or 0)
                   for row in by_person.values() if isinstance(row, dict))
    ranked = sorted(
        ((person, int((row or {}).get("turns") or 0),
          list((row or {}).get("bots") or []))
         for person, row in by_person.items() if isinstance(row, dict)),
        key=lambda kv: (-kv[1], kv[0]),
    )
    values = {
        "people": len(people),
        "requests_total": requests,
        "apps_with_a_named_user": len(by_app),
        "people_held_back": block.get("requesters_withheld"),
        # ``name`` is the id said out loud; the admin reader replaces it with
        # a display name it resolves at read time, so the durable spine never
        # accrues a name that later changes.
        # ``bots`` rides along because the display-name resolver the admin
        # reader uses is per-bot; without it the chart could only ever show
        # the raw id.
        "top": [{"person_id": person, "name": hl.humanize_id(person),
                 "requests": turns, "bots": bots}
                for person, turns, bots in ranked[:BAR_ROWS] if turns > 0],
    }
    detail = {
        "source": block.get("producer"),
        "window": block.get("window"),
        "requesters": people,
        "people_held_back": block.get("requesters_withheld"),
        "field": "users.by_person.<person>.turns",
        "privacy": "ids live here and in the chart on this page, never in a "
                   "sentence and never in a person's own weekly note",
    }
    key = hl.plural_key("users.count", len(people), zero="users.none")
    return _Draft(sentence=hl.say(key, count=len(people)), values=values,
                  detail=detail, metric=len(people), fmt=lambda v: f"{int(v)}")


#: The v1 cut, in the operator's order (D-T11). ``critical=False`` on all
#: four is a decision, not a default — see the module docstring.
SPECS: tuple[tuple[ModuleSpec, Callable[[dict[str, Any]], _Draft]], ...] = (
    (ModuleSpec("apps_leaderboard", "Apps leaderboard", "requests_total"), _apps),
    (ModuleSpec("reliability_history", "Reliability history",
                "times_missed"), _reliability),
    (ModuleSpec("cost_trajectory", "Cost trajectory", "spend_this_week"), _cost),
    (ModuleSpec("users_activity", "Users activity", "people"), _users),
)

_BUILDERS = {spec.module_id: build for spec, build in SPECS}


# ── trends (editions only — never a recomputation of history) ────────────────

def _trend(spec: ModuleSpec, draft: _Draft,
           priors: list[dict[str, Any]]) -> dict[str, Any]:
    """This edition's metric against the most recent EARLIER edition's.

    ``priors`` is newest-first. The comparison runs the same builder over the
    earlier edition's stored numbers — which is what keeps "last week's
    figure" meaning exactly what "this week's figure" means, forever, even
    after a producer changes shape. It reads editions and nothing else.
    """
    looked_at = priors[:MAX_TREND_LOOKBACK]
    base = {
        "metric": spec.metric,
        "current": draft.metric,
        "previous": None,
        "compared_with": None,
        "delta": None,
        "percent_change": None,
        "direction": None,
        "editions_looked_back": len(looked_at),
    }
    if draft.metric is None:
        return {**base, "available": False,
                "reason": "this week has no number to compare"}
    if not looked_at:
        return {**base, "available": False,
                "reason": "this is the first week on record"}

    for prior in looked_at:
        try:
            previous = _BUILDERS[spec.module_id](prior).metric
        except Exception:
            # A prior edition written by an older/newer schema must not take
            # the week down; it simply is not comparable.
            previous = None
        if previous is None:
            continue
        delta = draft.metric - previous
        pct = (delta / abs(previous)) if previous else None
        return {
            **base,
            "available": True,
            "previous": previous,
            "compared_with": prior.get("edition_id"),
            "delta": round(delta, 6) if isinstance(delta, float) else delta,
            "percent_change": round(pct, 4) if pct is not None else None,
            "direction": _direction(delta, pct),
            "reason": None,
        }
    return {**base, "available": False,
            "reason": f"no earlier week within {MAX_TREND_LOOKBACK} editions "
                      f"recorded this number"}


def _direction(delta: float | int, pct: float | None) -> str:
    if delta == 0 or (pct is not None and abs(pct) < FLAT_BAND):
        return "flat"
    return "up" if delta > 0 else "down"


def _trend_sentence(trend: dict[str, Any], draft: _Draft) -> str:
    """The headline's second sentence, or none at all on a pod's first week.

    THE FIRST WEEK SAYS NOTHING HERE, and that is the point. This used to
    return "There is nothing to compare it with yet." — which is true, and
    which every module said, so a new pod's page repeated one sentence four
    times down its own first screen. The fact is said ONCE now, in the week
    bar above the grid (``page.first_week``), and each card carries its own
    forward line instead. ``editions_looked_back == 0`` is exactly "no
    earlier week exists anywhere", so suppressing it here loses nothing: the
    page-level sentence covers every card at once.

    A GAP still speaks per-card, because a gap is per-module — one module can
    have four comparable weeks behind it while another has none.
    """
    if not trend.get("available"):
        if trend.get("editions_looked_back"):
            return hl.say("trend.gap")
        return ""
    direction = trend["direction"]
    if direction == "flat":
        return hl.say("trend.flat")
    return hl.say(f"trend.{direction}", previous=draft.fmt(trend["previous"]))


# ── assembly ─────────────────────────────────────────────────────────────────

def build_module(spec: ModuleSpec, build: Callable[[dict[str, Any]], _Draft],
                 edition: dict[str, Any],
                 priors: list[dict[str, Any]]) -> dict[str, Any]:
    """One module: headline, values, trend, detail."""
    draft = build(edition)
    # What will stand where this card's trend line goes, once there is one.
    # Per-module wording on purpose: four cards each promising "the trend
    # line starts next week" is the repetition this brief came to remove,
    # moved one line down the card.
    forward = hl.say(f"forward.{spec.module_id}")
    if not draft.measurable:
        return {
            "module_id": spec.module_id,
            "title": spec.title,
            "critical": spec.critical,
            "measurable": False,
            "headline": hl.say("shared.cannot_measure",
                               reason=hl.say(str(draft.reason_key))),
            "values": None,
            # Still a full trend shell — a reader must not have to branch on
            # module shape to find out there is no trend.
            "trend": _trend(spec, draft, priors),
            "forward_note": forward,
            "remediation": draft.remediation,
            "detail": draft.detail,
        }
    trend = _trend(spec, draft, priors)
    return {
        "module_id": spec.module_id,
        "title": spec.title,
        "critical": spec.critical,
        "measurable": True,
        "headline": hl.join(draft.sentence, _trend_sentence(trend, draft)),
        "values": draft.values,
        "trend": trend,
        "forward_note": forward,
        "remediation": draft.remediation,
        "detail": draft.detail,
    }


def build_modules(edition: dict[str, Any], priors: list[dict[str, Any]], *,
                  now: datetime) -> dict[str, Any]:
    """The whole module set for one edition.

    ``priors`` are earlier editions, newest-first. Pure with respect to the
    clock and the disk: given the same edition and the same priors it returns
    the same payload, which is what makes the fixture tests equality
    assertions rather than approximations.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "edition_id": edition.get("edition_id"),
        "computed_at": iso_z(now),
        "based_on": {
            "edition_id": edition.get("edition_id"),
            "edition_computed_at": edition.get("computed_at"),
            "edition_sealed": edition.get("sealed"),
            "window": edition.get("window"),
            "editions_on_record": len(priors) + 1,
        },
        "modules": [build_module(spec, build, edition, priors)
                    for spec, build in SPECS],
    }


def _money(value: float | int) -> str:
    """``0.75 -> "$0.75"``; sub-cent amounts keep enough digits to be true.

    Rounding $0.004 to "$0.00" would have the pod announce it spent nothing
    on a week it did spend — small, but a false sentence.
    """
    amount = float(value)
    if 0 < amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"
