"""Host machine health metrics for the admin UI.

Reports CPU, memory, disk, load average, and uptime for the machine the
admin server runs on (the pod's dedicated Mac). Powered by `psutil`.

Phase 5 of docs/spec-alerts-signal-store-2026-05-07.md adds a
:func:`emit_signals_from_snapshot` helper that mirrors crit-tier
metrics into the Signal store. Warn-tier metrics intentionally don't
fire signals — the bar is "broken or actually-needs-action" — but a
crit value (cpu ≥90%, mem ≥90%, disk ≥95%, load ≥2.0/cpu) materializes
as a maintenance-flavor host-scoped Signal.

DELIBERATE EXCEPTION — disk fires at WARN (≥85%), not only crit
(docs/spec-delta-disk-reclaim-2026-06-21.md). Unlike a CPU/mem/load
spike, which is transient and only worth an alert once it's actually
broken, a slowly-filling disk has *lead time*: the warning is only
useful BEFORE the disk is full. So the disk_low Signal fires at warn
(severity "warn") and escalates to alert at crit, while cpu/mem/load
stay crit-only. The disk Signal also carries a reclaimable-space
breakdown (see disk_reclaim.scan_reclaimable) and a fill projection so
the operator can act on the warning instead of just watching the gauge.

`psutil` is declared in pyproject.toml, so a clean `pip install -e packages/admin`
provides it. To stay safe on stale venvs that haven't been reinstalled since
this module landed, the import is soft: when psutil is missing,
`collect_host_health()` returns `{"ok": True, "available": False, ...}` and
the endpoint stays a healthy 200 — the dashboard hides the strip, and the
pod-health scanner surfaces the missing package with a one-click install fix.

Status thresholds (per metric):
    cpu_percent   ≥ 90 → crit, ≥ 75 → warn, else ok
    mem_percent   ≥ 90 → crit, ≥ 80 → warn, else ok
    disk_percent  ≥ 95 → crit, ≥ 85 → warn, else ok
    load1_per_cpu ≥ 2.0 → crit, ≥ 1.0 → warn, else ok

Overall `status` is the worst of the four, so the UI can render a single
green/yellow/red dot.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import disk_reclaim

_log = logging.getLogger(__name__)

try:
    import psutil  # type: ignore[import-not-found]
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False


if _PSUTIL_AVAILABLE:
    _BOOT_TIME = psutil.boot_time()
    _CPU_COUNT = psutil.cpu_count(logical=True) or 1
    # Prime the cpu_percent counter so the first real call is meaningful.
    psutil.cpu_percent(interval=None)
else:
    _BOOT_TIME = 0.0
    _CPU_COUNT = 1


def is_available() -> bool:
    return _PSUTIL_AVAILABLE


def _classify(value: float, warn: float, crit: float) -> str:
    if value >= crit:
        return "crit"
    if value >= warn:
        return "warn"
    return "ok"


def _worst(*statuses: str) -> str:
    order = {"ok": 0, "warn": 1, "crit": 2}
    return max(statuses, key=lambda s: order.get(s, 0))


# ── Sleep-gap detection (Phase 8.2 — dedicated-Apple support) ─────────────────
#
# A dedicated host must never sleep: launchd StartInterval jobs don't fire
# during sleep and KeepAlive gateways are suspended, so a sleep gap means the
# whole pod was silently dark. The kernel records the last sleep/wake pair in
# kern.sleeptime / kern.waketime (zero when the host hasn't slept since boot),
# which gives gap detection with no state file and no cadence assumptions.

_SLEEP_MIN_SECONDS = 120          # ignore sub-2-minute blips (e.g. update reboots' dark wake)
_SLEEP_RECENT_WINDOW = 24 * 3600  # report a sleep for a day, then auto-resolve

_sleep_cache: tuple[float, dict[str, Any]] | None = None
_SLEEP_CACHE_TTL = 30.0  # /api/host-health is dashboard-polled; don't fork sysctl per poll


def _read_kern_timeval(name: str) -> float:
    """Epoch seconds from a `sysctl -n kern.sleeptime`-style timeval
    ('{ sec = 1781041273, usec = 128099 } ...'). 0.0 when unavailable."""
    try:
        out = subprocess.run(
            ["/usr/sbin/sysctl", "-n", name],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return 0.0
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return float(m.group(1)) if m else 0.0


def sleep_posture(now: float | None = None) -> dict[str, Any]:
    """Last sleep/wake info from the kernel.

    Returns ``{last_sleep_at, last_wake_at, last_sleep_seconds,
    slept_recently, status}`` where status is "alert" when the host slept
    for ≥2 minutes within the last 24 hours, else "ok". Sleep evidence
    resets on reboot (the kernel counters zero) — that's fine; a reboot
    is a different, visible event, while sleep is the silent one.
    """
    global _sleep_cache
    now = now if now is not None else time.time()
    if _sleep_cache is not None and (now - _sleep_cache[0]) < _SLEEP_CACHE_TTL:
        return _sleep_cache[1]

    sleep_t = _read_kern_timeval("kern.sleeptime")
    wake_t = _read_kern_timeval("kern.waketime")
    duration = max(0.0, wake_t - sleep_t) if (sleep_t > 0 and wake_t > sleep_t) else 0.0
    slept_recently = (
        duration >= _SLEEP_MIN_SECONDS
        and (now - wake_t) <= _SLEEP_RECENT_WINDOW
    )
    posture = {
        "last_sleep_at": sleep_t or None,
        "last_wake_at": wake_t or None,
        "last_sleep_seconds": int(duration),
        "slept_recently": slept_recently,
        "status": "alert" if slept_recently else "ok",
    }
    _sleep_cache = (now, posture)
    return posture


def collect_host_health() -> dict[str, Any]:
    """Snapshot of host health. Safe to call from a Flask handler.

    When psutil is unavailable, returns a degraded payload with
    ``available: False`` so callers can render an "unavailable" state
    without a 5xx response.
    """
    if not _PSUTIL_AVAILABLE:
        return {
            "ok": True,
            "available": False,
            "reason": "psutil not installed in admin venv — "
                      "run pod-health scan and click Fix, or "
                      "`/Users/Shared/evolve-venv/bin/pip install psutil`",
            "hostname": socket.gethostname(),
            "captured_at": time.time(),
        }

    cpu_percent = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    # Check the Data volume (/Users) rather than the small read-only System
    # volume (/). On macOS APFS these are separate volumes in the same
    # container; the System volume is ~15-20 GB and rarely fills up, while
    # the Data volume is where all user files, transcripts, and logs live.
    # Checking "/" produces a misleadingly low reading (~34%) while /Users
    # reflects what actually matters for pod health.
    try:
        disk = psutil.disk_usage("/Users")
        _disk_mount = "/Users"
    except Exception:
        disk = psutil.disk_usage("/")
        _disk_mount = "/"
    load1, load5, load15 = psutil.getloadavg()
    uptime_seconds = int(time.time() - _BOOT_TIME)
    load1_per_cpu = load1 / _CPU_COUNT

    cpu_status = _classify(cpu_percent, warn=75, crit=90)
    mem_status = _classify(mem.percent, warn=80, crit=90)
    disk_status = _classify(disk.percent, warn=85, crit=95)
    load_status = _classify(load1_per_cpu, warn=1.0, crit=2.0)

    return {
        "ok": True,
        "available": True,
        "hostname": socket.gethostname(),
        "cpu_count": _CPU_COUNT,
        "cpu_percent": round(cpu_percent, 1),
        "cpu_status": cpu_status,
        "memory": {
            "total_bytes": mem.total,
            "used_bytes": mem.total - mem.available,
            "available_bytes": mem.available,
            "percent": round(mem.percent, 1),
            "status": mem_status,
        },
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "percent": round(disk.percent, 1),
            "status": disk_status,
            "mount": _disk_mount,
        },
        "load_avg": {
            "load1": round(load1, 2),
            "load5": round(load5, 2),
            "load15": round(load15, 2),
            "per_cpu_1m": round(load1_per_cpu, 2),
            "status": load_status,
        },
        "uptime_seconds": uptime_seconds,
        "boot_time": _BOOT_TIME,
        # Sleep gaps don't feed the overall dot — it reflects *current*
        # resource pressure — but emit_signals_from_snapshot fires a
        # host_slept Signal off this section.
        "sleep": sleep_posture(),
        "status": _worst(cpu_status, mem_status, disk_status, load_status),
        "captured_at": time.time(),
    }


# ── Signal mirror (Phase 5 of the alerts/signal-store consolidation) ──────────

# Per-metric label, status-key, value-key, unit, and a ``warn_fires`` flag.
# Warn-tier values don't fire for cpu/mem/load — the bar there is "broken or
# actually-needs-action", and a warn spike is transient. DISK is the
# deliberate exception (warn_fires=True): disk-fill has lead time, so the
# disk_low Signal fires at warn (severity "warn") and escalates to alert at
# crit. See the module docstring + spec-delta-disk-reclaim-2026-06-21.md.
# Bumping warn→crit on a re-observe escalates the existing Signal's severity.
_HOST_METRICS = (
    ("cpu_high", "CPU saturation", "cpu_status", "cpu_percent", "%", False),
    ("memory_high", "Memory pressure", ("memory", "status"), ("memory", "percent"), "%", False),
    ("disk_low", "Disk nearly full", ("disk", "status"), ("disk", "percent"), "%", True),
    ("load_high", "Load average high", ("load_avg", "status"), ("load_avg", "per_cpu_1m"), "/cpu", False),
)

# Disk-fill rolling history (for the fill projection on the disk_low Signal).
_DISK_HISTORY_MAX_SAMPLES = 50
_PROJECTION_MIN_SAMPLES = 2
_PROJECTION_MIN_SPAN_SECONDS = 18 * 3600  # "about a day", with tolerance
# /api/host-health is dashboard-polled (potentially seconds apart). Without a
# floor between samples, 50 samples would cover minutes and the day-spanning
# projection would never fire — so the emit path throttles to ~1 sample/hour,
# giving the 50-sample window ~2 days of reach.
_DISK_SAMPLE_MIN_INTERVAL = 3600.0


def _resolve(snapshot: dict[str, Any], path):
    if isinstance(path, tuple):
        cur: Any = snapshot
        for key in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        return cur
    return snapshot.get(path)


# ── Disk-fill history + projection (best-effort, never raises) ────────────────


def _disk_history_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "host_health" / "disk_history.jsonl"


def _read_disk_history(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text()
    except (FileNotFoundError, PermissionError, OSError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict) and {"ts", "used_bytes", "total_bytes"} <= rec.keys():
            out.append(rec)
    return out


def _append_disk_sample(
    shared_dir: Path,
    ts: float,
    used_bytes: int,
    total_bytes: int,
    *,
    min_interval: float = 0.0,
) -> list[dict[str, Any]]:
    """Append a disk sample to the rolling jsonl and return the capped history.

    Whole-file rewrite (read → append → trim → atomic temp+rename) so the
    sample cap is enforced and readers never see a torn file. Evolve owns
    ``{shared_dir}/host_health/`` so no sudo. Best-effort: any I/O failure
    returns the in-memory history without raising.

    ``min_interval`` throttles cadence: when the most recent sample is newer
    than ``min_interval`` seconds the call is a no-op (returns the existing
    history). Default 0 always appends — the emit path passes the hourly
    floor; direct callers (e.g. tests) append every time.
    """
    path = _disk_history_path(shared_dir)
    history = _read_disk_history(path)
    if min_interval and history:
        try:
            too_recent = (float(ts) - float(history[-1]["ts"])) < min_interval
        except (KeyError, TypeError, ValueError):
            too_recent = False  # malformed last sample → don't block the append
        if too_recent:
            return history
    history.append(
        {"ts": float(ts), "used_bytes": int(used_bytes), "total_bytes": int(total_bytes)}
    )
    history = history[-_DISK_HISTORY_MAX_SAMPLES:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(s) for s in history) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception as e:
        _log.debug("disk_history append failed: %s", e)
    return history


def _fill_projection(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """%/week + ETA-to-full from the rolling disk history.

    Returns None when there isn't enough signal: fewer than 2 samples, a span
    under ~a day, an unknown volume size, or a flat/draining trend. Never
    fabricates — a disk that isn't filling yields no "time to full".
    """
    if len(history) < _PROJECTION_MIN_SAMPLES:
        return None
    first, last = history[0], history[-1]
    try:
        span = float(last["ts"]) - float(first["ts"])
        total = float(last.get("total_bytes") or 0)
        used = float(last["used_bytes"])
        delta = used - float(first["used_bytes"])
    except (KeyError, TypeError, ValueError):
        return None
    if span < _PROJECTION_MIN_SPAN_SECONDS or total <= 0:
        return None
    rate_per_sec = delta / span
    if rate_per_sec <= 0:
        return None  # stable or draining — no honest "time to full"
    pct_per_week = (rate_per_sec * 7 * 86400) / total * 100.0
    free = max(0.0, total - used)
    proj: dict[str, Any] = {
        "pct_per_week": round(pct_per_week, 1),
        "samples": len(history),
        "span_seconds": int(span),
    }
    eta_seconds = free / rate_per_sec
    proj["eta_days"] = round(eta_seconds / 86400.0, 1)
    return proj


def _projection_phrase(proj: dict[str, Any]) -> str:
    parts = [f"+{proj['pct_per_week']}%/week"]
    eta_days = proj.get("eta_days")
    if eta_days is not None:
        if eta_days >= 14:
            parts.append(f"~{round(eta_days / 7)} weeks to full")
        elif eta_days >= 1:
            parts.append(f"~{round(eta_days)} days to full")
        else:
            parts.append("<1 day to full")
    return ", ".join(parts)


# Short body labels (the category labels in disk_reclaim are fuller).
_RECLAIM_BODY_LABELS = {
    "npm_cache": "npm cache",
    "evolve_logs_oversized": "oversized logs",
    "oc_rotated_logs": "rotated logs",
}


def _disk_signal_enrichment(
    snapshot: dict[str, Any],
    hostname: str,
    value: Any,
    history: list[dict[str, Any]],
    reclaim_roots,
) -> tuple[str, dict[str, Any]]:
    """Build the (body, extra_details) for the disk_low Signal.

    Best-effort: a scan or projection failure degrades to the plain
    percentage body and an empty extra-details dict — it never raises into
    the emit loop. The reclaimable scan is read-only and needs no sudo.
    """
    base = f"Disk {value}% on {hostname}"
    extra: dict[str, Any] = {}
    segments: list[str] = []

    # Fill projection (omitted cleanly when history is insufficient).
    proj = None
    try:
        proj = _fill_projection(history)
    except Exception:
        proj = None
    if proj:
        extra["fill_projection"] = proj
        segments.append(_projection_phrase(proj))

    # Reclaimable-space breakdown (only when we know the volume size — a
    # degraded snapshot with no total_bytes shouldn't trigger a fs walk).
    disk = snapshot.get("disk") or {}
    if disk.get("total_bytes"):
        try:
            if reclaim_roots:
                scan = disk_reclaim.scan_reclaimable(reclaim_roots)
            else:
                scan = disk_reclaim.scan_reclaimable_cached(disk_reclaim.DEFAULT_ROOTS)
            cats = scan.get("categories") or []
            total = int(scan.get("total_bytes") or 0)
            extra["reclaimable_bytes"] = total
            extra["reclaimable"] = disk_reclaim.trim_categories_for_signal(cats)
            if any(c.get("partial") for c in cats):
                extra["reclaimable_partial"] = True
            if total > 0:
                breakdown = ", ".join(
                    f"{disk_reclaim.human_bytes(c['bytes'])} "
                    f"{_RECLAIM_BODY_LABELS.get(c['category'], c['category'])}"
                    for c in cats
                    if c.get("bytes")
                )
                seg = f"~{disk_reclaim.human_bytes(total)} reclaimable"
                if breakdown:
                    seg += f" ({breakdown})"
                segments.append(seg)
        except Exception as e:
            _log.debug("reclaimable scan failed: %s", e)

    body = base
    if segments:
        body += "; " + "; ".join(segments)
    body += ". Review on the Alerts page."

    extra.setdefault(
        "what_it_means",
        f"The pod host's data volume is {value}% full. Disk-fill has lead "
        "time, so this fires early (at 85%, not only at the 95% critical "
        "line) while there's still room to act."
        + (
            f" About {disk_reclaim.human_bytes(extra['reclaimable_bytes'])} "
            "is reclaimable from regenerable caches and oversized logs."
            if extra.get("reclaimable_bytes")
            else ""
        ),
    )
    extra.setdefault(
        "fix_steps",
        "1. Review the reclaimable breakdown in this alert's details.\n"
        "2. Reclaim regenerable npm caches and oversized logs (one-click "
        "reclaim is a follow-up; for now clear `~/.npm/_cacache` / `_npx` "
        "per account).\n"
        "3. If the projection shows steady growth, find the largest growing "
        "directories under /Users and prune.",
    )
    return body, extra


def emit_signals_from_snapshot(
    snapshot: dict[str, Any],
    shared_dir: Path,
    *,
    reclaim_roots=None,
) -> int:
    """Mirror crit-tier metrics from a snapshot into the Signal store.

    Phase 5 of docs/spec-alerts-signal-store-2026-05-07.md. Returns the
    count of crit metrics observed. Best-effort — never raises into the
    caller. Sweeps any host_health Signals whose metric returned to ok
    on this snapshot.

    The DISK metric is the deliberate exception to "warn doesn't fire": it
    fires at warn (≥85%, severity "warn") and escalates to alert at crit,
    and carries a reclaimable-space breakdown + fill projection (see
    docs/spec-delta-disk-reclaim-2026-06-21.md). cpu/mem/load stay crit-only.
    The return value counts crit metrics only, so a warn-tier disk fire does
    not inflate it.

    ``reclaim_roots`` overrides the directories the reclaimable scan walks
    (default ``/Users``); tests inject a fixture tree so the scan never
    touches the real filesystem.

    The hostname is the scope key, so the same metric on different
    machines doesn't collide.
    """
    if not snapshot.get("available"):
        return 0
    hostname = snapshot.get("hostname") or "unknown"

    try:
        import importlib
        signals_store = importlib.import_module("signals.store")
        make_signature = importlib.import_module("schema.signal").make_signature
    except Exception:
        return 0

    # Roll the disk-fill history on every snapshot (independent of whether the
    # disk Signal fires this time) so the projection has data when it does —
    # throttled to ~1 sample/hour inside _append_disk_sample.
    disk_history: list[dict[str, Any]] = []
    disk_section = snapshot.get("disk") or {}
    if disk_section.get("total_bytes"):
        try:
            disk_history = _append_disk_sample(
                shared_dir,
                ts=float(snapshot.get("captured_at") or time.time()),
                used_bytes=int(disk_section.get("used_bytes") or 0),
                total_bytes=int(disk_section.get("total_bytes") or 0),
                min_interval=_DISK_SAMPLE_MIN_INTERVAL,
            )
        except Exception:
            disk_history = []

    kept_signatures: set[str] = set()
    crit_count = 0

    for metric_type, label, status_path, value_path, unit, warn_fires in _HOST_METRICS:
        status = _resolve(snapshot, status_path)
        value = _resolve(snapshot, value_path)
        if status == "crit":
            severity = "alert"
        elif status == "warn" and warn_fires:
            severity = "warn"
        else:
            continue
        if status == "crit":
            crit_count += 1
        signature = make_signature("host_health", metric_type, hostname)
        kept_signatures.add(signature)

        body = f"{label}: {value}{unit} (crit threshold reached)"
        details: dict[str, Any] = {
            "hostname": hostname,
            "metric": metric_type,
            "current": value,
            "status": status,
        }
        if metric_type == "disk_low":
            body, extra = _disk_signal_enrichment(
                snapshot, hostname, value, disk_history, reclaim_roots
            )
            details.update(extra)

        try:
            signals_store.observe(
                shared_dir,
                signature=signature,
                producer="host_health",
                type=metric_type,
                flavor="maintenance",
                severity=severity,
                scope="host",
                title=f"{label} on {hostname}",
                body=body,
                details=details,
            )
        except Exception:
            continue

    # Sleep gap (Phase 8.2): on a dedicated host, sleep means the entire
    # daemon fleet was silently dark for the gap. Fires while the most
    # recent sleep is <24h old, then sweep-resolves — the window (rather
    # than resolve-on-next-snapshot) keeps the Signal alive past the
    # notifier's flap-suppression debounce so the operator actually hears
    # about a one-time gap.
    sleep_info = snapshot.get("sleep") or {}
    if sleep_info.get("status") == "alert":
        signature = make_signature("host_health", "host_slept", hostname)
        kept_signatures.add(signature)
        dur_min = round((sleep_info.get("last_sleep_seconds") or 0) / 60)
        wake_at = sleep_info.get("last_wake_at")
        woke = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(wake_at))
            if wake_at else "unknown"
        )
        try:
            signals_store.observe(
                shared_dir,
                signature=signature,
                producer="host_health",
                type="host_slept",
                flavor="maintenance",
                severity="alert",
                scope="host",
                title=f"Host slept on {hostname}",
                body=(
                    f"This Mac slept for ~{dur_min} min (woke {woke}). "
                    "Scheduled jobs and bot gateways do not run during sleep — "
                    "the pod was dark for the gap. On a dedicated host, disable "
                    "sleep on AC power: sudo pmset -c sleep 0 displaysleep 0"
                ),
                details={
                    "hostname": hostname,
                    "last_sleep_at": sleep_info.get("last_sleep_at"),
                    "last_wake_at": wake_at,
                    "last_sleep_seconds": sleep_info.get("last_sleep_seconds"),
                },
            )
        except Exception as e:
            # Best-effort like the metric loop above, but not silent — a
            # store-write failure here means a real outage went unreported.
            _log.warning("host_slept signal write failed: %s", e)

    try:
        signals_store.sweep_resolve(
            shared_dir,
            producer="host_health",
            kept_signatures=kept_signatures,
            reason="auto-resolve: metric no longer firing",
        )
    except Exception:
        pass

    return crit_count
