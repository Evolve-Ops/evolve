#!/usr/bin/env python3
"""
evolve/analyze.py — Weekly pattern detection and proposal generation

Reads 7 days of metrics files, identifies recurring problems,
and writes structured improvement proposals to the shared directory.

Scheduled via launchd: Sundays at 02:00 AM local time.

Usage:
    python3 analyze.py --network config/network.json
    python3 analyze.py --shared-dir /Users/Shared/evolve --days 7
"""

import argparse
import fcntl
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

from calibration import CalibrationLoader
from evolve_config import (
    CANONICAL_SHARED_DIR,
    resolve_network_path,
    is_module_enabled,
    is_rsi_enabled,
    get_detector_enabled,
    bot_home as _bot_home,
)
from proposal_routing import (
    apply_legacy_investigation_routing,
    proposal_surfaces_in_pending_inbox,
)


# ── Proposal types ─────────────────────────────────────────────────────────────

PROPOSAL_TYPES = {
    "config_change": "Change to openclaw.json or bot configuration",
    "script_change": "Change to a script or scheduled job",
    "workflow_change": "Change to how the bot handles a class of tasks",
    "investigation": "Investigate a pattern before proposing a fix",
}


def load_metrics_range(shared_dir: str, bot_id: str, days: int) -> list[dict]:
    metrics_dir = Path(shared_dir) / "metrics"
    result = []
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        path = metrics_dir / str(d) / f"{bot_id}.json"
        if path.exists():
            try:
                result.append(json.loads(path.read_text()))
            except Exception:
                pass
    return result


def load_rejection_history(shared_dir: str) -> list[dict]:
    path = Path(shared_dir) / "feedback" / "rejections.jsonl"
    if not path.exists():
        return []
    rejections = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rejections.append(json.loads(line))
                except Exception:
                    pass
    return rejections


def already_proposed(shared_dir: str, pattern_key: str) -> bool:
    """Check if a pattern has an open or recently deployed proposal."""
    # NOTE: "forge-results" is the legacy pre-rename directory; keep it in the
    # search list so already-validated proposals on un-migrated pods are still
    # considered. Migration script merges this into "validation-results/".
    for status in ("pending", "reviewed", "approved", "rejected", "apply-results", "validation-results", "forge-results"):
        proposals_dir = Path(shared_dir) / "proposals" / status
        if not proposals_dir.exists():
            continue
        for f in proposals_dir.glob("*.json"):
            try:
                p = json.loads(f.read_text())
                if p.get("pattern_key") == pattern_key:
                    return True
            except Exception:
                pass
    return False


# ── Detectors ──────────────────────────────────────────────────────────────────

def detect_high_maintenance_ratio(bot_id: str, history: list[dict], days: int, cal: CalibrationLoader | None = None) -> dict | None:
    """Flag if maintenance ratio has been consistently high."""
    if len(history) < 3:
        return None

    cfg = cal.detector("high_maintenance_ratio") if cal else {}
    avg_threshold = cfg.get("avg_ratio_threshold", 0.35)
    consec_threshold = cfg.get("consecutive_days_threshold", 3)
    consec_window = cfg.get("consecutive_check_window", 5)

    ratios = [d.get("maintenance_ratio", 0) for d in history]
    avg_ratio = mean(ratios)
    consecutive_high = sum(1 for r in ratios[:consec_window] if r > avg_threshold)

    if avg_ratio < avg_threshold and consecutive_high < consec_threshold:
        return None

    # Check rejection history — if this was already rejected as "too-aggressive", skip
    pattern_key = f"{bot_id}:high_maintenance_ratio"
    confidence = cal.effective_confidence("high_maintenance_ratio") if cal else cfg.get("confidence_base", 0.75)

    return {
        "pattern_key": pattern_key,
        "target_bot": bot_id,
        "type": "investigation",
        "problem": f"Maintenance ratio averaged {avg_ratio:.0%} over the last {len(history)} days "
                   f"(target: <{avg_threshold:.0%}). {consecutive_high} of last {consec_window} days above {avg_threshold:.0%}.",
        "data": {
            "maintenance_ratios": ratios[:7],
            "average": round(avg_ratio, 3),
            "days_above_threshold": consecutive_high,
        },
        "root_cause": "Unknown — investigation needed. Common causes: config errors requiring "
                      "repeated fixes, cron jobs failing, recurring permission issues.",
        "alternatives_considered": [],
        "confidence": confidence,
        "proposed_change": {
            "description": "Review session logs for the top Tier 2 session patterns. "
                           "Identify the single most common maintenance trigger and file "
                           "a specific fix proposal.",
            "action": "investigate",
        },
        "minimum_test": "Review last 3 Tier 2 sessions and identify root cause",
        "risk": "low",
        "forge_required": False,
    }


