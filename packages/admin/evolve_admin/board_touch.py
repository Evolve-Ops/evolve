"""board_touch.py — D-TM3/6/7/10: the daemon owns the clock.

Design: ``internal/design-pa-tasks-and-follow-through-2026-09-18.md`` §1,
D-TM3 (the scheduler itself), D-TM5 (the pace table :func:`board_store.
next_touch_for` already implements), D-TM6 (two reminders then ``ask``),
D-TM7 (``owner: other`` escalates to a drafted nudge). Every card with a
``next_touch`` carries a promise; this module is the process that keeps
it, so a missed follow-up is a MEASURED miss (D-TM10), never silence.

:func:`sweep` is the whole daemon job — installed every 5 minutes (see
"Not wired into deploy.py" below). One tick: find cards whose ``next_touch``
has arrived, hand each off by its ``touch_action`` (:data:`board_store.
TOUCH_ACTIONS`), then ledger anything that went due and was never resolved
within its window as a miss.

**Platform contract (D-AP1, `design-application-platform-2026-09-22.md`
§4).** This chip is what makes the ``schedule.touch(card, when, zone)``
verb real: :func:`sweep` fires a touch when ``when`` (``next_touch``)
arrives, and ``board_store.set_pace``/``record_touch`` now accept ``tz``
("zone") so a fired touch reschedules in the pod's own timezone
(:func:`evolve_admin.config.resolve_pod_timezone`), not hardcoded UTC. No
PA-private state: everything here is a read or a write of the Board/
Tracker store (D-TM11) through existing writers; no new store, no reminder
path that calls a model, no roster-by-file read, no PA-only notification
route (the send path is the ``delivery.send_to_owner`` contract row,
:func:`evolve_admin.app_contract.send_to_owner` over the dispatcher's
per-bot primitive — the same surface a second app would use).

**Deviations from the brief** (recorded, not silently worked around —
same convention ``board_worker.py``'s module docstring uses):

1. **``check_source`` ships as a pluggable, per-source-kind probe
   interface whose SHIPPED probes all return ``"unknown"``.** The D-EM
   email-thread-tracking chips (``email-todo-rubric-and-extraction``,
   ``email-thread-tracking-and-waiting-on-them``) and any calendar-move /
   document-watch equivalent are still queued, unbuilt on ``main`` — there
   is no real "has the thread got a reply" primitive to call yet. Per
   D-CS7 ("a control names the subject it judged and reports ``unknown``
   when it cannot"), the stub probes say so honestly rather than
   fabricating "unchanged" (which would silently manufacture a back-off
   the pod has no evidence for). :data:`DEFAULT_PROBES` is the seam: a
   real integration replaces one dict entry, nothing else here changes.
   Tests supply fixture probes to exercise the changed/unchanged/
   escalation logic without a real integration.
2. **No launchd wiring in this PR.** Same reason as ``board_worker.py``
   deviation 6: ``deploy.py``/``cli.py`` are both sitting on
   ``tools/file-size-baseline.txt``'s frozen line count. Run manually
   for now:

       python3 -m evolve_admin.board_touch_runner --shared-dir /Users/Shared/evolve

   Wiring one ``_install_launchd_board_touch`` call (StartInterval 300s,
   same shape as ``_install_launchd_delivery_monitor``) is a one-function,
   near-zero-net-growth follow-up.
3. **The miss ledger reuses ``board_worker``'s ledger, not a second one.**
   Every touch miss is written through :func:`board_worker.
   record_delivery_outcome` with ``action_id="touch:<verb>"`` (human
   legibility only) and ``producer="board_touch"`` (the actual,
   collision-proof discriminator :func:`_count_touch_misses` reads). One
   ledger the pod report reads, per the brief's "same sweep the delivery
   monitor uses."
4. **No cross-process lock between this daemon and ``board_worker``'s.**
   Both are independent processes read-modify-writing the same
   ``board.json`` (plain JSON, no ``flock``) — the same lockless-writer
   exposure ``board_worker.py``'s route-layer callers already carry today
   (its own module docstring notes the in-process ``WRITE_LOCK`` is never
   taken by a daemon caller either); adding this chip's daemon as a third
   writer widens an existing gap rather than introducing a new one. A
   real fix is a file lock shared by both runners — a follow-up that
   touches ``board_worker.py``'s own save cycle too, out of this chip's
   scope.

**Guardrail.** Neither :func:`sweep` nor anything it calls for ``remind``
references a model client — there is no parameter, import, or code path
here that could construct one. ``do_action``/``check_source``-changed
hand off to the ALREADY-EXISTING, separately-daemonized D-BI4 worker
(``board_worker.py``) via the same ``instruction``/``assigned`` events a
UI tap produces; whatever model call that worker later makes is its own,
pre-existing, reviewed path — never spent by the scheduler tick itself.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import app_contract
from . import board_store
from . import board_worker
from . import config

log = logging.getLogger(__name__)

#: D-TM3's marker event: "a card is due." Appended before dispatch (the
#: idempotency guard AND the miss-sweep's own evidence trail).
TOUCH_DUE_EVENT = "touch_due"

#: D-TM6: after this many trailing ``remind`` touches with nothing else
#: interrupting the streak, the NEXT one fires ``ask`` instead.
REMIND_ASK_THRESHOLD = 2

#: item 6's default window for the two synchronous, zero-model actions.
#: ``do_action``/``check_source`` reuse the worker's own configured window
#: (:func:`board_worker.worker_config`) — "the delivery window for the
#: worker paths."
DEFAULT_TOUCH_WINDOW_MINUTES = 60

#: Prefix on the shared ledger's ``action_id``, for human legibility only
#: (a glance at the ledger tells the two classes apart). NOT the
#: discriminator a reader filters on — that is :data:`TOUCH_PRODUCER`,
#: written into the row's ``producer`` field; an ``action_id`` string is a
#: real D-BI5 action name elsewhere and nothing stops a future one from
#: starting with this prefix too.
TOUCH_MISS_PREFIX = "touch:"

#: This module's ledger-row discriminator (:func:`board_worker.
#: record_delivery_outcome`'s ``producer``) — the actual, collision-proof
#: way :func:`_count_touch_misses` tells a touch miss from a delegation
#: miss in the shared ledger.
TOUCH_PRODUCER = "board_touch"

#: :func:`_sweep_misses`'s scan look-back — wide enough that a daemon
#: outage (box asleep/rebooted over a weekend) doesn't let a stale
#: ``touch_due`` age out of the scan range before it's ever examined.
#: Bounded by :func:`_recent_days`'s own 8-day hard cap.
MISS_SWEEP_LOOKBACK_MINUTES = 7 * 24 * 60

#: D-TM7's drafted-nudge action id (added to ``card["actions"]``, never
#: auto-sent — D-BI4's approval gate handles "sent only on approval per
#: the rung" at execution time via this entry's ``act`` field).
NUDGE_ACTION_ID = "drafted_nudge"


CheckSourceProbe = Callable[[dict[str, Any], dict[str, Any]], str]


def _probe_unknown(card: dict[str, Any], network: dict[str, Any]) -> str:  # noqa: ARG001
    """The shipped stand-in for every source kind (deviation 1): honestly
    "I cannot check this yet," never a fabricated "unchanged."""
    return "unknown"


