#!/usr/bin/env python3
"""
evolve/profile_builder.py — Bot capability profile aggregator

Reads session_summary records from annotation JSONL files and builds a
weighted capability profile for each bot. The profile is used by:
  - gallery_recommender.py — to match gallery apps against bot needs
  - ideation.py (future) — to focus novel app concept generation

Output: {sharedDir}/{botId}/recommendations/profile.json

Runs as part of the monthly recommendations cron, before gallery_recommender.

Usage:
    python3 profile_builder.py --shared-dir /Users/Shared/evolve --bot team_bot_a
    python3 profile_builder.py --shared-dir /Users/Shared/evolve --all-bots
    python3 profile_builder.py --shared-dir /Users/Shared/evolve --bot team_bot_a --report
"""

# identity: this module was SWEPT onto applications.app_identity.resolve_app_id in AL-1.4b (area 4c):
# ``load_installed_pkg_ids`` carried a ``pkg_id or id`` chain and now calls the
# resolver. The remaining mentions are its docstring (explaining why the name
# and the pkg_id-first ordering must stay — gallery_recommender compares this
# set against gallery/index.json's pkg_id column) and one prose comment.

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from evolve_admin.applications.app_identity import (  # pyright: ignore[reportMissingImports]
    resolve_app_id,
)

# ── Profile class definitions ─────────────────────────────────────────────────
# Each class is defined by a set of capability domains that indicate it.
# A bot's profile_class is the class whose member domains have the highest
# aggregate weight in the profile_vector.

PROFILE_CLASS_DOMAINS: list[tuple[str, list[str]]] = [
    ("personal-lifestyle",        ["health-nutrition", "health-fitness", "travel", "home-management"]),
    ("professional-productivity", ["task-management", "calendar", "document-generation"]),
    ("executive-admin",           ["calendar", "email", "travel", "slack-comms"]),
    ("creative",                  ["creative-writing", "document-generation"]),
    ("technical",                 ["evolve-system", "task-management", "document-generation"]),
]

# Minimum aggregate domain weight to assign a class (below = "mixed")
PROFILE_CLASS_THRESHOLD = 0.20


def load_session_summaries(shared_dir: Path, bot_id: str, days: int) -> list[dict]:
    """Load session_summary records from per-bot annotation JSONL files."""
    annotation_dir = shared_dir / "annotations" / bot_id
    summaries = []
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        p = annotation_dir / f"{d}.jsonl"
        if not p.exists():
            continue
        try:
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
        except Exception:
            pass
    return summaries


def load_installed_pkg_ids(shared_dir: Path, bot_id: str) -> list[str]:
    """Load the resolved app id of every installed manifest for this bot.

    AL-1.4b: the per-manifest id comes from the ONE resolver instead of the
    inline ``pkg_id or id`` chain this function used to carry. That chain was
    the first two rungs of the canonical order, so any manifest carrying
    either field answers exactly as before; the resolver additionally reaches
    the v7-arc ``spec_id`` / ``instance_id`` rungs, which the old chain read
    as "" — dropping those apps out of the installed set and letting the
    gallery recommender re-recommend an app the bot already has.

    The name stays ``load_installed_pkg_ids``, and the values are still
    ``pkg_id``-first, because ``gallery_recommender.score_app`` compares this
    set against ``gallery/index.json``'s ``pkg_id`` column — the gallery
    package key, which is exactly what a gallery-installed manifest resolves
    to (``pkg_id`` leads the legacy chain).
    """
    manifest_dir = shared_dir / "applications" / bot_id
    if not manifest_dir.exists():
        return []
    ids = []
    for f in manifest_dir.glob("manifest-*.json"):
        try:
            m = json.loads(f.read_text())
            app_id = resolve_app_id(m)
            if app_id:
                ids.append(app_id)
        except Exception:
            pass
    return ids


def load_installed_app_names(shared_dir: Path, bot_id: str) -> list[str]:
    """Load normalized names of all installed app manifests for this bot.

    Used alongside pkg_id matching to catch duplicates where the manifest
    was scanner-detected (no gallery pkg_id) but describes the same app.
    """
    manifest_dir = shared_dir / "applications" / bot_id
    if not manifest_dir.exists():
        return []
    names = []
    for f in manifest_dir.glob("manifest-*.json"):
        try:
            m = json.loads(f.read_text())
            name = m.get("name", "")
            if name:
                names.append(name.lower().strip())
        except Exception:
            pass
    return names


