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
from datetime import date, datetime, timezone, tzinfo
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


def pod_iso_week(network_path: Path | None = None) -> str:
    """Current ISO week as ``YYYY-WNN`` in the pod's local TZ.

    Used for weekly-warn dedup keys so the alert fires once per local
    ISO week regardless of when the watchdog tick happens to land.
    """
    return pod_today(network_path).strftime("%G-W%V")