def detect_unexpected_billing_mode(bot_id: str, history: list[dict], config: dict | None = None) -> dict | None:
    """Flag if unexpected billing mode turns occurred.

    This detects provider-specific billing drift — e.g. an Anthropic bot with a
    flat-rate MAX subscription accidentally falling back to metered API key billing.
    Only fires when unexpected_billing_turns > 0 in the metrics history.

    For providers where every turn is metered by design (OpenAI, Google, local
    models, etc.) this detector is silent — unexpected_billing_turns will always
    be 0 because the plugin only sets that flag for genuine drift events.
    """
    days_with_unexpected = [d for d in history if d.get("unexpected_billing_turns", 0) > 0]
    if not days_with_unexpected:
        return None

    total_unexpected = sum(d.get("unexpected_billing_turns", 0) for d in days_with_unexpected)
    pattern_key = f"{bot_id}:unexpected_billing_mode"

    # Get primary model for provider-specific root cause messaging
    primary_model = ""
    if config:
        primary_model = config.get("bots", {}).get(bot_id, {}).get("primaryModel", "")
    provider = primary_model.split("/")[0] if "/" in primary_model else "unknown"

    if provider == "anthropic":
        # The original prescription here was "restore the MAX token" — that
        # advice is dead since MAX coverage ended (every Anthropic turn now
        # bills at API rates regardless of which auth profile is active). If
        # this detector is firing in the post-MAX era, the underlying signal
        # is the plugin's stale unexpected_billing_mode flag; the right fix
        # is to investigate why the plugin marked turns as "unexpected"
        # rather than to chase auth profiles.
        root_cause = (
            "Plugin flagged turns as unexpected_billing_mode. Since MAX coverage "
            "ended, every Anthropic turn bills at API rates regardless of auth "
            "profile, so the historical 'restore MAX token' fix no longer applies. "
            "Investigate why the plugin emitted the flag — it may indicate plugin "
            "state drift rather than a real billing anomaly."
        )
        proposed_change = {
            "description": "Investigate plugin unexpected_billing_mode emissions; do not modify auth profiles",
            "action": "investigation",
        }
        minimum_test = "Inspect turn telemetry for the affected days; verify whether billing actually diverged from the bot's configured Anthropic profile"
        alternatives = [
            {
                "hypothesis": "Stale flag from pre-MAX-end plugin code",
                "rejected_because": "Cannot rule out — needs plugin-side inspection",
            }
        ]
    else:
        root_cause = (
            f"Unexpected billing mode detected for provider '{provider}'. "
            "The bot is using a more expensive billing mode than configured."
        )
        proposed_change = {
            "description": "Review auth-profiles.json and verify the correct billing profile is active",
            "action": "investigation",
        }
        minimum_test = "Verify the bot's active auth profile matches the intended billing mode"
        alternatives = []

    return {
        "pattern_key": pattern_key,
        "target_bot": bot_id,
        "type": "config_change",
        "problem": f"Unexpected billing mode detected on {len(days_with_unexpected)} day(s) "
                   f"({total_unexpected} affected turns total).",
        "data": {
            "days_affected": [d["date"] for d in days_with_unexpected],
            "total_unexpected_turns": total_unexpected,
            "provider": provider,
        },
        "root_cause": root_cause,
        "alternatives_considered": alternatives,
        "confidence": 0.92,
        "proposed_change": proposed_change,
        "minimum_test": minimum_test,
        "risk": "low",
        "forge_required": False,
    }


def detect_declining_resolution_rate(bot_id: str, history: list[dict], cal: CalibrationLoader | None = None) -> dict | None:
    """Flag if first-response resolution rate is declining."""
    cfg = cal.detector("declining_resolution_rate") if cal else {}
    min_history = cfg.get("min_history_days", 7)
    recent_window = cfg.get("recent_window_days", 3)
    min_decline = cfg.get("min_decline_threshold", 0.15)

    if len(history) < min_history:
        return None

    recent = history[:min_history]
    rates = []
    for d in recent:
        sessions = d.get("session_count", 0)
        frr = d.get("first_response_resolutions", 0)
        if sessions > 0:
            rates.append(frr / sessions)

    if len(rates) < recent_window + 1:
        return None

    recent_avg = mean(rates[:recent_window])
    prior_avg = mean(rates[recent_window:])

    if prior_avg - recent_avg < min_decline:
        return None

    pattern_key = f"{bot_id}:declining_resolution_rate"
    confidence = cal.effective_confidence("declining_resolution_rate") if cal else cfg.get("confidence_base", 0.65)

    return {
        "pattern_key": pattern_key,
        "target_bot": bot_id,
        "type": "investigation",
        "problem": f"First-response resolution rate dropped from {prior_avg:.0%} to {recent_avg:.0%} "
                   f"over the last {min_history} days.",
        "data": {
            "resolution_rates_7d": [round(r, 3) for r in rates],
            "recent_avg": round(recent_avg, 3),
            "prior_avg": round(prior_avg, 3),
            "drop": round(prior_avg - recent_avg, 3),
        },
        "root_cause": "Unknown — could be new task types the bot handles poorly, context "
                      "accumulation causing less focused responses, or a change in how "
                      "requests are being made.",
        "alternatives_considered": [],
        "confidence": confidence,
        "proposed_change": {
            "description": "Review sessions where resolution took 3+ turns and identify "
                           "the common pattern. Common fix: add examples to AGENTS.md or "
                           "SOUL.md, or reduce context bloat.",
            "action": "investigate",
        },
        "minimum_test": "Review 5 multi-turn sessions from the past week",
        "risk": "low",
        "forge_required": False,
    }