def classify_profile(vector: dict[str, float]) -> str:
    """Assign a profile class based on aggregate domain weights."""
    if not vector:
        return "mixed"
    class_scores: dict[str, float] = {}
    for class_name, domains in PROFILE_CLASS_DOMAINS:
        class_scores[class_name] = sum(vector.get(d, 0.0) for d in domains)
    best_class, best_score = max(class_scores.items(), key=lambda x: x[1])
    return best_class if best_score >= PROFILE_CLASS_THRESHOLD else "mixed"


def build_profile(bot_id: str, shared_dir: Path, days: int = 90) -> dict:
    """Build a capability profile from session history.

    Returns a profile dict ready to write to profile.json.
    """
    summaries = load_session_summaries(shared_dir, bot_id, days)
    installed = load_installed_pkg_ids(shared_dir, bot_id)
    installed_names = load_installed_app_names(shared_dir, bot_id)

    # Tally sessions per capability domain
    domain_counts: dict[str, int] = {}
    for s in summaries:
        for cap in s.get("applications_invoked", []):
            domain_counts[cap] = domain_counts.get(cap, 0) + 1

    # Normalise to 0-1 (share of all application-tagged sessions)
    total = sum(domain_counts.values()) or 1
    profile_vector = {
        k: round(v / total, 3)
        for k, v in sorted(domain_counts.items(), key=lambda x: -x[1])
    }

    profile_class = classify_profile(profile_vector)

    # Dominant caps: top 5 by session count
    dominant_caps = sorted(domain_counts, key=lambda k: -domain_counts[k])[:5]

    # Unique days with at least one session
    active_days = len({s.get("ts", "")[:10] for s in summaries if s.get("ts")})

    return {
        "schema_version": 1,
        "bot_id": bot_id,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "total_sessions": len(summaries),
        "usage_days": active_days,
        "profile_vector": profile_vector,
        "profile_class": profile_class,
        "dominant_caps": dominant_caps,
        "installed_pkg_ids": installed,
        "installed_app_names": installed_names,
    }


def write_profile(profile: dict, bot_id: str, shared_dir: Path) -> None:
    """Write profile.json to {sharedDir}/{botId}/recommendations/."""
    recs_dir = shared_dir / bot_id / "recommendations"
    recs_dir.mkdir(parents=True, exist_ok=True)
    profile_path = recs_dir / "profile.json"
    content = json.dumps(profile, indent=2)
    try:
        profile_path.write_text(content)
    except PermissionError:
        fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        subprocess.run(["sudo", "/bin/cp", tmp, str(profile_path)],
                       capture_output=True, check=False)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def run_profile_builder(bot_id: str, shared_dir: Path, days: int = 90) -> dict:
    """Build and persist a bot capability profile. Returns the profile dict."""
    profile = build_profile(bot_id, shared_dir, days)
    write_profile(profile, bot_id, shared_dir)
    print(
        f"[profile] {bot_id}: {profile['total_sessions']} sessions over {days}d, "
        f"class={profile['profile_class']}, "
        f"top_caps={profile['dominant_caps'][:3]}"
    )
    return profile


def main() -> None:
    parser = argparse.ArgumentParser(description="Build bot capability profile")
    parser.add_argument("--shared-dir", default="/Users/Shared/evolve")
    parser.add_argument("--bot", dest="bot_id", help="Single bot to profile")
    parser.add_argument("--all-bots", action="store_true", help="Profile all bots with annotation data")
    parser.add_argument("--days", type=int, default=90, help="Lookback window in days")
    parser.add_argument("--report", action="store_true", help="Print profile without writing")
    args = parser.parse_args()

    shared_dir = Path(args.shared_dir)

    if args.all_bots:
        ann_dir = shared_dir / "annotations"
        bots = [d.name for d in ann_dir.iterdir() if d.is_dir()] if ann_dir.exists() else []
    elif args.bot_id:
        bots = [args.bot_id]
    else:
        parser.error("Specify --bot BOT_ID or --all-bots")

    for bot in bots:
        if args.report:
            profile = build_profile(bot, shared_dir, args.days)
            print(json.dumps(profile, indent=2))
        else:
            run_profile_builder(bot, shared_dir, args.days)


if __name__ == "__main__":
    main()
