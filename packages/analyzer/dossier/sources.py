"""dossier.sources — one tolerant collector per producer.

THE TRI-STATE LAW, stated once and obeyed everywhere below. A collector
returns ``None`` when its producer has no data for the window, and a dict of
numbers when it does. It NEVER returns zeros to mean "nothing there". Over a
longitudinal spine the difference is everything: a run of ``0`` reads as "the
pod went quiet", a run of ``null`` reads as "this producer was not running" —
and a spine that cannot tell those apart will one day be used to conclude the
wrong one. The same law applies one level down: inside a present block, a
sub-value whose own producer had nothing is ``null``, not ``0``.

THE SECOND LAW: no collector raises. Every one of them is reading files owned
by other users, written by daemons that may not be installed, on a pod that
may be mid-upgrade. A weekly writer that dies on one unreadable bot loses the
week for all of them, and the week cannot be recovered. So each collector
catches, records what it could not read (``*_unreadable`` lists — a blind
read is reported, never laundered into a clean zero), and returns what it
has.

THE THIRD LAW: read-only. Nothing here writes, migrates, or repairs anything
it reads. In particular manifests are parsed straight off disk rather than
through ``manifest.list_manifests``, which migrates each file it touches —
a weekly reporter must not mutate bot-owned state as a side effect of
counting it.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from dossier.window import EditionWindow

#: Token/count fields summed across the window, shared by the rollup body and
#: its per-model buckets. A field the rollup stops emitting contributes
#: nothing rather than KeyError-ing the week.
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "event_count",
)

#: The money field is named differently at the two levels ``cost_rollup``
#: writes: ``total_usd`` on the rollup body, ``cost_usd`` inside each
#: ``by_model`` bucket. Summing the body's name over a model bucket reads
#: every model's spend as $0 — silently, and forever, since a zero is a
#: perfectly plausible number. Hence one constant per level rather than a
#: single shared tuple.
_COST_KEY_ROLLUP = "total_usd"
_COST_KEY_MODEL = "cost_usd"

_COST_FIELDS = (_COST_KEY_ROLLUP,) + _TOKEN_FIELDS
_MODEL_FIELDS = (_COST_KEY_MODEL,) + _TOKEN_FIELDS

#: usage-by-app's coverage counters, carried verbatim into the edition. This
#: is NOT optional decoration: that producer's contract says in so many words
#: that a reader which renders apps without rendering coverage is lying by
#: omission — an empty ``apps`` map means something entirely different when
#: 147 turns went unattributed than when the pod genuinely ran nothing. The
#: live 2026-W35 edition is exactly that case.
_COVERAGE_FIELDS = (
    "attributed_turns",
    "inferred_turns",
    "unattributed_turns",
    "legacy_schema_turns",
    "evolve_overhead_turns",
    "app_turns_total",
)

#: The usage-by-app windows snapshotted into ``per_app``. These are the
#: producer's OWN rolling windows as of its own run date — deliberately NOT
#: relabelled as the edition's week (see the module docstring of ``dossier``).
_USAGE_WINDOWS = ("d7", "d30")

#: The per-person rollup's file (``analyzer/usage_by_user.py``, shipped
#: 2026-08-30). A SIBLING of ``usage-by-app.json``, not a key inside it —
#: reading the field name the producer never writes is how a "not measured
#: yet" block stays permanently true no matter how long its producer has
#: been running.
_USER_ROLLUP_FILENAME = "usage-by-user.json"

#: The per-person window the users module speaks about. ``d7`` for the same
#: reason ``per_app`` leads with it: a week's worth of people is what a
#: weekly page is about.
_USER_WINDOW = "d7"

#: How many days of scheduled-fire history one edition records. Four weeks:
#: long enough that a weekly app appears four times, short enough that the
#: strip stays one row of readable cells on a card.
FIRE_WINDOW_DAYS = 28

#: The attribution grade that means "a schedule started this turn"
#: (``analyzer/usage_by_app.GRADES``, AL-1.2's ``scheduledAttribution``
#: join). The ONLY grade a fire history may count: an ``explicit`` turn is a
#: person asking for the app by hand, and counting it would report a pod
#: whose crons are all dead as perfectly reliable.
_SCHEDULED_GRADE = "scheduled"

#: Cron-name → app_id map written by deploy (``app_cron_map.py``). Read for
#: the apps Evolve KNOWS run on a schedule but has no fire evidence for —
#: the difference between "this app has never run" and "nothing here
#: records when it runs".
_APP_CRON_MAP_FILENAME = "app-cron-map.json"

BandCounts = dict[str, int]


# ── helpers ──────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _num(value: Any) -> float | int:
    """A JSON number, or 0 for anything else. Only ever used INSIDE a block
    already known to have data — never to manufacture a present-looking zero."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _round_usd(value: float | int) -> float:
    return round(float(value), 6)


def _empty_cost(fields: tuple[str, ...] = _COST_FIELDS) -> dict[str, Any]:
    return {field: 0 for field in fields}


