#!/usr/bin/env python3
"""value_baseline.py — per-bot utilization baseline + ``bot_underused`` Signal.

Spec: internal/spec-value-baseline-2026-06-10.md (U0; build slices B1 + B2).

Answers "is this pod underused, and which bot is the idle one?" from data
already on disk. Pure Python, no LLM calls, no new collection.

What counts as value (spec §2): a bot delivers value when **a person
chooses to use it** or **it proactively delivers something a person
receives**. Volume, chattiness, breadth, cost, and mood are explicitly
excluded — no metric here may reward higher turn counts.

The metric set (spec §4.2, all tri-state — ``None`` means *couldn't
measure*, never coerced to 0):

  * ``active_human_days_7d/_28d`` — days with ≥ 1 ``user_turn`` cost
    event. Day-granular by design: one human interaction makes the day
    active; ten more add nothing.
  * ``proactive_runs_7d/_28d``    — count of ``cron_app`` cost events
    (scheduled app runs; heartbeats and subagents excluded). Honest
    label: runs, not confirmed deliveries (U2.1 upgrades the source).
  * ``app_coverage_28d``          — apps used / apps installed. ``None``
    when no apps are installed (undefined, not 0%).
  * ``value_trend_28d``           — (human days + proactive runs) this
    28d vs the prior 28d. Descriptive only; never gates the predicate.
  * ``usage_breadth_28d``         — distinct (noun × verb) cells from
    ObservationTuples. Descriptive only; DNT opt-out reads as ``None``.

Day-level measurability (spec §4.3): a day is *measurable* when its
metrics file exists AND (turn_count == 0 OR ≥ 1 cost event is readable).
A day with turns but no cost events is unmeasurable — activity happened
but nothing can say who triggered it; counting it either way would lie.
A window below the 80% measurability floor renders its metrics ``None``.

Rollup (spec §5.2): one pod-wide file per day at
``{shared_dir}/metrics/value/{anchor_date}.json``. ``utilization_state``
(active | underused | unmeasurable) is computed ONCE here — the tile
chip, the Value view, and the Signal producer all read this field rather
than re-implementing the predicate. Rollups older than 90 days are
pruned in the same pass.

Signals (spec §6): the strict v1 predicate fires ``bot_underused`` only
when age ≥ 28d, measurability ≥ 80%, zero human days AND zero proactive
runs over 28d. Unmeasurable bots never fire. When more than half the
old-enough fleet is unmeasurable, a single pod-scope
``value_baseline_coverage`` warn fires instead — that shape means the
recording pipeline is broken, not that the bots are idle. Recovery is
``sweep_resolve``; dismissal is the durable per-bot opt-out.

Runs as a post-step of the nightly measure job (spec §5.1 Option A — no
new LaunchDaemon). Standalone CLI shares the same code path:

    python3 -m value_baseline --shared-dir /Users/Shared/evolve [--rank]
    python3 -m value_baseline --shared-dir ... --bot-id <bot>   # read-only

Consumption guardrail (spec §2.2): no generator subscribes to
``bot_underused``; the only consumers are the operator surfaces and the
proof artifact until the signal is validated on live data.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from schema.signal import make_signature
from signals import store as signals_store
from tile_metrics import (
    _apps_used_in_window,
    _list_app_manifests,
    _load_daily_metric,
    _read_cost_events,
)

PRODUCER = "value_baseline"

# Spec §4.3: a window with < 80% measurable days renders its metrics null.
MEASURABILITY_FLOOR = 0.8

# Spec §6.3 gate 1: bots younger than this are onboarding, not underused.
AGE_GATE_DAYS = 28

# Spec §5.2: prune rollups older than this (matches signal-archive
# retention; the trend only needs 56d).
ROLLUP_RETENTION_DAYS = 90

ROLLUP_VERSION = 1

# Day-facts span: 28d current window + 28d prior window for the trend.
_SPAN_DAYS = 56


# ─────────────────────────────────────────────────────────────────────────────
# Day-level measurability (spec §4.3)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DayFacts:
    """One bot-day, classified.

    ``measurable`` is False when the day's metrics file is missing OR the
    metrics/cost-events cross-check fails (turns recorded but no cost
    events readable — the annotation pipeline can't say who triggered
    them). ``human_events`` / ``cron_app_events`` are only meaningful on
    measurable days.
    """

    measurable: bool
    human_events: int = 0
    cron_app_events: int = 0
    metrics: dict | None = None


def classify_day(shared_dir: Path, bot_id: str, d: date) -> DayFacts:
    """Classify one (bot, day) per the spec §4.3 tri-state rules."""
    metrics = _load_daily_metric(shared_dir, bot_id, d)
    if metrics is None:
        return DayFacts(measurable=False)

    human = 0
    cron_app = 0
    n_events = 0
    for rec in _read_cost_events(shared_dir, bot_id, d):
        n_events += 1
        kind = rec.get("trigger_kind")
        if kind == "user_turn":
            human += 1
        elif kind == "cron_app":
            cron_app += 1

    try:
        turn_count = int(metrics.get("turn_count", 0) or 0)
    except (TypeError, ValueError):
        turn_count = 0
    if turn_count > 0 and n_events == 0:
        # Activity happened but the annotation pipeline can't attribute
        # it — counting the day as active OR inactive would lie.
        return DayFacts(measurable=False)

    return DayFacts(
        measurable=True,
        human_events=human,
        cron_app_events=cron_app,
        metrics=metrics,
    )


def collect_day_facts(
    shared_dir: Path, bot_id: str, anchor: date, span_days: int = _SPAN_DAYS
) -> dict[date, DayFacts]:
    """Classify every day in ``[anchor - span + 1, anchor]``."""
    out: dict[date, DayFacts] = {}
    for i in range(span_days):
        d = anchor - timedelta(days=i)
        out[d] = classify_day(shared_dir, bot_id, d)
    return out


@dataclass(frozen=True)
class WindowFacts:
    """Aggregates over the measurable days of one window."""

    window_days: int
    measurable_days: int
    human_days: int  # days with ≥1 user_turn event (day-granular)
    cron_app_runs: int  # total cron_app events across measurable days

    @property
    def meets_floor(self) -> bool:
        return self.measurable_days >= MEASURABILITY_FLOOR * self.window_days


def window_facts(
    facts: dict[date, DayFacts], start: date, end_inclusive: date
) -> WindowFacts:
    """Aggregate day facts over ``[start, end_inclusive]``."""
    window_days = (end_inclusive - start).days + 1
    measurable = human_days = cron_runs = 0
    d = start
    while d <= end_inclusive:
        f = facts.get(d)
        if f is not None and f.measurable:
            measurable += 1
            if f.human_events > 0:
                human_days += 1
            cron_runs += f.cron_app_events
        d += timedelta(days=1)
    return WindowFacts(
        window_days=window_days,
        measurable_days=measurable,
        human_days=human_days,
        cron_app_runs=cron_runs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot metrics-file history (anchor + age)
# ─────────────────────────────────────────────────────────────────────────────


def _metrics_date_dirs(shared_dir: Path) -> list[date]:
    """All date-named dirs under {shared_dir}/metrics/, sorted ascending.

    Skips non-date entries (notably the ``value/`` rollup dir this module
    writes).
    """
    base = shared_dir / "metrics"
    out: list[date] = []
    try:
        children = list(base.iterdir())
    except OSError:
        return out
    for child in children:
        if not child.is_dir():
            continue
        try:
            out.append(date.fromisoformat(child.name))
        except ValueError:
            continue
    return sorted(out)


def _bot_metrics_dates(
    shared_dir: Path, bot_id: str, all_dates: list[date]
) -> tuple[date | None, date | None]:
    """(first, last) date with a metrics file for this bot, or (None, None)."""
    first: date | None = None
    last: date | None = None
    for d in all_dates:
        if (shared_dir / "metrics" / d.isoformat() / f"{bot_id}.json").exists():
            if first is None:
                first = d
            last = d
    return first, last


# ─────────────────────────────────────────────────────────────────────────────
# Descriptive metrics: app coverage + usage breadth
# ─────────────────────────────────────────────────────────────────────────────


def _app_coverage(
    shared_dir: Path,
    bot_id: str,
    facts: dict[date, DayFacts],
    start: date,
    end_inclusive: date,
    network: dict | None,
) -> dict[str, Any]:
    """``apps.used_28d / apps.total`` via the existing tile helpers.

    Mirrors ``tile_metrics.compute_tile_data``: total spans manifests AND
    apps that ran in the window, so a manifest-less legacy app can't push
    used > total. ``None`` when no apps exist (coverage is undefined,
    not 0%).
    """
    by_date = {
        d: f.metrics
        for d, f in facts.items()
        if f.metrics is not None and start <= d <= end_inclusive
    }
    manifest_ids = set(_list_app_manifests(shared_dir, bot_id, network))
    used_ids = _apps_used_in_window(by_date, start, end_inclusive + timedelta(days=1))
    total_ids = manifest_ids | used_ids
    if not total_ids:
        return {"value": None, "apps_total": 0, "apps_used": 0}
    return {
        "value": round(len(used_ids) / len(total_ids), 3),
        "apps_total": len(total_ids),
        "apps_used": len(used_ids),
    }


def _usage_breadth(
    shared_dir: Path, bot_id: str, start: date, end_inclusive: date
) -> dict[str, Any]:
    """Distinct (noun × verb) cells from ObservationTuples over the window.

    Descriptive only (spec §2.1) — never enters any predicate. ``None``
    when no observation files exist for the window: the observations
    pipeline didn't run, or the bot's DNT opt-out is on. Absence must
    read as *can't tell*, never as zero breadth.
    """
    obs_dir = shared_dir / "observations" / bot_id
    if not obs_dir.is_dir():
        return {"value": None}
    cells: set[tuple[str, str]] = set()
    saw_file = False
    d = start
    while d <= end_inclusive:
        path = obs_dir / f"{d.isoformat()}.jsonl"
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                # Unreadable ≠ read-and-empty: don't let a permission
                # error turn "can't tell" into a confident 0.
                d += timedelta(days=1)
                continue
            saw_file = True
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                noun = rec.get("noun")
                verb = rec.get("verb")
                if isinstance(noun, str) and isinstance(verb, str) and noun and verb:
                    cells.add((noun, verb))
        d += timedelta(days=1)
    if not saw_file:
        return {"value": None}
    return {"value": len(cells)}


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot baseline entry (spec §5.2 per-bot shape)
# ─────────────────────────────────────────────────────────────────────────────


def _window_metric(wf: WindowFacts, value: int | None = None) -> dict[str, Any]:
    """Render one window metric with its coverage (spec §4.3 shape)."""
    return {
        "value": value if wf.meets_floor else None,
        "measurable_days": wf.measurable_days,
        "window_days": wf.window_days,
    }


def compute_bot_entry(
    shared_dir: Path,
    bot_id: str,
    *,
    network: dict | None = None,
    all_metric_dates: list[date] | None = None,
    fallback_anchor: date | None = None,
) -> dict[str, Any]:
    """Compute the full §5.2 per-bot entry, ``utilization_state`` included.

    Windows anchor on the bot's most recent metrics-file day (the
    ``tile_metrics`` anchor convention); ``fallback_anchor`` (default:
    today) is used when the bot has no metrics files at all — every day
    is then unmeasurable and the state comes out ``unmeasurable``.
    """
    if all_metric_dates is None:
        all_metric_dates = _metrics_date_dirs(shared_dir)
    first_day, last_day = _bot_metrics_dates(shared_dir, bot_id, all_metric_dates)
    anchor = last_day or fallback_anchor or date.today()
    age_days = (anchor - first_day).days if first_day is not None else None

    facts = collect_day_facts(shared_dir, bot_id, anchor)

    w7 = window_facts(facts, anchor - timedelta(days=6), anchor)
    w28 = window_facts(facts, anchor - timedelta(days=27), anchor)
    w28_prior = window_facts(
        facts, anchor - timedelta(days=55), anchor - timedelta(days=28)
    )

    current = (
        w28.human_days + w28.cron_app_runs if w28.meets_floor else None
    )
    prior = (
        w28_prior.human_days + w28_prior.cron_app_runs
        if w28_prior.meets_floor
        else None
    )
    trend = {
        "value": current - prior if current is not None and prior is not None else None,
        "current": current,
        "prior": prior,
    }

    state, reason = _utilization_state(age_days, w28)

    return {
        "active_human_days_7d": _window_metric(w7, w7.human_days),
        "active_human_days_28d": _window_metric(w28, w28.human_days),
        "proactive_runs_7d": _window_metric(w7, w7.cron_app_runs),
        "proactive_runs_28d": _window_metric(w28, w28.cron_app_runs),
        "app_coverage_28d": _app_coverage(
            shared_dir, bot_id, facts, anchor - timedelta(days=27), anchor, network
        ),
        "value_trend_28d": trend,
        "usage_breadth_28d": _usage_breadth(
            shared_dir, bot_id, anchor - timedelta(days=27), anchor
        ),
        "age_days": age_days,
        "anchor_date": anchor.isoformat(),
        "utilization_state": state,
        "state_reason": reason,
    }


def _utilization_state(
    age_days: int | None, w28: WindowFacts
) -> tuple[str, str]:
    """The ONE place the active/underused/unmeasurable predicate lives.

    Spec §5.2: the tile chip, the Value view, and the Signal producer all
    read the rollup's ``utilization_state`` — nothing re-implements this.
    state_reason copy is operator-facing (Plex test, spec §7.3): plain
    words, no internal vocabulary.
    """
    if age_days is None:
        return "unmeasurable", "no usage records exist for this bot yet"
    if age_days < AGE_GATE_DAYS:
        return (
            "unmeasurable",
            f"first usage records are only {age_days} days old — too new to assess",
        )
    if not w28.meets_floor:
        return (
            "unmeasurable",
            f"usage records cover only {w28.measurable_days} of the last "
            f"{w28.window_days} days",
        )
    if w28.human_days == 0 and w28.cron_app_runs == 0:
        return (
            "underused",
            f"no human use and no scheduled app runs in the last "
            f"{w28.window_days} days ({w28.measurable_days} days of records)",
        )
    return (
        "active",
        f"{w28.human_days} day(s) with human use and {w28.cron_app_runs} "
        f"scheduled app run(s) in the last {w28.window_days} days",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pod-wide rollup (spec §5.2)
# ─────────────────────────────────────────────────────────────────────────────


def compute_rollup(
    shared_dir: Path,
    bot_ids: list[str],
    *,
    network: dict | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Compute the pod-wide rollup dict for the whole fleet."""
    today = today or date.today()
    all_dates = _metrics_date_dirs(shared_dir)
    bots: dict[str, dict[str, Any]] = {}
    for bot_id in sorted(bot_ids):
        bots[bot_id] = compute_bot_entry(
            shared_dir,
            bot_id,
            network=network,
            all_metric_dates=all_dates,
            fallback_anchor=today,
        )
    anchors = [
        date.fromisoformat(e["anchor_date"])
        for e in bots.values()
        if e.get("age_days") is not None
    ]
    pod_anchor = max(anchors) if anchors else today
    return {
        "version": ROLLUP_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "anchor_date": pod_anchor.isoformat(),
        "bots": bots,
    }