def detect_zero_activity(bot_id: str, history: list[dict], days: int, cal: CalibrationLoader | None = None) -> dict | None:
    """Flag if a bot has had no activity for several days — may indicate it's stuck."""
    if not history:
        pattern_key = f"{bot_id}:no_data"
        cfg = cal.detector("zero_activity") if cal else {}
        confidence = cal.effective_confidence("zero_activity") if cal else cfg.get("confidence_base", 0.85)
        return {
            "pattern_key": pattern_key,
            "target_bot": bot_id,
            "type": "investigation",
            "problem": f"No metrics data found for {bot_id} in the last {days} days. "
                       "Bot may not be running or Evolve plugin may not be installed.",
            "data": {"days_checked": days, "days_with_data": 0},
            "root_cause": "Evolve plugin not installed, bot not running, or shared directory permissions issue.",
            "alternatives_considered": [],
            "confidence": confidence,
            "proposed_change": {
                "description": "Verify bot is running: openclaw gateway status. "
                               "Verify Evolve plugin is installed: openclaw plugins list. "
                               "Check shared directory permissions: ls -la /Users/Shared/evolve/annotations/",
                "action": "investigate",
            },
            "minimum_test": "Confirm bot responds to a message",
            "risk": "low",
            "forge_required": False,
        }
    return None


# ── Main ───────────────────────────────────────────────────────────────────────

def load_manifests(shared_dir: str, bot_id: str) -> list[dict]:
    """Load all application manifests for a bot."""
    manifest_dir = Path(shared_dir) / "applications" / bot_id
    if not manifest_dir.exists():
        return []
    manifests = []
    for f in manifest_dir.glob("manifest-*.json"):
        try:
            manifests.append(json.loads(f.read_text()))
        except Exception:
            pass
    return manifests


def load_session_summaries(shared_dir: str, bot_id: str, days: int) -> list[dict]:
    """Load session_summary records from annotation files."""
    annotation_dir = Path(shared_dir) / "annotations" / bot_id
    summaries = []
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        p = annotation_dir / f"{d}.jsonl"
        if not p.exists():
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("type") == "session_summary":
                        summaries.append(record)
                except Exception:
                    pass
    return summaries


def detect_low_satisfaction_application(
    bot_id: str, shared_dir: str, days: int
) -> list[dict]:
    """
    Detector: application manifests with low satisfaction + corroborating session data.

    A manifest rated ≤3/5 is a signal worth acting on, but only useful if
    we can say *why* — so we cross-reference with session summaries that
    invoked that application to build a specific proposal.
    """
    manifests = load_manifests(shared_dir, bot_id)
    if not manifests:
        return []

    summaries = load_session_summaries(shared_dir, bot_id, days)

    proposals = []
    for manifest in manifests:
        app_id = manifest.get("id", "")
        app_name = manifest.get("name", app_id)
        satisfaction = manifest.get("satisfaction_score", None)
        known_issues = manifest.get("known_issues", [])

        if satisfaction is None or satisfaction > 3:
            continue

        # Find sessions that invoked this application
        app_sessions = [
            s for s in summaries
            if app_id in s.get("applications_invoked", [])
            or app_name.lower() in " ".join(s.get("applications_invoked", [])).lower()
        ]

        # Build correction evidence from those sessions
        correction_sessions = [s for s in app_sessions if s.get("correction_count", 0) > 0]
        efficiency_sessions = [s for s in app_sessions if s.get("efficiency_flag", False)]
        recent_outcomes = [s.get("outcome", "") for s in app_sessions[:3]]

        # Compose specific problem description from actual evidence
        evidence_parts = []
        if correction_sessions:
            evidence_parts.append(
                f"{len(correction_sessions)} of {len(app_sessions)} recent sessions needed corrections"
            )
        if efficiency_sessions:
            evidence_parts.append(
                f"{len(efficiency_sessions)} sessions took more turns than expected"
            )
        if known_issues:
            evidence_parts.append(
                f"Known issues: {'; '.join(str(i) for i in known_issues[:2])}"
            )

        if not evidence_parts and not known_issues:
            # Only manifest score — still worth proposing but lower confidence
            evidence_parts.append(f"Rated {satisfaction}/5 at last manifest review")

        problem = (
            f"Application '{app_name}' is rated {satisfaction}/5. "
            + " ".join(evidence_parts) + "."
        )

        # Propose a concrete workflow investigation with specific evidence
        change_desc = f"Review and improve application '{app_name}'. "
        if known_issues:
            change_desc += f"Address known issues: {'; '.join(str(i) for i in known_issues)}. "
        if recent_outcomes:
            change_desc += f"Recent session outcomes: {' | '.join(o for o in recent_outcomes if o)}. "
        change_desc += "Update SOUL.md, AGENTS.md, or relevant skill to address root cause, then re-score the manifest."

        confidence = 0.65 if not app_sessions else min(0.90, 0.65 + 0.08 * len(correction_sessions))

        pattern_key = f"{bot_id}:low_satisfaction:{app_id}"
        proposals.append({
            "pattern_key": pattern_key,
            "target_bot": bot_id,
            "type": "workflow_change",
            "problem": problem,
            "data": {
                "application_id": app_id,
                "satisfaction_score": satisfaction,
                "known_issues": known_issues,
                "sessions_with_application": len(app_sessions),
                "correction_sessions": len(correction_sessions),
                "efficiency_flag_sessions": len(efficiency_sessions),
                "recent_outcomes": recent_outcomes,
            },
            "root_cause": (
                "Application is underperforming based on manifest rating and session evidence. "
                "Root cause may be in SOUL.md rules not being followed, missing context in "
                "AGENTS.md, a broken tool integration, or a poorly designed workflow."
            ),
            "alternatives_considered": [],
            "confidence": round(confidence, 2),
            "proposed_change": {
                "description": change_desc,
                "action": "workflow_change",
            },
            "minimum_test": f"Satisfaction score for '{app_name}' improves to ≥4/5 at next manifest review",
            "risk": "low",
            "forge_required": False,
        })

    return proposals


