#!/usr/bin/env python3
"""
evolve/expansion.py — Proactive application expansion engine

Identifies application gaps and suggests new applications based on:
  1. Session topic clustering — what does the user ask about repeatedly
     that isn't covered by a named application manifest?
  2. User profile analysis — what domains does the user operate in
     (from USER.md, MEMORY.md) where no structured application exists?
  3. Improvisation detection — sessions where the bot solved something
     ad-hoc that could be formalized into a reusable application

This runs monthly (first Sunday, 04:00 AM) and generates suggestions
in the form of investigation proposals that Pod_admin can accept or dismiss.
Unlike other detectors that flag problems, these are opportunities.

Design:
  - Does NOT generate config_change or script_change proposals
  - Generates investigation proposals only — "consider building X"
  - Requires Pod_admin's explicit approval before any application manifest is created
  - Cost: one tier3 LLM call per run (topic clustering), zero otherwise

Output: proposals written to shared/proposals/pending/ like other detectors.
  These are tagged with type="investigation" and source="expansion_engine".

Usage:
    python3 expansion.py --network config/network.json
    python3 expansion.py --shared-dir /Users/Shared/evolve --bot admin_bot
    python3 expansion.py --shared-dir /Users/Shared/evolve --bot admin_bot --dry-run
    python3 expansion.py --shared-dir /Users/Shared/evolve --bot admin_bot --report
"""

import argparse
import json
import re
import subprocess
import uuid
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path

from analyze import write_proposal


EXPANSION_VERSION = 1
LOOKBACK_DAYS = 30          # session history window
MIN_TOPIC_SESSIONS = 3      # topic must appear in at least N sessions to be interesting
MAX_SUGGESTIONS = 5         # max proposals per run

# Map expansion priority → recommendation confidence
_PRIORITY_CONFIDENCE = {"high": 0.75, "medium": 0.60, "low": 0.45}


# ── Known application domains ─────────────────────────────────────────────────
# These already have manifests (or are well-covered). We flag gaps, not duplicates.

# Generic application domains — deployment-agnostic.
# The expansion engine reads actual approved manifests at runtime and merges
# them with this base set. Deployment-specific applications appear in manifests.
KNOWN_APPLICATION_DOMAINS = {
    "health-nutrition", "health-fitness", "calendar", "email", "travel",
    "home-management", "task-management", "evolve-system",
    "creative-writing", "slack-comms", "document-generation",
    "web-search", "general",
}

# Stop words for topic extraction
TOPIC_STOP_WORDS = {
    "the", "a", "an", "to", "of", "in", "and", "or", "for", "this", "that",
    "it", "is", "was", "are", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "shall", "can", "i", "you", "we", "he", "she", "they", "me",
    "him", "her", "us", "them", "my", "your", "our", "their", "its",
    "what", "which", "who", "how", "when", "where", "why", "not", "no",
    "yes", "so", "but", "if", "then", "at", "by", "with", "from", "into",
    "on", "up", "out", "about", "just", "also", "well", "very", "really",
    "please", "let", "get", "got", "go", "going", "make", "made",
}


# ── Data loading ──────────────────────────────────────────────────────────────

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


def load_user_profile(workspace_dir: str) -> str:
    """Load USER.md and MEMORY.md for user profile context."""
    profile_parts = []
    for fname in ("USER.md", "MEMORY.md"):
        p = Path(workspace_dir) / fname
        if p.exists():
            try:
                content = p.read_text()[:2000]
                profile_parts.append(f"=== {fname} ===\n{content}")
            except Exception:
                pass
    return "\n\n".join(profile_parts)


def load_existing_proposal_keys(shared_dir: str) -> set[str]:
    """Load pattern_keys of proposals already in the queue (avoid re-proposing)."""
    keys: set[str] = set()
    for status in ("pending", "reviewed", "approved"):
        d = Path(shared_dir) / "proposals" / status
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                p = json.loads(f.read_text())
                if p.get("pattern_key"):
                    keys.add(p["pattern_key"])
            except Exception:
                pass
    return keys


# ── Topic extraction ──────────────────────────────────────────────────────────