def _add_cost(
    acc: dict[str, Any], src: dict[str, Any], fields: tuple[str, ...] = _COST_FIELDS
) -> None:
    for field in fields:
        acc[field] += _num(src.get(field))


def _finish_cost(
    acc: dict[str, Any], cost_key: str = _COST_KEY_ROLLUP
) -> dict[str, Any]:
    out = dict(acc)
    out[cost_key] = _round_usd(out[cost_key])
    return out


# ── costs (week-aligned: per-day cost rollups summed over the ISO week) ──────

def collect_costs(
    shared_dir: Path,
    bots: Iterable[str],
    window: EditionWindow,
) -> dict[str, Any] | None:
    """Spend + tokens over the edition's week, from the daily cost rollups.

    Source: ``{shared_dir}/metrics/{bot}/cost-<YYYY-MM-DD>.json``, one file
    per bot per day, written by ``cost_rollup``. This is the one genuinely
    week-aligned producer in the edition: the rollups are per-day, so summing
    the seven pod-local dates of the window IS the week.

    ``cost_rollup`` deliberately writes NO file for a day with no events, so
    an absent file is "no data for that day" — reported as ``days_with_data``
    rather than filled in with a zero. A bot with no rollup at all in the
    window gets ``null``, not an all-zero row: an inactive bot and an
    un-instrumented one are different facts.

    Returns ``None`` when not one rollup file exists across every bot and
    every day — the whole producer is absent.
    """
    days = window.days
    by_bot: dict[str, Any] = {}
    pod_acc = _empty_cost()
    by_model: dict[str, dict[str, Any]] = {}
    any_data = False

    for bot in bots:
        bot_acc = _empty_cost()
        bot_models: dict[str, dict[str, Any]] = {}
        days_seen: list[str] = []
        for day in days:
            rollup = _read_json(
                shared_dir / "metrics" / bot / f"cost-{day.isoformat()}.json"
            )
            if rollup is None:
                continue
            days_seen.append(day.isoformat())
            _add_cost(bot_acc, rollup)
            _add_cost(pod_acc, rollup)
            for model, row in (rollup.get("by_model") or {}).items():
                if not isinstance(row, dict):
                    continue
                key = str(model)
                _add_cost(
                    bot_models.setdefault(key, _empty_cost(_MODEL_FIELDS)),
                    row, _MODEL_FIELDS,
                )
                _add_cost(
                    by_model.setdefault(key, _empty_cost(_MODEL_FIELDS)),
                    row, _MODEL_FIELDS,
                )
        if not days_seen:
            by_bot[bot] = None
            continue
        any_data = True
        by_bot[bot] = {
            **_finish_cost(bot_acc),
            "days_with_data": len(days_seen),
            "days_in_window": len(days),
            "by_model": {
                m: _finish_cost(v, _COST_KEY_MODEL)
                for m, v in sorted(bot_models.items())
            },
        }

    if not any_data:
        return None
    return {
        "source": "cost_rollup",
        "window": "edition_week",
        **_finish_cost(pod_acc),
        "days_in_window": len(days),
        "bots_with_data": sum(1 for v in by_bot.values() if v is not None),
        "by_model": {
            m: _finish_cost(v, _COST_KEY_MODEL)
            for m, v in sorted(by_model.items())
        },
        "by_bot": by_bot,
    }


# ── per-app usage (the producer's own rolling windows, snapshotted) ──────────