def detect_promise_breach(bot_id: str, shared_dir: str, days: int, cal: CalibrationLoader | None = None) -> dict | None:
    """
    Detector: bot regularly makes promises it doesn't keep.

    Reads session summaries for promises_made, then checks whether those
    sessions are followed by a session that resolves the promise (heuristic:
    capability or keyword match in next session). High promise count with
    low follow-through → SOUL.md or AGENTS.md change needed.
    """
    cfg = cal.detector("promise_breach") if cal else {}
    min_summaries = cfg.get("min_summaries", 3)
    promise_rate_threshold = cfg.get("promise_rate_threshold", 0.40)

    summaries = load_session_summaries(shared_dir, bot_id, days)
    if len(summaries) < min_summaries:
        return None

    sessions_with_promises = [s for s in summaries if s.get("promises_made")]
    if not sessions_with_promises:
        return None

    promise_rate = len(sessions_with_promises) / len(summaries)
    if promise_rate < promise_rate_threshold:
        return None

    sample_promises = [
        p
        for s in sessions_with_promises[:5]
        for p in s.get("promises_made", [])[:2]
    ][:5]

    pattern_key = f"{bot_id}:promise_breach"
    return {
        "pattern_key": pattern_key,
        "target_bot": bot_id,
        "type": "workflow_change",
        "problem": (
            f"{len(sessions_with_promises)} of {len(summaries)} recent sessions contain "
            f"bot commitments ('I\'ll remember', 'I\'ll check', etc.) — {promise_rate:.0%} of sessions. "
            "High promise rate may indicate the bot over-commits without following through."
        ),
        "data": {
            "sessions_with_promises": len(sessions_with_promises),
            "total_sessions": len(summaries),
            "promise_rate": round(promise_rate, 2),
            "sample_promises": sample_promises,
        },
        "root_cause": (
            "Bot is making forward commitments ('I'll remember', 'I'll follow up') "
            "at high frequency. This may be appropriate, or it may indicate a pattern of "
            "over-promising. The SOUL.md rule 'words are commitments' may need "
            "reinforcement or the bot may need a concrete post-task checklist."
        ),
        "alternatives_considered": [],
        "confidence": cal.effective_confidence("promise_breach") if cal else cfg.get("confidence_base", 0.60),
        "proposed_change": {
            "description": (
                "Review the sample promises. Determine if they are being kept (check AGENTS.md "
                "post-task checklist, daily notes, etc.). If not, add a mandatory verification "
                "step to AGENTS.md: after each session, confirm each commitment was executed."
            ),
            "action": "workflow_change",
        },
        "minimum_test": f"Promise rate drops below {promise_rate_threshold / 2:.0%} in next 7-day window, or all promises are verifiably kept",
        "risk": "low",
        "forge_required": False,
    }


def detect_efficiency_problems(bot_id: str, shared_dir: str, days: int, cal: CalibrationLoader | None = None) -> dict | None:
    """
    Detector: conversations are frequently longer than task complexity warrants.

    Reads session summaries for efficiency_flag=True sessions and clusters
    them to identify if there's a common capability or pattern.
    """
    cfg = cal.detector("efficiency_problems") if cal else {}
    min_summaries = cfg.get("min_summaries", 5)
    min_flagged = cfg.get("min_flagged_count", 3)
    flag_rate_threshold = cfg.get("flag_rate_threshold", 0.25)

    summaries = load_session_summaries(shared_dir, bot_id, days)
    if len(summaries) < min_summaries:
        return None

    flagged = [s for s in summaries if s.get("efficiency_flag") is True]
    if len(flagged) < min_flagged:
        return None

    flag_rate = len(flagged) / len(summaries)
    if flag_rate < flag_rate_threshold:
        return None

    # Find common applications in flagged sessions
    app_counts: dict[str, int] = {}
    for s in flagged:
        for cap in s.get("applications_invoked", []):
            app_counts[cap] = app_counts.get(cap, 0) + 1
    top_apps = sorted(app_counts.items(), key=lambda x: -x[1])[:3]

    sample_outcomes = [s.get("outcome", "") for s in flagged[:3]]

    pattern_key = f"{bot_id}:efficiency_problems"
    return {
        "pattern_key": pattern_key,
        "target_bot": bot_id,
        "type": "workflow_change",
        "problem": (
            f"{len(flagged)} of {len(summaries)} recent sessions had turn counts "
            f"higher than expected for their complexity ({flag_rate:.0%}). "
            + (f"Most affected applications: {', '.join(c for c,_ in top_apps)}." if top_apps else "")
        ),
        "data": {
            "flagged_sessions": len(flagged),
            "total_sessions": len(summaries),
            "flag_rate": round(flag_rate, 2),
            "top_applications": top_apps,
            "sample_outcomes": sample_outcomes,
        },
        "root_cause": (
            "Bot is taking more turns than expected to complete tasks. Common causes: "
            "asking clarifying questions it could answer from context, not being direct enough, "
            "missing context in SOUL.md or AGENTS.md about how to handle these task types."
        ),
        "alternatives_considered": [],
        "confidence": cal.effective_confidence("efficiency_problems") if cal else cfg.get("confidence_base", 0.70),
        "proposed_change": {
            "description": (
                "Review the sample outcomes. Identify what made these sessions long. "
                "If the bot is asking unnecessary clarifying questions, add guidance to SOUL.md. "
                "If it's missing domain context, add it to AGENTS.md or the relevant application area."
            ),
            "action": "workflow_change",
        },
        "minimum_test": f"Efficiency flag rate drops below {flag_rate_threshold / 2:.0%} in next 7-day window",
        "risk": "low",
        "forge_required": False,
    }


