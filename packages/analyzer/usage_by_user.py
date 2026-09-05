#!/usr/bin/env python3
"""
usage_by_user.py — Per-user × per-app usage rollup (D-S2 track 3).

Brief: internal/dispatch/done/usage-by-user-app.md. WHY: the management
pillar's missing rollup — "who uses Morning Brief, and how much" — for
the dossier's Users module (design-pod-dossier D-T6), the AL-4.x access
work (audiences need usage-by-audience truth) and alpha verdict
instrumentation. No new collection: the raw material is already on disk.

This is AL-1.3's sibling, keyed the same way. ``usage_by_app.py`` folds
turn annotations by ``app_id``; this folds the SAME annotations by
``app_id`` × **requester**, and by requester alone for the per-bot
totals. It shares AL-1.3's arithmetic by importing it — the windows,
the metric accumulators, the grade classifier and the Evolve-overhead
split all come from ``usage_by_app``, so the two files' numbers are
produced by one implementation, not two that agree today.

Where the requester comes from
------------------------------
Turn annotations carry app attribution and cost but **no identity**.
``session_summary`` records — written to the SAME daily annotation file
by ``SessionSummarizer`` — carry ``recurring_request.requester``, the
``platform:senderId`` key ``buildRecurringRequest`` stamps. So the join
is ``turn_annotation.session_id`` → ``session_summary.session_id`` →
``recurring_request.requester``.

That key is used **exactly as emitted**. Nothing here re-derives a
platform from an id's shape (``usage_analytics._infer_platform`` does,
for OC's turn-collector rows, and the recurring-request writer
deliberately refuses to: a guessed platform mis-attributes across id
spaces). Display-name resolution is the roster's job at read time — see
``routes_analytics``' ``resolve_display_name`` enrichment — never this
rollup's.

WHAT THIS CAN AND CANNOT SEE (the honesty contract)
---------------------------------------------------
``recurring_request`` is produced only for a human-initiated session
carrying a normalizable ask, from a requester who has not opted out
(``recurringRequest.ts``, three gates, each failing closed). So a
large share of turns have **no** resolvable requester, and that is a
property of the source, not a failure here. Consequently:

* Turns with no resolvable requester land in ``unattributed_user`` —
  shown, never hidden, never guessed. ``coverage`` carries the share,
  and a reader that renders ``users`` without rendering that share is
  lying by omission (the same rule AL-1.3 states for ``unattributed``).
* ``unattributed_user`` is split three ways so the operator can tell
  the reasons apart, exactly as AL-1.3 splits out ``legacy_schema_turns``:
  ``no_session_id_turns`` (the annotation carried no session),
  ``no_summary_turns`` (a session whose summary is not in the window —
  most often a session still open, or one that ended after this run),
  and ``summary_without_requester_turns`` (a summary exists and no
  requester reached this rollup from it: a cron/heartbeat session, an
  unkeyable ask, or an identity the do-not-track gate withheld).
  Absence of a requester is never read as "nobody".

  That last bucket deliberately does NOT separate "the summary stated
  no requester" from "the gate withheld the one it stated". A per-turn
  split would tell the operator, session by session, that SOMEBODY here
  opted out — and whether a person opted out is that person's business
  (``feedback_user_observation_optout``, and the same rule that keeps
  ``conversation_recurrence``'s gate report to counts rather than
  identities). The aggregate is reported instead, as
  ``user_attribution.requesters_withheld``.
* Per-user metrics inside an app keep AL-1.3's grade split: ``total``
  is ``scheduled + explicit`` and ``inferred`` rides beside it. There
  is no key in this payload that sums an inferred turn into a
  deterministic one.
* Evolve's own subagent overhead is split out with AL-1.3's join, under
  AL-1.3's exact condition (no app id AND no user), so the two files
  report the same ``evolve_overhead`` turns rather than two numbers an
  operator would have to reconcile.

DO-NOT-TRACK: THE FOURTH READ PATH, THE SAME GATE
-------------------------------------------------
``recurring_request`` rows are the evidence store
``evolve_admin.applications.conversation_recurrence`` guards, and that
module's contract is blunt: every function that turns a ``shared_dir``
into evidence routes through ``apply_do_not_track_gates``, because "a
third read path that skips this is the same bug again". This module is
the fourth, and it calls that chokepoint — not a local re-implementation
of it. The gate is applied at the WRITER, so the artifact on disk is
already gated; gating at the route would leave ungated identities in a
0644 file.

The gate is applied per distinct ``(requester, record-reported bot_id)``
pair, and the row it is handed carries the directory it was read from as
``source_bot`` — so a record self-reporting some other bot is gated
against that bot too and cannot shop for one whose switch is still on.

**Fail closed.** When the gate cannot be evaluated at all — the per-bot
signal is off, the roster overlay is present but unreadable, or
``evolve_admin`` is not importable in this interpreter — NO turn is
attributed to any user. The payload is still written, with
``user_attribution.available: false`` and a ``reason``, so a reader can
say "withheld" rather than showing an empty ``users`` map that reads as
"nobody uses this".

Privacy scope
-------------
One bot per run; no cross-bot reads ever occur (AL-1.3's invariant,
kept). Per-user data is OPERATOR-facing (D-T10's cut) — the admin route
below is the only reader added here, and no evo/bot-facing tool is
extended. The requester keys this file holds are the same keys already
present in ``{shared}/annotations/<bot>/*.jsonl``, so the 0644 output
adds no exposure class the source did not already have.

Output: ``{sharedDir}/{botId}/usage-by-user.json``, mode 0644, written
atomically via ``usage_by_app``'s writer (tmp + rename in the
destination dir, mode pinned before the rename).

Reader: admin ``/api/analytics/usage/by-user``. ``load_usage_by_user()``
below is its single entry point; ``{}`` means "not measured", never
zero usage.

Schedule: daily at 03:37, right after usage_by_app (03:35).

Usage:
    python3 usage_by_user.py --network /Users/Shared/evolve/network.json
    python3 usage_by_user.py --bot team_bot_a --shared-dir /Users/Shared/evolve
    python3 usage_by_user.py --bot team_bot_a --shared-dir /Users/Shared/evolve --report
    python3 usage_by_user.py --bot team_bot_a --shared-dir /Users/Shared/evolve --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_config import CANONICAL_SHARED_DIR
from evolve_util import now_iso_micro as _now_iso

# The annotation reader is exec_outcome_watchdog's — one windowed reader
# for {shared}/annotations/<bot>/<date>.jsonl, selecting the record kind
# rather than opening the same paths a second time.
from exec_outcome_watchdog import read_turn_annotations

# AL-1.3's arithmetic, imported rather than re-derived. The brief's
# instruction is to EXTEND the rollup, not fork it: these are the
# windows the two files must agree on, the metric accumulators, the
# grade classifier and the Evolve-overhead join. A second copy of any of
# them is a second definition of "a turn".
from usage_by_app import (
    DETERMINISTIC_GRADES,
    EVOLVE_OVERHEAD_TRIGGER_KINDS,
    MAX_WINDOW_DAYS,
    WINDOWS,
    _add_metrics,
    _empty_metrics,
    _resolve_annotation_cost,
    _round_cost,
    _session_trigger_kinds,
    _sum_metrics,
    classify_grade,
    with_evolve_bot,
    write_rollup_json,
)
from turn_cost import load_pricing_catalog

# ── Constants ────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1
OUTPUT_FILENAME = "usage-by-user.json"

#: Grades kept per user inside an app. ``total`` is the deterministic
#: sum (``DETERMINISTIC_GRADES``); ``inferred`` is reported beside it and
#: never folded in. The scheduled/explicit sub-split is deliberately NOT
#: repeated at the user level — attribution SOURCE is a property of the
#: app join, not of who asked, and repeating it would multiply the
#: payload by two for no reader.
USER_GRADE_BLOCKS = ("total", "inferred")

#: Reasons the whole user breakdown can be withheld. The first three are
#: the do-not-track chokepoint's own verdicts; the fourth is this
#: module's, for an interpreter where the chokepoint is not importable.
WITHHELD_SIGNAL_DISABLED = "signal_disabled"
WITHHELD_OVERLAY_UNREADABLE = "overlay_unreadable"
WITHHELD_EXCLUDED_REQUESTER = "excluded_requester"
WITHHELD_GATE_UNAVAILABLE = "gate_unavailable"


# ── The do-not-track chokepoint ──────────────────────────────────────────────

def _gate_module():
    """Import the do-not-track chokepoint, or ``None``.

    Returned rather than raised so the caller can fail CLOSED with a
    named reason. ``evolve_admin`` is installed in the pod venv (several
    analyzer modules import it the same way), but an interpreter without
    it must withhold identities, not proceed ungated.
    """
    try:
        from evolve_admin.applications import (  # type: ignore
            conversation_recurrence as cr,
        )
    except Exception:  # noqa: BLE001 — any import fault means "cannot gate"
        return None
    return cr


def gate_requesters(
    shared_dir: Path,
    bot_id: str,
    pairs: set[tuple[str, str]],
) -> tuple[set[tuple[str, str]], dict[str, Any]]:
    """Filter ``(requester, record_bot_id)`` pairs through the DNT gates.

    ``pairs`` is the distinct set observed in the window, so the gate
    runs once per identity rather than once per session.

    Returns ``(allowed_pairs, report)``. ``report`` always says whether
    attribution is available and why — a gate that reports nothing when
    it withholds nothing is indistinguishable from a gate that is not
    running, which is the exact state ``conversation_recurrence`` was in
    on its CLI path before it was fixed.
    """
    report: dict[str, Any] = {
        "available": True,
        "reason": None,
        "requesters_in": len(pairs),
        "requesters_withheld": 0,
        "gate_report": None,
    }
    if not pairs:
        return set(), report

    cr = _gate_module()
    if cr is None:
        report["available"] = False
        report["reason"] = WITHHELD_GATE_UNAVAILABLE
        report["requesters_withheld"] = len(pairs)
        return set(), report

    rows = [
        cr.RecurrenceRow(
            # The gate reads requester + the two bot keys. label/hour/day
            # are carried honestly (never synthesised) but are not part
            # of the verdict; see conversation_recurrence._gate_keys.
            label="",
            requester=requester,
            hour=0,
            day="",
            bot_id=record_bot,
            source_bot=bot_id,
        )
        for requester, record_bot in sorted(pairs)
    ]
    kept, gate_report = cr.apply_do_not_track_gates(shared_dir, rows, bot_id=bot_id)
    allowed = {(r.requester, r.bot_id) for r in kept}
    report["requesters_withheld"] = len(pairs) - len(allowed)
    report["gate_report"] = gate_report.to_dict()

    # A bot-wide verdict (the per-bot switch off, or an overlay that is
    # present but unreadable) withholds EVERY row. Name it, so the reader
    # says "withheld" instead of "nobody".
    reasons = {e.reason for e in gate_report.exclusions}
    if not allowed:
        for reason in (
            cr.GATE_SIGNAL_DISABLED,
            cr.GATE_OVERLAY_UNREADABLE,
            cr.GATE_EXCLUDED_REQUESTER,
        ):
            if reason in reasons:
                report["available"] = False
                report["reason"] = reason
                break
    return allowed, report


# ── The session → requester join ─────────────────────────────────────────────

def session_requesters(
    shared_dir: Path,
    bot_id: str,
    *,
    today: date,
    days: int = MAX_WINDOW_DAYS,
) -> tuple[dict[str, str], set[str], dict[str, Any]]:
    """Build ``session_id -> requester`` for the window, do-not-track gated.

    Reads ``session_summary`` records across the SAME span the turn
    rollup walks, in ONE pass, rather than per day: a session's summary
    is written when the session ENDS, which is often a different UTC day
    from the turns it covers.

    Returns ``(session_id -> requester, sessions_that_have_a_summary,
    report)``. The second element is what lets the caller tell "a
    summary exists and states no requester" apart from "no summary in
    the window at all" — two very different facts that a single missing
    key would flatten into one.

    A session that ends after this run has no summary yet, so its turns
    land in ``unattributed_user`` for this run and are attributed by the
    next one — the rollup recomputes the whole window daily, so the
    boundary self-heals. It is reported as ``no_summary_turns`` in the
    meantime, never as "no user".
    """
    summaries = read_turn_annotations(
        shared_dir, bot_id, days=days, today=today,
        record_type="session_summary",
    )
    raw: dict[str, tuple[str, str]] = {}
    summarised: set[str] = set()
    for rec in summaries:
        session_id = rec.get("session_id")
        if not (isinstance(session_id, str) and session_id):
            continue
        summarised.add(session_id)
        rr = rec.get("recurring_request")
        if not isinstance(rr, dict):
            continue
        requester = rr.get("requester")
        if not (isinstance(requester, str) and requester):
            continue
        record_bot = str(rec.get("bot_id") or "")
        # Last summary wins: a re-summarised session states its requester
        # again, and the later statement is the current one.
        raw[session_id] = (requester, record_bot)

    allowed, report = gate_requesters(
        shared_dir, bot_id, {pair for pair in raw.values()}
    )
    mapping = {
        session_id: requester
        for session_id, (requester, record_bot) in raw.items()
        if (requester, record_bot) in allowed
    }
    report["sessions_with_requester"] = len(mapping)
    report["sessions_seen"] = len(summarised)
    return mapping, summarised, report


# ── Rollup ───────────────────────────────────────────────────────────────────

def _empty_unattributed() -> dict[str, Any]:
    return {
        **_empty_metrics(),
        "no_session_id_turns": 0,
        "no_summary_turns": 0,
        "summary_without_requester_turns": 0,
    }


def rollup_bot(
    shared_dir: Path,
    bot_id: str,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Fold the trailing 30 days into the per-user payload dict.

    Pure: reads only ``{shared}/annotations/<bot>/`` (turn rows + summary
    rows), the cost-event siblings for AL-1.3's overhead join, and the
    do-not-track gate's own inputs. Returns the payload; the caller
    writes it.
    """
    shared_dir = Path(shared_dir)
    if today is None:
        today = datetime.now(timezone.utc).date()

    # One pass over the window's session summaries yields both the
    # gated requester join and the set of sessions that HAVE a summary.
    requesters, summarised, attribution = session_requesters(
        shared_dir, bot_id, today=today
    )

    # Pricing catalog for AL-1.3's zero-cost re-estimate path — read once
    # per rollup so the two files price a turn identically (audit B6).
    catalog = load_pricing_catalog(shared_dir)

    # user -> window -> metrics (per-bot totals by user)
    users: dict[str, dict[str, dict[str, Any]]] = {}
    # app_id -> window -> user -> grade block -> metrics
    apps: dict[str, dict[str, dict[str, dict[str, dict[str, Any]]]]] = {}
    # app_id -> window -> metrics (that app's turns with no resolvable user)
    apps_unattributed: dict[str, dict[str, dict[str, Any]]] = {}
    unattributed: dict[str, dict[str, Any]] = {
        key: _empty_unattributed() for key in WINDOWS
    }
    overhead: dict[str, dict[str, Any]] = {key: _empty_metrics() for key in WINDOWS}
    seen_ts: dict[str, dict[str, str]] = {}   # user -> {first, last}

    for offset in range(MAX_WINDOW_DAYS):
        day = today - timedelta(days=offset)
        # days=1 per call so the FILE date (not a parsed ts) decides
        # window membership — one file is one UTC day by construction.
        records = read_turn_annotations(shared_dir, bot_id, days=1, today=day)
        if not records:
            continue
        windows_hit = [key for key, span in WINDOWS.items() if offset < span]
        trigger_kinds: dict[str, str] | None = None

        for rec in records:
            app_id, grade = classify_grade(rec)
            cost = _resolve_annotation_cost(rec, catalog, shared_dir)
            session_id = rec.get("session_id")
            session_id = session_id if isinstance(session_id, str) else ""
            user = requesters.get(session_id) if session_id else None

            if user is None:
                if app_id is None:
                    # AL-1.3's exact overhead condition, so the two files
                    # report the same evolve_overhead turns.
                    if trigger_kinds is None:
                        trigger_kinds = _session_trigger_kinds(
                            shared_dir, bot_id, day
                        )
                    if trigger_kinds.get(session_id) in EVOLVE_OVERHEAD_TRIGGER_KINDS:
                        for key in windows_hit:
                            _add_metrics(overhead[key], rec, cost)
                        continue
                if not session_id:
                    reason = "no_session_id_turns"
                elif session_id in summarised:
                    reason = "summary_without_requester_turns"
                else:
                    reason = "no_summary_turns"
                for key in windows_hit:
                    _add_metrics(unattributed[key], rec, cost)
                    unattributed[key][reason] += 1
                if app_id is not None:
                    per_app = apps_unattributed.setdefault(
                        app_id, {key: _empty_metrics() for key in WINDOWS}
                    )
                    for key in windows_hit:
                        _add_metrics(per_app[key], rec, cost)
                continue

            bucket = users.setdefault(
                user, {key: _empty_metrics() for key in WINDOWS}
            )
            for key in windows_hit:
                _add_metrics(bucket[key], rec, cost)

            # ISO-8601 UTC strings from one writer — lexicographic order
            # is chronological order.
            ts = rec.get("ts")
            if isinstance(ts, str) and ts:
                bounds = seen_ts.setdefault(user, {"first": ts, "last": ts})
                if ts < bounds["first"]:
                    bounds["first"] = ts
                if ts > bounds["last"]:
                    bounds["last"] = ts

            if app_id is None:
                continue
            app = apps.setdefault(app_id, {key: {} for key in WINDOWS})
            # ``total`` is AL-1.3's DETERMINISTIC_GRADES, read from its
            # constant rather than restated — a grade added there must not
            # silently start counting as deterministic here. Anything not
            # on that list falls to ``inferred``, the side that claims less.
            block = "total" if grade in DETERMINISTIC_GRADES else "inferred"
            for key in windows_hit:
                per_user = app[key].setdefault(
                    user, {b: _empty_metrics() for b in USER_GRADE_BLOCKS}
                )
                _add_metrics(per_user[block], rec, cost)

    return _assemble(
        bot_id, users, apps, apps_unattributed, unattributed, overhead,
        seen_ts, attribution, today,
    )


