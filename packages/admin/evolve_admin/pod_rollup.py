"""
pod_rollup.py — Cross-bot aggregation for the pod-level dashboard widgets.

Computes four summary views that aggregate across all member bots:

  - spend         per-bot cost breakdown + pod total + projected month-end
  - attention     bots with firing signals or active health chips
  - activity      which bots are busiest (turns + sessions in last 7d)
  - deadlines     upcoming deadlines from any bot's application manifests

All inputs are read-only. This module does not write.

Data sources (all already exist per-bot):
  - tile_metrics.compute_tile_data output (activity, cost, health chips)
  - signals/firing/*.json (signals store — firing signals per bot)
  - applications/{bot}/*.json (manifest crons + objective for deadline hints)

Called by the /api/pod/rollup endpoint in server.py.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Spend rollup
# ─────────────────────────────────────────────────────────────────────────────


def compute_spend_rollup(
    shared_dir: Path,
    bots: dict[str, dict],
    today: date | None = None,
) -> dict[str, Any]:
    """Return a per-bot spend breakdown with pod total and projected month-end.

    Uses the tile.cost fields already computed by tile_metrics (if available)
    or re-reads daily metrics directly as a fallback. Always reads from the
    same metrics files as compute_tile_data — no additional data sources.

    Shape returned::

        {
            "by_bot": {
                "team_bot_a": {"usd_7d": 1.23, "usd_28d": 4.20, "pct_of_pod": 0.32},
                ...
            },
            "pod_total_7d": 3.84,
            "pod_total_28d": 14.17,
            "projected_month_end": 18.50,    # linear extrapolation from 28d
            "bots_with_spend_7d": 5,
            "currency": "USD",
        }

    Why 28d (not 30d): bots have strong weekly cycles, so a 30-day
    window biases the average depending on whether it caught 5 vs 4
    Saturdays. 28d (4 weeks) gives every weekday the same weight in
    the rollup and makes the prior-window comparison trustworthy.
    """
    today = today or date.today()
    shared_dir = Path(shared_dir)

    by_bot: dict[str, dict] = {}
    pod_7d = 0.0
    pod_28d = 0.0

    for bot_id, bot_data in bots.items():
        tile = bot_data.get("tile")
        if tile and tile.get("cost"):
            c = tile["cost"]
            usd_7d = float(c.get("usd_7d") or 0)
            usd_28d = float(c.get("usd_28d") or 0)
        else:
            # fallback: read metrics files directly
            usd_7d = _sum_cost_from_metrics(shared_dir, bot_id, 7, today)
            usd_28d = _sum_cost_from_metrics(shared_dir, bot_id, 28, today)

        by_bot[bot_id] = {
            "usd_7d": round(usd_7d, 4),
            "usd_28d": round(usd_28d, 4),
            "pct_of_pod": None,  # filled below once we have the total
        }
        pod_7d += usd_7d
        pod_28d += usd_28d

    # Back-fill percentage column now that we have pod total.
    for bot_id in by_bot:
        if pod_28d > 0:
            by_bot[bot_id]["pct_of_pod"] = round(
                by_bot[bot_id]["usd_28d"] / pod_28d, 4
            )

    # Projected month-end: scale daily rate from the 28d window to a full
    # calendar month. Falls back to None very early in the month (< 3d
    # elapsed), when the daily rate is too noisy to extrapolate from.
    days_elapsed = today.day  # 1 on the 1st, 31 on the 31st
    import calendar
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    if days_elapsed >= 3 and pod_28d > 0:
        # Approximation: assume spend is uniform across the 28d window.
        daily_rate = pod_28d / 28.0
        projected = daily_rate * days_in_month
    else:
        projected = None

    bots_with_spend = sum(
        1 for v in by_bot.values() if v["usd_7d"] > 0
    )

    return {
        "by_bot": by_bot,
        "pod_total_7d": round(pod_7d, 4),
        "pod_total_28d": round(pod_28d, 4),
        "projected_month_end": round(projected, 2) if projected is not None else None,
        "bots_with_spend_7d": bots_with_spend,
        "currency": "USD",
    }


def _sum_cost_from_metrics(
    shared_dir: Path, bot_id: str, days: int, today: date
) -> float:
    """Sum total_cost_estimated from daily metrics files for the last N days."""
    total = 0.0
    metrics_dir = shared_dir / "metrics"
    for i in range(days):
        d = today - timedelta(days=i)
        mf = metrics_dir / d.isoformat() / f"{bot_id}.json"
        if not mf.exists():
            continue
        try:
            m = json.loads(mf.read_text())
            total += float(m.get("total_cost_estimated") or 0)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return total


# ─────────────────────────────────────────────────────────────────────────────
# Live spend rollup (raw turn JSONL — same source as the Usage Summary card)
# ─────────────────────────────────────────────────────────────────────────────
#
# ``compute_spend_rollup`` (above) is the legacy aggregate-based path: it
# reads end-of-day ``metrics/{date}/{bot}.json`` rollups. Those files are
# written the morning AFTER the day they cover, so they silently report
# zero for "today" all day and missed the 2026-05-20 4× spike entirely
# (see ``internal/incident-cost-alerting-blackout-2026-05-20.md``).
#
# ``compute_spend_rollup_live`` reads the raw turn JSONL the burst
# detector and Usage Summary card already read, so intraday "today" and
# per-day arrays for the trailing 28 days are available to evo and any
# other caller that needs to spot a same-day spike.


def _load_live_daily_costs(
    shared_dir: Path,
    bot_id: str,
    days: int,
    today: date,
) -> dict:
    """Read ``{shared_dir}/{bot}/turns/turns-<date>.jsonl`` for the last ``days``.

    Returns::

        {
            "ok": bool,                     # turn dir is readable
            "per_date": {
                "YYYY-MM-DD": {"usd": float, "turns": int, "session_ids": set[str]},
                ...
            },
        }

    Only the new-style shared turn dir is consulted (world-readable to
    the evolve service user); the older bot-workspace fallback paths
    used by ``usage_analytics._find_turns_dirs`` are not — pod_state.usage
    runs from the admin server which already has ACL read on the shared
    dir, and the workspace-memory path needs a sudo grant we don't want
    to fan out from a tool handler.
    """
    out: dict = {"ok": False, "per_date": {}}
    turns_dir = Path(shared_dir) / bot_id / "turns"
    if not turns_dir.is_dir():
        return out

    try:
        from usage_analytics import _estimate_turn_cost  # type: ignore[import]
    except Exception:
        def _estimate_turn_cost(_t: dict) -> float:  # type: ignore[misc]
            return 0.0

    per_date: dict[str, dict] = {}
    for i in range(days):
        d = today - timedelta(days=i)
        iso = d.isoformat()
        path = turns_dir / f"turns-{iso}.jsonl"
        if not path.exists():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            ts = t.get("ts") or ""
            if not isinstance(ts, str) or len(ts) < 10:
                continue
            d_str = ts[:10]
            recorded = float(t.get("cost") or 0)
            cost = recorded if recorded else _estimate_turn_cost(t)
            slot = per_date.setdefault(
                d_str, {"usd": 0.0, "turns": 0, "session_ids": set()}
            )
            slot["usd"] += cost
            slot["turns"] += 1
            sid = t.get("session_id") or ""
            if sid:
                slot["session_ids"].add(sid)

    out["per_date"] = per_date
    out["ok"] = True  # turn dir exists → we observed this bot, even if 0 turns
    return out


def compute_spend_rollup_live(
    shared_dir: Path,
    bots: dict[str, dict],
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Per-bot + pod-wide spend with intraday + per-day arrays.

    Reads the same raw turn JSONL the Usage Summary card and burst
    detector read, so values match what the operator sees in the UI.
    Distinct from :func:`compute_spend_rollup`, which preserves the
    legacy tile-data path used by /api/pod/rollup.

    Shape returned::

        {
            "as_of": "2026-05-22T14:30:00",  # current wall clock
            "today": "2026-05-22",            # today's ISO date
            "source": "live_jsonl",          # or "lagged_metrics" on fallback
            "by_bot": {
                "team_bot_a": {
                    "usd_today": 1.23,        # partial day, intraday
                    "usd_yesterday": 3.45,    # full prior calendar day
                    "usd_7d": 12.34,          # last 7 days incl today
                    "usd_28d": 45.67,
                    "pct_of_pod": 0.32,
                    "turns_today": 12,
                    "turns_yesterday": 45,
                    "daily_7d": [             # oldest→newest, 7 entries
                        {"date": ISO, "usd": float, "turns": int},
                        ...
                    ],
                    "daily_28d": [...],       # same shape, 28 entries
                },
                ...
            },
            "pod_total_today": 5.00,
            "pod_total_yesterday": 12.00,
            "pod_total_7d": 60.0,
            "pod_total_28d": 200.0,
            "pod_daily_7d": [...],
            "pod_daily_28d": [...],
            "projected_month_end": 220.0,
            "bots_with_spend_7d": 5,
            "currency": "USD",
        }

    Falls back to :func:`compute_spend_rollup` when **no** bot has a
    readable turn dir under ``{shared_dir}/<bot>/turns``. In that case
    ``source`` is ``"lagged_metrics"``, intraday fields are zero
    (no live data available) and per-day arrays are empty.
    """
    today = today or date.today()
    now = now or datetime.now()
    shared_dir = Path(shared_dir)

    # Per-bot live read; track whether ANY bot is observable live so we
    # can decide between live and lagged modes.
    live_per_bot: dict[str, dict] = {}
    any_live = False
    for bot_id in bots:
        live = _load_live_daily_costs(shared_dir, bot_id, days=28, today=today)
        if live["ok"]:
            any_live = True
        live_per_bot[bot_id] = live["per_date"]

    if not any_live:
        # No bot has a readable turn dir — fall back to the aggregate.
        legacy = compute_spend_rollup(shared_dir, bots, today=today)
        legacy["as_of"] = now.isoformat(timespec="seconds")
        legacy["today"] = today.isoformat()
        legacy["source"] = "lagged_metrics"
        legacy["pod_total_today"] = 0.0
        legacy["pod_total_yesterday"] = 0.0
        legacy["pod_daily_7d"] = []
        legacy["pod_daily_28d"] = []
        for _bid, row in legacy["by_bot"].items():
            row.setdefault("usd_today", 0.0)
            row.setdefault("usd_yesterday", 0.0)
            row.setdefault("turns_today", 0)
            row.setdefault("turns_yesterday", 0)
            row.setdefault("daily_7d", [])
            row.setdefault("daily_28d", [])
        return legacy

    # 28-day window, oldest → newest, ending at today (inclusive).
    dates_28 = [today - timedelta(days=i) for i in range(27, -1, -1)]
    pod_daily: dict[str, dict] = {
        d.isoformat(): {"usd": 0.0, "turns": 0} for d in dates_28
    }

    by_bot: dict[str, dict] = {}
    pod_today_usd = 0.0
    pod_yesterday_usd = 0.0
    pod_7d_sum = 0.0
    pod_28d_sum = 0.0

    for bot_id in bots:
        per_date = live_per_bot.get(bot_id) or {}
        daily_28 = []
        for d in dates_28:
            iso = d.isoformat()
            slot = per_date.get(iso) or {"usd": 0.0, "turns": 0}
            usd = float(slot.get("usd", 0.0))
            turns = int(slot.get("turns", 0))
            daily_28.append({"date": iso, "usd": round(usd, 4), "turns": turns})
            pod_daily[iso]["usd"] += usd
            pod_daily[iso]["turns"] += turns

        daily_7 = daily_28[-7:]
        usd_today = daily_28[-1]["usd"]
        usd_yesterday = daily_28[-2]["usd"]
        turns_today = daily_28[-1]["turns"]
        turns_yesterday = daily_28[-2]["turns"]
        usd_7d = sum(e["usd"] for e in daily_7)
        usd_28d = sum(e["usd"] for e in daily_28)

        by_bot[bot_id] = {
            "usd_today": round(usd_today, 4),
            "usd_yesterday": round(usd_yesterday, 4),
            "usd_7d": round(usd_7d, 4),
            "usd_28d": round(usd_28d, 4),
            "pct_of_pod": None,
            "turns_today": turns_today,
            "turns_yesterday": turns_yesterday,
            "daily_7d": daily_7,
            "daily_28d": daily_28,
        }
        pod_today_usd += usd_today
        pod_yesterday_usd += usd_yesterday
        pod_7d_sum += usd_7d
        pod_28d_sum += usd_28d

    # Back-fill pct_of_pod against the 28d denominator (matches the
    # legacy rollup so callers comparing the two see consistent shares).
    for bot_id in by_bot:
        if pod_28d_sum > 0:
            by_bot[bot_id]["pct_of_pod"] = round(
                by_bot[bot_id]["usd_28d"] / pod_28d_sum, 4
            )

    pod_daily_28 = [
        {
            "date": d.isoformat(),
            "usd": round(pod_daily[d.isoformat()]["usd"], 4),
            "turns": int(pod_daily[d.isoformat()]["turns"]),
        }
        for d in dates_28
    ]
    pod_daily_7 = pod_daily_28[-7:]

    # Projected month-end: linear extrapolation from the 28d daily rate.
    # Same logic as compute_spend_rollup; null in the first 2 days of a
    # month when the rate is too noisy to extrapolate from.
    import calendar
    days_elapsed = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    if days_elapsed >= 3 and pod_28d_sum > 0:
        daily_rate = pod_28d_sum / 28.0
        projected: float | None = daily_rate * days_in_month
    else:
        projected = None

    bots_with_spend_7d = sum(1 for v in by_bot.values() if v["usd_7d"] > 0)

    return {
        "as_of": now.isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "source": "live_jsonl",
        "by_bot": by_bot,
        "pod_total_today": round(pod_today_usd, 4),
        "pod_total_yesterday": round(pod_yesterday_usd, 4),
        "pod_total_7d": round(pod_7d_sum, 4),
        "pod_total_28d": round(pod_28d_sum, 4),
        "pod_daily_7d": pod_daily_7,
        "pod_daily_28d": pod_daily_28,
        "projected_month_end": (
            round(projected, 2) if projected is not None else None
        ),
        "bots_with_spend_7d": bots_with_spend_7d,
        "currency": "USD",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Bots needing attention
# ─────────────────────────────────────────────────────────────────────────────


def compute_attention_summary(
    shared_dir: Path,
    bots: dict[str, dict],
) -> dict[str, Any]:
    """Return bots that have firing signals or critical/warning health chips.

    Shape returned::

        {
            "needs_attention": [
                {
                    "bot_id": "team_bot_a",
                    "firing_signals": 3,
                    "critical_chips": 1,
                    "warn_chips": 2,
                    "reasons": ["gateway down", "stale heartbeat"],
                },
                ...
            ],
            "all_clear": bool,        # True when needs_attention is empty
            "total_firing_signals": 7,
        }
    """
    shared_dir = Path(shared_dir)
    # Count firing signals per bot from the signals store.
    firing_by_bot = _count_firing_signals(shared_dir)

    needs_attention = []
    total_firing = sum(firing_by_bot.values())

    for bot_id, bot_data in bots.items():

        firing = firing_by_bot.get(bot_id, 0)
        tile = bot_data.get("tile") or {}
        chips = tile.get("health_chips") or []
        critical_chips = [c for c in chips if c.get("severity") == "critical"]
        warn_chips = [c for c in chips if c.get("severity") == "warn"]

        if firing > 0 or critical_chips or warn_chips:
            reasons = [c.get("label", "") for c in (critical_chips + warn_chips)]
            needs_attention.append({
                "bot_id": bot_id,
                "firing_signals": firing,
                "critical_chips": len(critical_chips),
                "warn_chips": len(warn_chips),
                "reasons": reasons[:5],  # cap at 5 to keep payload small
            })

    # Sort: critical first (bots with any critical chip), then by firing count.
    needs_attention.sort(
        key=lambda x: (-(x["critical_chips"] > 0), -x["firing_signals"], -x["warn_chips"])
    )

    return {
        "needs_attention": needs_attention,
        "all_clear": len(needs_attention) == 0,
        "total_firing_signals": total_firing,
    }


def _count_firing_signals(shared_dir: Path) -> dict[str, int]:
    """Count firing signals per bot_id from the signals store.

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): goes through ``signals.store.iter_active`` rather than
    globbing ``{shared_dir}/signals/firing/`` directly. Each signal may
    have a bot_id field; signals without one (pod/host scope) are not
    attributed to a specific bot for the per-tile attention widget.
    """
    counts: dict[str, int] = {}
    try:
        from signals import store as _signals_store  # type: ignore[import-not-found]
    except Exception:
        return counts
    try:
        for sig in _signals_store.iter_active(shared_dir, state="firing"):
            data = sig.to_dict()
            bot_id = data.get("bot_id") or data.get("scope_id") or None
            if bot_id:
                counts[bot_id] = counts.get(bot_id, 0) + 1
    except Exception:
        return counts
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Activity heat
# ─────────────────────────────────────────────────────────────────────────────


def compute_activity_heat(
    shared_dir: Path,
    bots: dict[str, dict],
) -> dict[str, Any]:
    """Return bots ranked by activity (turns + sessions) in the last 7 days.

    Shape returned::

        {
            "ranked": [
                {
                    "bot_id": "admin_bot",
                    "turns_7d": 142,
                    "sessions_7d": 28,
                    "human_pct": 0.62,   # fraction of turns from user
                    "status": "online",  # online | active | offline
                },
                ...
            ],
            "pod_turns_7d": 350,
            "pod_sessions_7d": 68,
            "busiest_bot": "admin_bot",   # None when no activity
        }
    """
    ranked = []
    pod_turns = 0
    pod_sessions = 0

    for bot_id, bot_data in bots.items():
        tile = bot_data.get("tile") or {}
        a = tile.get("activity") or {}
        turns_7d = int(a.get("turns_7d") or 0)
        sess_7d = int(a.get("sessions_7d") or 0)
        human_pct = a.get("human_pct_7d")

        status = "offline"
        if bot_data.get("live"):
            status = "online"
        elif bot_data.get("last_metric_date"):
            status = "active"

        ranked.append({
            "bot_id": bot_id,
            "turns_7d": turns_7d,
            "sessions_7d": sess_7d,
            "human_pct": human_pct,
            "status": status,
        })
        pod_turns += turns_7d
        pod_sessions += sess_7d

    # Sort: most turns first, then sessions as tie-breaker.
    ranked.sort(key=lambda x: (-x["turns_7d"], -x["sessions_7d"]))

    busiest = ranked[0]["bot_id"] if ranked and ranked[0]["turns_7d"] > 0 else None

    return {
        "ranked": ranked,
        "pod_turns_7d": pod_turns,
        "pod_sessions_7d": pod_sessions,
        "busiest_bot": busiest,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Upcoming deadlines
# ─────────────────────────────────────────────────────────────────────────────


def compute_upcoming_deadlines(
    shared_dir: Path,
    bots: dict[str, dict],
    today: date | None = None,
    horizon_days: int = 14,
) -> dict[str, Any]:
    """Scan application manifests across all bots for upcoming deadlines.

    Deadlines are extracted from manifest fields in priority order:
      1. ``crons`` field — scheduled apps get their next-run date estimated
         from the cron schedule for display purposes.
      2. ``objective`` / ``goals`` free-text — we surface the text so the
         UI can show it without parsing.

    This function surfaces *scheduled* applications as "upcoming" when they
    have cron entries, since those represent recurring commitments. Free-form
    deadline parsing from objective text is a nice-to-have in v1.1; for now
    we surface the app name + cron schedule so the operator can see what's
    scheduled.

    Shape returned::

        {
            "items": [
                {
                    "bot_id": "admin_bot",
                    "app_id": "morning_briefing",
                    "app_name": "Morning Briefing",
                    "schedule": "0 7 * * *",   # raw cron string or None
                    "next_run_label": "daily at 07:00",
                    "objective": "Deliver a morning summary",
                },
                ...
            ],
            "total": 12,   # all scheduled apps found
        }

    Note: True arbitrary deadlines (tasks with specific due dates stored in
    app data files) require per-app data reads that vary by app. This widget
    surfaces the structural metadata that all apps share (cron schedules)
    rather than diving into app-specific data files.
    """
    today = today or date.today()
    shared_dir = Path(shared_dir)
    items: list[dict] = []

    for bot_id, bot_data in bots.items():
        apps_dir = shared_dir / "applications" / bot_id
        if not apps_dir.exists():
            continue
        for manifest_file in sorted(apps_dir.glob("*.json")):
            if manifest_file.name.startswith("."):
                continue
            try:
                manifest = json.loads(manifest_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            app_id = manifest.get("id") or manifest_file.stem
            app_name = (
                manifest.get("display_name")
                or manifest.get("name")
                or app_id
            )
            objective = (
                manifest.get("objective")
                or manifest.get("goals")
                or manifest.get("purpose")
                or ""
            )
            crons = manifest.get("crons") or []

            if not crons:
                continue  # only surface apps with schedules

            # Extract the first cron schedule string for display.
            schedule = _first_cron_schedule(crons)
            next_run_label = _describe_cron(schedule) if schedule else None

            items.append({
                "bot_id": bot_id,
                "app_id": app_id,
                "app_name": app_name,
                "schedule": schedule,
                "next_run_label": next_run_label,
                "objective": str(objective)[:120] if objective else None,
            })

    return {
        "items": items,
        "total": len(items),
    }


def _first_cron_schedule(crons: list) -> str | None:
    """Extract the first usable cron schedule string from a manifest's crons list."""
    for entry in crons:
        if isinstance(entry, str):
            # Raw crontab line: "0 7 * * * /path/to/script"
            parts = entry.strip().split()
            if len(parts) >= 5:
                return " ".join(parts[:5])
            if entry.startswith("@"):
                return entry.split()[0]
        elif isinstance(entry, dict):
            schedule = (
                entry.get("schedule")
                or entry.get("cron")
                or entry.get("cron_string")
            )
            if schedule:
                # schedule may itself be "0 7 * * * /path"
                parts = str(schedule).strip().split()
                if parts[0].startswith("@"):
                    return parts[0]
                if len(parts) >= 5:
                    return " ".join(parts[:5])
                return str(schedule)
    return None


_CRON_KEYWORDS: dict[str, str] = {
    "@reboot": "on reboot",
    "@hourly": "every hour",
    "@daily": "daily",
    "@weekly": "weekly",
    "@monthly": "monthly",
    "@yearly": "yearly",
    "@annually": "yearly",
}


def _describe_cron(schedule: str) -> str:
    """Produce a human-readable description of a 5-field cron schedule.

    Covers the most common cases. Falls back to the raw string when the
    schedule is exotic.
    """
    if not schedule:
        return schedule
    if schedule.startswith("@"):
        return _CRON_KEYWORDS.get(schedule, schedule)
    parts = schedule.split()
    if len(parts) < 5:
        return schedule
    minute, hour, dom, month, dow = parts[:5]

    # Every-minute
    if all(p in ("*", "*/1") for p in parts[:5]):
        return "every minute"
    # Daily at specific time
    if dom == "*" and month == "*" and dow == "*":
        if minute.isdigit() and hour.isdigit():
            return f"daily at {int(hour):02d}:{int(minute):02d}"
        if minute == "0" and "/" in hour:
            return f"every {hour.split('/')[-1]} hours"
        if minute.isdigit() and hour == "*":
            return f"every hour at :{minute.zfill(2)}"
    # Weekly
    _DOW = {
        "0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
        "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday",
    }
    if dom == "*" and month == "*" and dow in _DOW:
        if minute.isdigit() and hour.isdigit():
            return f"weekly on {_DOW[dow]} at {int(hour):02d}:{int(minute):02d}"
    # Monthly
    if month == "*" and dow == "*" and dom.isdigit():
        if minute.isdigit() and hour.isdigit():
            return f"monthly on day {dom} at {int(hour):02d}:{int(minute):02d}"
    return schedule


# ─────────────────────────────────────────────────────────────────────────────
# Combined rollup
# ─────────────────────────────────────────────────────────────────────────────


def compute_pod_rollup(
    shared_dir: Path,
    bots: dict[str, dict],
    today: date | None = None,
) -> dict[str, Any]:
    """Compute all four pod-level rollup sections in one call.

    ``bots`` is the ``bots`` dict from /api/status (already contains
    tile data when the caller overlays it via tile_metrics). Each entry
    may have a ``tile`` key with activity/cost/chips.

    Shape::

        {
            "spend":     { ... compute_spend_rollup output ... },
            "attention": { ... compute_attention_summary output ... },
            "activity":  { ... compute_activity_heat output ... },
            "deadlines": { ... compute_upcoming_deadlines output ... },
        }
    """
    today = today or date.today()
    shared_dir = Path(shared_dir)

    return {
        "spend": compute_spend_rollup(shared_dir, bots, today=today),
        "attention": compute_attention_summary(shared_dir, bots),
        "activity": compute_activity_heat(shared_dir, bots),
        "deadlines": compute_upcoming_deadlines(shared_dir, bots, today=today),
    }
