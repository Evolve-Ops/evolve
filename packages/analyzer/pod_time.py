"""pod_time — pod-local timezone helpers for daily-cap rollover.

Caps and dedup keys roll at midnight in the pod's local timezone, NOT
UTC. A pod-admin on Pacific time reading "Daily warn $5" expects the
cap to reset when their wall clock crosses midnight — UTC midnight
arrives at 4–5pm local, which is wrong.

Resolution order for the pod's TZ:
  1. ``network.json::pod.timezone`` if explicitly set (e.g. for multi-
     region deployments where the pod operator overrides the mini's
     system TZ).
  2. The system local TZ — which on a single-mini install is exactly
     what the operator wants.

Usage:
    from pod_time import pod_today_str, pod_now
    dedup_key = f"spend_alert/threshold/{bot}/{pod_today_str()}"

This module is intentionally small and dependency-light: it's imported
on the hot path of every cost-watchdog tick and every spend-alert
dispatch. The network.json read is cheap (the file is tiny) and not
cached — if the operator changes pod TZ mid-day they shouldn't have to
restart any daemon to see the new rollover.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_NETWORK_PATH = Path("/Users/Shared/evolve/network.json")


def _resolve_pod_tz(network_path: Path | None = None) -> ZoneInfo | None:
    """Return the configured pod TZ, or None to fall back to system local."""
    path = network_path or _DEFAULT_NETWORK_PATH
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tz_name = (data.get("pod") or {}).get("timezone")
    if not isinstance(tz_name, str) or not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return None


def pod_tz(network_path: Path | None = None) -> tzinfo:
    """The pod's timezone: the configured override, else the system local TZ.

    Use this when you need to place a UTC instant on the pod's calendar —
    notably when reading turn JSONL, whose FILENAMES and ``ts`` values are
    UTC (``TurnObserver`` writes ``new Date().toISOString()``) while the
    day an operator means is this one. Converting the instant is the only
    correct way to cross that boundary; comparing a UTC ``ts[:10]`` prefix
    to a pod-local date is off by the UTC offset for part of every day.
    """
    tz = _resolve_pod_tz(network_path)
    if tz is not None:
        return tz
    local = datetime.now().astimezone().tzinfo
    # .astimezone() on a naive datetime always yields an aware one, so this
    # is never None in practice; the fallback keeps the return type honest.
    return local if local is not None else timezone.utc


def pod_now(network_path: Path | None = None) -> datetime:
    """Current time as an aware datetime in the pod's local TZ."""
    tz = _resolve_pod_tz(network_path)
    if tz is not None:
        return datetime.now(tz)
    # System local TZ via aware-conversion of naive local time.
    return datetime.now().astimezone()


def pod_today(network_path: Path | None = None) -> date:
    """Today's date in the pod's local TZ.

    Use this for cap-rollover boundaries (daily warn, L1 reset at
    midnight) and dedup keys keyed by date.
    """
    return pod_now(network_path).date()


def pod_today_str(network_path: Path | None = None) -> str:
    """Today's date as ``YYYY-MM-DD`` in the pod's local TZ."""
    return pod_today(network_path).isoformat()


def pod_local_day_iso(
    ts: object,
    tz: tzinfo | None = None,
    network_path: Path | None = None,
) -> str | None:
    """The pod-local date (``YYYY-MM-DD``) of a UTC turn timestamp.

    Turn JSONL is UTC on both axes — ``TurnObserver`` writes
    ``turns-${new Date().toISOString().slice(0, 10)}.jsonl`` and every ``ts``
    is a ``Z`` instant — while caps, thresholds and dedup keys roll at
    pod-local midnight. Crossing that boundary means converting the INSTANT;
    comparing a UTC ``ts[:10]`` prefix against a pod-local date is off by the
    pod's UTC offset for part of every day, and west of UTC it files the
    entire local evening under tomorrow.

    Returns ``None`` for a missing or unparseable ``ts`` rather than guessing:
    a malformed record must drop out of the day buckets, not silently land in
    one.
    """
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # A ts without an offset is UTC by the writer's contract.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz if tz is not None else pod_tz(network_path)).date().isoformat()


def pod_local_day_start_utc(
    day: date,
    tz: tzinfo | None = None,
    network_path: Path | None = None,
) -> datetime:
    """The UTC instant at which pod-local ``day`` begins (local midnight).

    Anything that measures "how far into today are we" against a pod-local
    day total has to anchor here. Anchoring on UTC midnight instead divides
    a pod-local day's spend by the UTC hours elapsed, which west of UTC
    understates elapsed time by the offset and over-projects by up to ~5x
    late in the local evening.
    """
    zone = tz if tz is not None else pod_tz(network_path)
    return datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(timezone.utc)


def pod_iso_week(network_path: Path | None = None) -> str:
    """Current ISO week as ``YYYY-WNN`` in the pod's local TZ.

    Used for weekly-warn dedup keys so the alert fires once per local
    ISO week regardless of when the watchdog tick happens to land.
    """
    return pod_today(network_path).strftime("%G-W%V")