def _assemble(
    bot_id: str,
    users: dict[str, dict[str, dict[str, Any]]],
    apps: dict[str, dict[str, dict[str, dict[str, dict[str, Any]]]]],
    apps_unattributed: dict[str, dict[str, dict[str, Any]]],
    unattributed: dict[str, dict[str, Any]],
    overhead: dict[str, dict[str, Any]],
    seen_ts: dict[str, dict[str, str]],
    attribution: dict[str, Any],
    today: date,
) -> dict[str, Any]:
    """Shape the accumulators into the on-disk payload."""
    users_out: dict[str, Any] = {}
    for user, per_window in sorted(users.items()):
        entry: dict[str, Any] = {
            "first_seen_ts": seen_ts.get(user, {}).get("first"),
            "last_seen_ts": seen_ts.get(user, {}).get("last"),
        }
        for key in WINDOWS:
            entry[key] = _round_cost(dict(per_window[key]))
        users_out[user] = entry

    apps_out: dict[str, Any] = {}
    for app_id in sorted(set(apps) | set(apps_unattributed)):
        per_window = apps.get(app_id) or {key: {} for key in WINDOWS}
        un = apps_unattributed.get(app_id)
        entry = {}
        for key in WINDOWS:
            entry[key] = {
                "users": {
                    user: {
                        block: _round_cost(dict(blocks[block]))
                        for block in USER_GRADE_BLOCKS
                    }
                    for user, blocks in sorted((per_window.get(key) or {}).items())
                },
                "unattributed_user": _round_cost(
                    dict(un[key]) if un else _empty_metrics()
                ),
            }
        apps_out[app_id] = entry

    coverage: dict[str, Any] = {}
    for key in WINDOWS:
        attributed = _sum_metrics(users_out[u][key] for u in users_out)
        un = unattributed[key]
        # Denominator: user-attributed + unattributed. Evolve overhead is
        # deliberately OUT, for AL-1.3's reason — it is not a person, and
        # counting it would flatter or damn the number depending on how
        # chatty Evolve's own subagents were.
        denom_turns = attributed["turns"] + un["turns"]
        denom_cost = attributed["cost_estimated"] + un["cost_estimated"]
        coverage[key] = {
            "attributed_user_turns": attributed["turns"],
            "unattributed_user_turns": un["turns"],
            "no_session_id_turns": un["no_session_id_turns"],
            "no_summary_turns": un["no_summary_turns"],
            "summary_without_requester_turns": un["summary_without_requester_turns"],
            "evolve_overhead_turns": overhead[key]["turns"],
            "distinct_users": sum(
                1 for u in users_out if users_out[u][key]["turns"]
            ),
            "user_turns_total": denom_turns,
            "unattributed_user_turns_share": (
                round(un["turns"] / denom_turns, 4) if denom_turns else None
            ),
            "unattributed_user_cost_share": (
                round(un["cost_estimated"] / denom_cost, 4) if denom_cost else None
            ),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "bot_id": bot_id,
        "as_of_date": today.isoformat(),
        "windows": dict(WINDOWS),
        "user_attribution": attribution,
        "users": users_out,
        "apps": apps_out,
        "unattributed_user": {
            key: _round_cost(dict(unattributed[key])) for key in WINDOWS
        },
        "evolve_overhead": {
            key: _round_cost(dict(overhead[key])) for key in WINDOWS
        },
        "coverage": coverage,
    }