def collect_per_app(
    shared_dir: Path,
    bots: Iterable[str],
) -> dict[str, Any] | None:
    """Per-app usage from each bot's ``usage-by-app.json``.

    Keyed by ``app_id``  and aggregated across bots (the same
    app installed on two bots is one app with two bots' usage), carrying the
    producer's d7/d30 DETERMINISTIC totals plus its ``inferred`` column kept
    strictly beside them — the usage-by-app contract is that inferred rides
    next to the total, never inside it, and a downstream store that merged
    them would launder a guess into a measurement.

    ``coverage`` rides beside ``apps`` and is not optional: usage-by-app's
    own contract says a reader that renders apps without rendering coverage
    is lying by omission. An empty ``apps`` map means one thing when the pod
    ran nothing and something entirely different when every turn went
    unattributed — and only the coverage numbers separate them. (The live
    2026-W35 edition is the second case: zero apps, 147 unattributed turns.)

    ``source_windows`` records that these are the producer's rolling windows
    as of its own ``as_of_date``, NOT the edition's week. A reader that
    treats a rolling-d7 number as this week's number is wrong by up to six
    days, and the only defence is that the payload says which it is.

    The per-person half lives in :func:`collect_users`, over its own
    producer's own file. It used to be read out of a ``users`` key inside
    THIS file — a key ``usage_by_app`` has never written and never will,
    which is why the users module read "no per-person rollup on this pod"
    for as long as it existed. A block keyed on a name no writer emits is
    permanently, silently true.
    """
    apps: dict[str, Any] = {}
    unreadable: list[str] = []
    as_of: dict[str, Any] = {}
    coverage: dict[str, dict[str, int]] = {
        w: {f: 0 for f in _COVERAGE_FIELDS} for w in _USAGE_WINDOWS
    }
    coverage_by_bot: dict[str, Any] = {}
    any_rollup = False

    for bot in bots:
        payload = _read_json(shared_dir / bot / "usage-by-app.json")
        if payload is None:
            unreadable.append(bot)
            continue
        any_rollup = True
        as_of[bot] = payload.get("as_of_date")
        bot_cov: dict[str, Any] = {}
        for w in _USAGE_WINDOWS:
            src = (payload.get("coverage") or {}).get(w) or {}
            bot_cov[w] = {f: int(_num(src.get(f))) for f in _COVERAGE_FIELDS}
            for f in _COVERAGE_FIELDS:
                coverage[w][f] += bot_cov[w][f]
        coverage_by_bot[bot] = bot_cov
        for app_id, entry in (payload.get("apps") or {}).items():
            if not isinstance(entry, dict):
                continue
            row = apps.setdefault(
                str(app_id),
                {
                    "bots": [],
                    **{w: {"turns": 0, "cost_estimated": 0.0, "inferred_turns": 0}
                       for w in _USAGE_WINDOWS},
                    "last_seen_ts": None,
                },
            )
            if bot not in row["bots"]:
                row["bots"].append(bot)
            for w in _USAGE_WINDOWS:
                win = entry.get(w) or {}
                total = win.get("total") or {}
                inferred = win.get("inferred") or {}
                row[w]["turns"] += int(_num(total.get("turns")))
                row[w]["cost_estimated"] += float(_num(total.get("cost_estimated")))
                row[w]["inferred_turns"] += int(_num(inferred.get("turns")))
            last = entry.get("last_seen_ts")
            if isinstance(last, str) and (
                row["last_seen_ts"] is None or last > row["last_seen_ts"]
            ):
                row["last_seen_ts"] = last

    if not any_rollup:
        return None

    for row in apps.values():
        for w in _USAGE_WINDOWS:
            row[w]["cost_estimated"] = _round_usd(row[w]["cost_estimated"])
        row["bots"].sort()
    return {
        "source": "usage_by_app",
        "source_windows": {
            "note": "rolling windows as of each bot's own rollup run, "
                    "NOT the edition week",
            "windows": list(_USAGE_WINDOWS),
            "as_of_date_by_bot": as_of,
        },
        "apps": dict(sorted(apps.items())),
        "coverage": {w: _with_shares(coverage[w]) for w in _USAGE_WINDOWS},
        "coverage_by_bot": coverage_by_bot,
        "bots_without_rollup": sorted(unreadable),
    }


def _with_shares(counts: dict[str, int]) -> dict[str, Any]:
    """Coverage counts plus the unattributed share, or ``null`` for no turns.

    The share is ``None`` rather than ``0.0`` when the denominator is zero:
    "no turns at all" is not "perfect attribution", and this is the one
    number a reader uses to decide whether to believe the ``apps`` map above.
    """
    total = counts["app_turns_total"]
    return {
        **counts,
        "unattributed_turns_share": (
            round(counts["unattributed_turns"] / total, 4) if total else None
        ),
    }


