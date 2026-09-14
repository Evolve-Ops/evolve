"""Distinct-app dedup guard — Atlas conflation regression.

Pins the fix for incident-atlas-app-conflation-2026-06-22: the scanner's
file-overlap dedup collapsed four genuinely-distinct Atlas apps (daily-digest,
article-capture, on-demand-research, the moderation guard) into one Frankenstein
manifest, because they *share a library + data substrate by design*
(``scripts/atlas_lib/`` + ``archive/index.json`` + ``atlas/*`` — see
docs/atlas-app-manifests/README.md). On top of that, the survivor's
description/identity.purpose flipped every scan ("longest wins").

The fixtures below mirror the REAL atlas manifests (file lists + identities
extracted read-only from the live pod on 2026-06-22; no live bot data is
committed). They prove:

  1. two distinct apps that share most of their files are NOT merged;
  2. a survivor's identity is STABLE across two consecutive scans;
  3. genuine duplicates (same name) and thin re-mints STILL merge (no over-drop);
  4. the identity-fill-only rule in _merge_two_manifests;
  5. the evidence-overlap matcher won't re-absorb a re-detected distinct app.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402


# ── Atlas-derived file lists (real paths, shared substrate is deliberate) ─────

_ATLAS_LIB = [
    "scripts/atlas_lib/__init__.py",
    "scripts/atlas_lib/classifier.py",
    "scripts/atlas_lib/fetchers.py",
    "scripts/atlas_lib/archive.py",
    "scripts/atlas_lib/composer.py",
    "scripts/atlas_lib/hashing.py",
    "scripts/atlas_lib/telegram_api.py",
    "scripts/atlas_lib/config.py",
]
_SHARED = _ATLAS_LIB + ["atlas/sources.json", "archive/index.json"]

_DIGEST_FILES = _SHARED + [
    "scripts/atlas_digest.py",
    "scripts/atlas-digest-cron.sh",
    "digest/2026-06-05.md",
]
_CAPTURE_FILES = _SHARED + [
    "scripts/atlas_capture.py",
    "atlas/optout.json",
    "atlas/.capture-salt",
    "atlas/member-hash-salt.bin",
]


def _write(caps_dir: Path, app_id: str, **fields) -> Path:
    payload = {"id": app_id, **fields}
    p = caps_dir / f"{app_id}.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


def _daily_digest(caps_dir: Path) -> Path:
    return _write(
        caps_dir, "atlas-daily-digest",
        name="Atlas Daily Digest",
        objective="Give a configured community a single concise morning digest "
                  "of what changed in the agent ecosystem, classified for skimming.",
        description="Surfaces ecosystem developments to an enthusiast community "
                    "in a single daily message, classified into five buckets.",
        identity={"purpose": "Surface ecosystem developments daily, classified."},
        evidence_files=list(_DIGEST_FILES),
        created_at="2026-06-07T20:34:00Z",
        confidence=0.9,
    )


def _article_capture(caps_dir: Path) -> Path:
    return _write(
        caps_dir, "atlas-article-capture",
        name="Atlas Article Capture",
        objective="Make every URL a community member shares into a classified, "
                  "summarized, opt-out-respecting archive entry.",
        description="When a member posts a URL: fetch, summarize, classify, "
                    "archive, react with the bucket emoji.",
        identity={"purpose": "Capture and classify member-shared URLs."},
        evidence_files=list(_CAPTURE_FILES),
        created_at="2026-06-11T19:29:26Z",
        confidence=0.85,
    )


# ── 1. distinct apps sharing a substrate are NOT merged ───────────────────────


def test_distinct_atlas_apps_sharing_library_are_not_merged(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()
    _daily_digest(caps)
    _article_capture(caps)

    # Sanity: the file overlap is real and high enough to have triggered the
    # old ev_overlap>=0.50 merge.
    ev_d = _scanner._ev_path_keys({"evidence_files": _DIGEST_FILES})
    ev_c = _scanner._ev_path_keys({"evidence_files": _CAPTURE_FILES})
    overlap = len(ev_d & ev_c) / min(len(ev_d), len(ev_c))
    assert overlap >= 0.50, f"fixture must reproduce the overlap, got {overlap:.2f}"

    merged = _scanner._dedup_manifests(caps)
    assert merged == 0, "distinct apps must not be merged on shared files alone"
    remaining = sorted(p.stem for p in caps.glob("*.json"))
    assert remaining == ["atlas-article-capture", "atlas-daily-digest"]


# ── 2. identity is stable across two consecutive scans ────────────────────────


def test_identity_stable_across_two_consecutive_scans(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()
    _daily_digest(caps)
    _article_capture(caps)

    def _snapshot() -> dict:
        out = {}
        for p in caps.glob("*.json"):
            d = json.loads(p.read_text())
            out[d["id"]] = (
                d.get("name"), d.get("description"),
                (d.get("identity") or {}).get("purpose"),
            )
        return out

    _scanner._dedup_manifests(caps)
    first = _snapshot()
    _scanner._dedup_manifests(caps)
    second = _snapshot()

    assert first == second, "a stable app-id must keep a stable identity per scan"
    # And specifically: neither app's description bled into the other.
    cap = first["atlas-article-capture"]
    dig = first["atlas-daily-digest"]
    assert "URL" in cap[1] and "digest" not in cap[1].lower()
    assert "daily message" in dig[1] and "URL" not in dig[1]


# ── 3. over-drop guards: real duplicates STILL merge ──────────────────────────


def test_true_duplicate_same_name_still_merges(tmp_path):
    """Two manifests with the SAME name are a real duplicate — still merge."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write(caps, "atlas-daily-digest", name="Atlas Daily Digest",
           objective="Morning digest.", evidence_files=list(_DIGEST_FILES),
           created_at="2026-06-07T00:00:00Z")
    _write(caps, "atlas-daily-digest-v2", name="Atlas Daily Digest",
           objective="Morning digest.", evidence_files=["digest/2026-06-08.md"],
           created_at="2026-06-08T00:00:00Z")
    assert _scanner._dedup_manifests(caps) == 1