def extract_topics_from_summaries(summaries: list[dict]) -> Counter:
    """
    Extract a word frequency counter from session outcomes and capabilities.
    Filters stop words and short tokens. Returns Counter of meaningful terms.
    """
    word_counts: Counter = Counter()

    for s in summaries:
        # Use outcome text as the primary signal (what the session was actually about)
        outcome = s.get("outcome", "")
        # Also pull from applications_invoked (structured signal)
        caps = s.get("applications_invoked", [])

        # Tokenize outcome
        words = re.findall(r"[a-z]{4,}", outcome.lower())
        word_counts.update(w for w in words if w not in TOPIC_STOP_WORDS)

        # Applications already named
        for cap in caps:
            parts = cap.replace("-", " ").split()
            word_counts.update(p for p in parts if p not in TOPIC_STOP_WORDS and len(p) > 3)

    return word_counts


def identify_recurring_themes(
    summaries: list[dict],
    known_applications: set[str],
    min_sessions: int,
) -> list[dict]:
    """
    Find topic clusters that appear in multiple sessions but aren't
    covered by a known application.

    Returns list of {theme, sessions, sample_outcomes, gap_score}
    sorted by gap_score (most interesting first).
    """
    # Group sessions by their primary inferred topic
    theme_sessions: dict[str, list[dict]] = {}

    for s in summaries:
        outcome = s.get("outcome", "").lower()
        caps = s.get("applications_invoked", [])

        # If all applications are known → skip (already covered)
        unknown_apps = [c for c in caps if c not in known_applications]
        has_uncovered = bool(unknown_apps) or not caps

        if not has_uncovered and caps:
            continue  # fully covered session

        # Extract 2-3 word phrases as potential themes
        words = [w for w in re.findall(r"[a-z]{4,}", outcome)
                 if w not in TOPIC_STOP_WORDS]
        if not words:
            continue

        # Simple theme: most common meaningful word in this session's outcome
        theme = max(set(words), key=words.count) if words else None
        if not theme:
            continue

        if theme not in theme_sessions:
            theme_sessions[theme] = []
        theme_sessions[theme].append(s)

    # Filter to themes with enough signal
    themes = []
    for theme, sessions in theme_sessions.items():
        if len(sessions) < min_sessions:
            continue

        # Skip if this theme maps closely to a known application keyword
        known_app_words = {
            w for cap in known_applications
            for w in cap.replace("-", " ").split()
            if len(w) > 3
        }
        if theme in known_app_words:
            continue

        sample_outcomes = [s.get("outcome", "")[:100] for s in sessions[:3]]
        correction_rate = sum(1 for s in sessions if s.get("correction_count", 0) > 0) / len(sessions)
        efficiency_rate = sum(1 for s in sessions if s.get("efficiency_flag")) / len(sessions)

        # Gap score: sessions with corrections or inefficiency → higher priority
        gap_score = len(sessions) * (1 + correction_rate * 0.5 + efficiency_rate * 0.3)

        themes.append({
            "theme": theme,
            "sessions": len(sessions),
            "sample_outcomes": sample_outcomes,
            "correction_rate": round(correction_rate, 2),
            "efficiency_rate": round(efficiency_rate, 2),
            "gap_score": round(gap_score, 1),
        })

    return sorted(themes, key=lambda x: -x["gap_score"])


# ── LLM enrichment ────────────────────────────────────────────────────────────

