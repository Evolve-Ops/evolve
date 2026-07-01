"""Aggregate diagnose results + format human-readable + JSON output."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from .probes import (
    probe_network,
    probe_process,
    probe_subprocess,
    probe_schema,
)

CATEGORIES = {
    "network": probe_network,
    "process": probe_process,
    "subprocess": probe_subprocess,
    "schema": probe_schema,
}


def run_all(categories: list[str] | None = None) -> dict:
    """Run the named probes. Default: all of them. Returns aggregate report."""
    if categories is None:
        categories = list(CATEGORIES.keys())
    started = time.time()
    results: dict = {}
    for cat in categories:
        if cat not in CATEGORIES:
            results[cat] = {
                "status": "unknown",
                "observed": {},
                "notes": [f"unknown category: {cat}"],
                "baseline": {},
            }
            continue
        try:
            results[cat] = CATEGORIES[cat]()
        except Exception as e:
            results[cat] = {
                "status": "unknown",
                "observed": {},
                "notes": [f"probe raised exception: {type(e).__name__}: {e}"],
                "baseline": {},
            }
    elapsed = time.time() - started
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(elapsed, 2),
        "categories": results,
        "verdict": aggregate_verdict(results),
    }


def aggregate_verdict(results: dict) -> dict:
    """Compute the one-line summary verdict from per-probe statuses."""
    statuses = {cat: r.get("status", "unknown") for cat, r in results.items()}
    if any(s == "unhealthy" for s in statuses.values()):
        overall = "unhealthy"
    elif any(s == "anomaly" for s in statuses.values()):
        overall = "anomaly"
    elif all(s == "healthy" for s in statuses.values()):
        overall = "healthy"
    else:
        overall = "mixed"   # at least one 'unknown' but no 'unhealthy'/'anomaly'
    return {
        "overall": overall,
        "by_category": statuses,
        "summary": _summary_line(statuses, overall),
    }


def _summary_line(statuses: dict, overall: str) -> str:
    parts = [f"{cat}={status}" for cat, status in statuses.items()]
    icon = {"healthy": "✓", "anomaly": "⚠", "unhealthy": "✗", "mixed": "?", "unknown": "?"}[overall]
    return f"diagnose: {' '.join(parts)} — overall {icon} {overall}"


def format_report(result: dict) -> str:
    """Human-readable text report. Sections per category, verdict at the end."""
    lines = ["evolve-admin diagnose"]
    lines.append("=" * 21)
    lines.append(f"Time:      {result['ts']}")
    lines.append(f"Elapsed:   {result['elapsed_s']}s")
    lines.append("")

    for cat, data in result["categories"].items():
        status = data.get("status", "unknown")
        icon = {"healthy": "✓", "anomaly": "⚠", "unhealthy": "✗", "unknown": "?"}.get(status, "?")
        lines.append(f"── {cat} ── {icon} {status}")
        # Observed values (compact)
        for k, v in (data.get("observed") or {}).items():
            if isinstance(v, dict):
                inner = ", ".join(f"{ik}={iv}" for ik, iv in v.items())
                lines.append(f"  {k}: {{{inner}}}")
            elif isinstance(v, list):
                if not v:
                    lines.append(f"  {k}: (empty)")
                elif len(v) <= 5:
                    lines.append(f"  {k}: {v}")
                else:
                    lines.append(f"  {k}: {v[:5]}... (+{len(v)-5} more)")
            else:
                lines.append(f"  {k}: {v}")
        # Notes
        for note in (data.get("notes") or []):
            lines.append(f"  ! {note}")
        lines.append("")

    lines.append("=" * 21)
    lines.append(result["verdict"]["summary"])
    return "\n".join(lines)


def format_json(result: dict) -> str:
    """JSON output — for piping into a finding's Evidence section."""
    return json.dumps(result, indent=2, default=str)
