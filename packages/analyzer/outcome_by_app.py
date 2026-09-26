#!/usr/bin/env python3
"""
outcome_by_app.py — the outcome view (3.2): did the app do the job?

Design: internal/design-application-platform-2026-09-22.md §3.2, D-AP4/D-AP6
(ratified 2026-09-22). Brief: internal/dispatch/done/
outcome-view-over-the-tracker-and-board-log.md. Sibling of ``usage_by_app.py``
(AL-1.3) — same shape, same output discipline — but where usage answers "who
used this app and at what cost", this answers "what did the app deliver, and
what did the human then do".

For one bot, rolls up its Tracker (the Board store's cards + per-day event
log, ``board_store.py`` / D-TM11) into per-application, per-user, per-day
counts: cards the bot moved or resolved, bot jobs finished without the
operator, reminders acknowledged or re-asked, and proposals accepted, edited
or dismissed. Writes ``{shared}/{bot}/outcome-by-app.json``, mode 0644 (the
coverage-file perms lesson, #3387 — same as ``usage_by_app.py``).

No model calls anywhere in this module (D-OH1, D-AP4/#5). Every count is a
rollup over records the Board store already writes; a measure this module
cannot derive from those records is left out, not guessed at.

Which application a card counts toward (DEVIATION, disclosed)
----------------------------------------------------------------
The Board store (``board_store.py``) carries no ``app_id`` field on a card or
a list — item 12 of the platform design (the app-facing contract) is named
there as still absent, and item 8 (State) says the "bindable store" pattern
is "one instance, not a contract". Rather than invent a card-level field this
chip's brief does not ask for (it touches this module, ``app_outcomes.py``,
the Apps page and this module's own test — never ``board_store.py``), this
rollup derives the application from the **list's own shape** (D-TM11 build
item 0, already on every list): a `project`-shape list is the **Project
Manager** application's ground truth (group work, human-quotable ids,
assignees); an `assistant`-shape list (every bot's personal board, including
the one every board is born with) is the **Assistant** application's. Those
two names — :data:`APP_ID_BY_LIST_SHAPE` — are the Tracker's own two
applications (D-TM11's vocabulary), not any one product's installed
``app_id``: this module never special-cases the personal assistant by name
(D-AP6, "platform not PA") — it is simply the first app with rows, on
whichever bot has one. A list of an unrecognised shape (there are only two,
:data:`board_store.LIST_SHAPES`) contributes no rows to either application
rather than being guessed into one.

The "user" a row is attributed to is the card's ``assignee`` (a project
list's human-quotable member) when present, or the owning list's one member
when the list has exactly one (an assistant list always does), or
``"unassigned"`` when neither resolves — never the event's ``actor``, which
is WHO ACTED (``user`` / ``bot`` / ``operator``), not WHICH person.

Tri-state honesty: a bot that has never written a card of a given list shape
produces no entry for that application at all (the same omission
``usage_by_app`` uses for an app with no attributed turns) — the reader,
:mod:`app_outcomes`, is what turns "no bot has an entry for this app_id" into
the explicit ``cannot-measure`` a page renders, never a fabricated zero.
Every count that DOES land carries the ``card_ids`` that produced it (capped
at :data:`MAX_SOURCE_IDS`, with ``truncated`` set past the cap) so a number
on the page can be opened.

Proposals (D-TM4) and the touch scheduler (D-TM3) are queued chips, not yet
built — see §7 of design-pa-tasks-and-follow-through-2026-09-18.md. Their
three counts and two counts respectively are defined here against the event
vocabulary those chips are expected to use (:data:`PROPOSAL_SOURCE` for a
captured-from-conversation card; ``touch`` events with ``action: "remind"``
for reminders, which :func:`board_store.record_touch` already lets anything
call by hand). Until those chips land, on a real pod these read as honest
zeros — no card has ever been captured or reminded that way yet — not as
``cannot-measure``: the Tracker itself has rows, this particular shape of
event simply has none. ``proposals_edited`` has no event to count until
D-TM4 ships a "corrected in chat" event; it is always 0 here, disclosed
rather than approximated from a nearby field.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from evolve_config import CANONICAL_SHARED_DIR
from evolve_util import now_iso_micro as _now_iso

log = logging.getLogger(__name__)

# v1 — the first cut (build item 1 of the brief above).
SCHEMA_VERSION = 1
OUTPUT_FILENAME = "outcome-by-app.json"
OUTPUT_MODE = 0o644

#: Trailing windows, in days — same convention as usage_by_app.WINDOWS.
WINDOWS: dict[str, int] = {"d1": 1, "d7": 7, "d30": 30}
MAX_WINDOW_DAYS = max(WINDOWS.values())

#: Source-row ids are provenance ("a number on the page can be opened"),
#: not a second event log — capped per metric per window so one chatty
#: card cannot blow up the payload.
MAX_SOURCE_IDS = 50

#: The Tracker's two applications (D-TM11), keyed by list SHAPE — see the
#: module docstring's "DEVIATION, disclosed" note.
APP_ID_BY_LIST_SHAPE: dict[str, str] = {
    "assistant": "assistant",
    "project": "project-manager",
}

METRICS: tuple[str, ...] = (
    "moved_by_bot",
    "resolved_by_bot",
    "jobs_finished_without_operator",
    "reminders_acknowledged",
    "reminders_reasked",
    "proposals_accepted",
    "proposals_edited",
    "proposals_dismissed",
)

#: The ``stocked`` event's ``source`` value D-TM4's capture-from-turns is
#: expected to write for a card proposed from conversation (module
#: docstring). Fixed here so this rollup and that (unbuilt) chip agree on
#: the convention without a second design pass.
PROPOSAL_SOURCE = "proposal"

#: Lanes a card is genuinely FINISHED in — mirrors board_store.SETTLED_LANES,
#: repeated as a plain tuple so this module has no import-time dependency on
#: board_store's internals beyond the reads in :func:`_load_board_lazy`.
_SETTLED_LANES = ("done", "dropped")

#: D-TM7 / the maintenance bot's existing nag rule (design-pa-tasks-and-
#: follow-through-2026-09-18.md §0): waiting on someone (or on the bot's own
#: delegated work) past this many days is "blocked", on the PM's Friday
#: report today.
BLOCKED_AFTER_DAYS = 3


# ── Board store access (lazy — see module docstring's DEVIATION note) ──────

def _board_store():
    """The Board store module, imported lazily.

    Same shape as ``pod_report.py``'s ``evolve_admin.alerts.dispatcher``
    import: analyzer and admin share one uv workspace (root pyproject.toml),
    so this resolves at runtime and in tests, but the import stays deferred
    and guarded so a pod whose admin package is not on this path degrades to
    "cannot measure" rather than failing the whole rollup at import time.
    """
    from evolve_admin import board_store  # pyright: ignore[reportMissingImports]
    return board_store


def _empty_metrics() -> dict[str, Any]:
    return {"count": 0, "card_ids": [], "truncated": False}


def _add_source(metrics: dict[str, Any], card_id: str) -> None:
    metrics["count"] += 1
    ids = metrics["card_ids"]
    if card_id not in ids:
        if len(ids) < MAX_SOURCE_IDS:
            ids.append(card_id)
        else:
            metrics["truncated"] = True


def _sum_metrics(parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    out = _empty_metrics()
    seen: set[str] = set()
    truncated = False
    for part in parts:
        truncated = truncated or bool(part.get("truncated"))
        for card_id in part.get("card_ids") or []:
            seen.add(card_id)
    out["count"] = sum(p.get("count", 0) for p in parts)
    ids = sorted(seen)
    out["truncated"] = truncated or len(ids) > MAX_SOURCE_IDS
    out["card_ids"] = ids[:MAX_SOURCE_IDS]
    return out


# ── Card index: which application + user a card belongs to ────────────────

def _card_index(board: dict[str, Any]) -> dict[str, tuple[str, str, dict[str, Any]]]:
    """``card_id -> (app_id, user_id, card)`` from the board's current state.

    A card's list membership and assignee essentially never change after
    creation (board_store has no "move to another list" verb), so reading
    this off the live ``board.json`` snapshot — rather than replaying it
    from history — is exact, not an approximation.
    """
    bs = _board_store()
    lists_by_id = {lst.get("list_id"): lst for lst in board.get("lists", [])}
    out: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for card in board.get("cards", []):
        list_id = card.get("list_id") or bs.DEFAULT_LIST_ID
        lst = lists_by_id.get(list_id) or {}
        app_id = APP_ID_BY_LIST_SHAPE.get(lst.get("shape") or "")
        if app_id is None:
            continue  # an unrecognised shape names no application (honest skip)
        members = lst.get("members") or []
        assignee = card.get("assignee")
        if assignee:
            user_id = assignee
        elif len(members) == 1:
            user_id = members[0].get("id") or "unassigned"
        else:
            user_id = "unassigned"
        out[card["id"]] = (app_id, user_id, card)
    return out


# ── Event log access ────────────────────────────────────────────────────────

def _iter_events(shared_dir: Path, bot_id: str) -> Iterator[dict[str, Any]]:
    """Every event ever written for this bot, in chronological order.

    Board event-log files are small, per-day JSONL (one row per interaction,
    not per turn), so reading the whole history — rather than windowing the
    read the way ``usage_by_app`` must for turn annotations — is the simple
    and correct choice here: :func:`_job_finished_without_operator` needs a
    card's full lifecycle, which may have started well outside any trailing
    window.
    """
    bs = _board_store()
    events_dir = bs.board_dir(shared_dir, bot_id) / "events"
    if not events_dir.is_dir():
        return
    for path in sorted(events_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _events_by_card(shared_dir: Path, bot_id: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in _iter_events(shared_dir, bot_id):
        card_id = ev.get("card")
        if isinstance(card_id, str) and card_id:
            out[card_id].append(ev)
    # Files are read in filename (date) order and each file's rows are
    # already append-ordered, so this is defensive rather than load-bearing —
    # it protects against clock skew or a hand-edited fixture, cheaply.
    for card_id, evs in out.items():
        evs.sort(key=lambda e: e.get("ts") or "")
    return out


def _day(ev: dict[str, Any]) -> str:
    ts = ev.get("ts")
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 else ""


#: ``per_day[metric][day][user_id]`` -> the set of card ids that fired that
#: metric, that day, for that user. The nesting is what makes both
#: "per app, per day" (sum across users) and "per app, per user, per day"
#: (one user's slice) readable off the same accumulator without a second
#: pass over the event log.
PerDay = dict[str, dict[str, dict[str, set]]]


def _record(per_day: PerDay, metric: str, day: str, user_id: str, card_id: str) -> None:
    if not day:
        return
    per_day[metric].setdefault(day, {}).setdefault(user_id, set()).add(card_id)


# ── Per-card classification ─────────────────────────────────────────────────

def _classify_card(
    card_id: str, user_id: str, evs: list[dict[str, Any]], per_day: PerDay,
) -> None:
    """Fold one card's full event history into ``per_day``."""
    _classify_moves_and_resolutions(card_id, user_id, evs, per_day)
    _classify_job_lifecycle(card_id, user_id, evs, per_day)
    _classify_reminders(card_id, user_id, evs, per_day)
    _classify_proposal(card_id, user_id, evs, per_day)