#: {source kind -> probe}. A probe returns ``"changed"``, ``"unchanged"``,
#: or ``"unknown"``. Keyed by ``waiting_on.source`` first, falling back to
#: the card's own ``source``, matching :func:`_source_kind`.
DEFAULT_PROBES: dict[str, CheckSourceProbe] = {
    "email": _probe_unknown,
    "calendar": _probe_unknown,
    "document": _probe_unknown,
}


def _resolve_tz(network: dict[str, Any]) -> Any:
    """The pod's own zone (D-AP1's ``schedule.touch(card, when, zone)``) —
    threaded into :func:`board_store.record_touch`/``set_pace`` so a fired
    touch reschedules in local wall-clock time, not hardcoded UTC. Falls
    back to ``None`` (``next_touch_for``'s own UTC default) if the
    configured name is somehow invalid; a bad tz string must not crash
    the sweep.
    """
    try:
        return ZoneInfo(config.resolve_pod_timezone(network))
    except Exception:  # noqa: BLE001 — see docstring
        return None


def _source_kind(card: dict[str, Any]) -> str | None:
    waiting_on = card.get("waiting_on")
    if isinstance(waiting_on, dict) and waiting_on.get("source"):
        return waiting_on["source"]
    return card.get("source")


def _trailing_streak(
    card: dict[str, Any], *, action: str, results: "tuple[str, ...] | None" = None,
) -> int:
    """Count TRAILING ``touches[]`` entries matching ``action`` (and, if
    given, whose ``result`` is in ``results``), stopping at the first touch
    that doesn't match. Same shape as :func:`board_store.next_touch_for`'s
    own trailing-streak read — "no movement" is "nothing else interrupted
    the run of this one action."
    """
    streak = 0
    for touch in reversed(card.get("touches") or []):
        if not isinstance(touch, dict) or touch.get("action") != action:
            break
        if results is not None and touch.get("result") not in results:
            break
        streak += 1
    return streak