def rollup_dir(shared_dir: Path) -> Path:
    return shared_dir / "metrics" / "value"


# Spec §5.3: a rollup older than this is flagged stale at the surface —
# measurement degradation must be visible, not silently rendered as last
# week's numbers.
STALE_AFTER_HOURS = 48


def load_latest_rollup(shared_dir: Path) -> dict[str, Any] | None:
    """Newest readable daily rollup, or ``None`` when none exists.

    The B3 surfacing consumers (bot tile, Value view) call this rather
    than globbing the dir themselves, so the rollup layout has exactly
    one reader. Unparseable files are skipped (an interrupted write must
    not blank the whole view when an older rollup is still good).
    """
    base = rollup_dir(shared_dir)
    dated: list[tuple[date, Path]] = []
    for path in base.glob("*.json"):
        try:
            dated.append((date.fromisoformat(path.stem), path))
        except ValueError:
            continue
    for _, path in sorted(dated, reverse=True):
        try:
            rollup = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rollup, dict):
            return rollup
    return None


def rollup_is_stale(
    rollup: dict[str, Any], *, now: datetime | None = None
) -> bool:
    """Spec §5.3: True when the rollup is older than 48h.

    Prefers ``computed_at`` (the actual write time); falls back to
    ``anchor_date`` + the nightly cadence (the rollup for anchor day D is
    written early on D+1, so an anchor more than 2 days back means the
    nightly run has missed a cycle). Unparseable timestamps read as
    stale — "can't tell how old" must not render as "fresh".
    """
    now = now or datetime.now(timezone.utc)
    computed = rollup.get("computed_at")
    if isinstance(computed, str):
        try:
            ts = datetime.fromisoformat(computed)
        except ValueError:
            ts = None
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (now - ts) > timedelta(hours=STALE_AFTER_HOURS)
    try:
        anchor = date.fromisoformat(str(rollup.get("anchor_date")))
    except (TypeError, ValueError):
        return True
    return (now.date() - anchor).days > 2