def _classify_moves_and_resolutions(
    card_id: str, user_id: str, evs: list[dict[str, Any]], per_day: PerDay,
) -> None:
    for ev in evs:
        event, actor, day = ev.get("event"), ev.get("actor"), _day(ev)
        if not day or actor != "bot":
            continue
        if event == "triaged":
            metric = "resolved_by_bot" if ev.get("to") == "done" else "moved_by_bot"
            _record(per_day, metric, day, user_id, card_id)
        elif event == "dropped":
            _record(per_day, "resolved_by_bot", day, user_id, card_id)
        elif event == "delegation" and ev.get("to") == "done":
            _record(per_day, "resolved_by_bot", day, user_id, card_id)


#: An owner-of-bot job's terminal shape — the transitions that CLOSE a
#: delegation, whoever caused them. Distinct from :data:`_SETTLED_LANES`
#: because a ``delegation`` event to ``blocked`` also ends the window this
#: metric measures (the bot is no longer working it unattended) without
#: settling the card's lane.
_JOB_TERMINAL_DELEGATION_STATES = ("done", "blocked")


def _classify_job_lifecycle(
    card_id: str, user_id: str, evs: list[dict[str, Any]], per_day: PerDay,
) -> None:
    """D-AP6's second M1 measure: a bot job whose terminal outcome carries no
    human action between start (a hand-over to the bot) and finish.

    Start: an ``assigned`` event with ``to: "bot"``. Finish: the next
    ``delegation`` event reaching a terminal state, or a ``triaged``/
    ``dropped`` settling event, whichever comes first. Any event in between
    with ``actor: "user"`` — an approval decision, a manual move, a
    touch the person recorded themselves — taints the window: the job still
    finished, but not *without* the operator.
    """
    job_open = False
    tainted = False
    for ev in evs:
        event, actor = ev.get("event"), ev.get("actor")
        if actor == "user" and job_open:
            tainted = True
        is_terminal = job_open and (
            (event == "delegation" and ev.get("to") in _JOB_TERMINAL_DELEGATION_STATES)
            or (event == "triaged" and ev.get("to") in _SETTLED_LANES)
            or event == "dropped"
        )
        if is_terminal:
            if not tainted and actor == "bot":
                day = _day(ev)
                _record(per_day, "jobs_finished_without_operator", day, user_id, card_id)
            job_open = False
            tainted = False
        if event == "assigned" and ev.get("to") == "bot":
            job_open = True
            tainted = False