# ── Output ───────────────────────────────────────────────────────────────────

def usage_by_user_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / bot_id / OUTPUT_FILENAME


def write_usage_by_user(
    shared_dir: Path,
    bot_id: str,
    payload: dict[str, Any],
    *,
    dry_run: bool = False,
) -> Path:
    """Atomically write the payload to {shared}/{bot}/usage-by-user.json.

    Same discipline as AL-1.3, and literally the same writer: tmp +
    rename inside the DESTINATION dir with mode pinned to 0644 BEFORE
    the rename (``mkstemp`` creates 0600 and ``os.replace`` carries that
    mode onto the destination, which would lock every reader but the
    writing user out of the file).
    """
    out_path = usage_by_user_path(shared_dir, bot_id)
    if dry_run:
        print(f"[usage-by-user] [dry-run] would write {out_path}")
        return out_path
    return write_rollup_json(out_path, payload, prefix=".usage-by-user-")


def load_usage_by_user(shared_dir: Path, bot_id: str) -> dict[str, Any]:
    """Read one bot's per-user rollup. ``{}`` when absent or unreadable.

    The single reader for the admin route. An empty dict means "no
    rollup yet", which callers render as "not measured", never as zero
    usage — and is distinct from a written payload whose
    ``user_attribution.available`` is false, which means "withheld".
    """
    path = usage_by_user_path(shared_dir, bot_id)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# ── Orchestrator ─────────────────────────────────────────────────────────────