def detect_application_abandonment(
    bot_id: str, shared_dir: str, days: int
) -> list[dict]:
    """
    Detector: an application is being invoked but sessions consistently end
    unresolved — suggesting the application is broken or insufficient.

    Uses the application_usage field from daily metrics (written by measure.py).
    An application with ≥40% unresolved sessions over N days is a signal.
    """
    history = load_metrics_range(shared_dir, bot_id, days)

    # Aggregate application stats across all days in history
    app_totals: dict[str, dict] = {}
    for day_metrics in history:
        for cap, stats in day_metrics.get("application_usage", {}).items():
            if cap not in app_totals:
                app_totals[cap] = {"sessions": 0, "unresolved_sessions": 0,
                                   "correction_sessions": 0, "efficiency_sessions": 0}
            for k in app_totals[cap]:
                app_totals[cap][k] += stats.get(k, 0)

    proposals = []
    for cap, totals in app_totals.items():
        total = totals["sessions"]
        if total < 3:  # need enough signal
            continue
        unresolved = totals["unresolved_sessions"]
        unresolved_rate = unresolved / total
        if unresolved_rate < 0.40:
            continue

        correction_rate = totals["correction_sessions"] / total
        efficiency_rate = totals["efficiency_sessions"] / total

        problem = (
            f"Application '{cap}' has a {unresolved_rate:.0%} unresolved-session rate "
            f"over {total} sessions in the past {days} days."
        )
        if correction_rate > 0.3:
            problem += f" Also: {correction_rate:.0%} of sessions needed corrections."
        if efficiency_rate > 0.3:
            problem += f" {efficiency_rate:.0%} of sessions were efficiency-flagged."

        # Distinguish abandonment (user gave up) from just-difficult
        if unresolved_rate > 0.7:
            severity = "high"
            root_cause = (
                f"'{cap}' is being invoked but failing to produce resolved outcomes "
                f"in the majority of sessions. This suggests a broken integration, "
                f"missing tool permission, or an application that exists in name only."
            )
        else:
            severity = "medium"
            root_cause = (
                f"'{cap}' sessions frequently end without resolution. "
                f"This could mean the workflow is unclear, the bot is missing context "
                f"about how to complete these tasks, or the application needs better "
                f"tooling support."
            )

        pattern_key = f"{bot_id}:application_abandonment:{cap}"
        proposals.append({
            "pattern_key": pattern_key,
            "target_bot": bot_id,
            "type": "workflow_change",
            "problem": problem,
            "data": {
                "application": cap,
                "total_sessions": total,
                "unresolved_sessions": unresolved,
                "unresolved_rate": round(unresolved_rate, 3),
                "correction_rate": round(correction_rate, 3),
                "efficiency_rate": round(efficiency_rate, 3),
                "severity": severity,
            },
            "root_cause": root_cause,
            "alternatives_considered": [],
            "confidence": round(min(0.85, 0.55 + unresolved_rate * 0.3), 2),
            "proposed_change": {
                "description": (
                    f"Investigate why '{cap}' sessions end unresolved. "
                    f"Check: (1) tool integrations are working, "
                    f"(2) AGENTS.md has adequate context for this application, "
                    f"(3) the application manifest reflects current state. "
                    f"If the application is working correctly, update the "
                    f"session summarizer's unresolved keyword list — the "
                    f"outcome classifier may be wrong."
                ),
                "action": "workflow_change",
            },
            "minimum_test": f"Unresolved rate for '{cap}' drops below 25% over next 14 days",
            "risk": "low",
            "forge_required": False,
        })

    return proposals