def test_thin_remint_without_objective_still_merges(tmp_path):
    """A thin re-mint stub (named, but no objective/purpose/description) is not
    'established' — it must still merge on file overlap (keep-bias toward dedup).
    """
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write(caps, "atlas-capture", name="Atlas Capture",
           evidence_files=list(_CAPTURE_FILES), created_at="2026-06-11T00:00:00Z")
    _write(caps, "url-grabber", name="URL Grabber",  # different name, NO objective
           evidence_files=list(_CAPTURE_FILES), created_at="2026-06-12T00:00:00Z")
    assert _scanner._dedup_manifests(caps) == 1


def test_v7_arc_instances_share_files_still_merge(tmp_path):
    """Un-hydrated v7-arc instances (no name) must still merge on file overlap —
    the veto only fires for established, named, distinct apps."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    for inst in ("i-1", "i-2"):
        (caps / f"{inst}.json").write_text(json.dumps({
            "instance_id": inst,
            "manifest_shape": "v7-arc",
            "provenance": {"spec_id": "p-aaa", "spec_version": "1.0"},
            "realized_files": [{"path": p} for p in _CAPTURE_FILES],
            "status": "active",
        }))
    assert _scanner._dedup_manifests(caps) == 1


# ── 4. _merge_two_manifests: identity fill-only, never "longest wins" ──────────


def test_merge_does_not_overwrite_nonempty_identity_with_longer_loser():
    winner = {
        "id": "a", "name": "Article Capture",
        "description": "Capture URLs.",  # shorter
        "identity": {"purpose": "Capture URLs."},
    }
    loser = {
        "id": "b", "name": "Daily Digest",
        "description": "Distribute a long daily curated news digest every "
                       "morning to a Telegram group with five buckets.",  # longer
        "identity": {"purpose": "A much longer purpose string about digests."},
    }
    merged = _scanner._merge_two_manifests(winner, loser)
    assert merged["description"] == "Capture URLs."
    assert merged["identity"]["purpose"] == "Capture URLs."


def test_merge_fills_empty_identity_from_loser():
    winner = {"id": "a", "name": "App A", "description": "", "identity": {}}
    loser = {"id": "b", "name": "App B", "description": "From loser.",
             "identity": {"purpose": "Loser purpose."}}
    merged = _scanner._merge_two_manifests(winner, loser)
    assert merged["description"] == "From loser."
    assert merged["identity"]["purpose"] == "Loser purpose."


# ── 5. _are_distinct_apps predicate ───────────────────────────────────────────


def test_are_distinct_apps_predicate():
    a_raw = {"objective": "Capture URLs and archive them."}
    b_raw = {"objective": "Post a daily news digest to a group."}
    # distinct names + both have objectives → distinct
    assert _scanner._are_distinct_apps("Article Capture", a_raw,
                                       "Daily Digest", b_raw) is True
    # similar names → not distinct (likely same app)
    assert _scanner._are_distinct_apps("Daily Digest", b_raw,
                                       "Daily Digest Job", b_raw) is False
    # one side missing an objective/purpose/description → cannot assert distinct
    assert _scanner._are_distinct_apps("Article Capture", a_raw,
                                       "Daily Digest", {}) is False
    # empty name → not distinct
    assert _scanner._are_distinct_apps("", a_raw, "Daily Digest", b_raw) is False


def test_distinctive_name_similarity_handles_prefix_superset():
    f = _scanner._distinctive_name_similarity
    # identical names → fully similar
    assert f("Atlas Daily Digest", "Atlas Daily Digest") == 1.0
    # one qualifier token appended → still same-ish (>= veto threshold)
    assert f("Atlas Daily Digest", "Atlas Daily Digest Job") >= 0.55
    # bot-prefix-only shared, distinctive remainders unrelated → low
    assert f("Atlas Daily Digest", "Atlas Article Capture") < 0.55
    # prefix/superset that piles on 2 distinctive tokens → below threshold, so
    # two genuinely-different prefix-shaped apps can still be vetoed (Finding 1).
    assert f("Atlas Research", "Atlas Research Budget Guard") < 0.55


def test_prefix_superset_distinct_apps_are_not_merged(tmp_path):
    """'Atlas Research' vs 'Atlas Research Budget Guard' — a prefix/superset name
    pair that is two DIFFERENT apps — must not collapse on file overlap."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    files = ["scripts/atlas_lib/__init__.py", "scripts/atlas_lib/fetchers.py",
             "atlas/sources.json"]
    _write(caps, "atlas-research", name="Atlas Research",
           objective="Answer @-mentions with Brave + Haiku synthesis.",
           evidence_files=files + ["scripts/atlas_research.py"],
           created_at="2026-06-10T00:00:00Z")
    _write(caps, "atlas-research-budget-guard", name="Atlas Research Budget Guard",
           objective="Enforce per-day research spend caps before a query runs.",
           evidence_files=files + ["scripts/atlas_budget_guard.py", "atlas/budget.json"],
           created_at="2026-06-11T00:00:00Z")
    assert _scanner._dedup_manifests(caps) == 0


