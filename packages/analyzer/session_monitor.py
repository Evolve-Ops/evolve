"""
session_monitor.py — Runaway session detection for Evolve pods.

Reads annotation JSONL files and identifies sessions whose total input-token
accumulation exceeds a configured threshold. These are the "cost bombs" that
cause unexpected spend spikes.

Primary metric: total_input_tokens per session (sum across all turns).
Secondary: initial_context_tokens (max cache_write_tokens seen — the upload cost).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


# ── Session stats computation ─────────────────────────────────────────────────

def compute_session_stats(
    shared_dir: Path,
    bot_id: str,
    days: int = 7,
) -> list[dict]:
    """Return per-session stats for bot_id over the last N days.

    Each entry: {
      session_id, bot_id, date, session_class,
      initial_context_tokens,  # max cache_write_tokens seen — peak cache upload size
      max_input_tokens,        # max input_tokens in a single turn — peak context window
      total_input_tokens,      # sum of input_tokens across all turns — total token exposure
      total_output_tokens,
      total_cost, turn_count,
      first_turn_ts, last_turn_ts
    }
    Sorted by total_cost descending.
    """
    ann_dir = shared_dir / "annotations" / bot_id
    if not ann_dir.exists():
        return []

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    sessions: dict[str, dict] = {}

    for jf in sorted(ann_dir.glob("*.jsonl")):
        day_str = jf.stem[:10]
        if day_str < cutoff:
            continue
        for rec in _read_jsonl(jf):
            if rec.get("type") != "turn_annotation":
                continue
            sid = rec.get("session_id", "")
            if not sid:
                continue

            ts = rec.get("ts", "")
            input_tok = int(rec.get("input_tokens", 0) or 0)
            output_tok = int(rec.get("output_tokens", 0) or 0)
            cache_write = int(rec.get("cache_write_tokens", 0) or 0)
            cost = float(rec.get("cost_estimated", 0) or 0)
            sc = rec.get("session_class") or "ambiguous"

            if sid not in sessions:
                sessions[sid] = {
                    "session_id": sid,
                    "bot_id": bot_id,
                    "date": day_str,
                    "session_class": sc,
                    "initial_context_tokens": 0,
                    "max_input_tokens": 0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost": 0.0,
                    "turn_count": 0,
                    "first_turn_ts": ts,
                    "last_turn_ts": ts,
                }
            s = sessions[sid]
            s["total_input_tokens"] += input_tok
            s["total_output_tokens"] += output_tok
            s["total_cost"] += cost
            s["turn_count"] += 1
            if input_tok > s["max_input_tokens"]:
                s["max_input_tokens"] = input_tok
            if cache_write > s["initial_context_tokens"]:
                s["initial_context_tokens"] = cache_write
            if ts and (not s["first_turn_ts"] or ts < s["first_turn_ts"]):
                s["first_turn_ts"] = ts
            if ts and ts > s["last_turn_ts"]:
                s["last_turn_ts"] = ts
            # Keep most common session class
            if sc == "productive":
                s["session_class"] = sc

    for s in sessions.values():
        s["total_cost"] = round(s["total_cost"], 6)

    return sorted(sessions.values(), key=lambda x: -x["total_cost"])


def detect_runaway_sessions(
    shared_dir: Path,
    bot_id: str,
    threshold_tokens: int = 100_000,
    days: int = 7,
) -> dict:
    """Return flagged sessions exceeding threshold_tokens total input tokens.

    Returns: {
      threshold_tokens, flagged_sessions: [...],
      summary: {total_sessions_checked, flagged_count, flagged_cost, flagged_pct_of_total_cost}
    }
    """
    all_sessions = compute_session_stats(shared_dir, bot_id, days)
    flagged = [s for s in all_sessions if s["total_input_tokens"] >= threshold_tokens]

    total_cost = sum(s["total_cost"] for s in all_sessions)
    flagged_cost = sum(s["total_cost"] for s in flagged)

    return {
        "threshold_tokens": threshold_tokens,
        "flagged_sessions": flagged,
        "summary": {
            "total_sessions_checked": len(all_sessions),
            "flagged_count": len(flagged),
            "flagged_cost": round(flagged_cost, 4),
            "total_cost": round(total_cost, 4),
            "flagged_pct_of_total_cost": round(
                100 * flagged_cost / total_cost if total_cost > 0 else 0, 1
            ),
        },
    }


# ── Turn cost audit ───────────────────────────────────────────────────────────

def get_top_cost_turns(
    shared_dir: Path,
    bot_ids: list[str],
    days: int = 7,
    limit: int = 25,
    sort_by: str = "ts",
) -> list[dict]:
    """Return individual turns across all bots, sorted by ts (recent first)
    or cost_estimated (most expensive first).

    Each entry: {ts, bot_id, model, source, session_class, input_tokens,
                 output_tokens, cache_read_tokens, cache_write_tokens,
                 cost_estimated, session_id, turn_id}.

    sort_by: "ts" (default — most recent first) or "cost".
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    turns: list[dict] = []

    for bot_id in bot_ids:
        ann_dir = shared_dir / "annotations" / bot_id
        if not ann_dir.exists():
            continue
        for jf in sorted(ann_dir.glob("*.jsonl")):
            day_str = jf.stem[:10]
            if day_str < cutoff:
                continue
            for rec in _read_jsonl(jf):
                if rec.get("type") != "turn_annotation":
                    continue
                cost = float(rec.get("cost_estimated", 0) or 0)
                if cost <= 0:
                    continue
                turns.append({
                    "ts": rec.get("ts", ""),
                    "bot_id": bot_id,
                    "model": rec.get("model_selected", ""),
                    "source": rec.get("source", ""),
                    "session_class": rec.get("session_class", ""),
                    "model_tier": rec.get("model_tier", ""),
                    "input_tokens": int(rec.get("input_tokens", 0) or 0),
                    "output_tokens": int(rec.get("output_tokens", 0) or 0),
                    "cache_read_tokens": int(rec.get("cache_read_tokens", 0) or 0),
                    "cache_write_tokens": int(rec.get("cache_write_tokens", 0) or 0),
                    "cost_estimated": round(cost, 6),
                    "session_id": rec.get("session_id", ""),
                    "turn_id": rec.get("turn_id", ""),
                })

    if sort_by == "cost":
        turns.sort(key=lambda t: -t["cost_estimated"])
    else:
        turns.sort(key=lambda t: t["ts"], reverse=True)
    return turns[:limit]