def detect_promise_resolution(
    bot_id: str, shared_dir: str, days: int
) -> list[dict]:
    """
    Detector: cross-session promise resolution tracking.

    A promise made in session N should be acted on before session N+2.
    We can't perfectly track resolution (we don't know which sessions
    "complete" a promise), but we can detect:
    - Bot makes promises at high rate (already caught by detect_promise_breach)
    - Promises cluster around specific applications (those areas need structure)
    - Consecutive sessions with same application AND promises suggest nothing is
      being carried forward between them

    This is a lighter-weight signal: "promises pile up in application X" →
    that application needs a formal post-session checklist or task queue.
    """
    summaries = load_session_summaries(shared_dir, bot_id, days)
    if len(summaries) < 5:
        return []

    # Group promises by application
    app_promise_counts: dict[str, int] = {}
    app_session_counts: dict[str, int] = {}

    for s in summaries:
        caps = s.get("applications_invoked", [])
        promises = s.get("promises_made", [])
        if not promises:
            continue
        for cap in caps:
            app_promise_counts[cap] = app_promise_counts.get(cap, 0) + len(promises)
            app_session_counts[cap] = app_session_counts.get(cap, 0) + 1

    proposals = []
    for cap, promise_count in app_promise_counts.items():
        session_count = app_session_counts.get(cap, 1)
        if session_count < 3:
            continue
        promise_density = promise_count / session_count
        if promise_density < 1.5:  # less than 1.5 promises per session on avg
            continue

        pattern_key = f"{bot_id}:promise_resolution:{cap}"
        proposals.append({
            "pattern_key": pattern_key,
            "target_bot": bot_id,
            "type": "workflow_change",
            "problem": (
                f"Application '{cap}' averages {promise_density:.1f} bot commitments per session "
                f"across {session_count} sessions. High promise density with no "
                f"cross-session resolution tracking suggests work is being repeatedly "
                f"deferred without follow-through."
            ),
            "data": {
                "application": cap,
                "sessions_with_promises": session_count,
                "total_promises": promise_count,
                "promise_density": round(promise_density, 2),
            },
            "root_cause": (
                f"'{cap}' sessions regularly end with open commitments. "
                f"The bot is not completing promised tasks within the session, "
                f"and is not carrying them forward. "
                f"Deferred work should become scheduled follow-ups (the "
                f"Continuity Engine's defer tool), not verbal commitments."
            ),
            "alternatives_considered": [],
            "confidence": 0.70,
            "proposed_change": {
                "description": (
                    f"Add a post-session task creation rule to AGENTS.md for '{cap}': "
                    f"any commitment that can't be completed in-session must be "
                    f"written to memory/tasks.md with a concrete next-action, or "
                    f"scheduled as a follow-up via the Continuity Engine's "
                    f"defer tool."
                ),
                "action": "workflow_change",
            },
            "minimum_test": f"Promise density for application '{cap}' drops below 1.0 per session over next 14 days",
            "risk": "low",
            "forge_required": False,
        })

    return proposals


def run_detectors(
    bot_id: str,
    history: list[dict],
    days: int,
    shared_dir: str = "",
    network_config: dict | None = None,
    cal: CalibrationLoader | None = None,
) -> list[dict]:
    """Run all enabled detectors for a bot and return proposals."""
    cfg = network_config or {}
    proposals = []

    def _run(detector_name: str, fn, *args):
        # Check enabled via network_config (operator override), then calibration, then default True
        if not get_detector_enabled(cfg, detector_name):
            return
        if cal:
            det_cfg = cal.detector(detector_name)
            if det_cfg and not det_cfg.get("enabled", True):
                return
        result = fn(*args)
        if result is None:
            return
        if isinstance(result, list):
            proposals.extend(result)
        else:
            proposals.append(result)

    _run("zero_activity",           detect_zero_activity,           bot_id, history, days, cal)
    _run("unexpected_billing_mode", detect_unexpected_billing_mode, bot_id, history, cfg)
    _run("high_maintenance_ratio",  detect_high_maintenance_ratio,  bot_id, history, days, cal)
    _run("declining_resolution",    detect_declining_resolution_rate, bot_id, history, cal)
    _run("promise_breach",            detect_promise_breach,               bot_id, shared_dir, days, cal)
    _run("efficiency_problems",       detect_efficiency_problems,          bot_id, shared_dir, days, cal)
    _run("low_satisfaction",          detect_low_satisfaction_application, bot_id, shared_dir, days)
    _run("application_abandonment",   detect_application_abandonment,      bot_id, shared_dir, days)
    _run("promise_resolution",        detect_promise_resolution,           bot_id, shared_dir, days)
    _run("slack_quality_drop",        detect_slack_quality_drop,           bot_id, shared_dir, days)

    return proposals