def _classify_reminders(
    card_id: str, user_id: str, evs: list[dict[str, Any]], per_day: PerDay,
) -> None:
    """D-TM3's two follow-through measures, defined against the ``touch``
    event ``record_touch`` already writes (module docstring)."""
    for i, ev in enumerate(evs):
        if ev.get("event") != "touch" or ev.get("action") != "remind":
            continue
        day = _day(ev)
        if not day:
            continue
        prev_ev = evs[i - 1] if i > 0 else None
        if (prev_ev is not None and prev_ev.get("event") == "touch"
                and prev_ev.get("action") == "remind"):
            _record(per_day, "reminders_reasked", day, user_id, card_id)
        next_ev = evs[i + 1] if i + 1 < len(evs) else None
        if next_ev is not None and next_ev.get("actor") == "user":
            _record(per_day, "reminders_acknowledged", day, user_id, card_id)


def _classify_proposal(
    card_id: str, user_id: str, evs: list[dict[str, Any]], per_day: PerDay,
) -> None:
    """D-TM4's "swipe" question, made a measured rule (design §3.2)."""
    stocked = next((e for e in evs if e.get("event") == "stocked"), None)
    if stocked is None or stocked.get("source") != PROPOSAL_SOURCE:
        return
    for ev in evs:
        event, actor, day = ev.get("event"), ev.get("actor"), _day(ev)
        if actor != "user" or not day:
            continue
        if event == "dropped":
            _record(per_day, "proposals_dismissed", day, user_id, card_id)
            return
        if event == "triaged":
            _record(per_day, "proposals_accepted", day, user_id, card_id)
            return
    # proposals_edited: no "corrected in chat" event exists yet (D-TM4 is
    # unbuilt) — see the module docstring. Always 0 until that chip lands.