def _enrichment_value(card: dict[str, Any], field: str) -> Any:
    e = card.get("enrichment")
    if not isinstance(e, dict):
        return None
    wrapper = e.get(field)
    return wrapper.get("value") if isinstance(wrapper, dict) else None


def compose_remind_message(card: dict[str, Any]) -> str:
    """D-TM3's deterministic reminder text: title, outcome, due, and
    whatever the card already carries (contacts, location, links, a prior
    draft) — pure string assembly, no model call, ever.
    """
    lines = [card.get("title") or "Untitled"]
    outcome = (card.get("outcome") or "").strip()
    if outcome:
        lines.append(outcome)
    due = card.get("due")
    if due:
        lines.append(f"Due: {due}")
    contacts = _enrichment_value(card, "contacts")
    if isinstance(contacts, list):
        for c in contacts:
            if not isinstance(c, dict):
                continue
            bits = [b for b in (c.get("name"), c.get("phone"), c.get("email"))
                    if isinstance(b, str) and b.strip()]
            if bits:
                lines.append(" · ".join(bits))
    location = _enrichment_value(card, "location")
    if isinstance(location, dict) and location.get("text"):
        maps_url = location.get("maps_url")
        lines.append(f"{location['text']} ({maps_url})" if maps_url else location["text"])
    links = _enrichment_value(card, "links")
    if isinstance(links, list):
        for entry in links:
            if isinstance(entry, dict) and entry.get("url"):
                lines.append(f"{entry.get('label') or 'link'}: {entry['url']}")
    draft = ((card.get("delegation") or {}).get("result") or {}).get("text")
    if isinstance(draft, str) and draft.strip():
        lines.append(f"Draft: {draft.strip()}")
    return "\n".join(lines)


def _hand_off_to_worker(
    shared_dir: Path, bot_id: str, card_id: str, *, context: str, actor: str,
    now: datetime | None = None,
) -> None:
    """D-BI4's wake, forced even when the card is already ``owner: bot`` —
    unlike :func:`board_store.assign_card`'s idempotent no-op (a UI
    double-tap of the SAME assignment), a source change is new information
    the worker has not seen yet and must wake on regardless of the card's
    current owner.

    ``now`` — same injected-clock discipline as :func:`board_store.
    record_touch`: the sweep's own tick clock, not the wall clock, so a
    fixture test's timeline stays internally consistent.

    Refuses to clobber a delegation the worker is ALREADY actively
    progressing (``accepted``/``in_progress``/``returned_for_review``):
    resetting it to ``offered`` would erase real progress, and since
    ``card["instructed"]`` from that run is still set,
    ``board_worker``'s own ``assigned`` handler would then skip the new
    event outright (its guard is "not yet instructed") — leaving the card
    stuck at ``offered`` forever with no path back. The caller
    (:func:`_fire_check_source`) also clears the card's pace on a
    successful hand-off so this function is never reached a second time
    for the same delegation via the touch scheduler's own path; this
    check is the belt to that suspenders.
    """
    board = board_store.load_board(shared_dir, bot_id)
    card = board_store.find_card(board, card_id)
    if card is None:
        return
    delegation = card.get("delegation")
    if isinstance(delegation, dict) and delegation.get("state") in (
        "accepted", "in_progress", "returned_for_review",
    ):
        return
    from_owner = card.get("owner") or "me"
    card["owner"] = "bot"
    card["delegation"] = {"state": "offered", "updated_at": board_store._utcnow(now)}  # noqa: SLF001
    card.pop("waiting_on", None)
    board_store.save_board(shared_dir, bot_id, board)
    board_store.append_event(shared_dir, bot_id, {
        "event": "assigned", "card": card_id, "title": card.get("title"),
        "from": from_owner, "to": "bot", "context": context, "actor": actor,
    }, now=now)


def _add_drafted_nudge(shared_dir: Path, bot_id: str, card_id: str, *, actor: str) -> None:
    action = {
        "id": NUDGE_ACTION_ID, "label": "Draft a nudge",
        "kind": "llm", "_integration": None, "act": "send_nudge",
    }
    board_store.add_card_action(shared_dir, bot_id, card_id, action, actor=actor)