def detect_slack_quality_drop(
    bot_id: str, shared_dir: str, days: int
) -> list[dict]:
    """
    Detector: Slack reaction signals indicate declining output quality.

    Uses slack_signal records written by slack_signals.py.
    An application with average quality score < 0.45 over N signals
    is a weak negative signal; < 0.35 is strong.

    Only meaningful for Slack-integrated bots. Bots without Slack integration will produce no signals.
    """
    try:
        from slack_signals import load_slack_signals
    except ImportError:
        return []

    signals = load_slack_signals(shared_dir, bot_id, days)
    if not signals:
        return []

    # Aggregate by application
    app_stats: dict[str, dict] = {}
    for sig in signals:
        cap = sig.get("application", "general")
        if cap not in app_stats:
            app_stats[cap] = {"count": 0, "quality_sum": 0.0,
                              "negative": 0, "no_reaction": 0}
        s = app_stats[cap]
        s["count"] += 1
        s["quality_sum"] += sig.get("quality_score", 0.5)
        s["negative"] += sig.get("negative_reactions", 0)
        if sig.get("signal_type") == "no_reaction":
            s["no_reaction"] += 1

    proposals = []
    for cap, s in app_stats.items():
        count = s["count"]
        if count < 3:  # need minimum signal volume
            continue
        avg_quality = s["quality_sum"] / count
        if avg_quality >= 0.45:
            continue

        no_reaction_rate = s["no_reaction"] / count
        negative_count = s["negative"]

        if avg_quality < 0.35:
            severity = "high"
            confidence = 0.80
        else:
            severity = "medium"
            confidence = 0.65

        problem = (
            f"Slack quality signals for application '{cap}' average {avg_quality:.2f}/1.0 "
            f"across {count} messages in the past {days} days."
        )
        if negative_count > 0:
            problem += f" {negative_count} explicit negative reaction(s) received."
        if no_reaction_rate > 0.5:
            problem += f" {no_reaction_rate:.0%} of messages received no reaction after 24h."

        pattern_key = f"{bot_id}:slack_quality_drop:{cap}"
        proposals.append({
            "pattern_key": pattern_key,
            "target_bot": bot_id,
            "type": "workflow_change",
            "problem": problem,
            "data": {
                "application": cap,
                "avg_quality_score": round(avg_quality, 3),
                "signal_count": count,
                "negative_reactions": negative_count,
                "no_reaction_rate": round(no_reaction_rate, 3),
                "severity": severity,
                "days": days,
            },
            "root_cause": (
                f"Team members are not positively engaging with '{cap}' outputs. "
                f"This may indicate the output format is wrong for the audience, "
                f"the content is not actionable, the timing is off, or the application "
                f"is not aligned with what the team actually needs."
            ),
            "alternatives_considered": [
                {"hypothesis": "Team culture of low reactions",
                 "rejected_because": "Other applications have higher engagement rates"}
            ],
            "confidence": confidence,
            "proposed_change": {
                "description": (
                    f"Review recent '{cap}' outputs in Slack. Identify the pattern: "
                    f"(1) is the format right? (2) is the content useful? (3) right timing? "
                    f"(4) right audience? Then update SOUL.md or AGENTS.md for {bot_id} "
                    f"to address the root cause."
                ),
                "action": "workflow_change",
            },
            "minimum_test": f"Slack quality score for application '{cap}' reaches ≥0.60 over next {days} days",
            "risk": "low",
            "forge_required": False,
        })

    return proposals


