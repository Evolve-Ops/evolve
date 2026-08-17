#!/usr/bin/env python3
"""
weekly_review.py — RSI process health report.

Runs Sunday morning. Focuses exclusively on the RSI (Recursive Self-Improvement)
pipeline inside the Better Engine: proposal velocity, funnel health, approval
rates, rejection patterns, and cycle health score. Operational metrics (cost,
liveness, etc.) are covered by pod_report.py — this report is about whether the
Better Engine is working.

Output: /Users/Shared/evolve/reviews/YYYY-MM-DD.md
Sends:  via `openclaw message send` (primary channel, not hardcoded Telegram)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

try:
    from evolve_config import load_config, get_shared_dir, resolve_network_path
    from models import resolve_tier
except ImportError:
    def load_config(n=None): return json.loads(Path("/Users/Shared/evolve/network.json").read_text()) if Path("/Users/Shared/evolve/network.json").exists() else {}  # type: ignore[misc]
    def get_shared_dir(c): return Path("/Users/Shared/evolve")  # type: ignore[misc]
    def resolve_network_path(o=None): return Path("/Users/Shared/evolve/network.json")  # type: ignore[misc]
    def resolve_tier(t, c): return "anthropic/claude-haiku-4-5"  # type: ignore[misc]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return records


def _all_proposals(shared_dir: Path, stages: list[str] | None = None) -> list[dict]:
    """Read all proposal files across specified stages (default: proposal lifecycle stages).

    Note: apply-results/ is intentionally excluded — those are outcome records with a
    different structure (no created_at, no description). Use collect_applied_proposals()
    to read apply-results/ directly with its own field mapping.
    """
    if stages is None:
        stages = ["pending", "approved", "deployed", "rejected"]
    proposals = []
    for stage in stages:
        stage_dir = shared_dir / "proposals" / stage
        if not stage_dir.exists():
            continue
        for pf in stage_dir.glob("*.json"):
            try:
                data = json.loads(pf.read_text())
                data["_stage"] = stage
                proposals.append(data)
            except Exception:
                pass
    return proposals


def _proposal_created(p: dict) -> str:
    return p.get("created_at", p.get("deployed_at", ""))[:10]


def _is_empty_proposal(p: dict) -> bool:
    """An empty placeholder proposal: no usable description AND zero confidence.

    These are the ``[] No description (0% confidence)`` rows that the fresh-pod
    weekly review surfaced under "Decisions Needed" — there is nothing for the
    operator to decide on, so they are filtered from the display (not from the
    rsi-owned health scoring, which sees the raw pending list)."""
    desc = (p.get("description") or p.get("title") or "").strip()
    try:
        conf = float(p.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return not desc and conf <= 0.0


# ── ISO-week idempotency sentinel (duplicate-fire guard) ────────────────────────
# A launchd StartCalendarInterval that was MISSED (pod asleep / daemon down on
# Sunday) fires immediately on the next (re-)bootstrap — and a fresh-pod
# re-bootstrap can fire it more than once. The dispatcher's PER_EVENT_UNIQUE
# dedup_key has a 0s cooldown and the report body embeds a live "Generated:"
# timestamp, so the identical-content floor does NOT catch a same-week re-fire.
# This producer-level sentinel is the real once-per-ISO-week guard (same shape
# as weekly_bot_trends._already_sent_this_week, the sibling Sunday weekly).

def _sentinel_path(shared_dir: str | Path) -> Path:
    """Where the last-sent ISO week is recorded. Under ``{shared_dir}`` which
    the evolve daemon user owns — plain write, no sudo/staging."""
    return Path(shared_dir) / "state" / "weekly_review" / "last_sent_week"


def _already_sent_this_week(shared_dir: str | Path, iso_week: str) -> bool:
    """True if a review for ``iso_week`` was already sent. Robust against
    launchd make-up fires and manual re-runs regardless of body drift. Fails
    OPEN (returns False) if the sentinel can't be read — better a rare
    duplicate than a silently-suppressed weekly."""
    try:
        return _sentinel_path(shared_dir).read_text(encoding="utf-8").strip() == iso_week
    except (FileNotFoundError, OSError):
        return False


def _mark_sent_this_week(shared_dir: str | Path, iso_week: str) -> None:
    """Record ``iso_week`` as sent. Atomic temp-file + rename. Non-fatal on
    failure — the dispatcher cooldown is the backstop if the write fails."""
    path = _sentinel_path(shared_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(iso_week, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(
            f"[weekly_review] could not write ISO-week sentinel ({exc}); "
            "relying on dispatcher cooldown",
            file=sys.stderr,
        )


# ── RSI Data Collectors ────────────────────────────────────────────────────────

def collect_pending_proposals(shared_dir: Path) -> list[dict]:
    pending_dir = shared_dir / "proposals" / "pending"
    if not pending_dir.exists():
        return []
    proposals = []
    for f in sorted(pending_dir.glob("*.json")):
        try:
            proposals.append(json.loads(f.read_text()))
        except Exception:
            pass
    return proposals


def collect_applied_proposals(shared_dir: Path) -> list[dict]:
    """Get proposals applied in the last 7 days (from apply-results)."""
    cutoff = str(date.today() - timedelta(days=7))
    results_dir = shared_dir / "proposals" / "apply-results"
    # Also check deployed/ for compatibility
    deployed_dir = shared_dir / "proposals" / "deployed"

    applied = []
    for d in [results_dir, deployed_dir]:
        if not d.exists():
            continue
        for pf in d.glob("*.json"):
            try:
                data = json.loads(pf.read_text())
                ts = (data.get("applied_at") or data.get("deployed_at") or "")[:10]
                if ts >= cutoff:
                    applied.append(data)
            except Exception:
                pass
    return applied


def collect_proposal_velocity(shared_dir: Path) -> dict:
    """
    Proposals generated this week vs 4-week rolling average.
    Returns: {this_week, week_avg_4w, trend_pct}
    """
    today = date.today()
    cutoff_1w = str(today - timedelta(days=7))
    cutoff_4w = str(today - timedelta(days=28))

    all_p = _all_proposals(shared_dir)

    this_week = sum(1 for p in all_p if _proposal_created(p) >= cutoff_1w)
    four_weeks = sum(1 for p in all_p if cutoff_4w <= _proposal_created(p) < cutoff_1w)
    week_avg_4w = round(four_weeks / 4, 1) if four_weeks > 0 else 0.0

    if week_avg_4w > 0:
        trend_pct = round((this_week - week_avg_4w) / week_avg_4w * 100)
    else:
        trend_pct = 0

    return {
        "this_week": this_week,
        "week_avg_4w": week_avg_4w,
        "trend_pct": trend_pct,
    }


def collect_proposal_funnel(shared_dir: Path) -> dict:
    """
    Trailing 30-day proposal funnel:
      generated → approved → applied → measured

    Stages:
    - generated: any proposal created in past 30 days (all lifecycle stages)
    - approved:  proposals that reached approved/, deployed/, or apply-results/ (success)
    - applied:   proposals in apply-results/ with success=True, or in deployed/ (legacy)
    - measured:  applied proposals with a 'measurement' or 'outcome_measured' field

    apply-results/ is read separately with its own field mapping (applied_at, success)
    to avoid mixing it with proposal files that have created_at/description/etc.
    """
    today = date.today()
    cutoff = str(today - timedelta(days=30))

    # Proposal lifecycle files (pending, approved, deployed, rejected)
    all_p = _all_proposals(shared_dir)
    recent = [p for p in all_p if _proposal_created(p) >= cutoff]

    generated = len(recent)

    # Approved = reached approved stage or beyond (approved/, deployed/)
    approved = sum(
        1 for p in recent
        if p.get("_stage") in ("approved", "deployed")
    )

    # Applied and measured — read apply-results/ directly (different file structure)
    applied = 0
    measured = 0
    results_dir = shared_dir / "proposals" / "apply-results"
    if results_dir.exists():
        for pf in results_dir.glob("*.json"):
            try:
                r = json.loads(pf.read_text())
                ts = (r.get("applied_at") or "")[:10]
                if ts < cutoff:
                    continue
                if r.get("success", False):
                    applied += 1
                    if r.get("measurement") or r.get("outcome_measured"):
                        measured += 1
            except Exception:
                pass

    # Fallback: if no apply-results/, count deployed/ as applied (legacy pipeline)
    if applied == 0 and results_dir.exists() is False:
        applied = sum(1 for p in recent if p.get("_stage") == "deployed")
        measured = sum(
            1 for p in recent
            if p.get("_stage") == "deployed"
            and (p.get("measurement") or p.get("outcome_measured"))
        )

    # Approval count includes apply-results successes (they passed approval)
    approved = max(approved, applied)

    return {
        "generated": generated,
        "approved": approved,
        "applied": applied,
        "measured": measured,
        "approval_rate": round(approved / generated * 100) if generated > 0 else 0,
        "apply_rate": round(applied / approved * 100) if approved > 0 else 0,
        "measurement_rate": round(measured / applied * 100) if applied > 0 else 0,
    }


def collect_rejection_reasons(shared_dir: Path) -> list[dict]:
    """
    Proposals rejected in the past 7 days with their reason.
    Returns list of {id, description, reason, pattern_key}.
    """
    cutoff = str(date.today() - timedelta(days=7))
    rejected_dir = shared_dir / "proposals" / "rejected"
    if not rejected_dir.exists():
        return []

    rejected = []
    for pf in rejected_dir.glob("*.json"):
        try:
            data = json.loads(pf.read_text())
            ts = (data.get("rejected_at") or data.get("created_at") or "")[:10]
            if ts >= cutoff:
                rejected.append({
                    "id": data.get("id", pf.stem)[:16],
                    "description": data.get("description", data.get("title", ""))[:120],
                    "reason": data.get("rejection_reason", data.get("reason", "no reason recorded"))[:200],
                    "pattern_key": data.get("pattern_key", ""),
                })
        except Exception:
            pass
    return rejected


def compute_rsi_health_score(funnel: dict, velocity: dict, pending: list[dict]) -> dict:
    """
    Composite RSI Cycle Health Score (0–100).

    Components:
      velocity    25% — this_week vs 4-week avg (0 proposals = 0, at avg = 75, >avg = 100)
      approval    35% — approval_rate from funnel (linear: 0–100%)
      measurement 25% — measurement_rate from funnel (linear: 0–100%)
      freshness   15% — age of oldest pending proposal (0 days = 100, ≥14 days = 0)
    """
    # Velocity score (0–100)
    # No 4-week history → neutral 50 regardless of this week's count.
    # With history: at avg = 75pts, at 133% of avg = 100pts, at 0 = 0pts.
    avg = velocity.get("week_avg_4w", 0)
    this_w = velocity.get("this_week", 0)
    if avg == 0:
        velocity_score = 50  # no baseline yet — neutral
    else:
        ratio = this_w / avg
        velocity_score = min(100, int(ratio * 75))  # at avg = 75, at 133% avg = 100

    # Approval score
    approval_score = min(100, funnel.get("approval_rate", 0))

    # Measurement score
    measurement_score = min(100, funnel.get("measurement_rate", 0))

    # Freshness score — based on oldest pending proposal
    # Linear decay: 0–3 days = 100, 14+ days = 0 (range of 11 steps: days 4–14)
    oldest_days = 0
    for p in pending:
        created = p.get("created_at", "")[:10]
        if created:
            try:
                age = (date.today() - date.fromisoformat(created)).days
                oldest_days = max(oldest_days, age)
            except Exception:
                pass

    if oldest_days == 0 and not pending:
        freshness_score = 100  # no backlog
    elif oldest_days <= 3:
        freshness_score = 100
    elif oldest_days >= 14:
        freshness_score = 0
    else:
        freshness_score = int(100 - (oldest_days - 3) / 11 * 100)

    composite = int(
        velocity_score * 0.25
        + approval_score * 0.35
        + measurement_score * 0.25
        + freshness_score * 0.15
    )

    if composite >= 80:
        label = "Healthy"
        icon = "🟢"
    elif composite >= 55:
        label = "Needs Attention"
        icon = "⚠️"
    else:
        label = "At Risk"
        icon = "🔴"

    return {
        "score": composite,
        "label": label,
        "icon": icon,
        "components": {
            "velocity": velocity_score,
            "approval": approval_score,
            "measurement": measurement_score,
            "freshness": freshness_score,
        },
        "oldest_pending_days": oldest_days,
    }


def collect_detectors_fired(shared_dir: Path) -> list[str]:
    """Get detectors that fired in the last week, with proposal counts."""
    cutoff = str(date.today() - timedelta(days=7))
    detectors: dict[str, int] = {}

    for stage in ["pending", "approved", "deployed", "rejected"]:
        stage_dir = shared_dir / "proposals" / stage
        if not stage_dir.exists():
            continue
        for pf in stage_dir.glob("*.json"):
            try:
                data = json.loads(pf.read_text())
                created = data.get("created_at", "")[:10]
                if created < cutoff:
                    continue
                key = data.get("pattern_key", "")
                detector = key.split(":", 1)[-1] if ":" in key else key
                if detector:
                    detectors[detector] = detectors.get(detector, 0) + 1
            except Exception:
                pass

    return [
        f"{det} ({count} proposal{'s' if count > 1 else ''})"
        for det, count in sorted(detectors.items(), key=lambda x: -x[1])
    ]


def collect_kaizen_top_ideas(shared_dir: Path) -> list[str]:
    """Get top 3 ideas from the most recent Friday kaizen scan."""
    kaizen_dir = shared_dir / "kaizen"
    if not kaizen_dir.exists():
        return []

    files = sorted(kaizen_dir.glob("*.md"), reverse=True)
    if not files:
        return []

    try:
        content = files[0].read_text()
        lines = content.split("\n")
        in_ideas = False
        ideas = []
        for line in lines:
            if line.startswith("## Top Ideas"):
                in_ideas = True
                continue
            if in_ideas and line.startswith("## "):
                break
            if in_ideas and line.strip() and line.strip()[0].isdigit() and "." in line:
                ideas.append(line.strip())
        return ideas[:3]
    except Exception:
        return []


def collect_module_status(shared_dir: Path, config: dict) -> dict[str, str]:
    """Get trust validation status per module."""
    try:
        from evolve_config import get_modules
        modules_cfg = get_modules(config)
    except Exception:
        modules_cfg = {}

    import time as _time
    status: dict[str, str] = {}

    # Gateway Healing
    bots_cfg = config.get("bots", {})
    heal_ok = False
    for bot_id in bots_cfg:
        sf = shared_dir / "status" / f"{bot_id}.json"
        if sf.exists():
            try:
                data = json.loads(sf.read_text())
                age_s = _time.time() - data.get("ts_epoch", 0)
                if age_s < 86400 * 7:
                    heal_ok = True
            except Exception:
                pass
    status["Gateway Healing"] = "validated" if heal_ok else "unknown"

    # Pattern Detectors
    proposals_exist = any(
        (shared_dir / "proposals" / stage).exists()
        and any((shared_dir / "proposals" / stage).glob("*.json"))
        for stage in ["pending", "approved", "deployed", "rejected"]
    )
    status["Pattern Detectors"] = "validated" if proposals_exist else "unvalidated (no proposals yet)"

    # Continuity Engine
    ce_enabled = bool(modules_cfg.get("continuity_engine", {}).get("enabled", False))
    status["Continuity Engine"] = "enabled" if ce_enabled else "disabled"

    return status


# ── LLM synthesis ──────────────────────────────────────────────────────────────

def _synthesize_with_llm(data: dict, config: dict) -> dict | None:
    """Use Haiku (tier3) to synthesize decisions needed + recommended actions."""
    try:
        model = resolve_tier("tier3", config)
    except Exception:
        model = "anthropic/claude-haiku-4-5"

    context_parts: list[str] = []

    # Health score
    health = data.get("health_score", {})
    if health:
        context_parts.append(
            f"RSI HEALTH SCORE: {health.get('score', '?')}/100 — {health.get('label', '')} "
            f"(velocity: {health.get('components', {}).get('velocity', '?')}, "
            f"approval: {health.get('components', {}).get('approval', '?')}, "
            f"measurement: {health.get('components', {}).get('measurement', '?')}, "
            f"freshness: {health.get('components', {}).get('freshness', '?')})"
        )

    # Proposal velocity
    velocity = data.get("velocity", {})
    if velocity:
        context_parts.append(
            f"PROPOSAL VELOCITY: {velocity.get('this_week', 0)} this week "
            f"(4-week avg: {velocity.get('week_avg_4w', 0)}, "
            f"trend: {velocity.get('trend_pct', 0):+d}%)"
        )

    # Funnel
    funnel = data.get("funnel", {})
    if funnel:
        context_parts.append(
            f"30-DAY FUNNEL: {funnel.get('generated', 0)} generated → "
            f"{funnel.get('approved', 0)} approved ({funnel.get('approval_rate', 0)}%) → "
            f"{funnel.get('applied', 0)} applied ({funnel.get('apply_rate', 0)}%) → "
            f"{funnel.get('measured', 0)} measured ({funnel.get('measurement_rate', 0)}%)"
        )

    # Pending proposals
    pending = data.get("pending_proposals", [])
    if pending:
        context_parts.append(f"PENDING PROPOSALS ({len(pending)} awaiting review):")
        for p in pending[:8]:
            desc = p.get("description", p.get("title", "No description"))[:180]
            conf = p.get("confidence", 0)
            created = p.get("created_at", "")[:10]
            context_parts.append(
                f"  - [{p.get('pattern_key', '')}] {desc} (conf: {conf:.0%}, created: {created})"
            )
    else:
        context_parts.append("PENDING PROPOSALS: none")

    # Rejection reasons
    rejections = data.get("rejections", [])
    if rejections:
        context_parts.append(f"REJECTIONS THIS WEEK ({len(rejections)}):")
        for r in rejections[:3]:
            context_parts.append(f"  - [{r.get('pattern_key', '')}] {r.get('reason', '')} ")

    # Detectors
    detectors = data.get("detectors_fired", [])
    if detectors:
        context_parts.append(f"DETECTORS FIRED: {', '.join(detectors[:5])}")

    # Oldest pending age
    oldest_days = health.get("oldest_pending_days", 0)
    if oldest_days > 7:
        context_parts.append(f"OLDEST PENDING PROPOSAL: {oldest_days} days old (backlog concern)")

    context = "\n".join(context_parts)

    prompt = f"""You are helping an operator review the RSI (Recursive Self-Improvement) pipeline health for their AI assistant pod.