def collect_users(shared_dir: Path, bots: Iterable[str]) -> dict[str, Any]:
    """Who used the pod, from each bot's ``usage-by-user.json``.

    Source: ``analyzer/usage_by_user.py`` (the per-person × per-app rollup,
    daily at 03:37), read from ``{shared_dir}/{bot}/usage-by-user.json`` —
    a SIBLING of the per-app rollup, which is the correction this collector
    exists to make. The earlier read looked for a ``users`` key INSIDE
    ``usage-by-app.json``; that producer writes no such key, so the block
    reported "no per-person rollup on this pod" on a pod that had one.

    Present-with-schema whether or not the producer is: an absent rollup
    yields ``available: false`` with a reason rather than a bare ``null``,
    so a reader can be written against the shape either way.

    THE WITHHELD DISTINCTION IS CARRIED, NOT FLATTENED. That producer's
    do-not-track gate can withhold a person's rows; it reports that as
    ``user_attribution.available: false`` plus a reason, and this block
    passes both through. "The gate withheld them" is not "nobody used the
    pod", and a spine that cannot tell those apart will one day be read as
    the second.

    Requester ids ride in ``requesters``. They are ids, not names — the
    admin reader resolves display names at read time (``roster_resolver``)
    so the durable spine never accrues a name that later changes.
    """
    per_person: dict[str, dict[str, Any]] = {}
    by_app: dict[str, dict[str, Any]] = {}
    withheld = 0
    gate_reasons: list[str] = []
    bots_without_rollup: list[str] = []
    any_rollup = False
    gate_open = False

    for bot in bots:
        payload = _read_json(shared_dir / bot / _USER_ROLLUP_FILENAME)
        if payload is None:
            bots_without_rollup.append(bot)
            continue
        any_rollup = True
        attribution = payload.get("user_attribution") or {}
        if attribution.get("available") is True:
            gate_open = True
        else:
            reason = str(attribution.get("reason") or "").strip()
            if reason and reason not in gate_reasons:
                gate_reasons.append(reason)
        withheld += int(_num(attribution.get("requesters_withheld")))

        for requester, entry in (payload.get("users") or {}).items():
            if not isinstance(entry, dict):
                continue
            stats = entry.get(_USER_WINDOW) or {}
            acc = per_person.setdefault(
                str(requester), {"turns": 0, "cost_estimated": 0.0, "bots": []}
            )
            acc["turns"] += int(_num(stats.get("turns")))
            acc["cost_estimated"] += float(_num(stats.get("cost_estimated")))
            if bot not in acc["bots"]:
                acc["bots"].append(bot)

        for app_id, entry in (payload.get("apps") or {}).items():
            if not isinstance(entry, dict):
                continue
            window = entry.get(_USER_WINDOW) or {}
            bucket = by_app.setdefault(str(app_id), {})
            for requester, blocks in (window.get("users") or {}).items():
                if not isinstance(blocks, dict):
                    continue
                # ``total`` is the deterministic grades only; ``inferred``
                # rides beside it and is never folded in (AL-1.3 §3).
                total = blocks.get("total") or {}
                acc = bucket.setdefault(
                    str(requester), {"turns": 0, "cost_estimated": 0.0}
                )
                acc["turns"] += int(_num(total.get("turns")))
                acc["cost_estimated"] += float(_num(total.get("cost_estimated")))

    if not any_rollup:
        return {
            "available": False,
            "producer": "usage_by_user",
            "window": _USER_WINDOW,
            "by_app": None,
            "requesters": None,
            "by_person": None,
            "requesters_withheld": None,
            "note": "no bot on the pod has written a per-person rollup",
            "bots_without_rollup": sorted(bots_without_rollup),
        }
    if not gate_open:
        # Every rollup present said its gate could not run. Reporting people
        # here would be reporting the ones the gate never got to withhold.
        return {
            "available": False,
            "producer": "usage_by_user",
            "window": _USER_WINDOW,
            "by_app": None,
            "requesters": None,
            "by_person": None,
            "requesters_withheld": withheld or None,
            "note": "; ".join(gate_reasons) or "per-person attribution is off",
            "bots_without_rollup": sorted(bots_without_rollup),
        }

    for acc in per_person.values():
        acc["cost_estimated"] = _round_usd(acc["cost_estimated"])
        acc["bots"].sort()
    for bucket in by_app.values():
        for acc in bucket.values():
            acc["cost_estimated"] = _round_usd(acc["cost_estimated"])
    return {
        "available": True,
        "producer": "usage_by_user",
        "window": _USER_WINDOW,
        "by_person": dict(sorted(per_person.items())),
        "by_app": {k: dict(sorted(v.items())) for k, v in sorted(by_app.items())},
        "requesters": sorted(per_person),
        "requesters_withheld": withheld,
        "note": "",
        "bots_without_rollup": sorted(bots_without_rollup),
    }


# ── drafts (manifests + readiness bands) ─────────────────────────────────────

_BANDS = ("ready", "emerging", "weak", "unscored")
_DEFINITION_STATUSES = ("discovered", "defined")


def collect_drafts(
    bots: Iterable[str],
    *,
    bot_home: Callable[[str], Path],
) -> dict[str, Any] | None:
    """Manifest counts by definition status and readiness band, per bot.

    Manifests live with the bot at ``{bot_home}/.openclaw/workspace/manifests/``
    and are read straight off disk — see the module docstring's third law.

    Scoring goes through ``app_readiness.score_readiness``, the same pure
    function the Apps page uses, so a band in the spine means exactly what a
    band in the UI means. If that module cannot be imported (a bare analyzer
    checkout), bands come back ``null`` while the manifest counts still land:
    losing the scorer must not lose the census.

    Returns ``None`` when no bot has a readable manifest directory.
    """
    score = _readiness_scorer()
    by_bot: dict[str, Any] = {}
    unreadable: list[str] = []
    any_dir = False

    for bot in bots:
        d = bot_home(bot) / ".openclaw" / "workspace" / "manifests"
        try:
            files = sorted(
                f for f in d.iterdir()
                if f.suffix == ".json"
                and not f.name.startswith((".", "_"))
            )
        except OSError:
            # Includes both "no manifests dir" and "cannot traverse it". The
            # two are indistinguishable from here and both mean the same
            # thing to a reader: this bot contributed no census.
            unreadable.append(bot)
            by_bot[bot] = None
            continue
        any_dir = True
        by_bot[bot] = _draft_counts(files, score)

    if not any_dir:
        return None
    totals = _sum_draft_counts(v for v in by_bot.values() if v is not None)
    return {
        "source": "manifests",
        "window": "point_in_time",
        **totals,
        "by_bot": by_bot,
        "bots_unreadable": sorted(unreadable),
    }


def _readiness_scorer() -> Callable[[dict], Any] | None:
    try:
        from evolve_admin.applications.app_readiness import (  # type: ignore
            score_readiness,
        )
    except Exception:
        return None
    return score_readiness