def _fire_remind(
    shared_dir: Path, bot_id: str, network: dict[str, Any], card: dict[str, Any], *,
    now: datetime, tz: Any = None,
) -> dict[str, Any]:
    if _trailing_streak(card, action="remind") >= REMIND_ASK_THRESHOLD:
        return _fire_ask(shared_dir, bot_id, network, card, now=now, tz=tz)
    message = compose_remind_message(card)
    ok, err = app_contract.send_to_owner(bot_id, network, message)
    if not ok:
        raise RuntimeError(f"remind delivery failed for {bot_id}/{card['id']}: {err}")
    board_store.record_touch(
        shared_dir, bot_id, card["id"], action="remind", result="reminded",
        actor=bot_id, tz=tz, now=now)
    return {"bot_id": bot_id, "card_id": card["id"], "touch_action": "remind", "result": "reminded"}


def _fire_ask(
    shared_dir: Path, bot_id: str, network: dict[str, Any], card: dict[str, Any], *,
    now: datetime, tz: Any = None,
) -> dict[str, Any]:
    """D-TM6/item 5: the card rises to the Stack head with one question
    (*still on? keep / drop / later-than-later*). The Stack is a pure VIEW
    over lane + delegation state (``board_stack.stack_order``) — there is
    no separate "Stack lane" to move into. Raising the card to ``inbox``
    (if it isn't already) is what makes it appear there; ``drop`` already
    has "later than later" in :data:`board_store.DROP_REASONS` and a
    snooze-to-``later`` chip already exists (D-ST4) — the UI's existing
    swipe vocabulary answers the question, nothing new to build here.
    """
    card_id = card["id"]
    if card.get("lane") != "inbox":
        board_store.move_card(shared_dir, bot_id, card_id, "inbox", actor=bot_id, now=now)
    board_store.record_touch(
        shared_dir, bot_id, card_id, action="ask", result="asked", actor=bot_id, tz=tz, now=now)
    return {"bot_id": bot_id, "card_id": card_id, "touch_action": "ask", "result": "asked"}


def _fire_check_source(
    shared_dir: Path, bot_id: str, network: dict[str, Any], card: dict[str, Any], *,
    now: datetime, tz: Any = None, probes: dict[str, CheckSourceProbe],
) -> dict[str, Any]:
    card_id = card["id"]
    kind = _source_kind(card)
    probe = probes.get(kind or "", _probe_unknown)
    outcome = probe(card, network)
    if outcome == "changed":
        _hand_off_to_worker(
            shared_dir, bot_id, card_id,
            context=f"check_source: {kind or 'source'} changed", actor=bot_id, now=now)
        board_store.record_touch(
            shared_dir, bot_id, card_id, action="check_source", result="changed",
            actor=bot_id, tz=tz, now=now)
        # The card is now the D-BI4 worker's to progress (accept/decline,
        # then act). Clearing pace stops THIS scheduler from ever firing
        # check_source on it again while delegation is live — which would
        # otherwise re-enter `_hand_off_to_worker` and, absent that
        # function's own guard, clobber the worker's in-progress state.
        board_store.set_pace(
            shared_dir, bot_id, card_id, cadence=None, next_touch=None,
            touch_action=None, actor=bot_id, now=now)
        return {"bot_id": bot_id, "card_id": card_id, "touch_action": "check_source", "result": "changed"}
    if outcome == "unchanged":
        result = "no_change"
        # D-TM7: a SECOND consecutive unchanged probe on an owner:other card
        # (this one about to be the second, i.e. a first already happened)
        # earns a drafted nudge — never sent automatically (D-BI4's approval
        # gate handles that when the action is later tapped/instructed).
        if (card.get("owner") == "other"
                and _trailing_streak(card, action="check_source",
                                      results=board_store.NO_MOVEMENT_RESULTS) >= 1):
            _add_drafted_nudge(shared_dir, bot_id, card_id, actor=bot_id)
        board_store.record_touch(
            shared_dir, bot_id, card_id, action="check_source", result=result,
            actor=bot_id, tz=tz, now=now)
        return {"bot_id": bot_id, "card_id": card_id, "touch_action": "check_source", "result": result}
    # "unknown" — the pod cannot yet observe this source (deviation 1).
    # Recorded honestly (D-CS7); never folded into "unchanged", which would
    # fabricate a back-off the pod has no evidence for.
    board_store.record_touch(
        shared_dir, bot_id, card_id, action="check_source", result="unknown",
        actor=bot_id, tz=tz, now=now)
    return {"bot_id": bot_id, "card_id": card_id, "touch_action": "check_source", "result": "unknown"}


