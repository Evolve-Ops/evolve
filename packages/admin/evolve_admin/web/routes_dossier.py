"""HTTP routes behind the Pod Intelligence page — the window into the dossier.

GET  /api/dossier/current              this week's module set, ready to render
GET  /api/dossier/editions/<week_id>   one earlier week, in the same shape
GET  /api/dossier/profile              the operator's order / hidden / ratings
POST /api/dossier/profile              write them back — the ONE write here

Design: ``internal/design-pod-dossier-2026-08-24.md`` (D-T5 module cards +
the interest profile, D-T8 the 10th-grader bar, D-T9 the page).
Brief: ``internal/dispatch/done/pod-intelligence-shell.md``.

THE HOUSE RULE THIS FILE EXISTS TO KEEP. **Pages don't think.** The synthesis
layer (``dossier.modules``) already decided what this week means and said it
in one gated sentence per module; the page's job is to show that sentence.
So everything the browser needs is assembled HERE, server-side, from what is
on disk — and the page module renders strings it is handed. Concretely:

  * **No sentence is built in JavaScript.** Headlines come from the module
    set verbatim. The short trend chips and the fact labels below are the
    only operator-facing words this layer adds, they are noun phrases rather
    than sentences, and ``test_dossier_routes`` scores every one of them with
    ``dossier.readability``'s own acronym / field-name / jargon rules so they
    meet the same bar as the registry they sit beside.
  * **No field name reaches the browser's rendering path.** ``values`` is a
    dict keyed by field names; :data:`FACT_LABELS` turns the handful worth
    showing into ``{label, value}`` pairs and drops the rest. A module this
    server has never heard of renders headline + trend and no facts, which is
    the honest degradation — better a card with fewer numbers than a card
    captioned ``requests_not_tied_to_an_app``.
  * **No number is recomputed.** The multi-week series each card draws is
    lifted out of EARLIER MODULE SETS — each point is the number synthesis
    recorded for that week — never re-derived from producers that have since
    rolled over (``dossier.modules`` law 2, honoured on the read side too).
  * **THIS WEEK is drawn before history is.** The bar lists
    (:data:`BAR_SPECS`) and the reliability day strip (:func:`_strip`) come
    out of the current week's own values and need no earlier week at all.
    That is deliberate and it is a correction: the first version of this
    page could draw nothing until a pod had two weeks on record, so the
    surface an operator saw on install day was four paragraphs and no
    picture. The sparkline joins them the week a second week exists.
  * **The first week is said ONCE.** ``week.first_week_note`` carries it,
    above the grid. Every module used to append "there is nothing to compare
    it with yet" to its own headline, and four copies of one sentence is
    what a young pod's whole page then said. Each card carries its own
    forward line instead, from the same gated registry.
  * **A name is resolved here, never stored in the spine.** Person bars are
    labelled through :func:`_name_resolver` — the Usage page's own cache-only
    roster lookup — because the dossier keeps stable ids and a display name
    that changes should change on the page rather than leaving two names in
    five years of records.

WHAT IS DELIBERATELY NOT HERE. The interest profile is stored, not applied to
anything but this page: design §4a rule 1 routes "profile feeds proposal
ranking" to the ``rsi`` lane, and it is deposited there rather than built
here. And ``hidden`` is not enforced on the write path — see
``dossier.profile``'s docstring and :func:`_module_view`'s ``critical`` note.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple, Union

from flask import Flask, jsonify, request, Response

from ..config import CANONICAL_SHARED_DIR, load_network
from ..telemetry import get_logger
from .http_errors import error_response

_log = get_logger("web.routes_dossier")

#: What a handler here may return: a body, or a body plus a status code.
#: Spelled out because a bare ``-> Response`` annotation on a handler that
#: also returns ``(body, 404)`` is a type error the pyright ratchet counts.
RouteResult = Union[Response, Tuple[Response, int]]

#: How many weeks of history the page may draw and list. A five-year spine
#: holds 260 module sets and a card's sparkline stops being readable long
#: before that, so the reader bounds itself rather than loading the archive
#: to draw twelve points.
MAX_HISTORY_WEEKS = 12

#: The one write's request-body ceiling, in bytes. The profile is three
#: short lists; anything larger is a client bug or an attempt to use a
#: preference file as a store.
MAX_PROFILE_BODY = 64 * 1024

#: Plain-English labels for the values worth putting on a card's face, as
#: ``(field, label, kind)`` per module. This map is the ONLY thing standing
#: between a field name and the operator's screen, which is why an unmapped
#: field is dropped rather than title-cased: ``requests_not_tied_to_an_app``
#: prettied into "Requests Not Tied To An App" is still our schema talking.
#:
#: ``kind`` is per FIELD, never per module. A money module still counts
#: things: ``cost_trajectory`` carries an amount AND a number of bots, and a
#: module-wide money rule renders "8 bots" as "$8.00" — which is not a
#: rounding slip, it is a false statement about the pod's spend.
#:
#: Order within a module is the order shown. Every label is a noun phrase in
#: the register of the headlines beside it (design D-T8) and is gated by
#: ``test_fact_labels_meet_the_tenth_grader_bar``.
COUNT, MONEY, TEXT = "count", "money", "text"

FACT_LABELS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "apps_leaderboard": (
        ("requests_total", "Requests to apps", COUNT),
        ("apps_used", "Apps people used", COUNT),
        ("requests_not_tied_to_an_app", "Requests not tied to an app", COUNT),
    ),
    "reliability_history": (
        ("times_ran", "Times it ran", COUNT),
        ("times_missed", "Times it missed", COUNT),
        ("apps_on_a_schedule", "Apps that run on a timer", COUNT),
        ("apps_with_no_record", "Apps with no record yet", COUNT),
    ),
    "cost_trajectory": (
        # Already a string the synthesis formatted; passed through so the
        # card and the headline cannot spell one amount two ways.
        ("spend_display", "Spent this week", TEXT),
        ("bots_that_spent", "Bots that spent", COUNT),
        ("models_used", "Kinds of model used", COUNT),
    ),
    "users_activity": (
        ("people", "People", COUNT),
        ("requests_total", "Requests", COUNT),
        ("apps_with_a_named_user", "Apps with a named person", COUNT),
        ("people_held_back", "People who opted out", COUNT),
    ),
}

#: The ranked bar list each card draws for THIS week, as
#: ``(values field, value key, kind, unit label)``. Every one of these needs
#: no history at all — which is the point: a first-week page that draws
#: nothing until Monday is a page that looks broken on the day an operator
#: first opens it.
#:
#: Keyed by module id for the same reason :data:`MONEY_MODULES` is: the
#: cost split is an amount and the others are counts, and one shared rule
#: would render "3 requests" as "$3.00" or "$0.61" as "1".
BAR_SPECS: dict[str, tuple[str, str, str, str]] = {
    "apps_leaderboard": ("top", "requests", COUNT, "requests this week"),
    "users_activity": ("top", "requests", COUNT, "requests this week"),
    "reliability_history": ("top", "requests", COUNT, "days each app ran"),
    "cost_trajectory": ("by_bot_spend", "spend", MONEY, "spent this week"),
}

#: What one cell of the reliability day strip says when pointed at. Built
#: here rather than in the page for the house rule's sake, and held as a
#: table rather than an f-string so the readability test can score all three.
STRIP_WORDS: dict[str, str] = {
    "ran": "ran",
    "missed": "no run",
    "off": "not yet in use",
}

#: The legend under the strip. Two entries, because a legend for a state
#: nobody can see teaches nothing — "not yet in use" is grey and explained
#: by the cells around it.
STRIP_LEGEND: tuple[tuple[str, str], ...] = (
    ("ran", "ran"),
    ("missed", "no run"),
)

#: Modules whose TRENDED METRIC is money — the number the sparkline plots.
#: Keyed by module id rather than sniffed from the value because a count and
#: an amount are indistinguishable once they are both floats, and a spend
#: series labelled "24" instead of "$24.37" is a wrong chart, not a terse one.
MONEY_MODULES = frozenset({"cost_trajectory"})

#: Short chips for the trend the module set already computed. Phrases, not
#: sentences — the headline says the trend in English; this is the glanceable
#: form beside the title.
#:
#: DELIBERATELY UNCOLOURED. Whether "up" is good depends on the metric, and
#: nothing in the dossier declares a polarity for any of them. A green chip
#: on rising spend would be the page inventing a judgement the synthesis
#: never made, so every chip is neutral and the direction is carried by the
#: word plus an arrow. See the PR body's note on the mockup.
TREND_NO_MATCH = "nothing to compare yet"
TREND_FLAT = "about the same"


def _analyzer(mod: str):
    """Import an analyzer module at CALL time, not at registration time.

    The same lazy shape ``routes_signals`` uses, for the same reason: a
    checkout where ``dossier`` is momentarily unimportable should cost this
    page a 500, not the whole admin daemon its boot.
    """
    return importlib.import_module(mod)


def register_dossier_routes(app: Flask, network_path: Path) -> None:
    """Register the four Pod Intelligence endpoints. Three read, one write."""

    def _shared_dir() -> Path:
        # CANONICAL_SHARED_DIR, not a ``/Users/Shared`` literal: the fallback
        # has to be the platform's, or the whole page 404s on a Linux pod.
        return Path(
            load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
        )

    # ── reads ──────────────────────────────────────────────────────────────

    @app.get("/api/dossier/current")
    def api_dossier_current() -> RouteResult:
        """The newest week on record, rendered for the page.

        ``available: false`` when the writer has not run yet — a first-boot
        pod, or one whose weekly job was installed this morning. That is a
        different state from "a week with nothing in it", and the page says
        so rather than drawing four empty cards.
        """
        try:
            store = _analyzer("dossier.store")
            network = load_network(network_path)
            shared = Path(network.get("sharedDir", CANONICAL_SHARED_DIR))
            weeks = _weeks_on_record(store, shared)
            if not weeks:
                return jsonify({
                    "ok": True,
                    "available": False,
                    "weeks": [],
                    "weeks_on_record": 0,
                    "modules": [],
                })
            return jsonify(_render_week(
                store, shared, weeks[0], weeks, current_id=weeks[0],
                network=network,
            ))
        except Exception as e:  # pragma: no cover - defensive
            return error_response(e)

    @app.get("/api/dossier/editions/<week_id>")
    def api_dossier_edition(week_id: str) -> RouteResult:
        """One earlier week, in the shape ``/current`` returns.

        The edition-history affordance's read. Same payload shape on purpose:
        one renderer draws the page whichever week is showing, so a past week
        cannot drift into looking different from the present one.
        """
        try:
            store = _analyzer("dossier.store")
            network = load_network(network_path)
            shared = Path(network.get("sharedDir", CANONICAL_SHARED_DIR))
            try:
                _analyzer("dossier.window").parse_edition_id(week_id)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            weeks = _weeks_on_record(store, shared)
            if week_id not in weeks:
                return jsonify({
                    "ok": False,
                    "error": f"no week {week_id} on record",
                }), 404
            return jsonify(_render_week(
                store, shared, week_id, weeks, current_id=weeks[0],
                network=network,
            ))
        except Exception as e:  # pragma: no cover - defensive
            return error_response(e)

    @app.get("/api/dossier/profile")
    def api_dossier_profile_get() -> RouteResult:
        """The operator's arrangement of the page, or the empty one."""
        try:
            return jsonify({
                "ok": True,
                "profile": _analyzer("dossier.profile").load_profile(
                    _shared_dir()
                ),
            })
        except Exception as e:  # pragma: no cover - defensive
            return error_response(e)

    # ── the one write ──────────────────────────────────────────────────────

    @app.post("/api/dossier/profile")
    def api_dossier_profile_post() -> RouteResult:
        """Replace the operator's arrangement. Body: ``{order, hidden, ratings}``.

        Operator-scoped and pod-wide: it records how ONE person wants ONE page
        arranged. No bot writes it, no bot reads it, and nothing about a bot
        or a person is in it — only module ids.

        The whole profile is replaced, not merged, so an empty list can mean
        empty (see ``dossier.profile.save_profile``). Unrecognised entries are
        dropped by the store rather than 400'd: losing three good preferences
        because a fourth arrived malformed is the worse failure.
        """
        try:
            # cache=True: the body is read once and kept, so ``get_json``
            # below still sees it. Reading with cache=False here consumed
            # the stream and made every write look like an empty body.
            raw = request.get_data(cache=True) or b""
            if len(raw) > MAX_PROFILE_BODY:
                return jsonify({
                    "ok": False,
                    "error": "profile body too large",
                }), 413
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({
                    "ok": False,
                    "error": "expected a JSON object with order/hidden/ratings",
                }), 400
            profile = _analyzer("dossier.profile").save_profile(
                _shared_dir(), body, now=datetime.now(timezone.utc),
            )
            _log.info(
                "dossier profile saved: %d ordered, %d hidden, %d rated",
                len(profile["order"]), len(profile["hidden"]),
                len(profile["ratings"]),
            )
            return jsonify({"ok": True, "profile": profile})
        except OSError as e:
            return error_response(e, ok=False)
        except Exception as e:  # pragma: no cover - defensive
            return error_response(e, ok=False)