def run_usage_by_user(
    bot_id: str,
    shared_dir: Path,
    *,
    dry_run: bool = False,
    report: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Roll up one bot and (unless report/dry-run) write the file."""
    payload = rollup_bot(shared_dir, bot_id, today=today)

    if report:
        _print_report(payload)
        return payload

    write_usage_by_user(shared_dir, bot_id, payload, dry_run=dry_run)
    if not dry_run:
        cov = payload["coverage"]["d7"]
        share = cov["unattributed_user_turns_share"]
        share_str = "n/a" if share is None else f"{share:.1%}"
        avail = payload["user_attribution"]["available"]
        note = "" if avail else (
            f" [WITHHELD: {payload['user_attribution']['reason']}]"
        )
        print(
            f"[usage-by-user] {bot_id}: {cov['distinct_users']} users, "
            f"{cov['attributed_user_turns']} attributed turns (7d), "
            f"unattributed {share_str}{note}"
        )
    return payload


def _print_report(payload: dict[str, Any]) -> None:
    attribution = payload["user_attribution"]
    print(f"\n{payload['bot_id']} — per-user usage (7d window)")
    if not attribution["available"]:
        print(f"  user attribution WITHHELD — {attribution['reason']}")
    print(f"{'Requester':<40} {'Turns':>6} {'In tok':>9} {'Out tok':>9} "
          f"{'Cost':>9}  Last seen")
    print("-" * 96)
    rows = sorted(
        payload["users"].items(),
        key=lambda kv: kv[1]["d7"]["cost_estimated"],
        reverse=True,
    )
    for user, entry in rows:
        window = entry["d7"]
        print(
            f"{user:<40} {window['turns']:>6} {window['input_tokens']:>9} "
            f"{window['output_tokens']:>9} "
            f"${window['cost_estimated']:>8.4f}  "
            f"{(entry['last_seen_ts'] or '—')[:19]}"
        )
    cov = payload["coverage"]["d7"]
    un = payload["unattributed_user"]["d7"]
    share = cov["unattributed_user_turns_share"]
    share_str = "n/a" if share is None else f"{share:.1%}"
    print("-" * 96)
    print(
        f"{'unattributed_user':<40} {un['turns']:>6} {'':>9} {'':>9} "
        f"${un['cost_estimated']:>8.4f}  share {share_str}"
    )
    print(
        f"  ({un['no_session_id_turns']} no session, "
        f"{un['no_summary_turns']} no summary in window, "
        f"{un['summary_without_requester_turns']} summary states no requester)"
    )
    print(
        f"{'evolve_overhead (not a person)':<40} "
        f"{payload['evolve_overhead']['d7']['turns']:>6}"
    )

    print(f"\n{payload['bot_id']} — per-app × user (7d, deterministic total)")
    for app_id, entry in sorted(payload["apps"].items()):
        window = entry["d7"]
        per_user = window["users"]
        if not per_user and not window["unattributed_user"]["turns"]:
            continue
        print(f"  {app_id}")
        for user, blocks in sorted(
            per_user.items(),
            key=lambda kv: kv[1]["total"]["cost_estimated"],
            reverse=True,
        ):
            print(
                f"    {user:<38} {blocks['total']['turns']:>6} turns  "
                f"${blocks['total']['cost_estimated']:>8.4f}"
                f"  (+{blocks['inferred']['turns']} inferred)"
            )
        if window["unattributed_user"]["turns"]:
            print(
                f"    {'unattributed_user':<38} "
                f"{window['unattributed_user']['turns']:>6} turns  "
                f"${window['unattributed_user']['cost_estimated']:>8.4f}"
            )


# ── Main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-user × per-app usage rollup (D-S2 track 3)"
    )
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--network", help="Path to network.json (processes all bots)")
    parser.add_argument("--bot", dest="bot_id", help="Single bot to process")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute without writing")
    parser.add_argument("--report", action="store_true",
                        help="Print the 7d tables, no file writes")
    args = parser.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bots: list[str] = []

    if args.bot_id:
        bots = [args.bot_id]
    elif args.network:
        try:
            net = json.loads(Path(args.network).read_text())
            shared_dir = Path(net.get("sharedDir", str(shared_dir)))
            bots = with_evolve_bot(net.get("members", []))
        except Exception as exc:
            print(f"[usage-by-user] Failed to read network.json: {exc}",
                  file=sys.stderr)
            return 1
    else:
        net_path = shared_dir / "network.json"
        if net_path.exists():
            try:
                bots = with_evolve_bot(
                    json.loads(net_path.read_text()).get("members", [])
                )
            except Exception as exc:
                print(f"[usage-by-user] Failed to read {net_path}: {exc}",
                      file=sys.stderr)

    if not bots:
        parser.error("Specify --bot BOT_ID, --network PATH, or ensure network.json exists")

    for bot in bots:
        try:
            run_usage_by_user(
                bot, shared_dir, dry_run=args.dry_run, report=args.report,
            )
        except Exception as exc:  # one bot's failure must not stop the sweep
            print(f"[usage-by-user] {bot}: rollup failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