def write_proposal(proposal: dict, shared_dir: str) -> str:
    """Write a proposal JSON to the pending queue. Returns the proposal ID.

    Uses a per-pattern-key lock file to prevent duplicate proposals when two
    analyze.py instances run concurrently (e.g. manual trigger + scheduled).
    Uses atomic rename so readers never see a partial write.
    """
    pattern_key = proposal.get("pattern_key", "")
    pending_dir = Path(shared_dir) / "proposals" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    # Acquire a per-pattern-key lock before the already_proposed() check-then-write
    # to close the TOCTOU window where two instances could both pass the check.
    # Locks live in a sibling .locks/ dir (not pending/) so iter_proposals and any
    # other os.listdir(pending_dir) consumer doesn't have to skip them.
    lock_name = pattern_key.replace("/", "_").replace(":", "_") if pattern_key else "_generic"
    locks_dir = Path(shared_dir) / "proposals" / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{lock_name}.lock"
    lock_fh = open(lock_path, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    try:
        # Re-check inside the lock (the caller may have checked before acquiring)
        if pattern_key and already_proposed(shared_dir, pattern_key):
            # Another instance wrote this proposal while we waited for the lock
            lock_fh.close()
            return ""

        today = date.today().isoformat()
        proposal_id = f"prop-{today}-{str(uuid.uuid4())[:8]}"

        full_proposal = {
            "id": proposal_id,
            "generated": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "schema_version": 1,
            "category": "alert",  # default; individual detectors may override
            **proposal,
        }
        # Layer 2: a legacy type=="investigation" proposal is a "look into it"
        # FYI. Without surface/informational it routes to Alerts as a husk;
        # stamp surface="improvement" + informational=True so it lands in the
        # Recommendations Observations sub-block instead. Scoped to
        # investigations only — see proposal_routing.apply_legacy_investigation_routing.
        apply_legacy_investigation_routing(full_proposal)
        # No evolve_sig stamp: HMAC proposal signing was retired
        # 2026-08-18 (internal/design-proposal-signing-key-2026-08-18.md).
        # Nothing verified it — the sole verifier, the per-bot
        # apply.py daemon, polled a directory no arbiter status maps to.

        out_path = pending_dir / f"{proposal_id}.json"
        tmp_path = out_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(full_proposal, indent=2))
        tmp_path.rename(out_path)
        return proposal_id
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def send_telegram_alert(
    proposals: list[dict], network_config: dict, *, shared_dir: Path,
) -> None:
    """Send a chat-channel alert about pending proposals.

    Phase C of internal/spec-alert-subscriptions-2026-05-10.md: routes
    through alerts.dispatcher.send so operator preferences from the
    Subscriptions UI (decisions.proposal_ready) take effect.

    Surface-honest gate: the catalog breadcrumb for this event is
    "Recommendations → Inbox", so we only fire for — and only count —
    proposals that will actually land in the actionable Inbox
    (surface=improvement AND not informational). A proposal that routes
    to Alerts or the Observations sub-block would otherwise send the
    operator to an inbox it categorically cannot appear in. Predicate is
    the canonical one in proposal_routing (mirrored by self-improvement.js).
    """
    proposals = [p for p in proposals if proposal_surfaces_in_pending_inbox(p)]
    count = len(proposals)
    if count == 0:
        return

    # Phase F5: payload-driven render. Catalog body_template is
    #   "📋 {count} new proposal{s} ready\n{titles}"
    # so we build the titles list as one structured string and let the
    # catalog wrap it. The "Reply 'View …'" footer goes via the
    # catalog's action (ui_action → Recommendations → Inbox).
    title_lines = []
    for p in proposals:
        emoji = "🔴" if p.get("confidence", 0) > 0.85 else "⚠️"
        title_lines.append(f"{emoji} [{p['target_bot']}] {p['problem'][:80]}...")
    titles = "\n".join(title_lines)

    # dedup_key: hash the sorted proposal IDs so distinct batches push,
    # identical batches dedupe under the configured cooldown.
    import hashlib, sys as _sys
    ids = sorted(str(p.get("proposal_id") or p.get("id") or p.get("problem", ""))[:64]
                 for p in proposals)
    fingerprint = hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()[:16]

    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        print(f"[evolve/analyze] dispatcher import failed; alert dropped: {exc}",
              file=_sys.stderr)
        return

    try:
        outcome = _dispatch_send(
            shared_dir=shared_dir,
            network=network_config,
            source="analyze",
            severity=Severity.INFO,
            dedup_key=f"analyze/proposals_ready/{fingerprint}",
            catalog_event="decisions.proposal_ready",
            payload={
                "count": count,
                "s": "s" if count > 1 else "",
                "titles": titles,
            },
        )
    except Exception as exc:
        print(f"[evolve/analyze] dispatcher.send raised; alert dropped: {exc}",
              file=_sys.stderr)
        return

    if outcome.result == DispatchResult.SENT:
        print(f"[evolve/analyze] Alert sent ({count} proposal(s))")
    else:
        print(f"[evolve/analyze] Alert not sent: {outcome.result.value} "
              f"({outcome.error or 'no error detail'})")


def main():
    parser = argparse.ArgumentParser(description="Evolve weekly analysis engine")
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--network", default=str(resolve_network_path()), help="Path to network.json")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true", help="Print proposals without writing")
    args = parser.parse_args()

    shared_dir = args.shared_dir
    bots = []
    network_config = {"alerts": {}}

    if args.network:
        network_config = json.loads(Path(args.network).read_text())
        shared_dir = network_config.get("sharedDir", shared_dir)
        bots = network_config.get("members", [])

    if not is_rsi_enabled(network_config):
        print("[analyze] RSI feedback loop is disabled — exiting.")
        import sys; sys.exit(0)

    if not is_module_enabled(network_config, "analysis"):
        print("[analyze] analysis module is disabled — exiting.")
        import sys; sys.exit(0)

    if not bots:
        annotation_base = Path(shared_dir) / "annotations"
        bots = [d.name for d in annotation_base.iterdir() if d.is_dir()] if annotation_base.exists() else []

    cal = CalibrationLoader(shared_dir)
    all_proposals = []

    for bot_id in bots:
        history = load_metrics_range(shared_dir, bot_id, args.days)
        proposals = run_detectors(bot_id, history, args.days, shared_dir=shared_dir,
                                  network_config=network_config, cal=cal)

        for proposal in proposals:
            pattern_key = proposal.get("pattern_key", "")
            if already_proposed(shared_dir, pattern_key):
                print(f"[evolve/analyze] {bot_id}: skipping '{pattern_key}' — already pending")
                continue

            if args.dry_run:
                print(f"\n[DRY RUN] {bot_id}: {proposal['problem'][:100]}")
                print(f"  Type: {proposal['type']} | Confidence: {proposal.get('confidence', '?')}")
            else:
                proposal_id = write_proposal(proposal, shared_dir)
                if not proposal_id:
                    print(f"[evolve/analyze] {bot_id}: skipping '{pattern_key}' — concurrent duplicate detected")
                    continue
                print(f"[evolve/analyze] {bot_id}: wrote proposal {proposal_id} — {proposal['problem'][:60]}")
                # Mirror the routing fields write_proposal stamped on the
                # on-disk copy onto the batch dict so send_telegram_alert's
                # inbox-gating predicate sees the same surface/informational.
                apply_legacy_investigation_routing(proposal)
                all_proposals.append(proposal)

    if not args.dry_run and all_proposals:
        send_telegram_alert(
            all_proposals, network_config, shared_dir=Path(shared_dir),
        )

    print(f"[evolve/analyze] Done — {len(all_proposals)} proposal(s) generated")


if __name__ == "__main__":
    main()