# ── payload assembly (module-level so the tests can call it directly) ───────


def _weeks_on_record(store: Any, shared: Path) -> list[str]:
    """Week ids that have a MODULE SET, newest first, bounded.

    The module sets are asked, not the editions: a week the page can SAY
    something about is a week whose synthesis exists. An edition with no
    module set beside it is a measurement nobody has worded yet, and listing
    it would offer the operator a week that renders blank.
    """
    ids = store.iter_module_ids(shared)
    return list(reversed(ids))[:MAX_HISTORY_WEEKS]


def _render_week(
    store: Any,
    shared: Path,
    week_id: str,
    weeks: list[str],
    *,
    current_id: str,
    network: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One week's module set plus everything the page needs to draw it."""
    payload = store.load_modules(shared, week_id)
    if payload is None:
        # Raced with a prune, or unreadable. Same shape as "nothing yet" —
        # the page has one empty state, not two.
        return {"ok": True, "available": False, "weeks": [], "modules": [],
                "weeks_on_record": 0}

    # Earlier weeks, newest first, for the trend series. Bounded and loaded
    # once for the whole page rather than once per module.
    earlier = [w for w in weeks if w < week_id][:MAX_HISTORY_WEEKS]
    priors = [p for p in (store.load_modules(shared, w) for w in earlier)
              if isinstance(p, dict)]
    name_for = _name_resolver(network or {}, shared)

    return {
        "ok": True,
        "available": True,
        "week": _week_view(payload, is_current=(week_id == current_id),
                           first_week=(len(weeks) == 1)),
        "weeks": [_week_index_entry(store, shared, w, current_id) for w in weeks],
        "weeks_on_record": len(weeks),
        "modules": [_module_view(payload, m, priors, name_for)
                    for m in _modules_of(payload)],
    }


def _modules_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    mods = payload.get("modules")
    return [m for m in mods if isinstance(m, dict)] if isinstance(mods, list) else []


def _week_view(
    payload: dict[str, Any], *, is_current: bool, first_week: bool = False,
) -> dict[str, Any]:
    """The week bar's own facts — including the one every card used to say.

    ``first_week_note`` is where "this pod has no history yet" lives, ONCE,
    above the grid. Every module used to append that sentence to its own
    headline, so the first page a new operator ever saw repeated one line
    four times. The sentence still comes from the synthesis registry (so the
    readability gate scores it); only its PLACE changed.
    """
    based = payload.get("based_on") or {}
    window = based.get("window") or {}
    first, last = window.get("first_date"), window.get("last_date")
    return {
        "id": payload.get("edition_id"),
        "label": _span_label(first, last),
        "first_date": first,
        "last_date": last,
        "first_week_note": _first_week_note() if first_week else None,
        # ``complete`` is the synthesis layer's word for "this week has
        # finished". Renamed on the way out because the page says "still
        # going" to an operator, not "incomplete window".
        "week_finished": bool(window.get("complete")),
        "is_current": is_current,
        "computed_at": payload.get("computed_at"),
    }


def _first_week_note() -> str | None:
    """The registry's first-week sentence, or ``None`` if it cannot be read.

    A page that loses one reassuring line because the analyzer package is
    momentarily unimportable is a smaller failure than a page that 500s, so
    this degrades rather than raising — the rest of the week bar is fine
    without it.
    """
    try:
        return _analyzer("dossier.headlines").say("page.first_week")
    except Exception:  # pragma: no cover - defensive
        return None


def _week_index_entry(
    store: Any, shared: Path, week_id: str, current_id: str,
) -> dict[str, Any]:
    """One row of the week switcher. Cheap: dates come from the module set."""
    payload = store.load_modules(shared, week_id) or {}
    window = (payload.get("based_on") or {}).get("window") or {}
    return {
        "id": week_id,
        "label": _span_label(window.get("first_date"), window.get("last_date")),
        "is_current": week_id == current_id,
    }


def _module_view(
    payload: dict[str, Any],
    module: dict[str, Any],
    priors: list[dict[str, Any]],
    name_for=None,
) -> dict[str, Any]:
    """One module card's worth of already-decided facts.

    ``critical`` is passed through untouched. The renderer is where design
    §4a rule 2 lives: a critical module renders whatever the profile's
    ``hidden`` list says, collapsed and de-emphasized rather than silenced.
    Enforcing it here as well — refusing to store the preference — would put
    one rule in two places and let them disagree; the page always holds this
    flag, so the page is the place that can always obey it.
    """
    module_id = str(module.get("module_id") or "")
    values = module.get("values")
    trend = module.get("trend") or {}
    history = _history(module_id, payload, module, priors)
    return {
        "module_id": module_id,
        "title": module.get("title"),
        "critical": bool(module.get("critical")),
        "measurable": bool(module.get("measurable")),
        "headline": module.get("headline"),
        "trend_chip": _trend_chip(trend),
        "facts": _facts(module_id, values),
        "bars": _bars(module_id, values, name_for),
        "strip": _strip(values),
        "remediation": _remediation(module),
        "history": history,
        # Why a card has no series, said out loud rather than left as an
        # empty array the page has to interpret — and said in THIS module's
        # words, from the synthesis registry. It used to be one sentence
        # this layer wrote for every card, which meant a young pod read the
        # same line four times down one screen.
        "history_note": None if len(history) >= 2
        else module.get("forward_note"),
        "detail": module.get("detail") or {},
        "trend": trend,
    }


def _remediation(module: dict[str, Any]) -> dict[str, Any] | None:
    """The card's "and here is how to fix it", or ``None``.

    The sentence comes from the synthesis registry (so the readability gate
    scored it); this layer only checks the shape and passes the admin surface
    through as a page id the browser can navigate to. **No command is ever
    carried** — naming a surface is remediation, printing a privileged shell
    line into a web page is a habit nobody should be taught.
    """
    raw = module.get("remediation")
    if not isinstance(raw, dict):
        return None
    note, page = raw.get("note"), raw.get("page")
    if not isinstance(note, str) or not note.strip():
        return None
    return {"note": note, "page": str(page or "") or None}


def _facts(module_id: str, values: Any) -> list[dict[str, Any]]:
    """The card's small label/value row, in plain English.

    Tri-state all the way down: a mapped field whose value is ``None`` is
    rendered as "not measured", never as 0 — the same rule the Apps surface
    follows, for the same reason. A field with no label is dropped.
    """
    if not isinstance(values, dict):
        return []
    out: list[dict[str, Any]] = []
    for field, label, kind in FACT_LABELS.get(module_id, ()):
        if field not in values:
            continue
        raw = values[field]
        if isinstance(raw, (dict, list)):
            continue
        out.append({
            "label": label,
            "value": _display(kind, raw),
            "measured": raw is not None,
        })
    return out


def _display(kind: str, raw: Any) -> str | None:
    """One value, spelled the way its OWN field is spelled."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        if kind == MONEY:
            return _money(raw)
        if kind == COUNT:
            return f"{int(raw):,}"
    return str(raw)


def _bars(module_id: str, values: Any, name_for=None) -> dict[str, Any] | None:
    """This week's ranked bar list for the module, else ``None``.

    Needs no history — which is why it is the visual a page can show on the
    day it is installed. :data:`BAR_SPECS` says which field holds the rows
    and how their number is spelled.

    TWO OR MORE ROWS, always: a one-bar bar chart is a stat with extra ink
    (house dataviz rules), and the fact row beside it already carries that
    number.

    Person rows get a display NAME resolved here rather than in the spine:
    the dossier stores ids, which do not go stale, and a name that changes
    should change on the page the next time it is drawn.
    """
    if not isinstance(values, dict):
        return None
    spec = BAR_SPECS.get(module_id)
    if spec is None:
        return None
    field, value_key, kind, unit_label = spec
    top = values.get(field)
    if not isinstance(top, list) or len(top) < 2:
        return None
    rows = []
    for entry in top:
        if not isinstance(entry, dict):
            continue
        value = entry.get(value_key)
        name = entry.get("name")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if not isinstance(name, str):
            continue
        rows.append({
            "label": _person_name(entry, name_for) or name,
            "value": value,
            "value_display": _display(kind, value) or str(value),
        })
    if len(rows) < 2:
        return None
    return {"rows": rows, "unit_label": unit_label}


def _name_resolver(network: dict[str, Any], shared: Path):
    """``(person_id, bots) -> label``, or ``None`` when nothing can resolve.

    ONE resolver, not a second one. Names come from
    ``roster_resolver.resolve_display_name`` — the same cache-only lookup
    the Usage page's By User rows use — through ``routes_analytics``'s own
    key split and fallback label, so a person is named identically on both
    surfaces. Cache-only matters: a page render must never fan out to a chat
    platform's directory to draw a bar.

    Resolution happens HERE, at read time, and never in the spine: the
    dossier stores the stable ``platform:senderId`` key, so a person who
    changes their display name changes it on the page rather than leaving
    two names in five years of weekly records.
    """
    try:
        from ..roster_resolver import resolve_display_name
        from .routes_analytics import (
            _fallback_user_label, _split_requester_key,
        )
    except Exception:  # pragma: no cover - defensive
        return None

    def name_for(person_id: str, bots: list[str]) -> str | None:
        platform, user_id = _split_requester_key(person_id)
        if not user_id:
            return None
        # The rollup key is pod-wide; the resolver is per-bot, so ask each
        # bot that saw this person and take the first name any of them knows.
        for bot_id in list(bots) or [""]:
            try:
                name, _source = resolve_display_name(
                    network, bot_id, platform, user_id, shared)
            except Exception:  # noqa: BLE001 — a name must never 500 a page
                continue
            if isinstance(name, str) and name.strip():
                return name
        return _fallback_user_label(platform, user_id)

    return name_for


def _person_name(entry: dict[str, Any], name_for) -> str | None:
    """One bar row's person label, or ``None`` to keep what synthesis wrote."""
    person_id = entry.get("person_id")
    if name_for is None or not isinstance(person_id, str) or not person_id:
        return None
    bots = entry.get("bots")
    try:
        label = name_for(person_id, bots if isinstance(bots, list) else [])
    except Exception:  # pragma: no cover - defensive
        return None
    return label if isinstance(label, str) and label.strip() else None


def _strip(values: Any) -> dict[str, Any] | None:
    """The day strip: one cell per day of the fire window, or ``None``.

    The reliability card's picture, and the one visual on this page that is
    about a HABIT rather than a total — 28 cells say "this ran every morning
    for a month" in a way no number does. Every cell carries its own words,
    so a value is never reachable only by hovering (house dataviz rule): the
    misses are listed under the strip as well.
    """
    if not isinstance(values, dict):
        return None
    strip = values.get("strip")
    if not isinstance(strip, dict):
        return None
    days = strip.get("days")
    if not isinstance(days, list) or not days:
        return None
    cells = []
    for day in days:
        if not isinstance(day, dict):
            continue
        state = str(day.get("state") or "")
        word = STRIP_WORDS.get(state)
        if word is None:
            continue
        label = _short_date(day.get("date")) or str(day.get("date") or "")
        cells.append({"state": state, "tip": f"{label} · {word}"})
    if not cells:
        return None
    missed = [d for d in (strip.get("missed_dates") or []) if isinstance(d, str)]
    missed_labels = [(_short_date(d) or d) for d in missed]
    return {
        "label": strip.get("name") or strip.get("app_id"),
        "cells": cells,
        "legend": [{"state": state, "text": text} for state, text in STRIP_LEGEND],
        # Named, not just coloured. "with the misses named" is the whole
        # reason this module exists; a red square nobody can read the date
        # off is a picture of a miss, not a report of one.
        "missed_label": (
            f"missed {', '.join(missed_labels)}" if missed_labels else None
        ),
    }


def _history(
    module_id: str,
    payload: dict[str, Any],
    module: dict[str, Any],
    priors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """This module's recorded number, week by week, oldest first.

    Every point is read out of a module set — the number synthesis recorded
    for that week — and nothing is re-derived. That is what makes the line
    honest across a producer that changed shape three months ago: each point
    still means what it meant when it was written.

    A week whose module set never recorded a number for this module is
    SKIPPED rather than plotted as zero, and the gap closes silently: a line
    that dips to the floor for a week the pod was switched off would be the
    chart telling a story the numbers do not.
    """
    points: list[dict[str, Any]] = []
    for prior_payload in reversed(priors):   # priors are newest-first
        prior = _find_module(prior_payload, module_id)
        if prior is None:
            continue
        point = _history_point(module_id, prior_payload, prior)
        if point is not None:
            points.append(point)
    current = _history_point(module_id, payload, module)
    if current is not None:
        points.append(current)
    return points


def _history_point(
    module_id: str, payload: dict[str, Any], module: dict[str, Any],
) -> dict[str, Any] | None:
    value = (module.get("trend") or {}).get("current")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    window = (payload.get("based_on") or {}).get("window") or {}
    first = window.get("first_date")
    return {
        "week": payload.get("edition_id"),
        "label": _short_date(first) or str(payload.get("edition_id") or ""),
        "value": value,
        "value_display": (_money(value) if module_id in MONEY_MODULES
                          else f"{int(value):,}"),
    }


def _find_module(payload: dict[str, Any], module_id: str) -> dict[str, Any] | None:
    for candidate in _modules_of(payload):
        if candidate.get("module_id") == module_id:
            return candidate
    return None


def _trend_chip(trend: dict[str, Any]) -> dict[str, Any]:
    """The glanceable form of the trend the module set already computed."""
    if not trend.get("available"):
        looked = trend.get("editions_looked_back") or 0
        # No chip at all on a pod's first week: the week bar says it once,
        # and four chips repeating it is the same noise in a smaller font.
        # A GAP still gets one — that is per-module and genuinely news.
        return {"text": TREND_NO_MATCH if looked else None, "direction": None}
    direction = trend.get("direction")
    pct = trend.get("percent_change")
    if direction == "flat" or direction is None:
        return {"text": TREND_FLAT, "direction": "flat"}
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        return {"text": f"{direction} {abs(round(pct * 100))}%",
                "direction": direction}
    return {"text": str(direction), "direction": direction}


def _money(value: float | int) -> str:
    """Matches ``dossier.modules._money`` — including the sub-cent rule.

    A second spelling of the same amount is a reader's first reason to
    distrust the page, so the reader formats money exactly as the synthesis
    that wrote the headline did.
    """
    amount = float(value)
    if 0 < amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _short_date(iso_date: Any) -> str | None:
    """``"2026-08-24" -> "Aug 24"``. ``None`` for anything else."""
    if not isinstance(iso_date, str):
        return None
    try:
        year, month, day = (int(p) for p in iso_date.split("-", 2))
        return f"{_MONTHS[month - 1]} {day}"
    except (ValueError, IndexError):
        return None


def _span_label(first: Any, last: Any) -> str | None:
    """``"Aug 24 – Aug 30"`` — what the operator calls a week."""
    start, end = _short_date(first), _short_date(last)
    if start and end:
        return f"{start} – {end}"
    return start or end