Today is {date.today().isoformat()} (Sunday weekly review).

Here is the current RSI pipeline state:
{context}

Produce ONLY valid JSON with these two fields:

{{
  "decisions_needed": [
    {{"item": "clear description of decision", "why": "why it matters now", "urgency": "high|medium|low"}},
    ...
  ],
  "recommended_actions": [
    {{"action": "specific action to take", "reason": "why this is the right next step"}},
    {{"action": "specific action to take", "reason": "why this is the right next step"}},
    {{"action": "specific action to take", "reason": "why this is the right next step"}}
  ]
}}

Rules:
- decisions_needed: list proposals needing review and any RSI pipeline issues requiring operator attention, ordered by urgency
- recommended_actions: exactly 3 specific, actionable steps to improve the RSI cycle health
- Focus exclusively on the self-improvement pipeline — not operational metrics
- Be concise — one sentence per item
- Output ONLY the JSON object, no other text"""

    try:
        # polling-bypass: one-shot LLM completion call (not polling)
        result = subprocess.run(
            ["openclaw", "llm", "complete",
             "--model", model,
             "--max-tokens", "600",
             "--prompt", prompt],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"[weekly_review] LLM call failed: {result.stderr[:200]}", file=sys.stderr)
            return None
        output = result.stdout.strip()
        start = output.find("{")
        end = output.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        return json.loads(output[start:end])
    except Exception as e:
        print(f"[weekly_review] LLM synthesis failed: {e}", file=sys.stderr)
        return None


# ── Report builder ─────────────────────────────────────────────────────────────

def build_report(data: dict, synthesis: dict | None, review_date: date) -> str:
    lines = [f"# RSI Weekly Review — {review_date.isoformat()}", ""]

    # Pod-maturity gate (Mechanism 3, docs/spec-transient-signal-suppression-
    # 2026-06-23.md). On a too-young / no-history pod the funnel scores are 0
    # purely because nothing has accumulated yet — that is "warming up", not
    # "At Risk". Reframe the health header to an informational state and skip
    # the red icon entirely; the rsi-owned score is still computed (boundary:
    # reports owns presentation, not scoring) but not rendered as a verdict.
    maturity = data.get("maturity")
    warming_up = bool(maturity and not maturity.get("mature", True))

    if warming_up:
        lines.append("## RSI Cycle Health")
        lines.append("**🌱 Warming up — insufficient history to score**")
        lines.append("")
        reason = (maturity or {}).get("reason", "")
        age_days = (maturity or {}).get("pod_age_days")
        if isinstance(age_days, (int, float)):
            lines.append(
                f"This pod is still early in its first RSI cycles "
                f"(~{age_days:.0f} day{'s' if round(age_days) != 1 else ''} old). "
                "The cycle-health score needs at least one prior weekly cycle of "
                "approval/measurement data before it is meaningful."
            )
        else:
            lines.append(
                "The cycle-health score needs accumulated approval/measurement "
                "history before it is meaningful."
            )
        if reason:
            lines.append("")
            lines.append(f"_Warming up: {reason}._")
        lines.append("")

    # RSI Cycle Health Score
    health = data.get("health_score", {})
    if health and not warming_up:
        icon = health.get("icon", "⚠️")
        score = health.get("score", 0)
        label = health.get("label", "")
        comp = health.get("components", {})
        lines.append("## RSI Cycle Health")
        lines.append(f"**{icon} {score}/100 — {label}**")
        lines.append("")
        lines.append(
            f"| Component | Score |"
        )
        lines.append("|---|---|")
        lines.append(f"| Proposal Velocity (25%) | {comp.get('velocity', '?')}/100 |")
        lines.append(f"| Approval Rate (35%) | {comp.get('approval', '?')}/100 |")
        lines.append(f"| Measurement Rate (25%) | {comp.get('measurement', '?')}/100 |")
        lines.append(f"| Backlog Freshness (15%) | {comp.get('freshness', '?')}/100 |")
        oldest = health.get("oldest_pending_days", 0)
        if oldest > 0:
            lines.append(f"\nOldest pending proposal: {oldest} day{'s' if oldest != 1 else ''}")
        lines.append("")

    # Proposal Velocity
    velocity = data.get("velocity", {})
    lines.append("## 📈 Proposal Velocity (This Week vs 4-Week Avg)")
    this_w = velocity.get("this_week", 0)
    avg = velocity.get("week_avg_4w", 0)
    trend = velocity.get("trend_pct", 0)
    direction = f"+{trend}%" if trend >= 0 else f"{trend}%"
    lines.append(f"- This week: **{this_w}** proposals generated")
    lines.append(f"- 4-week avg: {avg}/week")
    if avg > 0:
        lines.append(f"- Trend: {direction}")
    lines.append("")

    # 30-Day Funnel
    funnel = data.get("funnel", {})
    lines.append("## 🔄 30-Day Proposal Funnel")
    gen = funnel.get("generated", 0)
    appr = funnel.get("approved", 0)
    appl = funnel.get("applied", 0)
    meas = funnel.get("measured", 0)
    lines.append(f"```")
    lines.append(f"Generated  {gen:>4}")
    lines.append(f"Approved   {appr:>4}  ({funnel.get('approval_rate', 0)}%)")
    lines.append(f"Applied    {appl:>4}  ({funnel.get('apply_rate', 0)}% of approved)")
    lines.append(f"Measured   {meas:>4}  ({funnel.get('measurement_rate', 0)}% of applied)")
    lines.append("```")
    lines.append("")

    # Decisions Needed
    lines.append("## 🎯 Decisions Needed This Week")
    if synthesis and synthesis.get("decisions_needed"):
        for item in synthesis["decisions_needed"]:
            urgency = item.get("urgency", "")
            tag = f" [{urgency.upper()}]" if urgency else ""
            lines.append(f"- **{item.get('item', '')}**{tag} — {item.get('why', '')}")
    elif data.get("pending_proposals"):
        pending = data["pending_proposals"]
        lines.append(f"- {len(pending)} proposal(s) awaiting review")
        for p in pending[:5]:
            desc = p.get("description", p.get("title", "No description"))[:120]
            conf = p.get("confidence", 0)
            lines.append(f"  - [{p.get('pattern_key', '')}] {desc} ({conf:.0%} confidence)")
    else:
        lines.append("- No proposals pending")
    lines.append("")

    # Detectors Fired
    lines.append("## 🔍 RSI Detectors Active This Week")
    detectors = data.get("detectors_fired", [])
    if detectors:
        for det in detectors:
            lines.append(f"- {det}")
    else:
        lines.append("- No detectors fired this week")
    lines.append("")

    # Rejection Reasons
    rejections = data.get("rejections", [])
    if rejections:
        lines.append("## ❌ Rejections This Week")
        for r in rejections:
            lines.append(f"- **{r.get('id', '')}** [{r.get('pattern_key', '')}]")
            lines.append(f"  Reason: {r.get('reason', 'no reason recorded')}")
        lines.append("")

    # Applied Last Week
    lines.append("## ✅ Applied Last Week")
    applied = data.get("applied_proposals", [])
    if applied:
        for p in applied:
            desc = p.get("description", p.get("title", "Unknown proposal"))[:100]
            ts = (p.get("applied_at") or p.get("deployed_at") or "")[:10]
            lines.append(f"- {desc} (applied {ts})")
    else:
        lines.append("- No proposals applied this week")
    lines.append("")

    # Community Intelligence
    kaizen_ideas = data.get("kaizen_ideas", [])
    if kaizen_ideas:
        lines.append("## 🌐 Community Intelligence")
        for idea in kaizen_ideas[:3]:
            lines.append(f"- {idea}")
        lines.append("")

    # Module Status
    lines.append("## 🔧 Module Status")
    module_status = data.get("module_status", {})
    if module_status:
        for module, mstatus in module_status.items():
            if mstatus == "validated":
                icon = "✅"
            elif mstatus.startswith("unvalidated"):
                icon = "⚠️"
            else:
                icon = "ℹ️"
            lines.append(f"- {icon} {module}: {mstatus}")
    else:
        lines.append("- No module data available")
    lines.append("")

    # Recommended Actions
    lines.append("## 💡 Recommended Actions")
    if synthesis and synthesis.get("recommended_actions"):
        for i, action in enumerate(synthesis["recommended_actions"][:3], 1):
            lines.append(
                f"{i}. **{action.get('action', '')}** — {action.get('reason', '')}"
            )
    else:
        pending = data.get("pending_proposals", [])
        if pending:
            lines.append(
                f"1. **Review {len(pending)} pending proposal(s)** — "
                "open Self-Improvement page to approve or reject"
            )
        else:
            lines.append("1. **No proposals to review** — system is still calibrating")
        lines.append(
            "2. **Check approval funnel** — "
            "if approval rate <50%, review detector thresholds"
        )
        lines.append(
            "3. **Verify measurement coverage** — "
            "applied proposals should have outcome data within 7 days"
        )
    lines.append("")

    lines.append(
        f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_"
    )

    return "\n".join(lines)


# ── Output & Send ──────────────────────────────────────────────────────────────

# Weekly reviews accumulate one .md per week with no programmatic reader (the
# /api/reviews/latest route that once served them was removed). Keep the most
# recent dozen for operator file-browsing; that's a full quarter of history.
_KEEP_REVIEWS = 12


def _prune_old_reviews(reviews_dir: Path, *, keep: int = _KEEP_REVIEWS) -> None:
    """Delete all but the newest ``keep`` review files. Date-named, so a lexical
    sort is chronological. Best-effort; never raises."""
    try:
        files = sorted(reviews_dir.glob("*.md"))
    except OSError:
        return
    for stale in files[:-keep] if keep else files:
        try:
            stale.unlink()
        except OSError:
            pass


def write_report(report: str, shared_dir: Path, review_date: date) -> Path:
    reviews_dir = shared_dir / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    out_path = reviews_dir / f"{review_date.isoformat()}.md"
    tmp_path = out_path.with_suffix(".md.tmp")
    tmp_path.write_text(report)
    tmp_path.rename(out_path)
    _prune_old_reviews(reviews_dir)
    return out_path


def send_report(
    text: str, config: dict, *, shared_dir: str | Path,
) -> bool:
    """Push the weekly RSI process-health report via the dispatcher.

    Phase C of docs/spec-alert-subscriptions-2026-05-10.md. Operator
    can mute via summaries.weekly_rsi_review subscription.

    Stays on the legacy ``message=`` path (not payload-driven). The
    body is a full multi-section Markdown report with tables and
    headings — the catalog's ``body_template`` cannot usefully abstract
    it. Source-side truncation to the Telegram 4096-char limit lives
    here too.
    """
    # Telegram has a 4096-char limit — truncate gracefully.
    if len(text) > 4000:
        text = text[:3950] + "\n\n_(report truncated — see reviews/ for full version)_"

    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        print(f"[weekly_review] dispatcher import failed; report dropped: {exc}",
              file=sys.stderr)
        return False

    from datetime import date as _date
    iso_week = _date.today().strftime("%G-W%V")
    try:
        outcome = _dispatch_send(
            shared_dir=Path(shared_dir),
            network=config,
            source="weekly_review",
            message=text,
            severity=Severity.INFO,
            dedup_key=f"weekly_review/{iso_week}",
            catalog_event="summaries.weekly_rsi_review",
        )
    except Exception as exc:
        print(f"[weekly_review] dispatcher.send raised; report dropped: {exc}",
              file=sys.stderr)
        return False

    if outcome.result == DispatchResult.SENT:
        # Stamp the once-per-week sentinel only on a real SENT, so a
        # no-recipient / deferred run can still retry later in the week.
        _mark_sent_this_week(shared_dir, iso_week)
        return True
    print(f"[weekly_review] Not sent ({outcome.result.value}): "
          f"{outcome.error or 'no error detail'}", file=sys.stderr)
    return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve RSI weekly review report")
    parser.add_argument(
        "--network", default=str(resolve_network_path()),
        help="Path to network.json config",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate and print report without saving or sending")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM synthesis (data-only output)")
    parser.add_argument("--no-send", action="store_true",
                        help="Save to file but do not send")
    parser.add_argument(
        "--always", action="store_true",
        help="Bypass the once-per-ISO-week sentinel (operator/debug use)",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    review_date = date.today()

    # Once-per-ISO-week idempotency guard (duplicate-fire / launchd make-up).
    # Checked before any LLM cost. --dry-run previews freely; --always bypasses.
    iso_week = review_date.strftime("%G-W%V")
    if not args.always and not args.dry_run and _already_sent_this_week(shared_dir, iso_week):
        print(
            f"[weekly_review] Already sent for {iso_week} — "
            "skipping (once-per-week sentinel)"
        )
        return

    print(f"[weekly_review] Generating RSI review for {review_date.isoformat()}")

    pending = collect_pending_proposals(shared_dir)
    applied = collect_applied_proposals(shared_dir)
    velocity = collect_proposal_velocity(shared_dir)
    funnel = collect_proposal_funnel(shared_dir)
    rejections = collect_rejection_reasons(shared_dir)
    health = compute_rsi_health_score(funnel, velocity, pending)

    # Pod-maturity gate (Mechanism 3). data_points = proposals that entered the
    # 30-day funnel window — the denominator the approval/measurement rates need.
    maturity = {"mature": True, "reason": "", "pod_age_days": None, "data_points": None}
    try:
        from signals import maturity_gate
        verdict = maturity_gate.evaluate(
            shared_dir,
            min_age_days=maturity_gate.RSI_REVIEW_MIN_AGE_DAYS,
            min_data_points=maturity_gate.RSI_REVIEW_MIN_DATA_POINTS,
            data_points=funnel.get("generated", 0),
        )
        maturity = {
            "mature": verdict.mature, "reason": verdict.reason,
            "pod_age_days": verdict.pod_age_days, "data_points": verdict.data_points,
        }
    except Exception as exc:  # fail OPEN to mature — never hide a real score
        print(f"[weekly_review] maturity gate unavailable ({exc}); scoring normally",
              file=sys.stderr)
    warming_up = not maturity["mature"]

    # Filter empty placeholder proposals ("No description / 0% confidence") from
    # the DISPLAY only — the rsi-owned health score still sees the raw pending.
    pending_display = [p for p in pending if not _is_empty_proposal(p)]

    data = {
        "pending_proposals": pending_display,
        "applied_proposals": applied,
        "velocity": velocity,
        "funnel": funnel,
        "rejections": rejections,
        "health_score": health,
        "maturity": maturity,
        "detectors_fired": collect_detectors_fired(shared_dir),
        "kaizen_ideas": collect_kaizen_top_ideas(shared_dir),
        "module_status": collect_module_status(shared_dir, config),
    }

    if warming_up:
        print(
            f"[weekly_review] RSI snapshot: 🌱 warming up — {maturity['reason']}"
        )
    else:
        print(
            f"[weekly_review] RSI snapshot: {health['icon']} {health['score']}/100 — "
            f"{len(pending_display)} pending, {velocity['this_week']} generated this week, "
            f"{funnel['approval_rate']}% approval rate (30d)"
        )

    synthesis = None
    if warming_up:
        # No history to reason over — the LLM would only invent decisions from
        # zeros. Skip it; the warming-up template carries the message.
        print("[weekly_review] Warming up — skipping LLM synthesis (no history yet)")
    elif not args.no_llm:
        print("[weekly_review] Synthesizing with Haiku...")
        synthesis = _synthesize_with_llm(data, config)
        if synthesis is None:
            print("[weekly_review] LLM unavailable — generating data-only report", file=sys.stderr)

    report = build_report(data, synthesis, review_date)

    if args.dry_run:
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60)
        print("[weekly_review] Dry run — report not saved or sent.")
        return

    out_path = write_report(report, shared_dir, review_date)
    print(f"[weekly_review] Report saved to {out_path}")

    if not args.no_send:
        sent = send_report(report, config, shared_dir=shared_dir)
        if sent:
            print("[weekly_review] Report sent via message channel")
        else:
            print("[weekly_review] Report send failed — check alerts config", file=sys.stderr)


if __name__ == "__main__":
    main()