_STATE_ORDER = {"active": 0, "unmeasurable": 1, "underused": 2}


def _metric_value(entry: dict[str, Any], name: str) -> Any:
    metric = entry.get(name)
    return metric.get("value") if isinstance(metric, dict) else None


def _rank_key(item: tuple[str, dict[str, Any]]) -> tuple:
    """Spec §9 rank key — explicit, not a published score.

    utilization_state (underused last), then active_human_days_28d desc,
    then proactive_runs_28d desc. ``None`` metric values sort after 0.
    """
    bot_id, e = item
    human = _metric_value(e, "active_human_days_28d")
    runs = _metric_value(e, "proactive_runs_28d")
    return (
        _STATE_ORDER.get(str(e.get("utilization_state")), 1),
        -(human if human is not None else -1),
        -(runs if runs is not None else -1),
        bot_id,
    )


def rank_bots(rollup: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Rollup bots sorted by the §9 rank key.

    Shared by the CLI ``--rank`` table and the Value-view endpoint so the
    ordering, like the predicate, exists in exactly one place.
    """
    bots = rollup.get("bots")
    if not isinstance(bots, dict):
        return []
    return sorted(bots.items(), key=_rank_key)


def write_rollup(shared_dir: Path, rollup: dict[str, Any]) -> Path:
    """Atomic temp-file + rename write of the daily rollup.

    Named by ``anchor_date`` so a re-run on the same data is idempotent
    (one pod-wide file per day). No sudo / /tmp staging — the dir lives
    under {shared_dir}, owned by the evolve user.
    """
    out_dir = rollup_dir(shared_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{rollup['anchor_date']}.json"
    fd, tmp = tempfile.mkstemp(dir=str(out_dir), prefix=".rollup-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rollup, fh, indent=2)
        os.replace(tmp, out_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError as cleanup_err:
            print(
                f"[value_baseline] temp-file cleanup failed: {cleanup_err}",
                file=sys.stderr,
            )
        raise
    return out_path


def prune_rollups(
    shared_dir: Path, *, today: date | None = None
) -> list[Path]:
    """Delete rollup files older than ROLLUP_RETENTION_DAYS. Returns removed."""
    today = today or date.today()
    cutoff = today - timedelta(days=ROLLUP_RETENTION_DAYS)
    removed: list[Path] = []
    base = rollup_dir(shared_dir)
    if not base.is_dir():
        return removed
    for path in base.glob("*.json"):
        try:
            d = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if d < cutoff:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                continue
    return removed


# ─────────────────────────────────────────────────────────────────────────────
# Signals (spec §6 — slice B2)
# ─────────────────────────────────────────────────────────────────────────────


def _underused_signal_spec(bot_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Build the observe() kwargs for one underused bot.

    Operator copy is spec §7.3 (Plex test — no internal vocabulary).
    Severity is deliberately NOT passed: the producer default ("info",
    registered in signals/producer_severity.py) is the single policy
    point.
    """
    anchor = date.fromisoformat(entry["anchor_date"])
    idle_since = anchor - timedelta(days=27)
    w28 = entry["active_human_days_28d"]
    return dict(
        signature=make_signature(PRODUCER, "bot_underused", bot_id),
        producer=PRODUCER,
        type="bot_underused",
        flavor="activity",
        scope="bot",
        bot_id=bot_id,
        title="A bot has been idle for 4 weeks",
        body=(
            f"Nobody has used *{bot_id}* since "
            f"{idle_since.strftime('%B')} {idle_since.day}, and it has no "
            "scheduled jobs delivering anything.\n"
            "If it's waiting on setup, connect it to a chat channel. "
            "If it's not needed, you can remove it.\n"
            "Dismiss this and we won't bring it up again."
        ),
        details=dict(
            days_with_human_use=0,
            scheduled_app_runs=0,
            days_with_usage_records=w28["measurable_days"],
            window_days=w28["window_days"],
            idle_since=idle_since.isoformat(),
            what_it_means=(
                "No one has used this bot recently and it isn't delivering "
                "anything on a schedule. It may be waiting on setup, or it "
                "may no longer be needed."
            ),
            fix_steps=(
                "1. If the bot is waiting on setup, connect it to a chat "
                "channel so people can reach it.\n"
                "2. If the bot isn't needed anymore, remove it.\n"
                "3. If this is deliberate (a seasonal or standby bot), "
                "dismiss this alert and it won't come up again."
            ),
        ),
    )


def _coverage_signal_spec(
    shared_dir: Path, broken_bots: list[str], fleet_size: int
) -> dict[str, Any]:
    """Pod-scope warn: most of the fleet can't be measured (spec §6.2).

    This shape means the recording pipeline is broken, not that the bots
    are idle — "distinguish tooling failure from findings" applied at the
    producer level. Per-bot unmeasurable entries stay quiet to avoid N
    copies of one pipeline problem.
    """
    return dict(
        signature=make_signature(PRODUCER, "value_baseline_coverage", "pod"),
        producer=PRODUCER,
        type="value_baseline_coverage",
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title="Can't tell which bots are in use — usage records are incomplete",
        body=(
            f"Usage records are missing or incomplete for {len(broken_bots)} "
            f"of {fleet_size} bots over the last 4 weeks, so Evolve can't "
            "tell which bots are actually in use. This usually means the "
            "nightly usage recording is failing — it does not mean the bots "
            "are idle.\n"
            f"Affected: {', '.join(sorted(broken_bots))}"
        ),
        details=dict(
            bots_without_records=sorted(broken_bots),
            fleet_size=fleet_size,
            what_it_means=(
                "Most bots don't have enough usage records to tell whether "
                "anyone is using them. That points at a recording problem, "
                "not idle bots."
            ),
            fix_steps=(
                f"1. Check the nightly recording log: "
                f"{shared_dir}/logs/measure.log\n"
                "2. Confirm the job is loaded: sudo launchctl print "
                "system/ai.openclaw.evolve.measure"
            ),
        ),
    )


def fire_signals(
    shared_dir: Path, rollup: dict[str, Any]
) -> tuple[int, int]:
    """Fire/bump Signals from the rollup, then sweep-resolve what cleared.

    Reads ``utilization_state`` from the rollup — the predicate is NOT
    re-implemented here (spec §5.2). Returns (n_fired, n_resolved).
    """
    bots: dict[str, dict[str, Any]] = rollup.get("bots", {})
    kept: set[str] = set()
    n_fired = 0

    for bot_id, entry in bots.items():
        if entry.get("utilization_state") != "underused":
            continue
        spec = _underused_signal_spec(bot_id, entry)
        kept.add(spec["signature"])
        try:
            signals_store.observe(shared_dir, **spec)
            n_fired += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"[value_baseline] observe failed for {spec['signature']}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    # Fleet-level measurement failure (spec §6.2): count only bots that
    # are OLD ENOUGH to have records but fall below the floor. Bots under
    # the age gate are also "unmeasurable" but that's onboarding, not a
    # broken pipeline — a fresh pod must not warn that recording failed.
    broken = [
        bot_id
        for bot_id, entry in bots.items()
        if entry.get("utilization_state") == "unmeasurable"
        and entry.get("age_days") is not None
        and entry["age_days"] >= AGE_GATE_DAYS
    ]
    if bots and len(broken) > len(bots) / 2:
        spec = _coverage_signal_spec(shared_dir, broken, len(bots))
        kept.add(spec["signature"])
        try:
            signals_store.observe(shared_dir, **spec)
            n_fired += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"[value_baseline] observe failed for {spec['signature']}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    n_resolved = 0
    try:
        resolved = signals_store.sweep_resolve(
            shared_dir,
            producer=PRODUCER,
            kept_signatures=kept,
            reason="auto-resolve: bot back in use (or condition cleared)",
        )
        n_resolved = len(resolved)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[value_baseline] sweep_resolve failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
    return n_fired, n_resolved


# ─────────────────────────────────────────────────────────────────────────────
# Nightly entrypoint (measure.py post-step) + CLI
# ─────────────────────────────────────────────────────────────────────────────


def run_nightly(
    shared_dir: Path,
    bot_ids: list[str],
    *,
    network: dict | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """One full pass: compute → write rollup → prune retention → signals.

    Called by measure.py after per-bot aggregation (spec §5.1 Option A)
    and by the no-flag CLI path — manual runs and the nightly step share
    this one code path.
    """
    rollup = compute_rollup(shared_dir, bot_ids, network=network, today=today)
    out_path = write_rollup(shared_dir, rollup)
    pruned = prune_rollups(shared_dir, today=today)
    n_fired, n_resolved = fire_signals(shared_dir, rollup)
    states = [e["utilization_state"] for e in rollup["bots"].values()]
    print(
        f"[value_baseline] {out_path.name}: {len(states)} bots "
        f"({states.count('active')} active, {states.count('underused')} underused, "
        f"{states.count('unmeasurable')} unmeasurable); "
        f"{n_fired} signal(s) observed, {n_resolved} resolved, "
        f"{len(pruned)} old rollup(s) pruned",
        flush=True,
    )
    return rollup


def _fmt(value: Any) -> str:
    return "n/a" if value is None else str(value)


def render_rank_table(rollup: dict[str, Any]) -> str:
    """Human-readable ranked table (proof artifact, spec §9).

    Rank key — explicit, not a published score: utilization_state
    (underused last), then active_human_days_28d desc, then
    proactive_runs_28d desc (``rank_bots``, shared with the Value view).
    """
    rows = [
        (
            bot_id,
            e["utilization_state"],
            _fmt(e["active_human_days_28d"]["value"]),
            _fmt(e["proactive_runs_28d"]["value"]),
            _fmt(e["app_coverage_28d"]["value"]),
            _fmt(e["value_trend_28d"]["value"]),
            f"{e['active_human_days_28d']['measurable_days']}/"
            f"{e['active_human_days_28d']['window_days']}",
        )
        for bot_id, e in rank_bots(rollup)
    ]
    header = (
        "bot", "state", "human days 28d", "app runs 28d",
        "app coverage", "trend", "days w/ records",
    )
    widths = [
        max(len(header[i]), *(len(r[i]) for r in rows)) if rows else len(header[i])
        for i in range(len(header))
    ]
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(header)),
        "  ".join("-" * w for w in widths),
    ]
    for r in rows:
        lines.append("  ".join(r[i].ljust(widths[i]) for i in range(len(r))))
    lines.append(f"\nas of {rollup['anchor_date']}")
    return "\n".join(lines)


