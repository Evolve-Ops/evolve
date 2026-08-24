#!/usr/bin/env python3
"""
evolve/measure.py — Daily metrics computation

Reads turn annotations from the shared directory and computes
daily performance metrics per bot.

Scheduled via launchd to run daily at 01:00 AM.

Usage:
    python3 measure.py --network /path/to/network.json
    python3 measure.py --bot-id bot1 --shared-dir /Users/Shared/evolve
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from evolve_config import (
        CANONICAL_SHARED_DIR,
        is_module_enabled,
        load_config,
        resolve_network_path,
    )
except ImportError:
    from platform_profile import get_profile as _gp
    CANONICAL_SHARED_DIR = Path(_gp().shared_dir_default)
    def load_config(n=None): return json.loads((CANONICAL_SHARED_DIR / "network.json").read_text()) if (CANONICAL_SHARED_DIR / "network.json").exists() else {}
    def is_module_enabled(c, n): return True
    def resolve_network_path(): return CANONICAL_SHARED_DIR / "network.json"

try:
    from calibration import CalibrationLoader
except ImportError:
    CalibrationLoader = None  # type: ignore[assignment,misc]

try:
    from app_session_correlator import correlate_sessions
except ImportError:
    correlate_sessions = None  # type: ignore[assignment]

# Pod-local timezone. Storage is always UTC; only rendering converts.
# Resolved lazily via network.json — falls back to Pacific if missing/invalid.
DEFAULT_TZ_NAME = "America/Los_Angeles"
_RESOLVED_TZ: ZoneInfo | None = None


def _resolve_tz(shared_dir: str = str(CANONICAL_SHARED_DIR)) -> ZoneInfo:
    """Resolve the pod timezone from network.json, caching the result.

    Fallback chain: network.json `timezone` field → DEFAULT_TZ_NAME constant.
    The result is cached at module level; call _load_measure_calibration to
    refresh after switching shared_dir.
    """
    global _RESOLVED_TZ
    if _RESOLVED_TZ is not None:
        return _RESOLVED_TZ
    name = DEFAULT_TZ_NAME
    try:
        net = Path(shared_dir) / "network.json"
        if net.exists():
            cfg = json.loads(net.read_text())
            cand = cfg.get("timezone")
            if isinstance(cand, str) and cand.strip():
                name = cand.strip()
    except (json.JSONDecodeError, OSError):
        pass
    try:
        _RESOLVED_TZ = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        _RESOLVED_TZ = ZoneInfo(DEFAULT_TZ_NAME)
    return _RESOLVED_TZ


def _resolve_local_tz(network_arg: "str | None" = None) -> ZoneInfo:
    """Back-compat wrapper around _resolve_tz (kept for #747's API surface).

    Accepts an explicit network.json path; otherwise resolves via the default
    shared_dir. Bypasses the cache so callers passing an explicit path get a
    fresh read.
    """
    if network_arg:
        try:
            cfg = json.loads(Path(network_arg).read_text())
            name = cfg.get("timezone") or DEFAULT_TZ_NAME
            return ZoneInfo(name)
        except (OSError, json.JSONDecodeError, ZoneInfoNotFoundError):
            return ZoneInfo(DEFAULT_TZ_NAME)
    return _resolve_tz()


LOCAL_TZ = _resolve_local_tz()


# ── Calibration (loaded at import time, refreshed in main) ────────────────────
# These module-level values are defaults; main() overwrites them after loading
# the shared_dir from network.json so the calibration path is correct.

def _load_measure_calibration(shared_dir: str = str(CANONICAL_SHARED_DIR)) -> None:
    """Load (or reload) measure calibration into module-level constants."""
    global _THRESHOLDS, _UNRESOLVED_KEYWORDS, _RESOLVED_TZ
    # Reset the cached TZ so the next _resolve_tz() call re-reads network.json
    # for this shared_dir (the default and the configured shared_dir may differ).
    _RESOLVED_TZ = None
    _resolve_tz(shared_dir)
    if CalibrationLoader is None:
        return
    cal = CalibrationLoader(shared_dir)
    measure_cfg = cal.load("measure").get("measure", {})
    thresholds = measure_cfg.get("status_thresholds", {})
    _THRESHOLDS["critical_maintenance_ratio"] = float(thresholds.get("critical_maintenance_ratio", 0.50))
    _THRESHOLDS["warning_maintenance_ratio"] = float(thresholds.get("warning_maintenance_ratio", 0.20))

    base_kws = set(measure_cfg.get("unresolved_keywords", _UNRESOLVED_KEYWORDS))
    remove_kws = set(measure_cfg.get("unresolved_keywords_remove", []))
    _UNRESOLVED_KEYWORDS = base_kws - remove_kws


_THRESHOLDS: dict = {
    "critical_maintenance_ratio": 0.50,
    "warning_maintenance_ratio": 0.20,
}
_UNRESOLVED_KEYWORDS: set = {
    "couldn't", "failed", "partial", "gave up", "unclear", "not completed", "no resolution"
}
_load_measure_calibration()  # Load community defaults on import


def _open_annotations_with_retry(
    path: Path,
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 60.0,
):
    """Open an annotation file, tolerating the brief perm race where
    the file was just written at mode 600 and the set_evolve_read_acl
    daemon hasn't applied yet.  Retries with linear backoff.

    Three attempts × 60s/120s linear backoff = up to ~3 min worst case.
    That's tolerable inside a daily cron that has 30-min jitter ahead of it.

    Raises:
        PermissionError  — if all attempts fail (re-raised from last attempt)
        OSError (other)  — raised immediately, not retried
    """
    last_err: PermissionError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return path.open()
        except PermissionError as e:
            last_err = e
            if attempt < max_attempts:
                delay = base_delay_seconds * attempt
                print(
                    f"[measure] PermissionError reading {path}; retrying in "
                    f"{delay:.0f}s (attempt {attempt}/{max_attempts})",
                    file=sys.stderr,
                )
                time.sleep(delay)
    assert last_err is not None
    raise last_err


def load_annotations(shared_dir: str, bot_id: str, target_date: date) -> list[dict]:
    """Load all turn annotations for a bot on a given local date."""
    annotation_file = Path(shared_dir) / "annotations" / bot_id / f"{target_date}.jsonl"
    if not annotation_file.exists():
        return []
    annotations = []
    with _open_annotations_with_retry(annotation_file) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    annotations.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return annotations


def compute_application_metrics(annotations: list[dict]) -> dict:
    """
    Aggregate per-application usage stats from session_summary records.

    Returns a dict keyed by application name:
        {app_name: {sessions, correction_sessions, efficiency_sessions, promise_sessions}}

    Only session_summary records are used — turn records don't carry
    application data (that's computed at session close by SessionSummarizer).
    """
    cap_stats: dict[str, dict] = {}

    for ann in annotations:
        if ann.get("type") != "session_summary":
            continue

        caps = ann.get("applications_invoked", [])
        has_correction = ann.get("correction_count", 0) > 0
        has_efficiency_flag = ann.get("efficiency_flag", False)
        has_promise = bool(ann.get("promises_made"))
        outcome = ann.get("outcome", "").lower()
        # Heuristic: session was abandoned/unresolved (keywords come from calibration)
        unresolved_keywords = _UNRESOLVED_KEYWORDS
        is_unresolved = any(kw in outcome for kw in unresolved_keywords)

        for cap in caps:
            if cap not in cap_stats:
                cap_stats[cap] = {
                    "sessions": 0,
                    "correction_sessions": 0,
                    "efficiency_sessions": 0,
                    "promise_sessions": 0,
                    "unresolved_sessions": 0,
                }
            s = cap_stats[cap]
            s["sessions"] += 1
            if has_correction:
                s["correction_sessions"] += 1
            if has_efficiency_flag:
                s["efficiency_sessions"] += 1
            if has_promise:
                s["promise_sessions"] += 1
            if is_unresolved:
                s["unresolved_sessions"] += 1

    return cap_stats


def compute_app_metrics(annotations: list[dict], session_app_map: dict) -> dict:
    """
    Aggregate per-app usage stats using the session-to-app map from the correlator.

    Parameters
    ----------
    annotations     : all annotation records for the day
    session_app_map : {session_id: [app_id, ...]} from correlate_sessions()

    Returns
    -------
    {app_id: {sessions, correction_sessions, efficiency_sessions,
              unresolved_sessions, productive_sessions, maintenance_sessions}}

    Session class (productive/maintenance) is derived via majority vote across
    turn_annotation records, mirroring the logic in compute_session_metrics().
    """
    if not session_app_map:
        return {}

    # Index turn annotations and session summaries by session_id
    turn_anns_by_sid:  dict[str, list[dict]] = {}
    summaries_by_sid:  dict[str, list[dict]] = {}
    for ann in annotations:
        sid = ann.get("session_id") or "unknown"
        if ann.get("type") == "turn_annotation":
            turn_anns_by_sid.setdefault(sid, []).append(ann)
        elif ann.get("type") == "session_summary":
            summaries_by_sid.setdefault(sid, []).append(ann)

    app_stats: dict[str, dict] = {}

    for sid, app_ids in session_app_map.items():
        if not app_ids:
            continue

        turn_anns = turn_anns_by_sid.get(sid, [])
        summaries  = summaries_by_sid.get(sid, [])

        # Session class via majority vote across turn annotations
        t_prod  = sum(1 for t in turn_anns if t.get("session_class") == "productive")
        t_maint = sum(1 for t in turn_anns if t.get("session_class") == "maintenance")
        if t_prod > t_maint:
            session_class = "productive"
        elif t_maint > t_prod:
            session_class = "maintenance"
        else:
            session_class = "ambiguous"

        # Session-level quality flags from the last session_summary
        last_summary = summaries[-1] if summaries else {}
        has_correction  = (last_summary.get("correction_count", 0) or 0) > 0
        has_efficiency  = bool(last_summary.get("efficiency_flag"))
        outcome         = (last_summary.get("outcome") or "").lower()
        is_unresolved   = any(kw in outcome for kw in _UNRESOLVED_KEYWORDS)

        for app_id in app_ids:
            if app_id not in app_stats:
                app_stats[app_id] = {
                    "sessions": 0,
                    "correction_sessions": 0,
                    "efficiency_sessions": 0,
                    "unresolved_sessions": 0,
                    "productive_sessions": 0,
                    "maintenance_sessions": 0,
                }
            s = app_stats[app_id]
            s["sessions"] += 1
            if has_correction:
                s["correction_sessions"] += 1
            if has_efficiency:
                s["efficiency_sessions"] += 1
            if is_unresolved:
                s["unresolved_sessions"] += 1
            if session_class == "productive":
                s["productive_sessions"] += 1
            elif session_class == "maintenance":
                s["maintenance_sessions"] += 1

    return app_stats


def compute_session_metrics(annotations: list[dict]) -> dict:
    """Group annotations by session and compute session-level metrics."""
    sessions: dict[str, list[dict]] = {}
    for ann in annotations:
        sid = ann.get("session_id") or "unknown"
        sessions.setdefault(sid, []).append(ann)

    productive = maintenance_count = ambiguous = 0
    first_response_resolutions = 0
    corrections = 0
    efficiency_flag_count = 0
    total_cost = 0.0
    unexpected_billing_turns = 0  # turns flagged by plugin as unexpected billing mode (e.g. Anthropic MAX drift)
    provider_cost: dict[str, float] = {}
    # For top_maintenance_signals: collect class_signals from maintenance-class turns
    maintenance_signal_counts: dict[str, int] = {}

    for sid, records in sessions.items():
        # Separate turn-level and session-level records
        turn_anns = [r for r in records if r.get("type") == "turn_annotation"]
        summaries  = [r for r in records if r.get("type") == "session_summary"]

        # Session class = majority vote across turn annotations.
        # (session_summary.session_class is derived from the same turns, so
        # we prefer the granular turn data when available.)
        t1 = sum(1 for t in turn_anns if t.get("session_class") == "productive")
        t2 = sum(1 for t in turn_anns if t.get("session_class") == "maintenance")
        if t1 > t2:
            session_class = "productive"
            productive += 1
        elif t2 > t1:
            session_class = "maintenance"
            maintenance_count += 1
        else:
            session_class = "ambiguous"
            ambiguous += 1

        # First-response resolution: session completed in a single turn.
        # Read from session_summary (written by SessionSummarizer at session close).
        # Use the LAST summary in case session_end fired more than once (crash/restart).
        if summaries and summaries[-1].get("first_response_resolution"):
            first_response_resolutions += 1

        # Efficiency flag: read from session_summary (last record).
        if summaries and summaries[-1].get("efficiency_flag"):
            efficiency_flag_count += 1

        corrections += sum(1 for t in turn_anns if t.get("correction_detected"))

        # Collect class_signals from maintenance turns for top_maintenance_signals.
        if session_class == "maintenance":
            for t in turn_anns:
                for sig in (t.get("class_signals") or []):
                    if isinstance(sig, str) and sig:
                        maintenance_signal_counts[sig] = maintenance_signal_counts.get(sig, 0) + 1

        for t in turn_anns:
            cost = t.get("cost_estimated", 0) or 0
            total_cost += cost
            # Per-provider cost breakdown.
            # Plugin writes model_selected; fall back to model for older records.
            model_str = t.get("model_selected") or t.get("model") or ""
            provider = model_str.split("/")[0] if "/" in model_str else "unknown"
            provider_cost[provider] = provider_cost.get(provider, 0.0) + cost
            # unexpected_billing_mode is set by the OC plugin when a bot unexpectedly
            # falls to a more expensive billing mode (e.g. Anthropic MAX → API key drift).
            if t.get("unexpected_billing_mode"):
                unexpected_billing_turns += 1

    # Top 5 maintenance signals by frequency
    top_maintenance_signals = sorted(
        maintenance_signal_counts.items(), key=lambda x: -x[1]
    )[:5]

    session_count = len(sessions)
    # Denominator is all sessions (including ambiguous) so the ratio reflects
    # actual share of bot time spent on maintenance, not just decided sessions.
    # Using only productive+maintenance would inflate the ratio when many sessions
    # are ambiguous (low-confidence), triggering false "critical" status.
    maintenance_ratio = maintenance_count / session_count if session_count > 0 else 0.0

    if maintenance_ratio > _THRESHOLDS["critical_maintenance_ratio"]:
        status = "critical"
    elif maintenance_ratio > _THRESHOLDS["warning_maintenance_ratio"] or unexpected_billing_turns > 0:
        status = "warning"
    else:
        status = "ok"

    return {
        "session_count": session_count,
        # Count only turn_annotation records; session_summary records are also in
        # the file and must not inflate the turn count.
        "turn_count": sum(1 for a in annotations if a.get("type") == "turn_annotation"),
        "productive_sessions": productive,
        "maintenance_sessions": maintenance_count,
        "ambiguous_sessions": ambiguous,
        "maintenance_ratio": round(maintenance_ratio, 3),
        "first_response_resolutions": first_response_resolutions,
        "correction_count": corrections,
        "efficiency_flag_count": efficiency_flag_count,
        "top_maintenance_signals": top_maintenance_signals,  # [[signal, count], ...] top 5
        "total_cost_estimated": round(total_cost, 4),
        "provider_cost": {k: round(v, 6) for k, v in provider_cost.items()},
        "unexpected_billing_turns": unexpected_billing_turns,
        "status": status,
    }


def measure_bot(bot_id: str, shared_dir: str, target_date: date) -> dict:
    """Compute full metrics for one bot on one date."""
    annotations = load_annotations(shared_dir, bot_id, target_date)
    session_metrics = compute_session_metrics(annotations)
    application_metrics = compute_application_metrics(annotations)

    # Phase 6: app-session correlation via file mtime
    session_app_map: dict = {}
    if correlate_sessions is not None:
        try:
            session_app_map = correlate_sessions(annotations, bot_id, shared_dir)
        except Exception as e:
            print(f"[measure] app correlation failed (non-fatal): {e}")

    app_metrics = compute_app_metrics(annotations, session_app_map)

    return {
        "schema_version": 2,
        "bot_id": bot_id,
        "date": str(target_date),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **session_metrics,
        "application_usage": application_metrics,
        "app_usage": app_metrics,
    }


def write_metrics(metrics: dict, shared_dir: str, bot_id: str, target_date: date) -> None:
    """Atomically write metrics JSON to the shared directory."""
    out_dir = Path(shared_dir) / "metrics" / str(target_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{bot_id}.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(metrics, indent=2))
    tmp_path.rename(out_path)
    print(f"[evolve/measure] {bot_id} {target_date}: {metrics['status']} "
          f"({metrics['session_count']} sessions, "
          f"maintenance={metrics['maintenance_ratio']:.0%})")


def main():
    parser = argparse.ArgumentParser(description="Evolve daily metrics measurement")
    parser.add_argument("--bot-id", help="Bot ID to measure (default: all bots in network)")
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR),
                        help="Path to shared evolve directory")
    parser.add_argument("--date", default=str(date.today()),
                        help="Date to measure (YYYY-MM-DD, default: today)")
    parser.add_argument("--network", default=str(resolve_network_path()), help="Path to network.json config")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date)
    shared_dir = args.shared_dir
    _cfg = load_config(args.network)

    # Reload calibration with correct shared_dir (may differ from default)
    _load_measure_calibration(shared_dir)

    if not is_module_enabled(_cfg, "metrics"):
        print("[measure] metrics module is disabled — exiting.")
        sys.exit(0)

    # Determine which bots to measure.
    # --bot-id takes priority: per-bot launchd plists pass this to scope a single bot.
    if args.bot_id:
        bots = [args.bot_id]
    elif args.network:
        network_file = Path(args.network)
        if not network_file.exists():
            print(f"[evolve/measure] network file not found: {args.network}", file=sys.stderr)
            sys.exit(1)
        network = json.loads(network_file.read_text())
        shared_dir = network.get("sharedDir", shared_dir)
        bots = network.get("members", [])
        # evolve bot now has the plugin installed — include it in daily measurement.
        if "evolve" not in bots:
            bots = bots + ["evolve"]
    else:
        # Discover from annotation directories
        annotation_base = Path(shared_dir) / "annotations"
        bots = [d.name for d in annotation_base.iterdir() if d.is_dir()] if annotation_base.exists() else []

    if not bots:
        print("[evolve/measure] No bots to measure", file=sys.stderr)
        sys.exit(1)

    for bot_id in bots:
        metrics = measure_bot(bot_id, shared_dir, target_date)
        write_metrics(metrics, shared_dir, bot_id, target_date)

    print(f"[evolve/measure] Done — measured {len(bots)} bot(s)")

    # Value-baseline post-step (internal/spec-value-baseline-2026-06-10.md §5.1
    # Option A): pod-wide utilization rollup + bot_underused signals, fed by
    # the per-bot files this run just wrote. Full-fleet runs only — a
    # --bot-id run would write a partial pod rollup and sweep-resolve the
    # other bots' still-valid signals. Non-fatal: a baseline failure must
    # not take down the metrics aggregation it rides on.
    if not args.bot_id:
        try:
            import value_baseline
            value_baseline.run_nightly(
                Path(shared_dir), list(bots), network=_cfg, today=target_date
            )
        except Exception as e:
            print(f"[measure] value_baseline post-step failed (non-fatal): {e}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