def enrich_themes_with_llm(
    themes: list[dict],
    user_profile: str,
    network_config: dict,
    max_suggestions: int,
) -> list[dict]:
    """
    Use tier3 LLM to turn raw theme clusters into specific capability suggestions.
    Returns list of enriched suggestion dicts.
    Falls back to raw themes if LLM unavailable.
    """
    if not themes:
        return []

    models = network_config.get("models", {})
    tier3_model = models.get("tier3", "")
    if not tier3_model:
        # Fallback: generate basic suggestions without LLM
        return [_basic_suggestion(t) for t in themes[:max_suggestions]]

    # Compact theme summary for the prompt
    theme_lines = []
    for t in themes[:max_suggestions * 2]:
        sample = " | ".join(t["sample_outcomes"][:2])
        theme_lines.append(
            f"Theme '{t['theme']}': {t['sessions']} sessions, "
            f"correction_rate={t['correction_rate']:.0%}, "
            f"sample: {sample[:120]}"
        )
    themes_text = "\n".join(theme_lines)

    prompt = f"""You are analyzing an AI assistant's usage patterns to identify application gaps.
The assistant already has structured applications for: {', '.join(sorted(KNOWN_APPLICATION_DOMAINS))}.

The following topic themes appear repeatedly in sessions that aren't covered by existing applications:
{themes_text}

User profile context:
{user_profile[:800]}

For each theme that represents a GENUINE, ACTIONABLE application gap (not noise), output ONE JSON line:
  {{
    "theme": "original theme word",
    "application_name": "Short Application Name (3-5 words)",
    "application_id": "kebab-case-id",
    "description": "1-2 sentences: what this application would do and why it's valuable",
    "evidence": "1 sentence: what in the usage data supports this gap",
    "priority": "high|medium|low",
    "related_to_existing": null or "closest existing application name"
  }}

Rules:
- Only suggest if the theme appears genuinely unserved (not just a variation of existing)
- Priority high: appears frequently, has corrections/inefficiency (bot is struggling without structure)
- Priority medium: appears frequently, smooth sessions (formalization would improve consistency)
- Priority low: appears occasionally, no clear problem (nice-to-have)
- Skip themes that are too vague (common English words with no domain meaning)
- Skip themes clearly covered by existing applications (e.g. "send" → email application exists)
- Output NO_GAPS if no genuine gaps found

Output only JSON lines, no other text."""

    try:
        # polling-bypass: one-shot LLM completion call (not polling)
        result = subprocess.run(
            ["openclaw", "llm", "complete",
             "--model", tier3_model,
             "--max-tokens", "600",
             "--prompt", prompt],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode != 0 or "NO_GAPS" in result.stdout:
            return [_basic_suggestion(t) for t in themes[:max_suggestions]]

        suggestions = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
                if d.get("application_name") and d.get("description"):
                    # Merge with original theme data
                    original = next((t for t in themes if t["theme"] == d.get("theme")), {})
                    d.update({
                        "sessions": original.get("sessions", 0),
                        "sample_outcomes": original.get("sample_outcomes", []),
                        "correction_rate": original.get("correction_rate", 0),
                        "gap_score": original.get("gap_score", 0),
                        "enriched_by_llm": True,
                    })
                    suggestions.append(d)
            except json.JSONDecodeError:
                pass

        if not suggestions:
            return [_basic_suggestion(t) for t in themes[:max_suggestions]]
        return suggestions[:max_suggestions]

    except (subprocess.TimeoutExpired, Exception):
        return [_basic_suggestion(t) for t in themes[:max_suggestions]]


def _basic_suggestion(theme: dict) -> dict:
    """Fallback suggestion without LLM enrichment."""
    theme_word = theme["theme"]
    return {
        "theme": theme_word,
        "application_name": f"{theme_word.title()} Support",
        "application_id": f"{theme_word}-support",
        "description": (
            f"Structured application for '{theme_word}' tasks, which appear in "
            f"{theme['sessions']} recent sessions without a dedicated application."
        ),
        "evidence": f"Appears in {theme['sessions']} sessions with {theme['correction_rate']:.0%} correction rate.",
        "priority": "high" if theme["gap_score"] > 10 else "medium" if theme["gap_score"] > 5 else "low",
        "related_to_existing": None,
        "sessions": theme["sessions"],
        "sample_outcomes": theme["sample_outcomes"],
        "correction_rate": theme["correction_rate"],
        "gap_score": theme["gap_score"],
        "enriched_by_llm": False,
    }


# ── Proposal generation ───────────────────────────────────────────────────────

def generate_expansion_proposals(
    suggestions: list[dict],
    bot_id: str,
    shared_dir: str,
    existing_keys: set[str],
) -> list[dict]:
    """Turn application suggestions into investigation proposals."""
    proposals = []

    for suggestion in suggestions:
        app_id = suggestion.get("application_id", suggestion["theme"])
        pattern_key = f"{bot_id}:expansion:{app_id}"

        if pattern_key in existing_keys:
            continue

        priority = suggestion.get("priority", "medium")
        confidence = {"high": 0.75, "medium": 0.60, "low": 0.45}.get(priority, 0.60)

        evidence = suggestion.get("evidence", "")
        sample_outcomes = suggestion.get("sample_outcomes", [])
        related = suggestion.get("related_to_existing")

        problem = (
            f"Potential application gap: '{suggestion['application_name']}'. "
            f"{evidence}"
        )
        if sample_outcomes:
            problem += f" Sample sessions: {' | '.join(o[:60] for o in sample_outcomes[:2] if o)}."

        proposed_change = (
            f"Consider creating an application manifest for '{suggestion['application_name']}'. "
            f"{suggestion.get('description', '')} "
        )
        if related:
            proposed_change += f"This is related to the existing '{related}' application but covers different ground. "
        proposed_change += (
            "Run: evolve-admin application new to create a manifest. "
            "Define goals, test cases, and success metrics before building."
        )

        proposals.append({
            "pattern_key": pattern_key,
            "target_bot": bot_id,
            "type": "investigation",
            "category": "improvement",
            "source": "expansion_engine",
            "problem": problem,
            "data": {
                "application_name": suggestion["application_name"],
                "application_id": app_id,
                "priority": priority,
                "sessions": suggestion.get("sessions", 0),
                "correction_rate": suggestion.get("correction_rate", 0),
                "gap_score": suggestion.get("gap_score", 0),
                "enriched_by_llm": suggestion.get("enriched_by_llm", False),
                "related_to_existing": related,
            },
            "root_cause": (
                f"The bot handles '{suggestion['application_name']}' tasks ad-hoc without a "
                f"structured application manifest. This means no defined tests, no satisfaction "
                f"tracking, and no systematic improvement. Formalizing it would close the "
                f"feedback loop for this domain."
            ),
            "alternatives_considered": [
                {"hypothesis": "Theme is noise / one-off sessions",
                 "rejected_because": f"Appears in {suggestion.get('sessions', 0)} sessions over {LOOKBACK_DAYS} days"},
            ],
            "confidence": confidence,
            "proposed_change": {
                "description": proposed_change,
                "action": "investigation",
            },
            "minimum_test": f"Application manifest created and first satisfaction score recorded within 30 days",
            "risk": "none",
            "forge_required": False,
        })

    return proposals


# ── Self-assessment detector ──────────────────────────────────────────────────

def detect_detector_staleness(shared_dir: str, bot_id: str) -> Optional[dict]:
    """
    Evolve self-assessment: are any detectors consistently generating proposals
    that Pod_admin always rejects? If a detector has a >80% rejection rate over
    10+ proposals, it's miscalibrated and should be reviewed.

    This is the 'Evolve reviews its own outcomes' piece from Phase 3.
    """
    from pathlib import Path as _Path
    import json as _json

    rejection_path = _Path(shared_dir) / "proposals" / "rejected"
    if not rejection_path.exists():
        return None

    pattern_key_counts: dict[str, int] = {}   # total proposals per detector prefix
    rejected_counts: dict[str, int] = {}       # rejected proposals per detector prefix

    # Count rejections
    for f in rejection_path.glob("*.json"):
        try:
            p = _json.loads(f.read_text())
            pk = p.get("pattern_key", "")
            if not pk:
                continue
            # Detector prefix = everything before the last colon segment
            parts = pk.split(":")
            detector_prefix = ":".join(parts[:2]) if len(parts) >= 2 else pk
            rejected_counts[detector_prefix] = rejected_counts.get(detector_prefix, 0) + 1
        except Exception:
            pass

    # Count all proposals (pending + approved + rejected + done)
    for status in ("pending", "approved", "rejected", "done"):
        d = _Path(shared_dir) / "proposals" / status
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                p = _json.loads(f.read_text())
                pk = p.get("pattern_key", "")
                if not pk:
                    continue
                parts = pk.split(":")
                detector_prefix = ":".join(parts[:2]) if len(parts) >= 2 else pk
                pattern_key_counts[detector_prefix] = pattern_key_counts.get(detector_prefix, 0) + 1
            except Exception:
                pass

    # Find detectors with high rejection rate
    stale_detectors = []
    for detector, total in pattern_key_counts.items():
        if total < 5:  # need enough signal
            continue
        rejected = rejected_counts.get(detector, 0)
        rejection_rate = rejected / total
        if rejection_rate >= 0.80:
            stale_detectors.append({
                "detector": detector,
                "total_proposals": total,
                "rejected": rejected,
                "rejection_rate": round(rejection_rate, 2),
            })

    if not stale_detectors:
        return None

    worst = sorted(stale_detectors, key=lambda x: -x["rejection_rate"])[0]

    pattern_key = f"{bot_id}:evolve_self_assessment:detector_staleness"
    return {
        "pattern_key": pattern_key,
        "target_bot": bot_id,
        "type": "investigation",
        "category": "alert",
        "source": "expansion_engine",
        "problem": (
            f"{len(stale_detectors)} Evolve detector(s) have rejection rates \u226580%. "
            f"Worst: '{worst['detector']}' at {worst['rejection_rate']:.0%} "
            f"({worst['rejected']}/{worst['total_proposals']} proposals rejected). "
            f"These detectors are generating noise, not value."
        ),
        "data": {
            "stale_detectors": stale_detectors,
        },
        "root_cause": (
            "One or more detectors are consistently generating proposals that Pod_admin rejects. "
            "This means the detector's threshold is too sensitive, the pattern it detects "
            "is not a real problem, or the proposed fix is wrong. The detector itself needs "
            "to be recalibrated or disabled."
        ),
        "alternatives_considered": [],
        "confidence": 0.85,
        "proposed_change": {
            "description": (
                "Review the rejection history for each flagged detector. "
                "For each rejected proposal, check: was the detected pattern real? "
                "Was the proposed fix wrong? Was the threshold too low? "
                "Update the detector logic in analyze.py accordingly, or add the "
                "pattern to a suppression list if it's a known false positive."
            ),
            "action": "investigation",
        },
        "minimum_test": "Rejection rate drops below 50% for flagged detectors over next 30 days",
        "risk": "none",
        "forge_required": False,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def run_expansion(
    bot_id: str,
    shared_dir: str,
    network_config: dict,
    days: int = LOOKBACK_DAYS,
    dry_run: bool = False,
) -> list[dict]:
    """
    Run the full expansion engine. Returns list of proposals generated.
    """
    workspace_dir = network_config.get("workspaceDir",
                                       str(Path.home() / ".openclaw" / "workspace"))

    print(f"[expansion] Starting for {bot_id} — {days}d lookback")

    # Load data
    summaries = load_session_summaries(shared_dir, bot_id, days)
    manifests = load_manifests(shared_dir, bot_id)
    user_profile = load_user_profile(workspace_dir)
    existing_keys = load_existing_proposal_keys(shared_dir)

    print(f"[expansion] {len(summaries)} session summaries, {len(manifests)} manifests")

    # Build set of known application names from manifests
    known_from_manifests = {m.get("id", "") for m in manifests if m.get("id")}
    known_applications = KNOWN_APPLICATION_DOMAINS | known_from_manifests

    # Theme analysis
    themes = identify_recurring_themes(summaries, known_applications, MIN_TOPIC_SESSIONS)
    print(f"[expansion] {len(themes)} recurring themes identified")

    proposals = []

    # LLM enrichment and proposal generation
    if themes:
        suggestions = enrich_themes_with_llm(themes, user_profile, network_config, MAX_SUGGESTIONS)
        expansion_proposals = generate_expansion_proposals(suggestions, bot_id, shared_dir, existing_keys)
        proposals.extend(expansion_proposals)
        print(f"[expansion] {len(expansion_proposals)} expansion proposal(s)")

    # Self-assessment
    self_assessment = detect_detector_staleness(shared_dir, bot_id)
    if self_assessment:
        sa_key = self_assessment["pattern_key"]
        if sa_key not in existing_keys:
            proposals.append(self_assessment)
            print(f"[expansion] Detector staleness proposal generated")

    # Write proposals (existing proposals system)
    if not dry_run:
        for proposal in proposals:
            proposal_id = write_proposal(proposal, shared_dir)
            print(f"[expansion] Wrote proposal: {proposal_id}")
    else:
        for p in proposals:
            print(f"[expansion] [dry-run] {p['pattern_key']}: {p['problem'][:80]}")

    # Also write to the recommendations engine (alongside proposals, not replacing)
    if not dry_run:
        _write_expansion_recommendations(proposals, bot_id, shared_dir)

    return proposals


def _write_expansion_recommendations(
    proposals: list[dict],
    bot_id: str,
    shared_dir: str,
) -> None:
    """Convert expansion proposals to recommendation objects and persist them.

    Runs alongside the existing write_proposal() path — the proposals system
    is preserved. This bridges expansion output into the unified recommendations
    schema consumed by the admin UI Applications tab and Better Engine.
    """
    try:
        from recommendations import write_recommendations
    except Exception as e:
        print(f"[expansion] Warning: could not import recommendations module: {e}")
        return

    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now_dt = _dt.now(_tz.utc)
    expiry = (now_dt + _td(days=90)).isoformat()
    month = now_dt.strftime("%Y-%m")

    recs = []
    for p in proposals:
        # Only convert application gap proposals (not self-assessment)
        source = p.get("source", "")
        if source != "expansion_engine":
            continue
        data = p.get("data", {})
        app_id = data.get("application_id", "")
        if not app_id:
            continue

        priority = data.get("priority", "medium")
        confidence = _PRIORITY_CONFIDENCE.get(priority, 0.60)

        recs.append({
            "rec_id": f"exp-{bot_id}-{app_id}-{month}",
            "schema_version": 1,
            "type": "app_recommendation",
            "source": "expansion",
            "bot_id": bot_id,
            "generated_at": now_dt.isoformat(),
            "expires_at": expiry,
            "confidence": confidence,
            "category": "new_application",
            "title": data.get("application_name", app_id.replace("-", " ").title()),
            "rationale": p.get("problem", ""),
            "evidence": {
                "session_count": data.get("sessions", 0),
                "application_domain": app_id,
                "correction_rate": data.get("correction_rate", 0),
                "gap_score": data.get("gap_score", 0),
                "sample_outcomes": p.get("data", {}).get("sample_outcomes", []),
                "enriched_by_llm": data.get("enriched_by_llm", False),
            },
            "payload": {
                "gallery_pkg_id": None,
                "draft_manifest": None,
                "expansion_proposal_key": p.get("pattern_key"),
            },
            "status": "new",
            "status_updated_at": None,
            "status_note": None,
        })

    if recs:
        write_recommendations(bot_id, recs, Path(shared_dir))
        print(f"[expansion] {len(recs)} recommendation(s) written to recommendations engine")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve proactive expansion engine")
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--network", default=str(resolve_network_path()), help="Path to network.json")
    parser.add_argument("--bot", dest="bot_id", required=True)
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true",
                        help="Show theme analysis without generating proposals")
    args = parser.parse_args()
    # Module enable/disable check
    try:
        from evolve_config import load_config as _lc2, is_module_enabled as _ime
        _cfg2 = _lc2(getattr(args, 'network', None))
        if not _ime(_cfg2, "expansion"):
            print(f"[expansion] expansion module is disabled — exiting.")
            import sys as _sys2; _sys2.exit(0)
    except Exception:
        pass

    shared_dir = args.shared_dir
    network_config: dict = {}
    if args.network:
        network_config = json.loads(Path(args.network).read_text())
        shared_dir = network_config.get("sharedDir", shared_dir)

    # Self-gate: only run on the first Sunday of the month (day <= 7)
    # launchd fires every Sunday; we skip the rest.
    if not args.dry_run and not args.report:
        today = date.today()
        if today.day > 7:
            print(f"[expansion] Not first Sunday of month (day={today.day}) — skipping")
            return

    if args.report:
        summaries = load_session_summaries(shared_dir, args.bot_id, args.days)
        manifests = load_manifests(shared_dir, args.bot_id)
        known = KNOWN_APPLICATION_DOMAINS | {m.get("id", "") for m in manifests}
        themes = identify_recurring_themes(summaries, known, MIN_TOPIC_SESSIONS)
        print(f"Expansion report — {args.bot_id} ({args.days}d)")
        print(f"{'Theme':<20} {'Sessions':>8} {'CorrRate':>9} {'GapScore':>9}")
        print("-" * 50)
        for t in themes[:15]:
            print(f"{t['theme']:<20} {t['sessions']:>8} {t['correction_rate']:>8.0%} {t['gap_score']:>9.1f}")
        return

    run_expansion(args.bot_id, shared_dir, network_config, args.days, args.dry_run)


if __name__ == "__main__":
    main()