def _draft_counts(files: list[Path], score: Callable[[dict], Any] | None) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "manifests": 0,
        "unparseable": 0,
        "definition_status": {s: 0 for s in _DEFINITION_STATUSES},
        "bands": ({b: 0 for b in _BANDS} if score is not None else None),
        "eligible_to_offer": (0 if score is not None else None),
    }
    for f in files:
        data = _read_json(f)
        if data is None:
            counts["unparseable"] += 1
            continue
        counts["manifests"] += 1
        # Absent/empty reads as "discovered" — the manifest contract's own
        # safe default. Never accidentally count an unvouched draft as
        # operator-vouched.
        status = str(data.get("definition_status") or "discovered")
        if status not in counts["definition_status"]:
            counts["definition_status"][status] = 0
        counts["definition_status"][status] += 1
        if score is None:
            continue
        try:
            readiness = score(data)
        except Exception:
            counts["unparseable"] += 1
            continue
        band = str(getattr(readiness, "band", "unscored"))
        counts["bands"][band] = counts["bands"].get(band, 0) + 1
        if getattr(readiness, "eligible_to_offer", False):
            counts["eligible_to_offer"] += 1
    return counts


def _sum_draft_counts(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    out: dict[str, Any] = {
        "manifests": sum(int(_num(r.get("manifests"))) for r in rows),
        "unparseable": sum(int(_num(r.get("unparseable"))) for r in rows),
        "definition_status": {},
        "bands": None,
        "eligible_to_offer": None,
    }
    for r in rows:
        for k, v in (r.get("definition_status") or {}).items():
            out["definition_status"][k] = out["definition_status"].get(k, 0) + int(_num(v))
    scored = [r for r in rows if r.get("bands") is not None]
    if scored:
        bands: dict[str, int] = {}
        for r in scored:
            for k, v in r["bands"].items():
                bands[k] = bands.get(k, 0) + int(_num(v))
        out["bands"] = bands
        out["eligible_to_offer"] = sum(
            int(_num(r.get("eligible_to_offer"))) for r in scored
        )
    for s in _DEFINITION_STATUSES:
        out["definition_status"].setdefault(s, 0)
    return out


# ── drift (pod baseline census) ──────────────────────────────────────────────

def collect_drift(
    shared_dir: Path,
    network: dict[str, Any],
    *,
    home_overrides: dict[str, Path] | None = None,
) -> dict[str, Any] | None:
    """Per-state drift counts from the pod-baseline census.

    Returns ``None`` when the pod has declared no baseline
    (``{shared_dir}/pod-baseline.json`` absent) — there is nothing to drift
    FROM, and reporting "0 deviations" for an undeclared pod would be the
    single most misleading number this edition could carry.

    ``counts`` comes from ``CensusReport.counts()``, which pre-fills a zero
    for every state on purpose, so "no loosened rows" and "this producer
    doesn't know about loosened" are different bytes.
    """
    try:
        from pod_baseline.census import classify_readings, read_pod_surfaces
        from pod_baseline.store import load_baseline
    except Exception:
        return None
    try:
        baseline = load_baseline(shared_dir)
    except Exception:
        return None
    if baseline is None:
        return None
    try:
        readings = read_pod_surfaces(network, home_overrides=home_overrides or {})
        report = classify_readings(baseline, readings)
    except Exception:
        return None

    rows = list(report.rows)
    return {
        "source": "pod_baseline",
        "window": "point_in_time",
        "counts": report.counts(),
        "rows": len(rows),
        "bots": len({r.bot_id for r in rows}),
        "stale_exceptions": sum(1 for r in rows if r.stale_exception),
        "undeclared_surfaces": list(report.undeclared_surfaces),
        "declared_sentinel_surfaces": list(report.declared_sentinel_surfaces),
    }


# ── signals ──────────────────────────────────────────────────────────────────

_SEVERITIES = ("info", "warn", "alert")


def collect_signals(
    shared_dir: Path,
    window: EditionWindow,
) -> dict[str, Any] | None:
    """Active-signal snapshot plus the week's opened/resolved counts.

    Two different measurements, both wanted, and the spine must not confuse
    them:

    * ``active`` is a POINT-IN-TIME snapshot taken at ``computed_at`` —
      what was firing/snoozed when the edition was computed.
    * ``transitions`` is WINDOWED — state changes recorded in
      ``signals/log/<YYYY-MM-DD>.jsonl`` during the edition's week. The log
      is UTC-named, so the window's pod-local bounds are converted to UTC
      before picking days, and each record's ``at`` is re-checked against the
      real bounds; a pod west of UTC would otherwise silently lose its
      Sunday-evening transitions to the next edition.

    Returns ``None`` when the signal store does not exist at all.
    """
    root = shared_dir / "signals"
    if not root.is_dir():
        return None

    active_by_severity: dict[str, int] = {s: 0 for s in _SEVERITIES}
    active_by_producer: dict[str, int] = {}
    active_by_bot: dict[str, int] = {}
    active_by_state: dict[str, int] = {"firing": 0, "snoozed": 0}
    total = 0
    for sub in ("firing", "snoozed"):
        d = root / sub
        try:
            paths = sorted(d.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            sig = _read_json(path)
            if sig is None:
                continue
            total += 1
            active_by_state[sub] = active_by_state.get(sub, 0) + 1
            sev = str(sig.get("severity") or "info")
            active_by_severity[sev] = active_by_severity.get(sev, 0) + 1
            producer = str(sig.get("producer") or "unknown")
            active_by_producer[producer] = active_by_producer.get(producer, 0) + 1
            bot = sig.get("bot_id")
            if isinstance(bot, str) and bot:
                active_by_bot[bot] = active_by_bot.get(bot, 0) + 1

    return {
        "source": "signals_store",
        "active": {
            "window": "point_in_time",
            "total": total,
            "by_state": active_by_state,
            "by_severity": active_by_severity,
            "by_producer": dict(sorted(active_by_producer.items())),
            "by_bot": dict(sorted(active_by_bot.items())),
        },
        "transitions": _signal_transitions(root, window),
    }


def _signal_transitions(root: Path, window: EditionWindow) -> dict[str, Any] | None:
    """Opened / resolved / dismissed / snoozed counts inside the window.

    ``None`` when the log directory is absent — the store exists but this
    particular producer (the state-change log) has written nothing.
    """
    log_dir = root / "log"
    if not log_dir.is_dir():
        return None
    counts: dict[str, int] = {}
    by_producer: dict[str, int] = {}
    seen_any_file = False
    for day in _utc_days_covering(window):
        path = log_dir / f"{day.isoformat()}.jsonl"
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        seen_any_file = True
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            at = _parse_ts(rec.get("at"))
            if at is None or not window.contains(at):
                continue
            to_state = str(rec.get("to_state") or "unknown")
            key = "opened" if rec.get("from_state") is None else to_state
            counts[key] = counts.get(key, 0) + 1
            producer = str(rec.get("producer") or "unknown")
            by_producer[producer] = by_producer.get(producer, 0) + 1
    if not seen_any_file:
        return None
    return {
        "window": "edition_week",
        "total": sum(counts.values()),
        "by_kind": dict(sorted(counts.items())),
        "by_producer": dict(sorted(by_producer.items())),
    }


def _utc_days_covering(window: EditionWindow) -> list[date]:
    """UTC dates whose log files can hold transitions inside ``window``.

    The log is named by UTC date; the window is pod-local. Converting both
    ends and walking the inclusive UTC date range is what keeps a pod west of
    UTC from losing the tail of its week (the turn-files lesson).
    """
    first = window.start.astimezone(timezone.utc).date()
    last = window.end.astimezone(timezone.utc).date()
    span = (last - first).days
    return [first + timedelta(days=i) for i in range(span + 1)]


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ── activity footprint ───────────────────────────────────────────────────────

def collect_activity(
    shared_dir: Path,
    bots: Iterable[str],
    window: EditionWindow,
) -> dict[str, Any] | None:
    """Which bots left an annotation footprint during the edition's week.

    A FOOTPRINT, not a measurement, and named to say so: this counts the
    presence of ``{shared_dir}/annotations/<bot>/<date>.jsonl`` and its
    ``cost_events-<date>.jsonl`` sibling, without opening either. It is here
    because it is the one activity signal that exists on every pod — the
    daily ``cost_rollup`` job is not scheduled everywhere, and on a pod
    without it the whole ``costs`` block is legitimately ``null`` while the
    bots were plainly busy.

    THE PRECISION CAVEAT, stated in the payload as well as here: those files
    are UTC-dated (every reader in the tree derives their names from
    ``datetime.now(timezone.utc).date()``), while the window is pod-local. So
    the scan covers the UTC dates OVERLAPPING the window, which is up to one
    extra day at each edge. For "did this bot do anything this week" that is
    fine; it is not a day count and must never be presented as one.

    Returns ``None`` when the annotations root does not exist at all.
    """
    root = shared_dir / "annotations"
    if not root.is_dir():
        return None
    utc_days = _utc_days_covering(window)
    by_bot: dict[str, Any] = {}
    for bot in bots:
        d = root / bot
        if not d.is_dir():
            by_bot[bot] = None
            continue
        turn_days = sum(1 for day in utc_days if (d / f"{day.isoformat()}.jsonl").is_file())
        cost_days = sum(
            1 for day in utc_days
            if (d / f"cost_events-{day.isoformat()}.jsonl").is_file()
        )
        by_bot[bot] = {
            "days_with_turn_annotation_files": turn_days,
            "days_with_cost_event_files": cost_days,
            "utc_days_scanned": len(utc_days),
            "active": bool(turn_days or cost_days),
        }
    return {
        "source": "annotations",
        "window": "edition_week (UTC-dated files overlapping it)",
        "measure": "file presence only — the files are not opened or counted",
        "bots_with_annotations": sum(1 for v in by_bot.values() if v is not None),
        "by_bot": by_bot,
    }


# ── scheduled-fire history (the reliability spine) ───────────────────────────

def _read_scheduled_turns(
    shared_dir: Path, bot_id: str, *, days: int, today: date,
) -> list[dict[str, Any]] | None:
    """Turn annotations for one bot over the trailing window, or ``None``.

    Goes through ``exec_outcome_watchdog.read_turn_annotations`` — the single
    windowed reader for ``{shared_dir}/annotations/<bot>/<date>.jsonl``, the
    same one ``usage_by_app`` folds. A second reader over the same store is
    how two surfaces start disagreeing about the same day.

    ``None`` when the reader itself is unimportable (a bare analyzer
    checkout). An unreadable BOT is not distinguishable from a quiet one at
    this layer and comes back as an empty list — the caller reports the
    difference it can actually see (no annotations directory at all).
    """
    try:
        from exec_outcome_watchdog import read_turn_annotations  # type: ignore
    except Exception:
        return None
    try:
        return read_turn_annotations(shared_dir, bot_id, days=days, today=today)
    except Exception:
        return []


def _cron_map_app_ids(shared_dir: Path, bot_id: str) -> list[str]:
    """App ids Evolve installed a cron for on this bot (AL-1.2's map)."""
    payload = _read_json(shared_dir / bot_id / _APP_CRON_MAP_FILENAME)
    if not payload:
        return []
    return sorted({
        str(v).strip() for v in payload.values()
        if isinstance(v, str) and v.strip()
    })


#: Runs needed before an app is credited with a rhythm. Two runs give one
#: gap, and one gap is an anecdote — the third is what makes a repeated
#: interval a cadence rather than a coincidence.
_MIN_RUNS_FOR_CADENCE = 3


def _observed_cadence(run_dates: list[str]) -> int | None:
    """How many days apart this app runs, from its OWN history, or ``None``.

    NOTHING DECLARES THIS. The manifests on a real pod carry no schedule
    (every ``configured_schedules`` on the live mini is empty), and the cron
    line that does exist lives in the bot's own OpenClaw store behind a
    cross-user read. So the cadence is read off the runs themselves: the
    most common gap between consecutive run days.

    WHY IT MATTERS THAT THIS EXISTS AT ALL. Without it every app is judged
    against a DAILY expectation, and a weekly app is reported as missing six
    days out of seven, forever — a healthy app with a failing grade, which
    is worse than no grade. With fewer than :data:`_MIN_RUNS_FOR_CADENCE`
    runs there is no rhythm to read, and the answer is ``None``: the module
    then reports runs and makes no claim about misses at all.
    """
    if len(run_dates) < _MIN_RUNS_FOR_CADENCE:
        return None
    days = [date.fromisoformat(d) for d in run_dates]
    gaps = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0]
    if not gaps:
        return None
    # Mode, ties broken toward the SHORTER interval: over-expecting a run is
    # a visible miss the operator can dismiss; under-expecting one hides a
    # dead schedule, which is the failure this module exists to catch.
    return min(set(gaps), key=lambda g: (-gaps.count(g), g))


def _expected_dates(
    run_dates: list[str], window_dates: list[str], cadence: int | None,
) -> list[str]:
    """The days this app was due, from its first run onward. ``[]`` if unknown.

    The grid is anchored on the FIRST recorded run, not on the window's
    edge: an app installed on Thursday was not due on Monday, and a card
    that said otherwise would greet every new app with a week of failure.
    """
    if not cadence or not run_dates:
        return []
    first = date.fromisoformat(run_dates[0])
    return [
        d for d in window_dates
        if d >= run_dates[0] and (date.fromisoformat(d) - first).days % cadence == 0
    ]


def collect_fires(
    shared_dir: Path,
    bots: Iterable[str],
    *,
    today: date,
    days: int = FIRE_WINDOW_DAYS,
) -> dict[str, Any] | None:
    """Day-by-day scheduled-run history per app over the trailing window.

    THIS IS THE RELIABILITY MODULE'S MEASUREMENT, and it is deliberately the
    narrow one: a turn annotation whose ``app_attribution`` is ``scheduled``
    is a schedule that fired and produced work. Nothing else counts. An
    ``explicit`` turn is a person invoking the app by hand, and folding those
    in would report a pod whose crons are all dead as perfectly reliable —
    which is precisely the failure the module exists to catch.

    THE DAY IS THE UNIT, not the run. Two fires on one day is still one day
    that worked; the run counts ride in ``runs_by_date`` for the detail pane.

    A MISS IS JUDGED AGAINST THE APP'S OWN RHYTHM (:func:`_observed_cadence`),
    never against a daily default. A weekly app measured daily is reported as
    missing six days in seven — a healthy app wearing a failing grade — and
    an app with too few runs to have a rhythm gets no miss claim at all
    rather than a guessed one.

    COVERAGE STARTS AT THE FIRST RUN, not at the window edge. An app
    installed on Thursday has not "missed" Monday through Wednesday, and a
    module that counted it that way would greet every new app with a failing
    grade. So ``days_covered`` runs from the app's first recorded run to the
    end of the window, and ``missed_dates`` are the days inside THAT span
    with nothing recorded.

    THE UTC CAVEAT, in the payload as well as here: the annotation files are
    UTC-dated, so a fire just before pod-local midnight lands on the next
    UTC day. For "did it run that day" over a 28-day strip that is a
    boundary blur of at most one cell, not a wrong answer — but the payload
    says so rather than letting a reader assume pod-local days.

    ``apps_without_history`` names the apps Evolve installed a cron for that
    have no scheduled turn at all. That is the explain-and-remediate row:
    "we know this one is on a schedule and nothing here has recorded it
    running" is a to-do, never a zero.

    Returns ``None`` when the annotations root does not exist — the whole
    producer is absent, which is a different fact from "nothing fired".
    """
    root = shared_dir / "annotations"
    if not root.is_dir():
        return None

    window_days = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    window_dates = [d.isoformat() for d in window_days]
    first_date, last_date = window_dates[0], window_dates[-1]

    runs: dict[str, dict[str, int]] = {}
    app_bots: dict[str, list[str]] = {}
    installed: dict[str, list[str]] = {}
    bots_without_annotations: list[str] = []
    reader_available = True

    for bot in bots:
        for app_id in _cron_map_app_ids(shared_dir, bot):
            installed.setdefault(app_id, []).append(bot)
        if not (root / bot).is_dir():
            bots_without_annotations.append(bot)
            continue
        records = _read_scheduled_turns(shared_dir, bot, days=days, today=today)
        if records is None:
            reader_available = False
            continue
        for rec in records:
            if rec.get("app_attribution") != _SCHEDULED_GRADE:
                continue
            app_id = rec.get("app_id")
            if not (isinstance(app_id, str) and app_id.strip()):
                continue
            day = str(rec.get("ts") or "")[:10]
            if day not in window_dates:
                continue
            app_id = app_id.strip()
            runs.setdefault(app_id, {})
            runs[app_id][day] = runs[app_id].get(day, 0) + 1
            bots_seen = app_bots.setdefault(app_id, [])
            if bot not in bots_seen:
                bots_seen.append(bot)

    apps: dict[str, Any] = {}
    for app_id, by_date in runs.items():
        ran = sorted(by_date)
        cadence = _observed_cadence(ran)
        expected = _expected_dates(ran, window_dates, cadence)
        missed = [d for d in expected if d not in by_date] if expected else []
        apps[app_id] = {
            "bots": sorted(app_bots.get(app_id, [])),
            "installed_by_evolve": app_id in installed,
            "runs_by_date": dict(sorted(by_date.items())),
            "first_run_date": ran[0],
            "last_run_date": ran[-1],
            "days_ran": len(ran),
            # How often this app runs, in days, READ OFF ITS OWN HISTORY —
            # never declared. ``null`` until it has run enough times to have
            # a rhythm, and every miss claim below is null with it.
            "cadence_days": cadence,
            "days_covered": len(expected) if expected else None,
            "days_missed": len(missed) if expected else None,
            "expected_dates": expected or None,
            "missed_dates": missed if expected else None,
            "runs_total": sum(by_date.values()),
        }

    without = sorted(app_id for app_id in installed if app_id not in apps)
    return {
        "source": "annotations (scheduled attribution) + app-cron-map",
        "grade": _SCHEDULED_GRADE,
        "window": {
            "days": days,
            "first_date": first_date,
            "last_date": last_date,
            "dates": window_dates,
            "note": "UTC-dated annotation files; a run just before pod-local "
                    "midnight lands on the next day of the strip",
        },
        "apps": dict(sorted(apps.items())),
        "apps_without_history": without,
        "apps_installed_on_a_schedule": {
            app_id: sorted(set(bot_ids)) for app_id, bot_ids in sorted(installed.items())
        },
        "bots_without_annotations": sorted(bots_without_annotations),
        "annotation_reader_available": reader_available,
    }


# ── roster activity ──────────────────────────────────────────────────────────

def collect_roster(
    network: dict[str, Any],
    bots: list[str],
    activity: dict[str, Any] | None,
    costs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Roster size and how many of its members were active in the window.

    "Active" needs a definition, and which definition was used is part of the
    measurement — so the payload carries it. The annotation footprint is
    preferred because it exists on every pod; the cost rollup is the fallback
    for a pod that somehow has rollups but no annotations. With NEITHER
    producer present, ``active_in_window`` is ``null``: an unmeasured pod is
    not an idle one, and a longitudinal reader must not be able to mistake
    the two.
    """
    active_ids: list[str] | None = None
    definition = None
    if activity is not None:
        by_bot = activity.get("by_bot") or {}
        active_ids = sorted(
            b for b, v in by_bot.items() if isinstance(v, dict) and v.get("active")
        )
        definition = "an annotation or cost-event file dated inside the week"
    elif costs is not None:
        by_bot = costs.get("by_bot") or {}
        active_ids = sorted(b for b, v in by_bot.items() if v is not None)
        definition = "at least one cost rollup during the edition week"
    return {
        "members": len(bots),
        "ids": list(bots),
        "primary": network.get("primary") or None,
        "active_in_window": None if active_ids is None else len(active_ids),
        "active_ids": active_ids,
        "activity_definition": definition,
    }