def _fire_do_action(
    shared_dir: Path, bot_id: str, network: dict[str, Any], card: dict[str, Any], *,
    now: datetime, tz: Any = None,
) -> dict[str, Any]:
    """D-BI5's existing worker path, reused: append the same ``instruction``
    event a UI tap produces and let the already-daemonized D-BI4 worker
    (``board_worker.py``) execute it. Which action fires: the card's own
    ``instructed`` marker (the action a prior tap/touch already named) when
    present, else the first of the card's ``actions[]`` — the card carries
    at most one obvious candidate for a scheduled "do it" touch, and
    :data:`board_actions.MAX_ACTIONS_PER_CARD` keeps that list short. This
    chip's own interpretive call: the schema (task-card-carries-outcome-
    owner-and-pace) names no dedicated "which action" field.
    """
    card_id = card["id"]
    instructed = card.get("instructed")
    if not isinstance(instructed, dict):
        instructed = {}
    action_id = instructed.get("action_id")
    label = instructed.get("label")
    est_cost = instructed.get("est_cost")
    if not action_id:
        actions = card.get("actions") or []
        first = actions[0] if actions and isinstance(actions[0], dict) else None
        if first is None:
            board_store.record_touch(
                shared_dir, bot_id, card_id, action="do_action",
                result="no_action_available", actor=bot_id, now=now)
            return {"bot_id": bot_id, "card_id": card_id, "touch_action": "do_action",
                    "result": "no_action_available"}
        action_id = first.get("id")
        label = first.get("label")
        est_cost = None
    board_store.instruct_card(
        shared_dir, bot_id, card_id, action_id=action_id,
        action_label=label or action_id, est_cost=est_cost, actor=bot_id, now=now)
    board_store.record_touch(
        shared_dir, bot_id, card_id, action="do_action", result="instructed",
        actor=bot_id, tz=tz, now=now)
    return {"bot_id": bot_id, "card_id": card_id, "touch_action": "do_action", "result": "instructed"}


def _dispatch_touch(
    shared_dir: Path, bot_id: str, network: dict[str, Any], card: dict[str, Any], action: str, *,
    now: datetime, tz: Any = None, probes: dict[str, CheckSourceProbe],
) -> dict[str, Any]:
    if action == "remind":
        return _fire_remind(shared_dir, bot_id, network, card, now=now, tz=tz)
    if action == "check_source":
        return _fire_check_source(shared_dir, bot_id, network, card, now=now, tz=tz, probes=probes)
    if action == "do_action":
        return _fire_do_action(shared_dir, bot_id, network, card, now=now, tz=tz)
    if action == "ask":
        return _fire_ask(shared_dir, bot_id, network, card, now=now, tz=tz)
    raise ValueError(f"unknown touch_action: {action!r}")


def _window_for(action: str | None, network: dict[str, Any]) -> int:
    if action in ("remind", "ask"):
        return DEFAULT_TOUCH_WINDOW_MINUTES
    return board_worker.worker_config(network).get("window_min", board_worker.DEFAULT_WINDOW_MINUTES)


def _recent_days(now: datetime, window_minutes: int) -> list[str]:
    """Date strings spanning ``[now - window_minutes, now]``, bounded to a
    sane number of days — a cheap, targeted scan instead of the full
    per-card history read."""
    start = now - timedelta(minutes=max(window_minutes, 0))
    days: list[str] = []
    d = start.date()
    end = now.date()
    while d <= end and len(days) < 8:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _recent_touch_due(
    shared_dir: Path, bot_id: str, card_id: str, *, now: datetime, window_minutes: int,
) -> datetime | None:
    """Item 1's idempotency guard: the timestamp of the most recent
    ``touch_due`` for ``card_id`` within the last ``window_minutes``, or
    None. Bounds re-firing to once per window — a card whose handling
    raised (or whose sweep tick never completed) gets retried only after
    the window elapses, which is exactly what makes it a MISS in the
    meantime (:func:`_sweep_misses`).
    """
    events_dir = board_store.board_dir(Path(shared_dir), bot_id) / "events"
    latest: datetime | None = None
    for day in _recent_days(now, window_minutes):
        p = events_dir / f"{day}.jsonl"
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != TOUCH_DUE_EVENT or row.get("card") != card_id:
                continue
            ts = board_store._parse_ts(row.get("ts"))  # noqa: SLF001
            if ts is not None and (latest is None or ts > latest):
                latest = ts
    # >= , not >: a touch_due exactly `window_minutes` old has fully elapsed
    # its window (the miss sweep would ledger it at this exact instant too —
    # see the matching `now <= deadline` in :func:`_sweep_misses`), so it
    # must be eligible to fire again, not read as "still in flight."
    if latest is None or now - latest >= timedelta(minutes=window_minutes):
        return None
    return latest


