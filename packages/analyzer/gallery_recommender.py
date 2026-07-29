#!/usr/bin/env python3
"""
evolve/gallery_recommender.py — Gallery app recommender

Loads the bot's capability profile (from profile_builder.py) and the gallery
catalog, scores gallery apps against the profile vector, and writes
install_existing recommendations.

Prerequisite: run profile_builder.py first to generate profile.json.

Usage:
    python3 gallery_recommender.py --shared-dir /Users/Shared/evolve --bot team_bot_a
    python3 gallery_recommender.py --shared-dir /Users/Shared/evolve --bot team_bot_a --dry-run
    python3 gallery_recommender.py --shared-dir /Users/Shared/evolve --bot team_bot_a --threshold 0.20
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Gallery catalog lives alongside this package in packages/gallery/catalog.json
_GALLERY_CATALOG = Path(__file__).parent.parent.parent / "packages" / "gallery" / "catalog.json"

DEFAULT_THRESHOLD = 0.15   # Minimum fit score to generate a recommendation
MAX_RECOMMENDATIONS = 5    # Cap per run to avoid overwhelming the user


def load_profile(bot_id: str, shared_dir: Path) -> dict | None:
    """Load the bot's capability profile. Returns None if not built yet."""
    path = shared_dir / bot_id / "recommendations" / "profile.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def load_catalog() -> list[dict]:
    """Load the gallery app catalog."""
    if not _GALLERY_CATALOG.exists():
        return []
    try:
        return json.loads(_GALLERY_CATALOG.read_text())
    except Exception:
        return []


def score_app(app: dict, profile: dict) -> float:
    """Compute a fit score for a gallery app against a bot profile.

    Score = sum of profile_vector weights for the app's application_tags,
    plus a +0.2 affinity boost if the app's categories include the bot's
    profile_class.

    Returns 0.0 for already-installed apps (matched by pkg_id or name).
    """
    installed = set(profile.get("installed_pkg_ids", []))
    if app.get("pkg_id") in installed:
        return 0.0

    # Name-based dedup: catches scanner-detected manifests that lack a
    # gallery pkg_id but describe the same app.
    installed_names = set(profile.get("installed_app_names", []))
    app_name = (app.get("name") or "").lower().strip()
    if app_name and app_name in installed_names:
        return 0.0

    vector = profile.get("profile_vector", {})
    score = sum(vector.get(tag, 0.0) for tag in app.get("application_tags", []))

    # Profile class affinity boost
    profile_class = profile.get("profile_class", "")
    if profile_class and profile_class in app.get("categories", []):
        score += 0.20

    return round(score, 3)


def build_recommendations(
    bot_id: str,
    shared_dir: Path,
    threshold: float = DEFAULT_THRESHOLD,
    max_recs: int = MAX_RECOMMENDATIONS,
) -> list[dict]:
    """Build gallery recommendation objects for a bot. Does not write to disk."""
    profile = load_profile(bot_id, shared_dir)
    if not profile:
        return []

    catalog = load_catalog()
    if not catalog:
        return []

    # Score all apps and take the top ones above threshold
    scored = sorted(
        ((score_app(app, profile), app) for app in catalog),
        key=lambda x: -x[0],
    )
    top = [(s, app) for s, app in scored if s >= threshold][:max_recs]

    now_dt = datetime.now(timezone.utc)
    expiry = (now_dt + timedelta(days=90)).isoformat()

    recs = []
    for score, app in top:
        match_strength = (
            "strong" if score >= 0.40
            else "moderate" if score >= 0.25
            else "weak"
        )
        matching_tags = [
            t for t in app.get("application_tags", [])
            if t in profile.get("profile_vector", {})
        ]
        pkg_id = app.get("pkg_id", "")
        app_name = app.get("name", "")
        if not pkg_id or not app_name:
            continue  # skip malformed catalog entries

        recs.append({
            # Stable rec_id (no month suffix) so write_recommendations can
            # carry status across monthly runs — once a user dismisses a
            # gallery rec, it stays dismissed instead of re-appearing each
            # month under a new id.
            "rec_id": f"gal-{bot_id}-{pkg_id}",
            "schema_version": 1,
            "type": "app_recommendation",
            "source": "gallery",
            "bot_id": bot_id,
            "generated_at": now_dt.isoformat(),
            "expires_at": expiry,
            "confidence": min(score, 1.0),
            "category": "install_existing",
            "title": app_name,
            "rationale": app.get("description", ""),
            "match_strength": match_strength,
            "evidence": {
                "fit_score": score,
                "matching_tags": matching_tags,
                "profile_class": profile.get("profile_class"),
                "profile_vector_snapshot": {
                    t: profile["profile_vector"][t]
                    for t in matching_tags
                    if t in profile.get("profile_vector", {})
                },
            },
            "payload": {
                "gallery_pkg_id": pkg_id,
                "gallery_app": app,
                "draft_manifest": None,
            },
            "status": "new",
            "status_updated_at": None,
            "status_note": None,
        })

    return recs


def run_gallery_recommender(
    bot_id: str,
    shared_dir: Path,
    threshold: float = DEFAULT_THRESHOLD,
    dry_run: bool = False,
) -> list[dict]:
    """Run the gallery recommender for one bot. Returns generated recommendations."""
    from recommendations import write_recommendations

    profile = load_profile(bot_id, shared_dir)
    if not profile:
        print(f"[gallery] {bot_id}: no profile.json — run profile_builder.py first")
        return []

    catalog = load_catalog()
    if not catalog:
        print(f"[gallery] catalog not found at {_GALLERY_CATALOG}")
        return []

    recs = build_recommendations(bot_id, shared_dir, threshold)

    if dry_run:
        print(f"[gallery] {bot_id}: {len(recs)} recommendation(s) [dry-run]")
        for r in recs:
            print(f"  [{r['match_strength']}] {r['title']} "
                  f"(score={r['confidence']:.2f}, tags={r['evidence']['matching_tags']})")
        return recs

    write_recommendations(bot_id, recs, shared_dir)
    print(f"[gallery] {bot_id}: {len(recs)} gallery recommendation(s) written")
    return recs


def main() -> None:
    parser = argparse.ArgumentParser(description="Gallery app recommender")
    parser.add_argument("--shared-dir", default="/Users/Shared/evolve")
    parser.add_argument("--bot", dest="bot_id", required=True)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="Minimum fit score (default: 0.15)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print recommendations without writing")
    args = parser.parse_args()
    run_gallery_recommender(args.bot_id, Path(args.shared_dir), args.threshold, args.dry_run)


if __name__ == "__main__":
    main()