# ── Backlog (the Project Manager's Friday-report figures, design §3.2) ─────

def _parse_iso_date(value: Any) -> "datetime | None":
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_blocked(card: dict[str, Any], today: datetime) -> bool:
    waiting_on = card.get("waiting_on")
    if isinstance(waiting_on, dict):
        since = _parse_iso_date(waiting_on.get("since"))
        if since and (today - since) > timedelta(days=BLOCKED_AFTER_DAYS):
            return True
    delegation = card.get("delegation")
    if isinstance(delegation, dict) and delegation.get("state") == "blocked":
        updated = _parse_iso_date(delegation.get("updated_at"))
        if updated and (today - updated) > timedelta(days=BLOCKED_AFTER_DAYS):
            return True
    return False


def _backlog_for(cards: list[dict[str, Any]], *, today: datetime) -> dict[str, Any]:
    """As-of-today backlog figures — open count, age, blocked-over-3-days.

    Generic Tracker figures (design §3.2's "rows here too"), not gated to
    any one application: the same shape the Project Manager's existing
    Friday Slack report already uses (design-pa-tasks-and-follow-through
    §0), computed here from the Tracker directly so parity week (D-TM9) can
    compare the two by eye.
    """
    open_cards = [c for c in cards if c.get("lane") not in _SETTLED_LANES]
    ages = []
    for card in open_cards:
        created = _parse_iso_date(card.get("created_at"))
        if created:
            ages.append(max(0.0, (today - created).total_seconds() / 86400))
    blocked = sum(1 for c in open_cards if _is_blocked(c, today))
    return {
        "open_cards": len(open_cards),
        "backlog_age_p50_days": (
            round(statistics.median(ages), 1) if ages else None),
        "backlog_age_max_days": round(max(ages), 1) if ages else None,
        "blocked_over_3_days": blocked,
    }


