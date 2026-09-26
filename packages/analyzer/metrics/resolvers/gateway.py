"""metrics.resolvers.gateway — gateway.up and gateway.consecutive_failures_24h.

``gateway.up``:
  Probe the bot's OpenClaw gateway at ``http://127.0.0.1:{port}/evolve/status``.
  Returns 1.0 on HTTP 200, 0.0 otherwise.

``gateway.consecutive_failures_24h``:
  Count incidents of type ``gateway_down`` for this bot in the last 24h.
  Reads from ``{shared_dir}/incidents/{YYYY-MM-DD}/{bot_id}-*.json``.

The port for each bot is read from the pod manifest at
``{shared_dir}/pod-manifest.json`` if present; otherwise falls back to
reading ``network.json``. Tests can monkeypatch ``_resolve_gateway_url``
to avoid real HTTP probes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metrics.registry import MetricSpec, MetricValue, register


_SHARED_DIR = Path("/Users/Shared/evolve")


def set_shared_dir(path: Path) -> None:
    """Test hook: override the shared directory these resolvers read from."""
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


def _pod_manifest_port(bot_id: str) -> int | None:
    pod_manifest = _SHARED_DIR / "pod-manifest.json"
    if not pod_manifest.exists():
        return None
    try:
        with pod_manifest.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    bots = data.get("bots") or {}
    bot_entry = bots.get(bot_id) or {}
    port = bot_entry.get("port")
    if isinstance(port, int):
        return port
    return None


def _network_port(bot_id: str) -> int | None:
    network = _SHARED_DIR / "network.json"
    if not network.exists():
        return None
    try:
        with network.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    bots = data.get("bots") or {}
    entry = bots.get(bot_id) or {}
    port = entry.get("port")
    if isinstance(port, int):
        return port
    return None


def _resolve_gateway_url(bot_id: str) -> str | None:
    port = _pod_manifest_port(bot_id) or _network_port(bot_id)
    if port is None:
        return None
    return f"http://127.0.0.1:{port}/evolve/status"


# ─────────────────────────────────────────────────────────────────────────────
# gateway.up
# ─────────────────────────────────────────────────────────────────────────────


def resolve_gateway_up(bot_id: str, as_of: datetime) -> MetricValue:  # noqa: ARG001
    url = _resolve_gateway_url(bot_id)
    if url is None:
        return MetricValue(
            value=0.0,
            confidence=0.3,
            source_note="no port configured for bot; cannot probe",
        )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310
            status = resp.status
            value = 1.0 if status == 200 else 0.0
            return MetricValue(
                value=value,
                confidence=1.0,
                source_note=f"HTTP {status} from {url}",
            )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return MetricValue(
            value=0.0,
            confidence=0.9,
            source_note=f"probe failed: {e}",
        )


register(
    MetricSpec(
        name="gateway.up",
        description="1.0 if the bot's OpenClaw gateway returns HTTP 200 at /evolve/status.",
        unit="bool",
        source="HTTP probe at http://127.0.0.1:{port}/evolve/status",
    ),
    resolve_gateway_up,
)


# ─────────────────────────────────────────────────────────────────────────────
# gateway.consecutive_failures_24h
# ─────────────────────────────────────────────────────────────────────────────


def _iter_incidents(bot_id: str, since: datetime, until: datetime) -> list[dict]:
    """Return gateway incidents for bot_id in the window [since, until]."""
    incidents: list[dict] = []
    if not (_SHARED_DIR / "incidents").exists():
        return incidents

    # Enumerate candidate date dirs between since and until (inclusive),
    # stepping one day at a time to avoid hour-level bugs.
    candidate_dates: set = set()
    cur = since.date()
    end_date = until.date()
    while cur <= end_date:
        candidate_dates.add(cur)
        cur = cur + timedelta(days=1)

    for d in candidate_dates:
        day_dir = _SHARED_DIR / "incidents" / d.isoformat()
        if not day_dir.exists():
            continue
        for path in day_dir.glob(f"{bot_id}-*.json"):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("bot_id") != bot_id and data.get("bot") != bot_id:
                continue
            # Best-effort timestamp filter
            ts_raw = data.get("timestamp") or data.get("at")
            if ts_raw:
                ts = _parse_iso(ts_raw)
                if ts is not None and ts < since:
                    continue
                if ts is not None and ts > until:
                    continue
            incidents.append(data)
    return incidents


def _parse_iso(raw: str) -> datetime | None:
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_gateway_failures_24h(bot_id: str, as_of: datetime) -> MetricValue:
    since = as_of - timedelta(hours=24)
    incidents = _iter_incidents(bot_id, since, as_of)
    count = sum(
        1
        for i in incidents
        if str(i.get("type", "")).startswith("gateway_")
        and str(i.get("type", "")) in ("gateway_down", "gateway_timeout")
    )
    return MetricValue(
        value=float(count),
        confidence=1.0,
        source_note=f"counted {count} gateway incidents in last 24h",
    )


register(
    MetricSpec(
        name="gateway.consecutive_failures_24h",
        description="Count of gateway_down/gateway_timeout incidents for this bot in the last 24h.",
        unit="count",
        source="{shared_dir}/incidents/{YYYY-MM-DD}/{bot_id}-*.json",
    ),
    resolve_gateway_failures_24h,
)


# ─────────────────────────────────────────────────────────────────────────────
# gateway.slow_incidents_24h
# ─────────────────────────────────────────────────────────────────────────────


def resolve_gateway_slow_incidents_24h(bot_id: str, as_of: datetime) -> MetricValue:
    """Count gateway_slow incidents for this bot in the last 24h.

    heal.py records a ``gateway_slow`` incident whenever a probe succeeds
    but takes longer than ``slowThresholdMs``. The diagnostician uses this
    as the verifiable claim for "raise slow threshold" proposals — after
    the operator applies the suggested threshold bump, this count should
    drop because slow-but-functional probes stop crossing the line.
    """
    since = as_of - timedelta(hours=24)
    incidents = _iter_incidents(bot_id, since, as_of)
    count = sum(1 for i in incidents if i.get("type") == "gateway_slow")
    return MetricValue(
        value=float(count),
        confidence=1.0,
        source_note=f"counted {count} gateway_slow incidents in last 24h",
    )


register(
    MetricSpec(
        name="gateway.slow_incidents_24h",
        description="Count of gateway_slow incidents for this bot in the last 24h.",
        unit="count",
        source="{shared_dir}/incidents/{YYYY-MM-DD}/{bot_id}-*.json",
    ),
    resolve_gateway_slow_incidents_24h,
)