# ── Cost efficiency score computation ─────────────────────────────────────────

def compute_cost_score(
    shared_dir: Path,
    bot_id: str,
    openclaw_settings: dict | None,
    days: int = 7,
) -> dict:
    """Behavioral cost efficiency score (0–100) for a bot.

    Pure outcome measurement — every component measures what actually
    happened over the last ``days`` window. The per-bot Context & Session
    Settings matrix on the same page already shows configuration; this
    score answers the complementary question, "did those choices pay
    off?" so a conservatively-configured bot whose cache is thrashing
    can score worse than an aggressively-configured one whose cache is
    hitting 95%.

    Components (totaling 100 pts):
      - Cache hit ratio        (30) — billable tokens served from cache
      - Heartbeat tax          (25) — share of spend on heartbeat turns
      - Tier discipline        (20) — maintenance turns on tier3
      - Runaway sessions       (25) — sessions exceeding 100k input tokens

    No-data fallback gives ~80% credit per component so brand-new bots
    start around a B, not an F. ``openclaw_settings`` is accepted for
    signature compatibility but no longer scored directly.
    """
    del openclaw_settings  # signature-compatible; matrix owns configuration display
    components: list[dict] = []

    # Gather every turn annotation in the window once; each component then
    # filters from the in-memory list rather than re-scanning JSONL files.
    ann_dir = shared_dir / "annotations" / bot_id
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    turns: list[dict] = []
    if ann_dir.exists():
        for jf in sorted(ann_dir.glob("*.jsonl")):
            if jf.stem[:10] < cutoff:
                continue
            for rec in _read_jsonl(jf):
                if rec.get("type") == "turn_annotation":
                    turns.append(rec)

    # ── 1. Cache hit ratio (30 pts) ──
    # cache_read / (cache_read + cache_write + input). Pass at 50%, full
    # credit at 80%. Below 50% means the prefix is rarely getting cached —
    # usually session churn (heartbeats fragmenting sessions) or prompt
    # churn (memory files changing turn-to-turn).
    cr = sum(int(t.get("cache_read_tokens", 0) or 0) for t in turns)
    cw = sum(int(t.get("cache_write_tokens", 0) or 0) for t in turns)
    inp = sum(int(t.get("input_tokens", 0) or 0) for t in turns)
    total_billable = cr + cw + inp
    if total_billable == 0:
        components.append({
            "name": "Cache hit ratio",
            "score": 24, "max": 30, "pass": True,
            "detail": "Not enough turns yet to measure",
            "fix": None,
        })
    else:
        ratio = cr / total_billable
        pts = round(30 * min(ratio / 0.80, 1.0))
        ok = ratio >= 0.50
        components.append({
            "name": "Cache hit ratio",
            "score": pts, "max": 30, "pass": ok,
            "detail": f"{round(ratio * 100)}% of billable tokens served from cache",
            "fix": None if ok else (
                "Low cache reuse. Enable Light Context Mode so heartbeats stop "
                "fragmenting sessions, and raise Reserve floor so compaction "
                "doesn't invalidate the cached prefix as often."
            ),
            "fix_field_keys": [] if ok else [
                "heartbeat.lightContext",
                "compaction.reserveTokensFloor",
            ],
        })

    # ── 2. Heartbeat tax (25 pts) ──
    # Share of cost on turns where source/channel marks the trigger as a
    # heartbeat. Full credit at ≤15%, pass at ≤30%, zero at ≥60%, linear
    # between. Some ambient bots will legitimately have high heartbeat
    # tax — that's fine; the metric flags "your heartbeats are expensive
    # relative to your real work," which is usually fixable.
    def _is_heartbeat(t: dict) -> bool:
        src = (t.get("source") or "").strip().lower()
        ch = (t.get("channel") or "").strip().lower()
        return src == "heartbeat" or ch == "heartbeat"
    hb_cost = sum(float(t.get("cost_estimated", 0) or 0) for t in turns if _is_heartbeat(t))
    total_cost = sum(float(t.get("cost_estimated", 0) or 0) for t in turns)
    if total_cost == 0:
        components.append({
            "name": "Heartbeat tax",
            "score": 20, "max": 25, "pass": True,
            "detail": "No cost data yet",
            "fix": None,
        })
    else:
        tax = hb_cost / total_cost
        if tax <= 0.15:
            pts = 25
        elif tax >= 0.60:
            pts = 0
        else:
            pts = round(25 * (0.60 - tax) / (0.60 - 0.15))
        ok = tax <= 0.30
        components.append({
            "name": "Heartbeat tax",
            "score": pts, "max": 25, "pass": ok,
            "detail": f"{round(tax * 100)}% of spend on heartbeat turns",
            "fix": None if ok else (
                "Heartbeats are eating budget. Light Context Mode + Session "
                "Isolation together typically cut heartbeat cost ~95%."
            ),
            "fix_field_keys": [] if ok else [
                "heartbeat.lightContext",
                "heartbeat.isolatedSession",
            ],
        })

    # ── 3. Tier discipline (20 pts) ──
    # Share of maintenance-classified turns routed to tier3 (the cheap
    # tier). Pass at 80%, full credit at 95%. When the classifier is
    # producing maintenance turns but they're not landing on tier3, the
    # fix is usually the AI Optimization tier3 model mapping, not a
    # cost-page setting — so this component links there, not the matrix.
    maint = [t for t in turns if t.get("session_class") == "maintenance"]
    if not maint:
        components.append({
            "name": "Tier discipline",
            "score": 16, "max": 20, "pass": True,
            "detail": "No maintenance turns yet — check back after bot activity",
            "fix": None,
        })
    else:
        on_t3 = sum(1 for t in maint if t.get("model_tier") == "tier3")
        rate = on_t3 / len(maint)
        pts = round(20 * min(rate / 0.95, 1.0))
        ok = rate >= 0.80
        components.append({
            "name": "Tier discipline",
            "score": pts, "max": 20, "pass": ok,
            "detail": f"{round(rate * 100)}% of maintenance turns on tier3 ({on_t3}/{len(maint)})",
            "fix": None if ok else (
                "Maintenance work isn't being downgraded. Check AI Optimization "
                "— tier3 should map to a low-cost model (e.g. Haiku)."
            ),
            "fix_page": None if ok else "ai-optimization",
        })

    # ── 4. Runaway sessions (25 pts) ──
    # Sessions exceeding 100k input tokens — clear sign that compaction /
    # pruning aren't keeping up, or memory files are massive. 25 at 0,
    # drop 8 per offending session.
    threshold = 100_000
    session_input: dict[str, int] = {}
    for t in turns:
        sid = t.get("session_id", "")
        if sid:
            session_input[sid] = session_input.get(sid, 0) + int(t.get("input_tokens", 0) or 0)
    runaway_count = sum(1 for tot in session_input.values() if tot >= threshold)
    if not session_input:
        components.append({
            "name": "Runaway sessions",
            "score": 20, "max": 25, "pass": True,
            "detail": "No session data yet",
            "fix": None,
        })
    elif runaway_count == 0:
        components.append({
            "name": "Runaway sessions",
            "score": 25, "max": 25, "pass": True,
            "detail": f"No sessions exceeded {threshold:,} tokens",
            "fix": None,
        })
    else:
        pts = max(0, 25 - runaway_count * 8)
        components.append({
            "name": "Runaway sessions",
            "score": pts, "max": 25, "pass": False,
            "detail": f"{runaway_count} session(s) exceeded {threshold:,} tokens in last {days} days",
            "fix": (
                "Sessions are growing unbounded. Raise Reserve floor and lower "
                "Memory flush threshold so compaction fires sooner; consider "
                "tightening Bootstrap max chars if memory files are heavy."
            ),
            "fix_field_keys": [
                "compaction.reserveTokensFloor",
                "compaction.memoryFlush.softThresholdTokens",
                "bootstrapTotalMaxChars",
            ],
        })

    total = sum(c["score"] for c in components)
    grade, label = _grade(total)
    return {
        "bot_id": bot_id,
        "score": total,
        "grade": grade,
        "label": label,
        "components": components,
        "window_days": days,
    }


def _grade(score: int) -> tuple[str, str]:
    if score >= 90:
        return "A", "Strong — bot is spending efficiently"
    if score >= 75:
        return "B", "Good — minor inefficiencies"
    if score >= 60:
        return "C", "Moderate — notable savings available"
    if score >= 40:
        return "D", "Poor — significant waste detected"
    return "F", "Critical — bot is bleeding budget"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return records