# ── Rollup ───────────────────────────────────────────────────────────────────

def rollup_bot(shared_dir: Path, bot_id: str, *, today: date | None = None) -> dict[str, Any]:
    """Fold one bot's Tracker into the outcome-by-app payload dict.

    Pure: reads only ``{shared}/boards/<bot_id>/`` and returns the payload.
    The caller writes it (:func:`write_outcome_by_app`).
    """
    shared_dir = Path(shared_dir)
    if today is None:
        today = datetime.now(timezone.utc).date()
    now_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    bs = _board_store()
    board = bs.load_board(shared_dir, bot_id)
    index = _card_index(board)
    events_by_card = _events_by_card(shared_dir, bot_id)

    # app_id -> metric -> day -> user_id -> {card_ids}
    per_app: dict[str, PerDay] = defaultdict(lambda: {m: {} for m in METRICS})
    cards_by_app: dict[str, list[dict[str, Any]]] = defaultdict(list)
    users_by_app: dict[str, set[str]] = defaultdict(set)

    for card_id, (app_id, user_id, card) in index.items():
        cards_by_app[app_id].append(card)
        users_by_app[app_id].add(user_id)
        _classify_card(card_id, user_id, events_by_card.get(card_id, []), per_app[app_id])

    apps_out: dict[str, Any] = {}
    for app_id, per_day in per_app.items():
        apps_out[app_id] = _assemble_app(
            per_day, today=today, users=users_by_app[app_id],
            backlog=_backlog_for(cards_by_app[app_id], today=now_dt),
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "bot_id": bot_id,
        "as_of_date": today.isoformat(),
        "windows": dict(WINDOWS),
        "apps": apps_out,
    }


def _window_totals(
    per_day: PerDay, *, today: date, span: int, only_user: "str | None" = None,
) -> dict[str, Any]:
    """One window's per-metric totals — every user's rows summed, or (with
    ``only_user``) just one user's slice of the same accumulator."""
    in_window = {m: [] for m in METRICS}
    for offset in range(span):
        day = (today - timedelta(days=offset)).isoformat()
        for metric in METRICS:
            by_user = per_day[metric].get(day) or {}
            if only_user is not None:
                ids = by_user.get(only_user)
                ids = set(ids) if ids else set()
            else:
                ids = set().union(*by_user.values()) if by_user else set()
            if ids:
                in_window[metric].append(
                    {"count": len(ids), "card_ids": sorted(ids)[:MAX_SOURCE_IDS],
                     "truncated": len(ids) > MAX_SOURCE_IDS})
    return {
        metric: _sum_metrics(in_window[metric]) if in_window[metric]
        else _empty_metrics()
        for metric in METRICS
    }


def _assemble_app(
    per_day: PerDay, *, today: date, users: Iterable[str], backlog: dict[str, Any],
) -> dict[str, Any]:
    windows = {key: _window_totals(per_day, today=today, span=span)
               for key, span in WINDOWS.items()}
    users_out = {
        user_id: {key: _window_totals(per_day, today=today, span=span, only_user=user_id)
                  for key, span in WINDOWS.items()}
        for user_id in sorted(users)
    }

    daily: dict[str, dict[str, int]] = {}
    for offset in range(MAX_WINDOW_DAYS):
        day = (today - timedelta(days=offset)).isoformat()
        daily[day] = {}
        for metric in METRICS:
            by_user = per_day[metric].get(day) or {}
            daily[day][metric] = len(set().union(*by_user.values())) if by_user else 0

    return {"windows": windows, "users": users_out, "daily": daily, "backlog": backlog}