# ── 6. matcher won't re-absorb a re-detected distinct app ─────────────────────


def _make_detected(app_id, name, description, files):
    return _scanner.DetectedApplication(
        id=app_id, name=name, confidence=0.9, evidence_files=list(files),
        evidence_summary="", suggested_goals=[], suggested_tests=[],
        suggested_privacy=[], description=description,
    )


def test_matcher_does_not_absorb_distinct_app_on_evidence_overlap(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()
    _article_capture(caps)  # established, distinct-named existing manifest
    index = _scanner._build_existing_manifest_index(caps, None)

    # A re-detected Daily Digest shares most files with Article Capture but is a
    # different app — it must NOT match (so it gets minted as its own manifest).
    detected = _make_detected(
        "atlas-daily-digest", "Atlas Daily Digest",
        "Post a single daily curated news digest to the group every morning.",
        _DIGEST_FILES,
    )
    assert _scanner._match_detected_to_existing(detected, index, set()) is None


def test_matcher_still_matches_same_app_via_evidence_when_thin(tmp_path):
    """An existing manifest with no objective (thin) is still matchable by
    evidence overlap even under a drifted name — pass (d) preserved."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write(caps, "i-7f3a", name="",  # thin existing (e.g. un-named instance)
           evidence_files=list(_CAPTURE_FILES), created_at="2026-06-11T00:00:00Z")
    index = _scanner._build_existing_manifest_index(caps, None)
    detected = _make_detected("url-capturer", "URL Capturer",
                              "Capture URLs.", _CAPTURE_FILES)
    match = _scanner._match_detected_to_existing(detected, index, set())
    assert match is not None and match["stem"] == "i-7f3a"
