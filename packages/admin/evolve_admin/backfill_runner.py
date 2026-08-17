#!/usr/bin/env python3
"""
evolve_admin/backfill_runner.py — Historical metrics backfill from OC turn logs.

Runs as the bot user (via sudo -u <bot>) so it can read
/Users/<bot>/.openclaw/workspace/memory/turns-YYYY-MM-DD.jsonl

Usage (invoked by `evolve-admin backfill`):
    python3 -m evolve_admin.backfill_runner \
        --bot-id admin_bot --days 30 --shared-dir /Users/Shared/evolve
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from .config import bot_home as _bot_home


COST_PER_TURN_ESTIMATE = 0.0003  # fallback estimate when cost field absent


def _turn_log_path(bot_id: str, target_date: date) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "workspace" / "memory" / f"turns-{target_date}.jsonl"


def _metrics_path(shared_dir: Path, bot_id: str, target_date: date) -> Path:
    return shared_dir / "metrics" / str(target_date) / f"{bot_id}.json"


def _read_turns(bot_id: str, target_date: date) -> list[dict]:
    log_path = _turn_log_path(bot_id, target_date)
    if not log_path.exists():
        return []
    turns = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return turns


def backfill_day(bot_id: str, target_date: date, shared_dir: Path) -> dict | None:
    """
    Read OC turn logs for one day and produce a metrics dict.
    Returns None if no data found.
    """
    turns = _read_turns(bot_id, target_date)
    if not turns:
        return None

    sessions: dict[str, list[dict]] = {}
    for t in turns:
        sid = t.get("session_id", "unknown")
        sessions.setdefault(sid, []).append(t)

    session_count = len(sessions)
    turn_count = len(turns)

    total_input_tokens = sum(t.get("input_tokens", 0) for t in turns)
    total_output_tokens = sum(t.get("output_tokens", 0) for t in turns)
    total_cache_read = sum(t.get("cache_read_tokens", 0) for t in turns)
    total_cache_write = sum(t.get("cache_write_tokens", 0) for t in turns)

    # Use actual cost if present, else estimate
    total_cost = sum(t.get("cost", 0) for t in turns)
    if total_cost == 0:
        total_cost = turn_count * COST_PER_TURN_ESTIMATE

    # Per-provider cost breakdown
    provider_cost: dict[str, float] = {}
    for t in turns:
        model = t.get("model", "") or ""
        provider = model.split("/")[0] if "/" in model else "unknown"
        provider_cost[provider] = provider_cost.get(provider, 0.0) + float(t.get("cost", 0))

    # unexpected_billing_mode is set by the plugin for provider-specific billing drift
    unexpected_billing_turns = sum(1 for t in turns if t.get("unexpected_billing_mode"))

    # Models used (most common)
    model_counts: dict[str, int] = {}
    for t in turns:
        m = t.get("model", "")
        if m:
            model_counts[m] = model_counts.get(m, 0) + 1
    primary_model = max(model_counts, key=lambda k: model_counts[k]) if model_counts else None

    status = "warning" if unexpected_billing_turns > 0 else "ok"

    return {
        "schema_version": 2,
        "bot_id": bot_id,
        "date": str(target_date),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Session counts — can't classify productive/maintenance without annotations
        "session_count": session_count,
        "turn_count": turn_count,
        "productive_sessions": 0,
        "maintenance_sessions": 0,
        "ambiguous_sessions": session_count,
        "maintenance_ratio": 0.0,
        "first_response_resolutions": 0,
        "correction_count": 0,
        "total_cost_estimated": round(total_cost, 6),
        "provider_cost": {k: round(v, 6) for k, v in provider_cost.items()},
        "unexpected_billing_turns": unexpected_billing_turns,
        "status": status,
        "application_usage": {},
        # Token counts from OC logs
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_write_tokens": total_cache_write,
        "primary_model": primary_model,
        # Backfill metadata
        "source": "backfill",
        "backfill": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Evolve metrics from OC turn logs")
    parser.add_argument("--bot-id", required=True, help="Bot ID (macOS username)")
    parser.add_argument("--days", type=int, default=30, help="Number of days to backfill")
    parser.add_argument("--shared-dir", default="/Users/Shared/evolve",
                        help="Path to shared evolve directory")
    args = parser.parse_args()

    bot_id = args.bot_id
    shared_dir = Path(args.shared_dir)
    today = date.today()

    written = 0
    skipped_existing = 0
    skipped_no_data = 0

    for i in range(args.days, 0, -1):
        target_date = today - timedelta(days=i)
        out_path = _metrics_path(shared_dir, bot_id, target_date)

        # Skip if real metrics file already exists
        if out_path.exists():
            existing = {}
            try:
                existing = json.loads(out_path.read_text())
            except Exception:
                pass
            if not existing.get("backfill"):
                skipped_existing += 1
                continue

        metrics = backfill_day(bot_id, target_date, shared_dir)
        if metrics is None:
            skipped_no_data += 1
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(metrics, indent=2))
        tmp.rename(out_path)
        written += 1
        print(f"[backfill] {bot_id} {target_date}: "
              f"{metrics['session_count']} sessions, "
              f"{metrics['turn_count']} turns, "
              f"${metrics['total_cost_estimated']:.4f} cost")

    print(f"[backfill] Done — {written} written, "
          f"{skipped_existing} skipped (real data exists), "
          f"{skipped_no_data} skipped (no OC logs)")


if __name__ == "__main__":
    main()