# ── Output ───────────────────────────────────────────────────────────────────

def outcome_by_app_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / bot_id / OUTPUT_FILENAME


def write_outcome_by_app(
    shared_dir: Path, bot_id: str, payload: dict[str, Any], *, dry_run: bool = False,
) -> Path:
    """Atomically write the payload to {shared}/{bot}/outcome-by-app.json.

    Same tmp+rename-with-mode-pinned-before-rename discipline as
    ``usage_by_app.write_rollup_json`` (0644 — the coverage-file perms
    lesson, #3387): ``mkstemp`` creates 0600 and ``os.replace`` would carry
    that mode onto the destination, locking every reader but the writing
    user out of a file every reader (evo, the admin API) needs.
    """
    out_path = outcome_by_app_path(shared_dir, bot_id)
    if dry_run:
        print(f"[outcome-by-app] [dry-run] would write {out_path}")
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out_path.parent), prefix=".outcome-by-app-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
        os.chmod(tmp, OUTPUT_MODE)
        os.replace(tmp, out_path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            # Already gone (interrupted rename) — nothing to clean up.
            print(f"[outcome-by-app] temp file vanished during cleanup: {tmp}",
                  file=sys.stderr)
        raise
    return out_path


def load_outcome_by_app(shared_dir: Path, bot_id: str) -> dict[str, Any]:
    """Read one bot's rollup. Returns ``{}`` when absent or unreadable.

    An empty dict means "no rollup yet" — callers (``app_outcomes.py``)
    render that as ``cannot-measure``, never as zero outcomes.
    """
    path = outcome_by_app_path(shared_dir, bot_id)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# ── Orchestrator ─────────────────────────────────────────────────────────────

def run_outcome_by_app(
    bot_id: str, shared_dir: Path, *, dry_run: bool = False, report: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    payload = rollup_bot(shared_dir, bot_id, today=today)
    if report:
        _print_report(payload)
        return payload
    write_outcome_by_app(shared_dir, bot_id, payload, dry_run=dry_run)
    if not dry_run:
        apps = payload.get("apps") or {}
        print(f"[outcome-by-app] {bot_id}: {len(apps)} application(s) with Tracker rows")
    return payload


def _print_report(payload: dict[str, Any]) -> None:
    apps = payload.get("apps") or {}
    print(f"\n{payload.get('bot_id')} — outcomes (7d window)")
    if not apps:
        print("  no application has any Tracker rows on this bot")
        return
    for app_id, entry in sorted(apps.items()):
        d7 = entry.get("windows", {}).get("d7", {})
        print(f"\n{app_id}:")
        for metric in METRICS:
            count = (d7.get(metric) or {}).get("count", 0)
            if count:
                print(f"  {metric}: {count}")
        backlog = entry.get("backlog") or {}
        print(f"  backlog: {backlog}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Outcome view rollup (design §3.2) — Tracker + board log")
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--network", help="Path to network.json (processes all bots)")
    parser.add_argument("--bot", dest="bot_id", help="Single bot to process")
    parser.add_argument("--dry-run", action="store_true", help="Compute without writing")
    parser.add_argument("--report", action="store_true", help="Print the 7d table, no file writes")
    args = parser.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bots: list[str] = []

    if args.bot_id:
        bots = [args.bot_id]
    elif args.network:
        try:
            net = json.loads(Path(args.network).read_text())
            shared_dir = Path(net.get("sharedDir", str(shared_dir)))
            bots = list(net.get("members", []))
        except Exception as exc:
            print(f"[outcome-by-app] Failed to read network.json: {exc}", file=sys.stderr)
            return 1
    else:
        net_path = shared_dir / "network.json"
        if net_path.exists():
            try:
                bots = list(json.loads(net_path.read_text()).get("members", []))
            except Exception as exc:
                print(f"[outcome-by-app] Failed to read {net_path}: {exc}", file=sys.stderr)

    if not bots:
        parser.error("Specify --bot BOT_ID, --network PATH, or ensure network.json exists")

    for bot in bots:
        try:
            run_outcome_by_app(bot, shared_dir, dry_run=args.dry_run, report=args.report)
        except Exception as exc:  # one bot's failure must not stop the sweep
            print(f"[outcome-by-app] {bot}: rollup failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