#: Card field: every ``touch_due`` timestamp whose miss has ALREADY been
#: ledgered. A list, not a scalar, and that is the whole point — the miss sweep
#: scans a seven-day window of ``touch_due`` events, so a card can legitimately
#: hold several unresolved ones at once. A single-valued stamp could only ever
#: remember the newest, and every older one was re-ledgered on the next tick
#: (reviews/pr-4410.md Hold 1): 288 duplicate rows per stale miss per day at the
#: 5-minute cadence, straight into the D-TM10 measurement this module exists to
#: produce.
MISSED_STAMPS_FIELD = "touch_missed_ats"

#: The single-valued field the first cut of this module wrote. Boards in the
#: wild still carry it, so it is READ for suppression and CLEARED alongside the
#: list — never written again.
LEGACY_MISSED_STAMP_FIELD = "touch_missed_at"


def _ledgered_miss_stamps(card: dict[str, Any]) -> set[str]:
    """Every ``touch_due`` ts already ledgered for this card, across the current
    list field and the legacy scalar."""
    out: set[str] = set()
    raw = card.get(MISSED_STAMPS_FIELD)
    if isinstance(raw, list):
        out.update(s for s in raw if isinstance(s, str))
    legacy = card.get(LEGACY_MISSED_STAMP_FIELD)
    if isinstance(legacy, str) and legacy:
        out.add(legacy)
    return out


def _record_miss_stamp(card: dict[str, Any], ts: str, *, now: datetime) -> None:
    """Mark one ``touch_due`` ts as ledgered, and prune the ones that can never
    be read again.

    Bounded by construction: :func:`_sweep_misses` never looks further back than
    :data:`MISS_SWEEP_LOOKBACK_MINUTES`, so a stamp older than that window
    cannot suppress anything and is dead weight on the board. Unparsable stamps
    are kept — dropping one would silently re-open its miss.
    """
    stamps = _ledgered_miss_stamps(card)
    stamps.add(ts)
    cutoff = now - timedelta(minutes=MISS_SWEEP_LOOKBACK_MINUTES)
    kept: list[str] = []
    for s in sorted(stamps):
        parsed = board_store._parse_ts(s)  # noqa: SLF001
        if parsed is None or parsed >= cutoff:
            kept.append(s)
    card[MISSED_STAMPS_FIELD] = kept
    card.pop(LEGACY_MISSED_STAMP_FIELD, None)


def _clear_missed_stamp(shared_dir: Path, bot_id: str, card_id: str) -> None:
    """Item 6: "clears on the next fired touch." Best-effort — a card that
    never carried a stamp is a no-op; a board that failed to reload just
    means the stamps linger until the next successful touch tries again.

    Clears BOTH the list and the legacy scalar: a card written by the previous
    cut of this module must not keep a stale scalar alive after a fired touch.
    """
    try:
        board = board_store.load_board(shared_dir, bot_id)
        card = board_store.find_card(board, card_id)
        if card is None:
            return
        had = (card.pop(MISSED_STAMPS_FIELD, None) is not None
               or card.pop(LEGACY_MISSED_STAMP_FIELD, None) is not None)
        if had:
            board_store.save_board(shared_dir, bot_id, board)
    except (OSError, ValueError) as exc:
        log.warning("board_touch: could not clear miss stamp for %s/%s: %s", bot_id, card_id, exc)


def _has_touch_since(card: dict[str, Any], ts: datetime) -> bool:
    for touch in reversed(card.get("touches") or []):
        if not isinstance(touch, dict):
            continue
        touch_ts = board_store._parse_ts(touch.get("at"))  # noqa: SLF001
        if touch_ts is None:
            continue
        if touch_ts >= ts:
            return True
        break  # touches[] is append-ordered — once older, every prior one is too
    return False


