#!/usr/bin/env python3
"""
pod_report.py — Pod operational triage report (v2).

Delivers a triage-shaped report via the pod's primary message channel
(network.json → alerts). Three buckets, only shown when non-empty:

  Broken   — current-state failures (audit findings, gateways down, daemon
             stalled). RED severity. Each line: emoji + one sentence + deep
             link.
  Trending — per-bot anomalies vs 30-day baseline. Replaces fixed-$
             thresholds. Lines grouped by metric, top 3 per metric.
  Queue    — proposals, blocked tasks awaiting human decision. Informational.

Empty state: one line summarizing yesterday's session count, spend, and
audit completion age. The reader skims it on their phone in 5 seconds.

The report covers `yesterday` (the prior calendar day, fully closed) so the
morning-report-at-08:00 reading "today" empty-data trap can't recur.

Config (network.json → pod_report):
    enabled        — master on/off (default: false)
    report_hour    — local hour for the daily report (0-23, default: 8)
    frequency      — "daily" | "weekdays" | "weekly" (default: "daily")
    weekly_day     — 0=Mon … 6=Sun when frequency="weekly" (default: 1)
    notify_on      — "always" | "yellow_or_red" | "red_only" (default: "always")

The launchd plist fires every hour at :00; should_run() self-gates on the
configured hour. Changing report_hour in the UI takes effect on the next
hour boundary without any plist regeneration.

Thresholds (network.json → pod_report.thresholds, optional overrides only):
    pod_silent_session_floor — pod is "silent" when total sessions < this.
                               Default 0 (i.e. only fully-zero days trigger).
    cost_anomaly_factor      — factor over baseline mean. Default 2.0.
    sessions_anomaly_factor  — factor under baseline mean. Default 0.3.
    cost_min_mean_usd        — sparse-bot suppression for cost. Default 0.50.
    sessions_min_mean        — sparse-bot suppression for sessions. Default 3.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from evolve_config import (
        load_config, get_shared_dir, get_members, get_alerts,
        resolve_network_path,
    )
except ImportError:
    def load_config(n=None): return json.loads(Path("/Users/Shared/evolve/network.json").read_text()) if Path("/Users/Shared/evolve/network.json").exists() else {}  # type: ignore[misc]
    def get_shared_dir(c): return Path(c.get("sharedDir", "/Users/Shared/evolve"))  # type: ignore[misc]
    def get_members(c): return c.get("members", [])  # type: ignore[misc]
    def get_alerts(c): return c.get("alerts", {})  # type: ignore[misc]
    def resolve_network_path(): return Path("/Users/Shared/evolve/network.json")  # type: ignore[misc]

from pod_report_baseline import (
    baseline_for_bot, history_for_bot, is_high_anomaly, is_low_anomaly, ratio,
)


# ── Constants ─────────────────────────────────────────────────────────────────

SECURITY_SNAPSHOT_STALE_MIN = 30.0
GATEWAY_UNREACHABLE_MIN_AGE_SEC = 5 * 60  # require 5 min sustained before flagging

# Chip-filter set for the per-bot chip section of the daily report.
# ``tile_metrics.compute_tile_data`` tags each chip with a digest_tier;
# only ``critical_now`` (gateway down, breaker tripped, infra daemon
# down, etc.) and ``today_news`` (same-day cost spike) reach the daily
# report. ``tile_only`` chips (standing setup tasks, 7d aggregates)
# stay in the admin UI tile view — they're not what changed today.
_DIGEST_TIERS_TO_INCLUDE = frozenset({"critical_now", "today_news"})

# Pod usage anomaly band. The "↑/↓N%" call-out only renders when the
# 7d-vs-prior-7d ratio is outside this band, so quiet weeks don't get
# a noisy "0% change" suffix and small fluctuations stay silent.
_POD_USAGE_DELTA_BAND = 0.15  # ±15%

# Chip ids that ``collect_bot_chips`` must NOT emit because pod_report
# already covers the same operational state via its own pod-level
# collectors. Skipping them here prevents double-counting.
_CHIP_IDS_HANDLED_ELSEWHERE = frozenset({
    "gateway_down",       # collect_broken handles via _list_unreachable_gateways
    "stale_heartbeat",    # pod_report runs offline; no live probe data
    "security_critical",  # collect_broken handles via audit snapshot rollup
})


# ── ReportLine ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReportLine:
    """One line in the rendered report.

    bucket      — "broken" | "trending" | "queue"
    severity    — "red" | "yellow" | "info" (drives the leading emoji)
    text        — the rendered line, no leading emoji
    deeplink    — optional path to the relevant admin UI page
    meta        — optional structured side-data the UI may render (e.g. sparklines).
                  Not rendered into the text body, only emitted in JSON output.
    signal_type — stable type slug for the alerts/signal-store consolidation
                  (Phase 2 of docs/spec-alerts-signal-store-2026-05-07.md).
                  e.g. "audit_critical", "cost_spike", "rsi_pending". Used as
                  the ``Signal.type`` and signature key when this line is
                  mirrored into the Signal store.
    """
    bucket: str
    severity: str
    text: str
    deeplink: str | None = None
    meta: dict | None = None
    signal_type: str = ""


# ── Threshold overrides (sparse — replaces v1 13-key threshold dict) ─────────

DEFAULT_OVERRIDES: dict = {
    "pod_silent_session_floor": 0,    # total sessions ≤ this triggers "Pod silent"
    "cost_anomaly_factor": 2.0,        # ratio over baseline mean for trending
    "sessions_anomaly_factor": 0.3,    # ratio under baseline mean for trending
    "cost_anomaly_factor_cold": 3.0,   # stricter factor when baseline n < 14
    "sessions_anomaly_factor_cold": 0.2,
    "cost_min_mean_usd": 0.50,         # sparse-bot suppression
    "sessions_min_mean": 3.0,          # sparse-bot suppression
}


def _load_overrides(config: dict, bot_id: str | None = None) -> dict:
    """Merge DEFAULT_OVERRIDES with network.json pod + per-bot overrides.

    Resolution order (lowest → highest precedence):
      1. DEFAULT_OVERRIDES (hardcoded floor)
      2. ``network.pod_report.thresholds`` (pod-wide override)
      3. ``network.pod_report.thresholds_per_bot[bot_id]`` (per-bot override)

    Pass ``bot_id=None`` for pod-scoped decisions (e.g. ``pod_silent``);
    pass the bot id for per-bot decisions (cost/session anomaly checks).
    Unknown keys at any layer are ignored.
    """
    merged = dict(DEFAULT_OVERRIDES)
    pod_cfg = (config.get("pod_report") or {})
    for k, v in (pod_cfg.get("thresholds") or {}).items():
        if k in DEFAULT_OVERRIDES:
            merged[k] = v
    if bot_id:
        per_bot = (pod_cfg.get("thresholds_per_bot") or {}).get(bot_id) or {}
        for k, v in per_bot.items():
            if k in DEFAULT_OVERRIDES:
                merged[k] = v
    return merged


# ── Bucket 1: Broken ──────────────────────────────────────────────────────────

def _read_audit_snapshot(shared_dir: Path) -> dict:
    """Read the current-findings snapshot written by audit.py at end of each run."""
    snapshot_path = shared_dir / "audit" / "current-findings.json"
    state = "missing"
    completed_at: str | None = None
    age_min = 999.0
    criticals: list[dict] = []
    warns: list[dict] = []

    if snapshot_path.exists():
        try:
            snap = json.loads(snapshot_path.read_text())
            completed_at = snap.get("audit_completed_at")
            if completed_at:
                ts = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            criticals = list(snap.get("critical", []))
            warns = list(snap.get("warn", []))
            state = "fresh" if age_min < SECURITY_SNAPSHOT_STALE_MIN else "stale"
        except Exception:
            state = "missing"

    return {
        "state": state,
        "age_min": age_min,
        "completed_at": completed_at,
        "critical_count": len(criticals) if state == "fresh" else 0,
        "warn_count": len(warns) if state == "fresh" else 0,
        "critical_findings": criticals if state == "fresh" else [],
    }


def _list_unreachable_gateways(shared_dir: Path, members: list[str]) -> list[str]:
    """Bots whose gateway has been unreachable for >= GATEWAY_UNREACHABLE_MIN_AGE_SEC.

    Reads status/{bot}.json written by heal.py every 5 min. The age guard
    prevents transient status-write races from showing up as gateway outages.
    """
    now = datetime.now(timezone.utc)
    unreachable: list[str] = []
    for bot_id in members:
        status_path = shared_dir / "status" / f"{bot_id}.json"
        try:
            d = json.loads(status_path.read_text())
        except Exception:
            unreachable.append(bot_id)
            continue
        if d.get("gateway_reachable", False):
            continue
        # Verify the unreachable state has persisted long enough
        ts_str = d.get("ts") or d.get("last_error_ts")
        if not ts_str:
            unreachable.append(bot_id)
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if (now - ts).total_seconds() >= GATEWAY_UNREACHABLE_MIN_AGE_SEC:
                unreachable.append(bot_id)
        except Exception:
            unreachable.append(bot_id)
    return unreachable


def _read_bot_metric(shared_dir: Path, bot_id: str, d: date, field: str) -> float | None:
    """Read one numeric field from metrics/{date}/{bot}.json. None if absent."""
    f = shared_dir / "metrics" / d.isoformat() / f"{bot_id}.json"
    if not f.exists():
        return None
    try:
        return float(json.loads(f.read_text()).get(field, 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _pod_total_sessions(
    shared_dir: Path, members: list[str], ref_date: date,
) -> tuple[int, list[str]]:
    """Return (total_sessions, reported_bots) for the pod on ref_date.

    ``reported_bots`` is the subset of ``members`` for which a per-bot
    metrics file exists and parses. Callers use ``len(reported_bots) <
    len(members)`` to distinguish a metrics-writer outage from a
    genuinely silent pod (when len matches and total is 0).
    """
    total = 0
    reported: list[str] = []
    for bot_id in members:
        v = _read_bot_metric(shared_dir, bot_id, ref_date, "session_count")
        if v is None:
            continue
        reported.append(bot_id)
        total += int(v)
    return total, reported


def _format_finding_titles(findings: list[dict], cap: int = 3) -> str:
    """Render up to ``cap`` finding titles inline as "msg (bot)" pieces.

    Each finding is shaped ``{"category", "bot_id", "message", "detail"}``
    per audit._write_findings_snapshot. We surface ``message`` + the bot
    scope so the operator can triage without opening the UI; the line
    used to read "Audit: 4 open CRITICAL · 4 open warn" with no detail.
    Falls back gracefully if a finding lacks fields.
    """
    if not findings:
        return "(no detail available)"
    pieces: list[str] = []
    for f in findings[:cap]:
        msg = (f.get("message") or f.get("category") or "finding").strip()
        bot = (f.get("bot_id") or "").strip()
        pieces.append(f"{msg} ({bot})" if bot else msg)
    rendered = ", ".join(pieces)
    leftover = len(findings) - cap
    if leftover > 0:
        rendered += f" +{leftover} more"
    return rendered


def collect_broken(
    shared_dir: Path,
    members: list[str],
    ref_date: date,
    overrides: dict,
) -> list[ReportLine]:
    """Lines for the Broken bucket — current-state failures."""
    lines: list[ReportLine] = []

    # 1. Audit health and findings
    sec = _read_audit_snapshot(shared_dir)
    if sec["state"] == "missing":
        lines.append(ReportLine(
            bucket="broken", severity="red",
            text="Audit: no snapshot — daemon has never completed a run",
            deeplink="/admin/maintenance?subtab=health",
            signal_type="audit_missing",
        ))
    elif sec["state"] == "stale":
        completed = sec["completed_at"] or "(unknown)"
        lines.append(ReportLine(
            bucket="broken", severity="red",
            text=f"Audit: snapshot stale — last completed {completed} ({_age_str(sec['age_min'])} ago)",
            deeplink="/admin/maintenance?subtab=health",
            signal_type="audit_stale",
        ))
    else:
        crits = sec["critical_count"]
        warns = sec["warn_count"]
        if crits > 0:
            text = f"Audit: {crits} CRITICAL — {_format_finding_titles(sec['critical_findings'])}"
            if warns > 0:
                text += f" · {warns} warn"
            lines.append(ReportLine(
                bucket="broken", severity="red",
                text=text,
                deeplink="/admin/security",
                signal_type="audit_critical",
            ))

    # 2. Liveness — gateways unreachable
    down = _list_unreachable_gateways(shared_dir, members)
    if down:
        s = "s" if len(down) > 1 else ""
        lines.append(ReportLine(
            bucket="broken", severity="red",
            text=f"Liveness: {len(down)} gateway{s} unreachable ({', '.join(down)})",
            deeplink="/admin/maintenance?subtab=health",
            signal_type="gateway_down",
            meta={"affected_bots": list(down)},
        ))

    # 3. Pod silent vs. metrics writer outage. Three states:
    #    - have < n          → writer outage (we can't trust this as pod-wide)
    #    - have == n, total≤floor → pod genuinely silent
    #    - have == n, total > floor → healthy; nothing to emit here
    # Before this split, "Pod silent: 0 sessions across 7 bots" fired even
    # when only 2 of 7 per-bot files existed, mislabeling a telemetry writer
    # crash as a pod outage (observed: 2026-05-10 partial metrics dir).
    total_sessions, reported = _pod_total_sessions(shared_dir, members, ref_date)
    silent_floor = overrides.get("pod_silent_session_floor", 0)
    n = len(members)
    if n > 0 and len(reported) < n:
        missing = [b for b in members if b not in reported]
        if not reported:
            text = (
                f"Metrics writer outage: no per-bot metrics for "
                f"{ref_date.isoformat()} (0 of {n} bots reported)"
            )
        else:
            text = (
                f"Metrics writer outage: only {len(reported)} of {n} bots "
                f"reported metrics for {ref_date.isoformat()} — missing "
                f"{', '.join(missing)}"
            )
        lines.append(ReportLine(
            bucket="broken", severity="red",
            text=text,
            deeplink="/admin/maintenance?subtab=health",
            signal_type="metrics_outage",
            meta={
                "reported_bots": reported,
                "missing_bots": missing,
                "date": ref_date.isoformat(),
            },
        ))
    elif n > 0 and total_sessions <= silent_floor:
        lines.append(ReportLine(
            bucket="broken", severity="red",
            text=f"Pod silent: {total_sessions} sessions across {n} bots {ref_date.isoformat()} — likely outage",
            deeplink="/admin/cost",
            signal_type="pod_silent",
        ))

    return lines


# ── Bucket 2: Trending ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _BotAnomaly:
    bot_id: str
    current: float
    mean: float
    ratio_to_mean: float
    is_cold: bool


def _format_anomalies(
    anomalies: list[_BotAnomaly],
    formatter,
    cap: int = 3,
) -> str:
    """Render a top-N anomaly list with an "+N more" suffix for the rest."""
    anomalies = list(anomalies)
    head = anomalies[:cap]
    rest = anomalies[cap:]
    parts = [formatter(a) for a in head]
    if rest:
        parts.append(f"+{len(rest)} more ({', '.join(a.bot_id for a in rest)})")
    return ", ".join(parts)


def _collect_cost_anomalies(
    shared_dir: Path,
    members: list[str],
    ref_date: date,
    overrides: dict,
    config: dict | None = None,
) -> list[_BotAnomaly]:
    """Bots whose ref_date cost is > factor× their 30d mean.

    Each bot's threshold lookup is per-bot when ``config`` is provided —
    callers in pod_report.run_report always pass it. ``overrides`` is the
    pod-scoped resolution and serves as the fallback when ``config`` is
    None (test helpers that don't have a config dict).
    """
    out: list[_BotAnomaly] = []
    for bot_id in members:
        current = _read_bot_metric(shared_dir, bot_id, ref_date, "total_cost_estimated")
        if current is None:
            continue
        bot_over = _load_overrides(config, bot_id=bot_id) if config else overrides
        factor = bot_over["cost_anomaly_factor"]
        factor_cold = bot_over["cost_anomaly_factor_cold"]
        min_mean = bot_over["cost_min_mean_usd"]
        b = baseline_for_bot("total_cost_estimated", bot_id, shared_dir, ref_date)
        f = factor_cold if b.is_cold else factor
        if is_high_anomaly(current, b, factor=f, min_mean=min_mean):
            out.append(_BotAnomaly(
                bot_id=bot_id, current=current, mean=b.mean,
                ratio_to_mean=ratio(current, b), is_cold=b.is_cold,
            ))
    out.sort(key=lambda a: -a.ratio_to_mean)
    return out


def _collect_session_drops(
    shared_dir: Path,
    members: list[str],
    ref_date: date,
    overrides: dict,
    config: dict | None = None,
) -> list[_BotAnomaly]:
    """Bots whose ref_date session count is < factor× their 30d mean.

    Same per-bot/pod resolution as ``_collect_cost_anomalies``.
    """
    out: list[_BotAnomaly] = []
    for bot_id in members:
        current = _read_bot_metric(shared_dir, bot_id, ref_date, "session_count")
        if current is None:
            continue
        bot_over = _load_overrides(config, bot_id=bot_id) if config else overrides
        factor = bot_over["sessions_anomaly_factor"]
        factor_cold = bot_over["sessions_anomaly_factor_cold"]
        min_mean = bot_over["sessions_min_mean"]
        b = baseline_for_bot("session_count", bot_id, shared_dir, ref_date)
        f = factor_cold if b.is_cold else factor
        if is_low_anomaly(current, b, factor=f, min_mean=min_mean):
            out.append(_BotAnomaly(
                bot_id=bot_id, current=current, mean=b.mean,
                ratio_to_mean=ratio(current, b), is_cold=b.is_cold,
            ))
    out.sort(key=lambda a: a.ratio_to_mean)  # most-dropped first
    return out


def _sparkline_meta(
    metric: str,
    anomalies: list[_BotAnomaly],
    shared_dir: Path,
    ref_date: date,
    head_limit: int = 3,
) -> dict:
    """Build sparkline payload for the named (head) bots only.

    The text line names up to `head_limit` bots. We attach 30-day series for
    those same bots — the UI renders one inline SVG per series. Bots in the
    "+N more" suffix are not series'd, since they aren't named in the line.

    history_for_bot is end-date-exclusive, so we pass `ref_date + 1 day` to
    get 30 days INCLUDING ref_date. That makes values[-1] == current, so the
    sparkline's rightmost point matches the value rendered in the line text.
    """
    end = ref_date + timedelta(days=1)
    series: list[dict] = []
    for a in anomalies[:head_limit]:
        history = history_for_bot(metric, a.bot_id, shared_dir, end)
        series.append({
            "bot_id": a.bot_id,
            "current": a.current,
            "mean": a.mean,
            "values": [v for _, v in history],
            "dates": [d for d, _ in history],
        })
    return {"metric": metric, "series": series}


def collect_trending(
    shared_dir: Path,
    members: list[str],
    ref_date: date,
    overrides: dict,
    config: dict | None = None,
) -> list[ReportLine]:
    """Lines for the Trending bucket — per-bot anomalies vs baseline.

    Passing ``config`` enables per-bot threshold resolution; the
    collectors fall back to ``overrides`` (pod-scoped) when it's omitted
    so existing test helpers keep working unchanged.
    """
    lines: list[ReportLine] = []

    # Cost spike
    spikes = _collect_cost_anomalies(shared_dir, members, ref_date, overrides, config)
    if spikes:
        def fmt(a: _BotAnomaly) -> str:
            tag = " (limited baseline)" if a.is_cold else ""
            return f"{a.bot_id} ${a.current:.2f} ({a.ratio_to_mean:.1f}× of ${a.mean:.2f}){tag}"
        lines.append(ReportLine(
            bucket="trending", severity="yellow",
            text=f"Cost spike: {_format_anomalies(spikes, fmt)}",
            deeplink="/admin/cost",
            meta={
                "sparklines": _sparkline_meta("total_cost_estimated", spikes, shared_dir, ref_date),
                "affected_bots": [a.bot_id for a in spikes],
            },
            signal_type="cost_spike",
        ))

    # Session drop
    drops = _collect_session_drops(shared_dir, members, ref_date, overrides, config)
    if drops:
        def fmt(a: _BotAnomaly) -> str:
            tag = " (limited baseline)" if a.is_cold else ""
            return f"{a.bot_id} {int(a.current)} ({a.ratio_to_mean:.2f}× of {a.mean:.0f} avg){tag}"
        lines.append(ReportLine(
            bucket="trending", severity="yellow",
            text=f"Session drop: {_format_anomalies(drops, fmt)}",
            deeplink="/admin/cost",
            meta={
                "sparklines": _sparkline_meta("session_count", drops, shared_dir, ref_date),
                "affected_bots": [a.bot_id for a in drops],
            },
            signal_type="session_drop",
        ))

    return lines


# ── Bucket 3: Queue ───────────────────────────────────────────────────────────

def _count_blocked_tasks(shared_dir: Path) -> int:
    """Count tasks in tasks/pending.jsonl with status='blocked'."""
    f = shared_dir / "tasks" / "pending.jsonl"
    if not f.exists():
        return 0
    count = 0
    try:
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                if t.get("status") == "blocked":
                    count += 1
            except json.JSONDecodeError:
                continue
    except OSError:
        return 0
    return count


def collect_queue(shared_dir: Path) -> list[ReportLine]:
    """Lines for the Queue bucket — things awaiting human decision."""
    lines: list[ReportLine] = []

    blocked = _count_blocked_tasks(shared_dir)
    if blocked > 0:
        lines.append(ReportLine(
            bucket="queue", severity="info",
            text=f"Tasks: {blocked} blocked",
            deeplink="/admin/maintenance?subtab=infrajobs",
            signal_type="tasks_blocked",
            meta={"blocked_count": blocked},
        ))

    return lines


# ── Per-bot operational chips ────────────────────────────────────────────────
#
# Brings the chip surface from tile_metrics.compute_tile_data into the
# daily report. This is the load-bearing addition from the 2026-06-05
# consolidation: prior to it, breaker trips and per-bot today-news cost
# spikes were only visible in the (separate) daily Bot digest, while
# the Pod report covered pod-level audit / liveness / trending. With
# the two reports merged, the Pod report now also surfaces per-bot
# critical chips. Chips covered by pod_report's own pod-level
# collectors (gateway_down, security_critical) are skipped to avoid
# double-counting — see ``_CHIP_IDS_HANDLED_ELSEWHERE``.


def _compute_bot_chips_safely(
    shared_dir: Path, bot_id: str, spec: dict, network: dict | None,
) -> list[dict]:
    """Call ``tile_metrics.compute_tile_data`` for one bot, returning
    its health_chips list (or [] on any error). Wraps every per-bot
    call so a single malformed bot can't kill the whole report.

    Lazy import keeps ``pod_report`` testable without the full analyzer
    package installed; production launchd invocations have it.
    """
    try:
        from tile_metrics import compute_tile_data  # type: ignore[import-not-found]
    except ImportError:
        return []
    bot_data = {
        "role": (spec or {}).get("role") or "member",
        "live_data": {},  # report runs offline; no gateway probe data here
    }
    try:
        tile = compute_tile_data(
            shared_dir=shared_dir,
            bot_id=bot_id,
            bot_data=bot_data,
            network=network or {"bots": {bot_id: spec or {}}},
        )
    except Exception:
        return []
    return tile.get("health_chips") or []


def collect_bot_chips(
    shared_dir: Path,
    network: dict | None,
    members: list[str],
) -> list[ReportLine]:
    """Lines for per-bot operational chips that reach the daily report.

    Walks each bot in ``members``, computes its tile chips, and emits
    a ReportLine for each chip whose ``digest_tier`` is in
    ``_DIGEST_TIERS_TO_INCLUDE``. Chips already covered by other
    pod_report collectors are skipped (``_CHIP_IDS_HANDLED_ELSEWHERE``).

    Bucket = "broken" for critical_now severity, "trending" for the
    today_news (warn) tier. That puts breaker trips next to pod-wide
    liveness/audit content, and same-day cost spikes next to the
    baseline-anomaly trending output.
    """
    lines: list[ReportLine] = []
    if not members:
        return lines
    bot_specs = (network or {}).get("bots") or {}
    for bot_id in members:
        spec = bot_specs.get(bot_id) if isinstance(bot_specs, dict) else None
        chips = _compute_bot_chips_safely(shared_dir, bot_id, spec or {}, network)
        for c in chips:
            if c.get("id") in _CHIP_IDS_HANDLED_ELSEWHERE:
                continue
            tier = c.get("digest_tier", "tile_only")
            if tier not in _DIGEST_TIERS_TO_INCLUDE:
                continue
            severity = c.get("severity") or "warn"
            # pod_report severity vocabulary is red/yellow/info.
            # tile_metrics severity is critical/warn — map directly.
            report_severity = "red" if severity == "critical" else "yellow"
            bucket = "broken" if tier == "critical_now" else "trending"
            label = str(c.get("label") or c.get("id") or "issue")
            detail = c.get("detail")
            text = f"{bot_id} — {label}"
            if detail:
                text += f" ({detail})"
            lines.append(ReportLine(
                bucket=bucket, severity=report_severity, text=text,
                deeplink=c.get("nav"),
                signal_type=str(c.get("id") or ""),
                meta={"bot_id": bot_id, "chip_id": c.get("id")},
            ))
    return lines


# ── New recommendations / findings since the last digest ──────────────────────


def _state_path(shared_dir: Path) -> Path:
    return shared_dir / "pod_report" / "state.json"


def _read_state(shared_dir: Path) -> dict:
    """Load the daily-digest state file. Empty dict on first run."""
    path = _state_path(shared_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(shared_dir: Path, state: dict) -> None:
    """Atomically write the state file."""
    path = _state_path(shared_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[pod_report] state write failed: {exc}", file=sys.stderr)


# Salience ladder for the daily report's recommendations/findings lines.
# Mirrors the ``Urgency`` ladder in schema/proposal.py (highest first). A
# lower rank number = more salient. Proposals missing/with an unknown
# ``urgency`` sort last (back-compat: pre-urgency proposals are least
# salient, never the daily lead).
_URGENCY_RANK: dict[str, int] = {
    "security_critical": 0,
    "operational_urgent": 1,
    "cost_alert": 2,
    "substrate_warn": 3,
    "improvement": 4,
    "hygiene": 5,
    "whimsy": 6,
}
_URGENCY_RANK_DEFAULT = 99  # unknown / missing → least salient


def _urgency_rank(urgency: str | None) -> int:
    return _URGENCY_RANK.get(urgency or "", _URGENCY_RANK_DEFAULT)


def _is_informational(action_kind: str) -> bool:
    """True if a proposal's action is an observation/FYI (Investigation, etc.)
    rather than an actionable change — defers to arbiter.apply's canonical
    predicate. An import failure is treated as 'not informational' (the item
    stays in the awareness bucket either way, so this only affects whether it
    could have been a recommendation)."""
    try:
        from arbiter.apply import is_informational_kind
    except Exception:
        return False
    return bool(is_informational_kind(action_kind))


def _charter_surface_map() -> dict[str, str | None]:
    """Map ``generator_id`` → charter-default ``surface`` (firing | drift |
    cleanup | improvement | None), read straight from the shipped
    ``generators/<id>/charter.yaml`` files.

    A proposal's *effective* surface is ``p.surface or charter_surface``:
    most generators (e.g. ``manifest_quality``, charter ``surface: drift``)
    leave the per-proposal override None and inherit the charter default, so
    the daily report must consult the charter to know a proposal's real
    surface — otherwise drift hygiene leaks into the lead. Read-only: no
    record IO / fingerprint checks (unlike ``Registry.load_all``, which
    initializes record files as a side effect). Best-effort — an unreadable
    or malformed charter is skipped, leaving its proposals to fall through to
    the awareness ("findings") bucket rather than crash the report.
    """
    out: dict[str, str | None] = {}
    try:
        from registry.charter_loader import load_charter_from_yaml
    except Exception:
        return out
    gen_dir = Path(__file__).resolve().parent / "generators"
    if not gen_dir.is_dir():
        return out
    for entry in sorted(gen_dir.iterdir()):
        if not entry.is_dir():
            continue
        charter_path = entry / "charter.yaml"
        if not charter_path.exists():
            charter_path = entry / "charter.yml"
            if not charter_path.exists():
                continue
        try:
            charter, _ = load_charter_from_yaml(charter_path)
        except Exception:
            continue
        out[charter.id] = charter.surface
    return out


def list_new_proposals(shared_dir: str | Path, since_iso: str | None) -> list[dict]:
    """Return RSI proposals created since ``since_iso``, most-salient first.

    First-run (``since_iso`` is None) returns [] — the report's job is
    to surface *change*, not to enumerate the standing queue. The admin
    UI is where the operator browses the full queue.

    Matches the admin-UI default queue filter (``subdirs=("pending",
    "snoozed")`` minus ``operator_ui`` proposals). Each dict carries the
    fields the daily-report selection ranks/splits on: ``urgency`` (the
    salience ladder), the *effective* ``surface`` (per-proposal override or
    charter default), and ``informational`` (observation/FYI vs. actionable).
    Sorted by urgency salience, newest-first within a tier — NOT by recency
    alone, so a high-urgency item beats a newer low-value one. Falls back to
    [] when the arbiter module can't be imported.
    """
    if since_iso is None:
        return []
    shared_dir_path = Path(shared_dir)
    # Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    # Phase B): arbiter.store.iter_proposals handles a missing dir, so no
    # direct proposals/pending/ existence probe is needed.
    try:
        from arbiter import store as arbiter_store
    except Exception:
        return []

    surface_by_gen = _charter_surface_map()

    out: list[dict] = []
    for p in arbiter_store.iter_proposals(
        shared_dir_path, subdirs=("pending", "snoozed")
    ):
        if getattr(p, "generator_id", None) == "operator_ui":
            continue
        created = getattr(p, "created_at", None) or ""
        if not created or created <= since_iso:
            continue
        gid = getattr(p, "generator_id", "") or ""
        # Effective surface: a per-proposal override wins, else the charter
        # default (manifest_quality et al. leave the override None → "drift").
        eff_surface = getattr(p, "surface", None) or surface_by_gen.get(gid)
        action_kind = getattr(getattr(p, "action", None), "kind", "") or ""
        out.append({
            "id": getattr(p, "id", ""),
            "bot_id": getattr(p, "bot_id", "") or "pod",
            "title": getattr(p, "title", "") or getattr(p, "summary", "") or "(untitled)",
            "created_at": created,
            "urgency": getattr(p, "urgency", None),
            "surface": eff_surface,
            "informational": _is_informational(action_kind),
        })
    # Most-salient first. Two stable passes: newest-first, then urgency-rank
    # ascending — so within an urgency tier the newest item leads, and across
    # tiers the higher-urgency item always wins regardless of age.
    out.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    out.sort(key=lambda d: _urgency_rank(d.get("urgency")))
    return out


def _summarize_proposal_bucket(
    proposals: list[dict], *, label: str, deeplink: str, signal_type: str,
) -> ReportLine | None:
    """Render one Queue line for a salience-ranked bucket of proposals.

    Title-only, the 3 most salient (the list is pre-sorted by
    :func:`list_new_proposals`), then a "+N more" suffix. Returns None for
    an empty bucket so the caller can drop the line entirely (silence means
    nothing changed in that bucket).
    """
    if not proposals:
        return None
    head = proposals[:3]
    leftover = len(proposals) - len(head)
    suffix = f" +{leftover} more" if leftover > 0 else ""
    pieces = [
        f"{p.get('bot_id') or 'pod'}: {p.get('title') or '(untitled)'}"
        for p in head
    ]
    text = f"{label}: " + ", ".join(pieces) + suffix
    return ReportLine(
        bucket="queue", severity="info", text=text,
        deeplink=deeplink, signal_type=signal_type,
        meta={"count": len(proposals),
              "ids": [p.get("id") for p in proposals]},
    )


def collect_new_recommendations(
    shared_dir: Path, since_iso: str | None,
) -> list[ReportLine]:
    """Up to two Queue lines summarizing self-improvement items created
    since the last successful pod_report run, split by the operator-facing
    taxonomy (Recommendations = act, Findings = aware):

      • "New recommendations: …" — actionable items that ask for an operator
        decision (effective ``surface == improvement`` AND not informational).
        Deeplinks to the Recommendations inbox (``/admin/improvements``).
      • "New findings: …" — awareness items the operator should know about
        but that carry no one-click action (anomaly ``firing`` / ``cleanup``
        surfaces, plus "look into it" observations). Deeplinks to Reports →
        Findings (``/admin/reports?subtab=alerts``).

    Each line lists the 3 most SALIENT items (urgency ladder, not recency)
    then "+N more". ``surface == drift`` hygiene nitpicks (manifest-quality
    residue) are excluded from BOTH lines — they belong in the calmer admin-
    UI tiles / Reports surfaces, not the daily lead. A bucket with nothing in
    it drops its line entirely. Producers that emit per-proposal
    ``decisions.proposal_ready`` events handle individual fresh
    announcements; these lines are the daily summary.
    """
    proposals = list_new_proposals(shared_dir, since_iso)
    if not proposals:
        return []

    recommendations: list[dict] = []
    findings: list[dict] = []
    for p in proposals:
        surface = p.get("surface")
        if surface == "drift":
            continue  # hygiene nitpicks — not the daily lead (calmer surfaces)
        if surface == "improvement" and not p.get("informational"):
            recommendations.append(p)  # actionable → Recommendations inbox
        else:
            findings.append(p)  # awareness → Reports → Findings

    lines: list[ReportLine] = []
    rec_line = _summarize_proposal_bucket(
        recommendations, label="New recommendations",
        deeplink="/admin/improvements", signal_type="new_recommendations",
    )
    if rec_line is not None:
        lines.append(rec_line)
    find_line = _summarize_proposal_bucket(
        findings, label="New findings",
        deeplink="/admin/reports?subtab=alerts", signal_type="new_findings",
    )
    if find_line is not None:
        lines.append(find_line)
    return lines


# ── Pod usage line ────────────────────────────────────────────────────────────


def render_pod_usage_line(
    shared_dir: Path, members: list[str], ref_date: date,
) -> str:
    """One-line pod usage + spend summary.

    Sums yesterday's per-bot ``session_count`` / ``total_cost_
    estimated`` from the metrics snapshots and computes a 7d-vs-prior-
    7d delta. The ↑/↓ delta only renders when the ratio crosses
    ``_POD_USAGE_DELTA_BAND`` so quiet weeks don't get a noisy
    "0% change" suffix.

    Returns "" when there's no metrics data for any bot — the rest of
    the report still renders without it.
    """
    if not members:
        return ""

    # Yesterday totals
    sessions_yesterday = 0
    spend_yesterday = 0.0
    saw_metrics = False
    for bot_id in members:
        s = _read_bot_metric(shared_dir, bot_id, ref_date, "session_count")
        c = _read_bot_metric(shared_dir, bot_id, ref_date, "total_cost_estimated")
        if s is not None:
            sessions_yesterday += int(s)
            saw_metrics = True
        if c is not None:
            spend_yesterday += float(c)

    if not saw_metrics:
        return ""

    # 7d window totals (ref_date is "yesterday"; the window is the
    # 7 days ending on ref_date)
    spend_7d = 0.0
    spend_prior_7d = 0.0
    for offset in range(7):
        d = ref_date - timedelta(days=offset)
        for bot_id in members:
            c = _read_bot_metric(shared_dir, bot_id, d, "total_cost_estimated")
            if c is not None:
                spend_7d += float(c)
        d_prior = ref_date - timedelta(days=7 + offset)
        for bot_id in members:
            c = _read_bot_metric(shared_dir, bot_id, d_prior, "total_cost_estimated")
            if c is not None:
                spend_prior_7d += float(c)

    delta = ""
    if spend_prior_7d > 0:
        ratio = spend_7d / spend_prior_7d - 1.0
        if abs(ratio) >= _POD_USAGE_DELTA_BAND:
            arrow = "↑" if ratio > 0 else "↓"
            delta = f", {arrow}{abs(ratio) * 100:.0f}% vs prior week"

    return (
        f"Pod: {sessions_yesterday} sessions {ref_date.isoformat()}, "
        f"${spend_yesterday:.2f} (${spend_7d:.2f} 7d{delta})"
    )


# ── Empty-state summary ───────────────────────────────────────────────────────

def _empty_state_summary(
    shared_dir: Path,
    members: list[str],
    ref_date: date,
) -> str:
    """One-line summary used when broken/trending/queue are all empty."""
    total_sessions, _ = _pod_total_sessions(shared_dir, members, ref_date)
    total_cost = 0.0
    for bot_id in members:
        v = _read_bot_metric(shared_dir, bot_id, ref_date, "total_cost_estimated")
        if v is not None:
            total_cost += v

    sec = _read_audit_snapshot(shared_dir)
    if sec["state"] == "fresh":
        audit_part = f"audit ran {_age_str(sec['age_min'])} ago"
    elif sec["state"] == "stale":
        # This branch is unreachable when called from a clean state — stale
        # snapshot is itself a Broken-bucket finding. Defensive only.
        audit_part = "audit snapshot stale"
    else:
        audit_part = "audit not yet run"

    return f"{total_sessions} sessions {ref_date.isoformat()}, ${total_cost:.2f} spend, {audit_part}"


def _find_last_non_green(shared_dir: Path, max_age_days: int = 14) -> dict | None:
    """Scan reports/*.json, return the most recent non-green entry within window.

    Returns {"generated_at": iso, "overall": str, "first_line": str} or None.
    The "first_line" is the lead bullet of the broken/trending bucket — gives
    the reader a one-line reminder of what was wrong, e.g. "Audit: 2 open
    CRITICAL".
    """
    reports_dir = shared_dir / "reports"
    if not reports_dir.exists():
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    best: dict | None = None
    best_ts: datetime | None = None
    for f in reports_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("overall") in (None, "green", "unknown"):
            continue
        ts_str = data.get("generated_at", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if best_ts is None or ts > best_ts:
            best_ts = ts
            # Pull a representative first line out of the report_text body.
            # The body starts with "📊 *Pod Report — ...*\n\n🔴 ..." — take
            # the first emoji-prefixed line.
            first_line = ""
            # ``⚠️`` is two codepoints (U+26A0 + U+FE0F variation
            # selector), so a `ln[:1]` check would miss it. Use
            # ``startswith`` for each allowed leading emoji.
            for ln in (data.get("report_text") or "").splitlines():
                ln = ln.strip()
                if ln and any(ln.startswith(e) for e in ("🔴", "⚠️", "📋")):
                    first_line = ln
                    break
            best = {
                "generated_at": ts_str,
                "overall": data["overall"],
                "first_line": first_line,
            }
    return best


# ── Render ────────────────────────────────────────────────────────────────────

_EMOJI = {"red": "🔴", "yellow": "⚠️", "info": "📋"}


def _age_str(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)}m"
    if minutes < 1440:
        return f"{minutes/60:.1f}h"
    return f"{int(minutes/1440)}d"


def render_report(
    label: str,
    broken: list[ReportLine],
    trending: list[ReportLine],
    queue: list[ReportLine],
    empty_summary: str,
    *,
    pod_usage_line: str = "",
) -> tuple[str, str]:
    """Render the three buckets into the final text + an "overall" status code.

    Returns (text, overall) where overall is "red" | "yellow" | "green".
    The overall is for compatibility with the admin UI's send-test endpoint
    which displays a status pill. red == anything broken, yellow == only
    trending, green == only queue or empty.

    The "📊 Pod report — {label}" title is rendered by the catalog
    body_template at dispatch time (see catalog.py:summaries.daily_pod_report),
    so this function returns only the body. Keeping the title here too
    would produce a double-titled chat message.

    ``pod_usage_line`` (new 2026-06-05): when non-empty, prepended at
    the top of the body so the operator sees yesterday's totals as the
    leading context. Blank-line separator between the usage line and
    the issue list. Pre-2026-06-05 the empty-state rendered "🟢 All
    clear · {empty_summary}" — that was retired per operator feedback
    (silence means clear; listing it is noise) and the silent-when-
    empty path is now handled by callers via should_send.
    """
    del label  # title is owned by the catalog body_template, not the body
    del empty_summary  # no longer rendered — retained for caller compat

    sections: list[str] = []
    if pod_usage_line:
        sections.append(pod_usage_line)

    issue_lines: list[str] = []
    for line in broken + trending + queue:
        emoji = _EMOJI.get(line.severity, "•")
        issue_lines.append(f"{emoji} {line.text}")
    if issue_lines:
        sections.append("\n".join(issue_lines))

    text = "\n\n".join(sections)
    overall = "red" if broken else ("yellow" if trending else "green")
    return text, overall


# ── Schedule gate ─────────────────────────────────────────────────────────────

def should_run(cfg: dict) -> tuple[bool, str]:
    """Decide whether *this* invocation should produce a report.

    Returns (run, label) — label is "Daily" when fired by the schedule, or
    "" when run=False. Called once at startup of the launchd job; a False
    return causes the script to exit 0 silently.

    The launchd plist fires every hour at :00; this function checks whether
    the current hour matches `report_hour` (the only configurable slot).
    """
    if not cfg.get("enabled", False):
        return False, ""

    freq = cfg.get("frequency", "daily")
    today = datetime.now()
    if freq == "weekdays" and today.weekday() >= 5:
        return False, ""
    if freq == "weekly" and today.weekday() != int(cfg.get("weekly_day", 1)):
        return False, ""

    if today.hour == int(cfg.get("report_hour", 8)):
        return True, "Daily"
    return False, ""


# ── Delivery ──────────────────────────────────────────────────────────────────

# overall status → dispatcher.Severity. red=worst, green=clean.
_SEVERITY_BY_OVERALL = {
    "red": "error",
    "yellow": "warning",
    "green": "info",
}


def send_message(
    text: str,
    *,
    shared_dir: Path,
    network: dict,
    label: str = "Daily",
    overall: str = "info",
) -> bool:
    """Route the rendered pod-report through the alerts dispatcher.

    Returns True iff DispatchResult.SENT. Caller writes the on-disk
    pod-report log + artifact regardless of return value (those record
    that a report ran, not that a chat message landed).

    Phase 3c of the alert-notifier spec: replaces the inline
    ``subprocess.run([openclaw, message, send, ...])`` with a
    dispatcher hand-off. Operator-tunable enable/cooldown via
    ``alerts.pod_report.{enabled, cooldown_seconds}``. Default
    cooldown is 0 — pod_report has its own natural daily cadence
    (the launchd hourly tick + report_hour gating in should_run);
    the dispatcher cooldown is a safety net, not the primary
    de-dup mechanism.
    """
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        # Best-effort log; main() still proceeds to write the on-disk
        # artifacts so a missing dispatcher import doesn't lose the run.
        print(f"[pod_report] dispatcher import failed; chat push dropped: {exc}",
              file=sys.stderr)
        return False

    severity_name = _SEVERITY_BY_OVERALL.get(overall, "info")
    severity = {
        "critical": Severity.CRITICAL, "error": Severity.ERROR,
        "warning": Severity.WARNING, "info": Severity.INFO,
    }[severity_name]
    try:
        # Phase F6: payload-driven. Catalog body_template is
        # "📊 Pod report — {label}\n{summary}" — the report body
        # (built upstream by run_report) goes in as the summary field.
        outcome = _dispatch_send(
            shared_dir=shared_dir,
            network=network,
            source="pod_report",
            severity=severity,
            dedup_key=f"pod_report/{label}",
            catalog_event="summaries.daily_pod_report",
            payload={"label": label, "summary": text},
        )
    except Exception as exc:
        print(f"[pod_report] dispatcher.send raised; chat push dropped: {exc}",
              file=sys.stderr)
        return False
    return outcome.result == DispatchResult.SENT


def write_log(shared_dir: Path, label: str, overall: str, sent: bool) -> None:
    log_path = shared_dir / "logs" / "pod-report.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = f"{now} [{label}] status={overall} sent={sent}\n"
        with log_path.open("a") as f:
            f.write(entry)
    except Exception:
        pass


def write_report_artifact(
    shared_dir: Path,
    label: str,
    overall: str,
    sent: bool,
    report_text: str,
) -> None:
    """Persist the rendered report so the admin UI's history view can show it."""
    out_dir = shared_dir / "reports"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        fname = now.strftime("%Y-%m-%dT%H-%M-%SZ") + f"-{label.lower()}.json"
        artifact = {
            "generated_at": now.isoformat(timespec="seconds"),
            "label": label,
            "overall": overall,
            "sent": sent,
            "report_text": report_text,
            "schema_version": 2,  # v2 = bucket-shaped report
        }
        (out_dir / fname).write_text(json.dumps(artifact, indent=2))
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def run_report(
    shared_dir: Path,
    members: list[str],
    overrides: dict,
    label: str,
    now: datetime | None = None,
    config: dict | None = None,
) -> tuple[str, str, dict]:
    """Build a full report. Returns (text, overall_status, structured_data).

    structured_data is the JSON-friendly view used by the admin UI's
    send-test endpoint and report history.

    ``config`` is the parsed network.json (or None for legacy callers).
    When provided, per-bot threshold overrides under
    ``pod_report.thresholds_per_bot.<bot>`` are honored. Without it,
    ``overrides`` is the pod-scoped fallback for every bot.
    """
    when = now or datetime.now()
    ref_date = (when - timedelta(days=1)).date()  # always "yesterday"

    broken = collect_broken(shared_dir, members, ref_date, overrides)
    trending = collect_trending(shared_dir, members, ref_date, overrides, config)
    queue = collect_queue(shared_dir)

    # 2026-06-05 consolidation: per-bot operational chips + new-recs
    # collectors moved here from the retired daily_bot_digest. Skip the
    # bot-chips collector when network is unavailable (legacy test
    # helpers); the rest of the report still renders.
    if config is not None:
        broken.extend(
            l for l in collect_bot_chips(shared_dir, config, members)
            if l.bucket == "broken"
        )
        trending.extend(
            l for l in collect_bot_chips(shared_dir, config, members)
            if l.bucket == "trending"
        )

    # Note: collect_bot_chips is called twice above so the bucket
    # routing stays explicit. Performance is fine — tile_metrics caches
    # per-bot lookups, and the per-call work is a thin filter pass.

    # New-recommendations line in the Queue bucket. Reads
    # ``{shared_dir}/pod_report/state.json::last_run_iso`` to filter
    # only proposals created since the last successful run. First-run
    # state is empty → no recs surfaced (list_new_proposals returns []
    # when since_iso is None) so a fresh install doesn't dump the full
    # standing queue into the operator's chat.
    state = _read_state(shared_dir)
    since_iso = state.get("last_run_iso") if isinstance(state, dict) else None
    queue.extend(collect_new_recommendations(shared_dir, since_iso))

    empty_summary = _empty_state_summary(shared_dir, members, ref_date)
    last_non_green = _find_last_non_green(shared_dir)

    # Pod usage header — yesterday + 7d totals, with a delta call-out
    # when the 7d ratio crosses the attention band. Replaces the old
    # ``🟢 All clear · {empty_summary}`` empty-state line.
    pod_usage_line = render_pod_usage_line(shared_dir, members, ref_date)

    text, overall = render_report(
        label, broken, trending, queue, empty_summary,
        pod_usage_line=pod_usage_line,
    )

    structured = {
        "report_text": text,
        "overall": overall,
        "schema_version": 2,
        "ref_date": ref_date.isoformat(),
        "buckets": {
            "broken": [_line_dict(l) for l in broken],
            "trending": [_line_dict(l) for l in trending],
            "queue": [_line_dict(l) for l in queue],
        },
        "pod_usage_line": pod_usage_line,
        "empty_summary": empty_summary,
        "last_non_green": last_non_green,  # None unless a recent non-green run exists
    }

    # Phase 2 of the alerts/signal-store consolidation
    # (docs/spec-alerts-signal-store-2026-05-07.md): mirror every report
    # line into the Signal store. Comprehensive sweep — anything from
    # this producer that doesn't fire this run gets auto-resolved.
    try:
        _emit_signals_from_buckets(shared_dir, broken, trending, queue, ref_date)
    except Exception:
        # Phase 2 best-effort — never break the report run on signal-side issues.
        pass

    # Convert per-bot gateway error-log patterns into Signals so they surface
    # on the Alerts page with context and a deeplink rather than as raw log lines.
    try:
        from bot_log_signal import emit_bot_log_signals
        emit_bot_log_signals(shared_dir, members)
    except Exception:
        pass

    return text, overall, structured


# ── Signal emission (Phase 2) ─────────────────────────────────────────────────

# Maps ReportLine.bucket → Signal.flavor. "broken" items are user-actionable
# breakage (Maintenance lane); "trending" + "queue" sit in the skim feed
# (Activity lane).
_BUCKET_TO_FLAVOR = {
    "broken": "maintenance",
    "trending": "activity",
    "queue": "activity",
}

# Maps ReportLine.severity → Signal.severity. pod_report uses red/yellow/info;
# the Signal store uses alert/warn/info.
_SEVERITY_MAP = {
    "red": "alert",
    "yellow": "warn",
    "info": "info",
}


def _humanize_signal_type(signal_type: str) -> str:
    return signal_type.replace("_", " ").capitalize()


def _pod_report_remediation(signal_type: str):
    """Map a pod_report signal_type to its server-side remediation, or None.

    Phase 4 PR-2: the metrics writer outage is the canonical pod_report
    signal that has a clean mechanical fix — running install-infra-jobs
    refreshes the LaunchDaemons that produce per-bot metrics. Idempotent;
    skips already-installed plists. (For the case where the daemons are
    installed but crashing — e.g. the get_detector_enabled NameError
    fixed in #1006 — install-infra-jobs is harmless and the operator
    still needs to inspect the daemon stderr; that escape hatch is
    spelled out in the confirm text.)

    Other pod_report signals (pod_silent, gateway_down, audit_critical,
    etc.) need operator judgment; no automation today.
    """
    try:
        from schema.signal import Remediation
    except ImportError:
        return None

    if signal_type == "metrics_outage":
        return Remediation(
            kind="install_infra_jobs",
            params={},
            label="Install infra jobs",
            confirm=(
                "Runs `sudo evolve-admin install-infra-jobs` to (re)install "
                "the LaunchDaemons that produce per-bot metrics. Idempotent "
                "— already-installed plists are skipped. If the daemons are "
                "installed but crashing, this won't help; check the daemon "
                "stderr at /Users/evolve/.openclaw/logs/evolve-analyze.err.log."
            ),
        )
    return None


# Severity-framework tag per signal_type emitted by pod_report. Spec:
# docs/spec-severity-framework-2026-05-18.md. audit_critical lives on
# the security vector because pod_report mirrors audit's pod-wide roll-
# up of CRITICAL findings. Everything else is operations or cost.
_POD_REPORT_SEVERITY_TAG: dict[str, tuple[str, int]] = {
    # ── broken (substrate health) ────────────────────────────────────────────
    "audit_missing":   ("operations", 1),  # audit cron hasn't run — advisory
    "audit_stale":     ("operations", 1),
    "audit_critical":  ("security",   3),  # pod-wide critical audit roll-up
    "gateway_down":    ("operations", 3),  # multi-bot gateway issue, pod scope
    "metrics_outage":  ("operations", 2),  # some bots not reporting
    "pod_silent":      ("operations", 1),  # ambient low-activity signal
    # ── trending (anomalies) ─────────────────────────────────────────────────
    "cost_spike":      ("cost",       2),  # producer overrides per-emit when severe
    "session_drop":    ("operations", 1),  # advisory; drop in user volume
    # ── queue ────────────────────────────────────────────────────────────────
    "tasks_blocked":   ("operations", 1),  # advisory; growing queue
}


def _pod_report_severity_tag(signal_type: str) -> tuple[str, int]:
    """Look up the (vector, magnitude) tag for a pod_report signal type.
    Unknown types fall through to operations:2 — conservative default."""
    return _POD_REPORT_SEVERITY_TAG.get(signal_type, ("operations", 2))


# Operator docs per signal_type (per PR #1603's convention). The Alerts UI
# renders these as "What this means" + numbered "How to fix" blocks.
# Unknown signal_types get no docs (the title+body+raw-details fallback
# rendering still works).
_OPERATOR_DOCS: dict[str, tuple[str, str]] = {
    "audit_missing": (
        "The audit cron has never produced a snapshot — the periodic "
        "security/health audit isn't running at all. Without it, the pod "
        "loses its primary visibility into current failures; nothing is "
        "actively dangerous, but problems can build up unnoticed.",
        "1. Confirm the audit LaunchDaemon is loaded:\n"
        "   ssh pod_admin_user@mini sudo launchctl print system/"
        "ai.openclaw.evolve.audit\n"
        "2. If not loaded, reinstall infra jobs:\n"
        "   ssh pod_admin_user@mini sudo evolve-admin install-infra-jobs\n"
        "3. Trigger an immediate run to verify:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.evolve.audit\n"
        "4. Tail the audit log to confirm it produced a snapshot:\n"
        "   ssh pod_admin_user@mini sudo tail -f "
        "/Users/Shared/evolve/logs/audit.log",
    ),
    "audit_stale": (
        "The audit cron last produced a snapshot too long ago — the "
        "daemon is loaded but isn't completing runs on schedule. Whatever "
        "the audit was last reporting may have changed since; treat the "
        "current Alerts page as out-of-date until this clears.",
        "1. Check the audit daemon status:\n"
        "   ssh pod_admin_user@mini sudo launchctl print system/"
        "ai.openclaw.evolve.audit\n"
        "2. Check the audit log stderr for crash loops:\n"
        "   ssh pod_admin_user@mini sudo /bin/cat "
        "/Users/Shared/evolve/logs/audit.err.log | tail -50\n"
        "3. Kickstart to retry:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.evolve.audit\n"
        "4. If the same crash recurs, the audit code is likely broken — "
        "check recent merges that touched `packages/analyzer/audit*.py`",
    ),
    "audit_critical": (
        "The audit found CRITICAL-tier security or operational issues "
        "still open across the pod. Each finding is its own audit Signal "
        "with its own remediation — this is the rollup that tells the "
        "operator there's at least one fire still burning. Drill in via "
        "Security to see specifics.",
        "1. Open Security in the admin UI to see the CRITICAL findings\n"
        "2. Each finding's Signal carries its own what_it_means + "
        "fix_steps (PR #1598 / #1603 sweep)\n"
        "3. Triage in order: keystore / auth issues first, then "
        "permission posture, then config drift\n"
        "4. Once every CRITICAL is resolved or dismissed, this rollup "
        "auto-clears on the next pod_report cycle",
    ),
    "gateway_down": (
        "One or more bot gateways are unreachable — operator chat with "
        "those bots is broken, and every monitor that needs the gateway "
        "(heal, exec-approvals dispatch, etc.) is degraded for them. "
        "Distinct from a single transient failure: this fires only "
        "after 5min sustained.",
        "1. Identify the affected bots from this Signal's "
        "`details.affected_bots`\n"
        "2. Check each gateway's status:\n"
        "   ssh pod_admin_user@mini sudo launchctl print "
        "system/ai.openclaw.gateway.<bot>\n"
        "3. Kickstart any that are not running:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>\n"
        "4. Tail stderr for crash info:\n"
        "   ssh pod_admin_user@mini sudo /bin/cat "
        "/Users/<bot>/.openclaw/logs/gateway.err.log | tail -50\n"
        "5. If the gateway crashes again, check Maintenance → Health "
        "for the recent error and inspect the bot's openclaw.json for "
        "a bad config",
    ),
    "metrics_outage": (
        "The metrics writer LaunchDaemons aren't producing per-bot "
        "metrics for the reference date — either the daemons crashed, "
        "or they're installed but a write is failing. Without metrics, "
        "the cost/session UI and the trending detectors silently lose "
        "their data source. A dedicated remediation (`install-infra-jobs`) "
        "is wired up — try the button on the Alerts page first.",
        "1. Click the 'Install infra jobs' button on this Signal in the "
        "Alerts UI (one-click; idempotent)\n"
        "2. If the button isn't available or the issue persists, run "
        "it directly on the mini:\n"
        "   ssh pod_admin_user@mini sudo evolve-admin install-infra-jobs\n"
        "3. Check the daemon stderr for crashes:\n"
        "   ssh pod_admin_user@mini sudo /bin/cat "
        "/Users/evolve/.openclaw/logs/evolve-analyze.err.log | tail -50\n"
        "4. Recent code change is the usual cause; look at "
        "`git log -- packages/analyzer/metrics/` for what changed",
    ),
    "pod_silent": (
        "The pod recorded zero sessions across all bots on the reference "
        "date — likely an outage (gateways down, dispatcher broken, or "
        "telemetry not landing). Distinct from `metrics_outage`: metrics "
        "did report; they reported zero. If the pod really is idle "
        "(weekend, vacation, etc.) raise the `pod_silent_session_floor` "
        "threshold to silence.",
        "1. Confirm the gateways are running:\n"
        "   ssh pod_admin_user@mini sudo launchctl list | grep gateway\n"
        "2. Check whether the bots' chat integrations are reachable — "
        "send a test message in the operator chat\n"
        "3. Look at the dispatcher log for delivery failures:\n"
        "   ssh pod_admin_user@mini sudo /bin/cat "
        "/Users/Shared/evolve/logs/dispatcher.log | tail -50\n"
        "4. If the pod is legitimately idle, raise "
        "`pod_report.thresholds.pod_silent_session_floor` in "
        "network.json so this stops firing on idle days",
    ),
    "cost_spike": (
        "One or more bots' cost on the reference date is significantly "
        "above their own 30-day baseline. This is a relative-trend "
        "check, distinct from `cost_watchdog.daily_spend_high` (absolute "
        "$). Drill into the named bots; the spike could be a real "
        "traffic surge or a regression.",
        "1. Open Cost → Bot detail for each named bot in this Signal's "
        "`details.affected_bots`\n"
        "2. Switch the timeframe to 30 days — eyeball the trend "
        "(step-change vs. a single anomaly day)\n"
        "3. If a single session caused it: inspect that session for "
        "retry storms / runaway subagents\n"
        "4. If sustained: look for a recent model upgrade or new cron "
        "(`git log -- bots/<bot>` and the bot's openclaw.json)\n"
        "5. Adjust `pod_report.cost_anomaly_factor` if the new baseline "
        "is expected and the alarms are noise",
    ),
    "session_drop": (
        "One or more bots' session count today is much lower than their "
        "30-day baseline. The bot may be misconfigured, the chat "
        "integration may be broken, or user demand may just be down. "
        "Advisory-tier — no automation; the right response depends on "
        "intent.",
        "1. Open Cost → Bot detail for each named bot — switch to "
        "session view\n"
        "2. Check whether the integration is reachable (try sending a "
        "test message in the bot's channel)\n"
        "3. Check the gateway log for delivery failures:\n"
        "   ssh pod_admin_user@mini sudo /bin/cat "
        "/Users/<bot>/.openclaw/logs/gateway.log | tail -100\n"
        "4. If the bot is intentionally quiet (e.g. weekend) dismiss "
        "this Signal — it will re-fire on the next anomaly",
    ),
    "tasks_blocked": (
        "Tasks have accumulated in the `blocked` state in "
        "`{shared_dir}/tasks/pending.jsonl` — they need a human "
        "decision before they can proceed. This is queue-tier, not a "
        "failure: the system is correctly stopping at a decision point "
        "and waiting for the operator.",
        "1. Open Maintenance → Infra jobs in the admin UI to see the "
        "blocked tasks\n"
        "2. For each: read the blocker reason — usually a missing "
        "approval, a config decision, or a credential rotation\n"
        "3. Either resolve the blocker (provide the input) or cancel "
        "the task if it's no longer relevant\n"
        "4. Once the queue drains, this Signal auto-clears on the next "
        "pod_report run",
    ),
}


def _operator_docs(signal_type: str) -> tuple[str, str] | None:
    """Look up the (what_it_means, fix_steps) for a pod_report signal type.
    Unknown types return None — UI falls back to title+body rendering."""
    return _OPERATOR_DOCS.get(signal_type)


def _emit_signals_from_buckets(
    shared_dir: Path,
    broken: list[ReportLine],
    trending: list[ReportLine],
    queue: list[ReportLine],
    ref_date: date,
) -> None:
    """Mirror each ReportLine into a Signal and sweep-resolve the rest.

    Phase 2 keeps signals pod-scoped (one Signal per (signal_type)). The
    ``meta.affected_bots`` field carries the per-bot detail for UI use;
    full per-bot signal granularity lands in Phase 5 when each producer
    migrates fully.
    """
    try:
        from signals import store as signals_store
        from schema.signal import make_signature
    except ImportError:
        return

    kept_signatures: set[str] = set()
    for line in (*broken, *trending, *queue):
        if not line.signal_type:
            continue
        flavor = _BUCKET_TO_FLAVOR.get(line.bucket)
        if flavor is None:
            continue
        severity = _SEVERITY_MAP.get(line.severity, "info")
        signature = make_signature("pod_report", line.signal_type, "pod")
        kept_signatures.add(signature)

        details = dict(line.meta or {})
        details["text"] = line.text
        details["bucket"] = line.bucket
        if line.deeplink:
            details["deeplink"] = line.deeplink
        details["ref_date"] = ref_date.isoformat()
        # Severity framework: attach (vector, magnitude). Active flag
        # reflects whether this is a currently-firing condition
        # (broken bucket = active; trending = not necessarily active).
        vector, magnitude = _pod_report_severity_tag(line.signal_type)
        details["vector"] = vector
        details["magnitude"] = magnitude
        if line.bucket == "broken":
            details["severity_active"] = True
        # Operator docs (PR #1603 convention). Rendered specially by
        # the Alerts UI as "What this means" + "How to fix" blocks.
        docs = _operator_docs(line.signal_type)
        if docs is not None:
            what_it_means, fix_steps = docs
            details["what_it_means"] = what_it_means
            details["fix_steps"] = fix_steps

        signals_store.observe(
            shared_dir,
            signature=signature,
            producer="pod_report",
            type=line.signal_type,
            flavor=flavor,
            severity=severity,
            scope="pod",
            title=_humanize_signal_type(line.signal_type),
            body=line.text,
            details=details,
            remediation=_pod_report_remediation(line.signal_type),
        )

    # Anything from pod_report that didn't fire this run → auto-resolve
    signals_store.sweep_resolve(
        shared_dir,
        producer="pod_report",
        kept_signatures=kept_signatures,
        reason="auto-resolve: condition cleared on next pod_report run",
    )


def _line_dict(line: ReportLine) -> dict:
    out = {
        "bucket": line.bucket,
        "severity": line.severity,
        "text": line.text,
        "deeplink": line.deeplink,
    }
    if line.meta:
        out["meta"] = line.meta
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve pod operational report (v2)")
    parser.add_argument("--network", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print report without sending")
    parser.add_argument("--force", action="store_true",
                        help="Bypass schedule gate — send report regardless of time/config")
    parser.add_argument("--output-json", default=None,
                        help="Write structured result to this path (used by admin UI test-send)")
    args = parser.parse_args()

    net_path = resolve_network_path(Path(args.network) if args.network else None)
    config = load_config(str(net_path) if net_path else None)
    shared_dir = get_shared_dir(config)
    members = get_members(config)
    pod_cfg = config.get("pod_report", {})
    overrides = _load_overrides(config)

    if args.force:
        run, label = True, "Manual"
    else:
        run, label = should_run(pod_cfg)
        if not run and not args.dry_run:
            sys.exit(0)
        if args.dry_run and not label:
            label = "Dry-run"

    text, overall, structured = run_report(shared_dir, members, overrides, label, config=config)

    notify_on = pod_cfg.get("notify_on", "always")
    suppress = (
        (notify_on == "red_only" and overall != "red") or
        (notify_on == "yellow_or_red" and overall == "green")
    )

    if args.output_json:
        try:
            Path(args.output_json).write_text(json.dumps(structured))
        except Exception:
            pass

    if args.dry_run:
        print(text)
        print(f"\n[dry-run] overall={overall} notify_on={notify_on}")
        return

    if suppress:
        write_log(shared_dir, label, overall, sent=False)
        write_report_artifact(shared_dir, label, overall, sent=False, report_text=text)
        sys.exit(0)

    sent = send_message(
        text, shared_dir=shared_dir, network=config,
        label=label, overall=overall,
    )
    write_log(shared_dir, label, overall, sent=sent)
    write_report_artifact(shared_dir, label, overall, sent=sent, report_text=text)
    # Advance the since-last-digest watermark so the next run's
    # new-recommendations collector only surfaces proposals created
    # AFTER this run. Advance even on dispatcher failure — better to
    # miss a beat than to dump the full standing queue into chat on
    # the next successful run.
    state = _read_state(shared_dir)
    if not isinstance(state, dict):
        state = {}
    state["last_run_iso"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_state(shared_dir, state)
    if not sent:
        sys.exit(1)


if __name__ == "__main__":
    main()