def _resolve_fleet(network: dict, shared_dir: Path) -> list[str]:
    """Pod membership — same resolution as measure.py: network members
    plus the primary 'evolve' bot, falling back to annotation-dir
    discovery when the network carries no member list."""
    bots = list(network.get("members") or [])
    if bots:
        if "evolve" not in bots:
            bots.append("evolve")
        return bots
    annotation_base = shared_dir / "annotations"
    if annotation_base.is_dir():
        return sorted(d.name for d in annotation_base.iterdir() if d.is_dir())
    return []


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-bot utilization baseline (spec internal/spec-value-baseline-2026-06-10.md)"
    )
    parser.add_argument("--shared-dir", default=None, help="Path to shared evolve dir")
    parser.add_argument("--network", default=None, help="Path to network.json")
    parser.add_argument(
        "--rank", action="store_true", help="Print the ranked fleet table"
    )
    parser.add_argument(
        "--bot-id", default=None,
        help="Single-bot run: print this bot's entry as JSON (no rollup write, no signals)",
    )
    args = parser.parse_args()

    from evolve_config import get_shared_dir, load_config

    network = load_config(args.network)
    shared_dir = Path(args.shared_dir) if args.shared_dir else get_shared_dir(network)

    if args.bot_id:
        entry = compute_bot_entry(shared_dir, args.bot_id, network=network)
        print(json.dumps(entry, indent=2))
        return

    bots = _resolve_fleet(network, shared_dir)
    if not bots:
        print("[value_baseline] no bots to assess", file=sys.stderr)
        sys.exit(1)
    rollup = run_nightly(shared_dir, bots, network=network)
    if args.rank:
        print(render_rank_table(rollup))


if __name__ == "__main__":
    main()