def sweep(
    shared_dir: Path, network: dict[str, Any] | None = None, *,
    now: datetime | None = None, probes: dict[str, CheckSourceProbe] | None = None,
) -> list[dict[str, Any]]:
    """D-TM3's daemon job. One tick: fire every due, unfired touch across
    every bot's board, then ledger anything that went due and was never
    resolved within its window as a miss.

    ``network`` (``network.json``) is only needed for ``remind``'s
    delivery and ``do_action``/``check_source``'s worker-window lookup;
    None (a bare board-only fixture) is accepted — a ``remind`` touch on a
    misconfigured bot then fails to send and becomes a miss on the next
    sweep, the honest outcome rather than a special case.
    """
    now = now or datetime.now(timezone.utc)
    network = network or {}
    probes = probes or DEFAULT_PROBES
    tz = _resolve_tz(network)
    results: list[dict[str, Any]] = []
    boards_root = Path(shared_dir) / "boards"
    if not boards_root.is_dir():
        return results
    for bot_dir in sorted(boards_root.iterdir()):
        if not bot_dir.is_dir():
            continue
        bot_id = bot_dir.name
        try:
            board = board_store.load_board(shared_dir, bot_id)
        except (OSError, ValueError) as exc:
            log.warning("board_touch: could not read board for %s: %s", bot_id, exc)
            continue
        for card in board.get("cards") or []:
            if card.get("lane") in board_store.SETTLED_LANES:
                continue
            action = card.get("touch_action")
            next_touch = card.get("next_touch")
            if not action or not next_touch:
                continue
            due_at = board_store._parse_ts(next_touch)  # noqa: SLF001
            if due_at is None or due_at > now:
                continue
            window = _window_for(action, network)
            card_id = card.get("id", "")
            if _recent_touch_due(shared_dir, bot_id, card_id, now=now, window_minutes=window) is not None:
                continue  # already fired within this window; awaiting resolution or the miss sweep
            board_store.append_event(shared_dir, bot_id, {
                "event": TOUCH_DUE_EVENT, "card": card_id, "title": card.get("title"),
                "action": action, "scheduled_for": next_touch,
            }, now=now)
            had_miss_stamp = bool(_ledgered_miss_stamps(card))
            try:
                results.append(_dispatch_touch(
                    shared_dir, bot_id, network, card, action, now=now, tz=tz, probes=probes))
            except Exception:  # noqa: BLE001 — one card's failure must not stop the sweep
                log.exception("board_touch: dispatch failed for %s/%s", bot_id, card_id)
                continue
            if had_miss_stamp:
                _clear_missed_stamp(shared_dir, bot_id, card_id)
    _sweep_misses(shared_dir, network, now=now)
    return results


def _sweep_misses(
    shared_dir: Path, network: dict[str, Any] | None = None, *, now: datetime,
) -> list[dict[str, Any]]:
    """Item 6: a ``touch_due`` whose window elapsed with no later touch
    recorded on the card is ledgered ``missed`` — through
    :func:`board_worker.record_delivery_outcome`, the SAME ledger a
    delegation miss lands in (deviation 3). Idempotent: a touch_due already
    already recorded in the card's ``touch_missed_ats`` is skipped on a repeat
    pass — per EVENT, not per card, so a card holding several unresolved
    ``touch_due`` events ledgers each exactly once.
    """
    network = network or {}
    written: list[dict[str, Any]] = []
    boards_root = Path(shared_dir) / "boards"
    if not boards_root.is_dir():
        return written
    for bot_dir in sorted(boards_root.iterdir()):
        if not bot_dir.is_dir():
            continue
        bot_id = bot_dir.name
        events_dir = bot_dir / "events"
        if not events_dir.is_dir():
            continue
        board: dict[str, Any] | None = None
        board_dirty = False
        for day in _recent_days(now, MISS_SWEEP_LOOKBACK_MINUTES):
            p = events_dir / f"{day}.jsonl"
            if not p.is_file():
                continue
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") != TOUCH_DUE_EVENT:
                    continue
                ts = board_store._parse_ts(row.get("ts"))  # noqa: SLF001
                if ts is None:
                    continue
                deadline = ts + timedelta(minutes=_window_for(row.get("action"), network))
                if now <= deadline:
                    continue
                card_id = row.get("card")
                if not card_id:
                    continue
                if board is None:
                    try:
                        board = board_store.load_board(shared_dir, bot_id)
                    except (OSError, ValueError):
                        board = {}
                card = board_store.find_card(board, card_id) if board else None
                if card is not None and _has_touch_since(card, ts):
                    continue  # resolved — nothing to ledger
                row_ts = row.get("ts")
                if card is not None and isinstance(row_ts, str) and (
                        row_ts in _ledgered_miss_stamps(card)):
                    continue  # already ledgered this exact miss
                ledger_row = board_worker.record_delivery_outcome(
                    shared_dir, bot_id=bot_id, card_id=card_id,
                    action_id=f"{TOUCH_MISS_PREFIX}{row.get('action')}",
                    window_start=row.get("ts"), window_end=board_store._utcnow(deadline),  # noqa: SLF001
                    outcome=board_worker.OUTCOME_MISSED, now=now, producer=TOUCH_PRODUCER)
                written.append(ledger_row)
                if card is not None and isinstance(row_ts, str):
                    _record_miss_stamp(card, row_ts, now=now)
                    board_dirty = True
                    board_store.append_event(shared_dir, bot_id, {
                        "event": "touch_missed", "card": card_id,
                        "title": card.get("title"), "action": row.get("action"), "actor": bot_id,
                    }, now=now)
        if board_dirty and board:
            try:
                board_store.save_board(shared_dir, bot_id, board)
            except OSError as exc:
                log.warning("board_touch: could not stamp misses for %s: %s", bot_id, exc)
    return written


def _count_touch_misses(shared_dir: Path, bot_id: str, *, since: datetime, until: datetime) -> int:
    count = 0
    ledger_root = board_worker.ledger_dir(shared_dir)
    if not ledger_root.is_dir():
        return 0
    day = since.date()
    while day <= until.date():
        p = ledger_root / f"{day.isoformat()}.jsonl"
        day += timedelta(days=1)
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("bot_id") != bot_id or row.get("outcome") != board_worker.OUTCOME_MISSED:
                continue
            if row.get("producer") != TOUCH_PRODUCER:
                continue
            ts = board_store._parse_ts(row.get("ts"))  # noqa: SLF001
            if ts is not None and since <= ts <= until:
                count += 1
    return count


def weekly_report_stats(shared_dir: Path, bot_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """D-TM10's per-bot line, one week trailing ``now``: touches due / on
    time / missed, real numbers from this chip's own events + ledger.
    Captures proposed/kept/dropped are the sibling capture chip's data
    (``capture-from-turns-is-gated-and-lands-as-a-proposal``, not landed) —
    rendered ``"unknown"`` per the brief, not a fabricated zero. The whole
    dict is ``{"bot_id": ..., "unknown": True}`` when the store itself
    could not be read (D-CS7: name the subject, never fail silently).
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    due = 0
    try:
        events_dir = board_store.board_dir(Path(shared_dir), bot_id) / "events"
        day = since.date()
        while day <= now.date():
            p = events_dir / f"{day.isoformat()}.jsonl"
            day += timedelta(days=1)
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("event") != TOUCH_DUE_EVENT:
                    continue
                ts = board_store._parse_ts(row.get("ts"))  # noqa: SLF001
                if ts is not None and since <= ts <= now:
                    due += 1
    except (OSError, json.JSONDecodeError, ValueError):
        # ValueError: board_store.validate_bot_id rejects a malformed id —
        # an "I cannot judge this bot" case, same D-CS7 unknown as a read
        # failure, not a crash the whole pod report pays for.
        return {"bot_id": bot_id, "unknown": True}
    missed = _count_touch_misses(shared_dir, bot_id, since=since, until=now)
    return {
        "bot_id": bot_id, "touches_due": due,
        "touches_on_time": max(0, due - missed), "touches_missed": missed,
        "captures_proposed": "unknown", "captures_kept": "unknown", "captures_dropped": "unknown",
    }


def format_report_line(bot_id: str, stats: dict[str, Any]) -> str:
    """D-TM10's rendered line for the pod report."""
    if stats.get("unknown"):
        return f"{bot_id}: touches unknown (store unreadable)"
    return (
        f"{bot_id}: touches due {stats['touches_due']} / on time "
        f"{stats['touches_on_time']} / missed {stats['touches_missed']}; "
        f"captures proposed {stats['captures_proposed']} / kept "
        f"{stats['captures_kept']} / dropped {stats['captures_dropped']}"
    )


__all__ = [
    "TOUCH_DUE_EVENT", "REMIND_ASK_THRESHOLD", "DEFAULT_TOUCH_WINDOW_MINUTES",
    "TOUCH_MISS_PREFIX", "NUDGE_ACTION_ID", "CheckSourceProbe", "DEFAULT_PROBES",
    "compose_remind_message", "sweep", "weekly_report_stats", "format_report_line",
]
