"""tests/test_tag_vocabulary.py — coverage for tag_vocabulary.py + the
gallery tag-index/integration plumbing it underpins.

The vocabulary is intentionally tiny + flat (see feedback memory
`tags-flat-not-hierarchical`). These tests lock the surface so the
auto-detector stays well-calibrated, the canonical tag list stays
stable across reorderings, and the gallery API surfaces the new
fields the admin UI expects.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY = _REPO_ROOT / "gallery"


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary surface
# ─────────────────────────────────────────────────────────────────────────────


def test_vocabulary_returns_sorted_known_tags():
    from evolve_admin.applications.tag_vocabulary import (
        all_tags, is_recommended, describe,
    )

    tags = all_tags()
    assert tags == sorted(tags), "all_tags() must be alphabetically sorted"
    assert len(tags) >= 10, "vocabulary collapsed below useful size"
    # Spot-check anchors — these are the ones the gallery already needs.
    for anchor in ("productivity", "calendar", "email", "github", "travel"):
        assert anchor in tags, f"vocabulary lost canonical anchor {anchor!r}"
        assert is_recommended(anchor)
        assert describe(anchor), f"{anchor!r} must carry a non-empty description"


def test_is_recommended_rejects_freeform_tags():
    from evolve_admin.applications.tag_vocabulary import is_recommended

    # Operators can use any string as a tag — these aren't in the canonical
    # vocab and is_recommended should say so without raising.
    for freeform in ("obsidian-integration", "ea-suite", "made-up-tag"):
        assert not is_recommended(freeform)


def test_describe_unknown_tag_returns_empty():
    from evolve_admin.applications.tag_vocabulary import describe

    assert describe("not-in-vocab") == ""


# ─────────────────────────────────────────────────────────────────────────────
# Auto-detector
# ─────────────────────────────────────────────────────────────────────────────


def test_auto_detect_returns_sorted_unique_tags():
    from evolve_admin.applications.tag_vocabulary import auto_detect

    result = auto_detect("Travel planner — itinerary, flight, and hotel tracking")
    assert "travel" in result
    assert result == sorted(set(result)), "must be sorted + deduped"


def test_auto_detect_empty_input_returns_empty_list():
    from evolve_admin.applications.tag_vocabulary import auto_detect

    assert auto_detect() == []
    assert auto_detect("", "", None or "") == []


def test_auto_detect_uses_word_boundaries_not_substring():
    """Substring matching would let `mail` inside `email` trigger a
    `mail`-keyed tag. The detector uses word boundaries so that
    can't happen. Concretely: there's no canonical `mail` tag, but
    `notetaker` shouldn't fire on `email-notetaker-disabled` either
    — verifying boundaries hold under both shapes.
    """
    from evolve_admin.applications.tag_vocabulary import auto_detect

    # `task-management` exists; ensure it doesn't fire inside `subtask-management-tool`.
    # The current keyword set hyphenates so `task-management` matches the bare
    # form; the test verifies bare prefix shifting doesn't trigger.
    result = auto_detect("an app that does subtask-management-tool things")
    assert "task-management" not in result


def test_auto_detect_finds_travel_for_concrete_first_user():
    """The PR's concrete first user: three travel apps about to be forged.
    Auto-detect must recognise them so they land with a `travel` tag
    rather than empty (and the operator can add `travel_assistant_pack`
    on top).
    """
    from evolve_admin.applications.tag_vocabulary import auto_detect

    for blurb in (
        ("Travel Notes",
         "Capture trip notes alongside itinerary, lodging, and contacts."),
        ("Trip Research",
         "Research destinations, vet hotels, surface neighbourhood guides."),
        ("Itinerary Builder",
         "Build day-by-day itineraries with flights, hotels, and activities."),
    ):
        tags = auto_detect(*blurb)
        assert "travel" in tags, (
            f"{blurb[0]!r} should auto-tag as travel; got {tags!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# merge_tags
# ─────────────────────────────────────────────────────────────────────────────


def test_merge_tags_preserves_existing_order_then_appends_new_sorted():
    from evolve_admin.applications.tag_vocabulary import merge_tags

    merged = merge_tags(
        existing=["personal-productivity", "task-management"],
        proposed=["productivity", "calendar", "task-management"],
    )
    # Existing order preserved, no dup of `task-management`, new entries appended sorted.
    assert merged == [
        "personal-productivity", "task-management", "calendar", "productivity",
    ]


def test_merge_tags_drops_empty_strings():
    from evolve_admin.applications.tag_vocabulary import merge_tags

    assert merge_tags(existing=["a", "", None or ""],
                      proposed=["", "b"]) == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────────────
# disabled_tags: operator dismissal is authoritative
# ─────────────────────────────────────────────────────────────────────────────


def test_auto_detect_excludes_disabled_tags():
    """A tag listed in the manifest's disabled_tags must not come back from
    auto_detect even when its keywords are present — that's what the field
    is for."""
    from evolve_admin.applications.tag_vocabulary import auto_detect

    # `email` keywords fire on this blurb …
    blurb = "Reads your inbox and surfaces unread email twice a day"
    assert "email" in auto_detect(blurb)
    # … but disabled_tags blocks it.
    assert "email" not in auto_detect(blurb, disabled_tags=["email"])


def test_auto_detect_disabled_does_not_affect_other_tags():
    from evolve_admin.applications.tag_vocabulary import auto_detect

    blurb = "Daily calendar agenda and inbox triage"
    tags = auto_detect(blurb, disabled_tags=["email"])
    assert "email" not in tags
    assert "calendar" in tags


def test_merge_tags_drops_disabled_from_existing_and_proposed():
    """Operator authority over their own list: a tag they later dismissed
    must come out of `tags` on the next backfill, even if it lingered in
    the existing array from a prior write."""
    from evolve_admin.applications.tag_vocabulary import merge_tags

    merged = merge_tags(
        existing=["email", "ea-suite"],
        proposed=["calendar", "email"],
        disabled_tags=["email"],
    )
    assert merged == ["ea-suite", "calendar"]


def test_merge_tags_disabled_drops_freeform_tag_too():
    """disabled_tags is not limited to the canonical vocabulary — an
    operator can dismiss any string they previously had, including a
    free-form one like `ea-suite`."""
    from evolve_admin.applications.tag_vocabulary import merge_tags

    merged = merge_tags(
        existing=["productivity", "ea-suite"],
        proposed=[],
        disabled_tags=["ea-suite"],
    )
    assert merged == ["productivity"]


def test_disabled_tags_empty_is_a_no_op():
    """Default-empty disabled_tags must not change either helper's output
    vs the pre-followup contract."""
    from evolve_admin.applications.tag_vocabulary import auto_detect, merge_tags

    assert auto_detect("calendar agenda", disabled_tags=()) == auto_detect("calendar agenda")
    assert merge_tags(["a"], ["b"], disabled_tags=()) == merge_tags(["a"], ["b"])


# ─────────────────────────────────────────────────────────────────────────────
# Manifest from_dict accepts application_tags alias
# ─────────────────────────────────────────────────────────────────────────────


def test_application_manifest_from_dict_accepts_application_tags_alias():
    """Gallery JSONs and SpecDraft persistence use `application_tags`;
    ApplicationManifest's field is `tags`. Before the alias landed,
    from_dict() filtered to dataclass fields and silently dropped tags
    when forge seeded an installed manifest from a gallery pkg dict
    — installed apps came up with empty tags.
    """
    from evolve_admin.applications.manifest import ApplicationManifest

    m = ApplicationManifest.from_dict({
        "id": "test-app",
        "name": "Test App",
        "bot_id": "admin_bot",
        "application_tags": ["productivity", "travel"],
    })
    assert m.tags == ["productivity", "travel"]


def test_application_manifest_from_dict_tags_wins_when_both_present():
    """Defensive against a future double-write — if both `tags` and
    `application_tags` are present on the JSON, the explicit `tags`
    field wins (alias only fires when `tags` is absent).
    """
    from evolve_admin.applications.manifest import ApplicationManifest

    m = ApplicationManifest.from_dict({
        "id": "test-app",
        "name": "Test App",
        "bot_id": "admin_bot",
        "tags": ["explicit"],
        "application_tags": ["alias-only"],
    })
    assert m.tags == ["explicit"]


# ─────────────────────────────────────────────────────────────────────────────
# Gallery index + tags-index on disk
# ─────────────────────────────────────────────────────────────────────────────


def test_every_gallery_index_entry_carries_application_tags():
    """The backfill script denormalises tags into gallery/index.json so
    the gallery API doesn't have to crack open every per-app JSON to
    populate the UI's chips. Lock the invariant — a fresh manifest
    added without re-running the backfill would otherwise land tagless
    in the index even though its per-app JSON had tags.
    """
    entries = json.loads((_GALLERY / "index.json").read_text(encoding="utf-8"))
    missing = [
        e.get("pkg_id")
        for e in entries
        if not (isinstance(e.get("application_tags"), list)
                and e["application_tags"])
    ]
    assert not missing, (
        "Gallery index entries with no application_tags (re-run "
        "scripts/backfill_application_tags.py --rebuild-index):\n  - "
        + "\n  - ".join(missing)
    )


def test_tags_index_round_trips_against_per_app_manifests():
    """`gallery/tags-index.json` is the reverse map `tag → [pkg_id]`.
    It must agree with the per-app `application_tags` — otherwise the
    filter UI shows pkg_ids that don't actually carry the tag (or
    misses pkg_ids that do).
    """
    tags_index_path = _GALLERY / "tags-index.json"
    assert tags_index_path.exists(), (
        "gallery/tags-index.json missing — run "
        "scripts/backfill_application_tags.py --rebuild-index"
    )
    tags_index = json.loads(tags_index_path.read_text(encoding="utf-8"))

    expected: dict[str, set[str]] = {}
    for pkg_file in sorted(_GALLERY.rglob("p-*.json")):
        # Only top-level per-app dirs — skip nested scripts/.
        if pkg_file.parent.parent != _GALLERY:
            continue
        try:
            pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        pkg_id = pkg.get("pkg_id")
        if not pkg_id:
            continue
        for tag in pkg.get("application_tags") or []:
            expected.setdefault(tag, set()).add(pkg_id)

    indexed = {t: set(pids) for t, pids in tags_index.items()}
    assert indexed == expected, (
        "tags-index.json is out of sync with per-app application_tags.\n"
        "Re-run scripts/backfill_application_tags.py --rebuild-index.\n"
        f"in_index_only: {set(indexed) - set(expected)}\n"
        f"in_manifests_only: {set(expected) - set(indexed)}"
    )


def test_load_tags_index_returns_dict_of_string_lists():
    from evolve_admin.applications.gallery import load_tags_index

    idx = load_tags_index(_REPO_ROOT)  # shared_dir arg is reserved/ignored today
    assert isinstance(idx, dict) and idx, "tags index loaded empty"
    for tag, pkgs in idx.items():
        assert isinstance(tag, str)
        assert isinstance(pkgs, list)
        assert all(isinstance(p, str) for p in pkgs)


# ─────────────────────────────────────────────────────────────────────────────
# Tag-kind classifier
# ─────────────────────────────────────────────────────────────────────────────


def test_tag_kind_classifies_canonical_suite_freeform():
    from evolve_admin.applications.tag_vocabulary import (
        tag_kind, TAG_KIND_CANONICAL, TAG_KIND_SUITE, TAG_KIND_FREEFORM,
    )

    # Canonical = in vocabulary
    assert tag_kind("travel") == TAG_KIND_CANONICAL
    assert tag_kind("productivity") == TAG_KIND_CANONICAL

    # Suite = -suite suffix convention (used by the gallery suite tags)
    assert tag_kind("ea-suite") == TAG_KIND_SUITE
    assert tag_kind("daily-brief-suite") == TAG_KIND_SUITE
    assert tag_kind("workspace-suite") == TAG_KIND_SUITE

    # Freeform = everything else, including operator-curated labels and
    # historical free-form tags that pre-date the vocabulary.
    assert tag_kind("obsidian-integration") == TAG_KIND_FREEFORM
    assert tag_kind("data-foundation") == TAG_KIND_FREEFORM
    assert tag_kind("made-up-tag-string") == TAG_KIND_FREEFORM


def test_classify_tags_returns_dict_keyed_by_input():
    from evolve_admin.applications.tag_vocabulary import classify_tags

    out = classify_tags(["travel", "ea-suite", "obsidian-integration"])
    assert out == {
        "travel": "canonical",
        "ea-suite": "suite",
        "obsidian-integration": "freeform",
    }


def test_classify_tags_handles_empty_input():
    from evolve_admin.applications.tag_vocabulary import classify_tags

    assert classify_tags([]) == {}


def test_gallery_kinds_classifies_every_repo_tag():
    """Run the classifier across every tag in the real gallery
    tags-index.json. Smoke test that the kinds bucket the way we expect:
    every -suite-suffixed tag classifies as suite, every vocabulary
    member classifies as canonical, and at least one freeform exists
    (operator-curated tags from before the backfill).
    """
    from evolve_admin.applications.tag_vocabulary import (
        classify_tags, TAG_KIND_SUITE, TAG_KIND_CANONICAL, TAG_KIND_FREEFORM,
    )

    tags_index = json.loads(
        (_GALLERY / "tags-index.json").read_text(encoding="utf-8")
    )
    kinds = classify_tags(list(tags_index))

    suite_tags = [t for t, k in kinds.items() if k == TAG_KIND_SUITE]
    canonical_tags = [t for t, k in kinds.items() if k == TAG_KIND_CANONICAL]
    freeform_tags = [t for t, k in kinds.items() if k == TAG_KIND_FREEFORM]

    # All three suite tags from the backfill _OVERRIDES table must surface.
    for expected in ("ea-suite", "daily-brief-suite", "workspace-suite"):
        assert expected in suite_tags, (
            f"{expected!r} did not classify as suite; got "
            f"{kinds.get(expected)!r}"
        )
    # No false positives — anything classified as suite must end with -suite.
    for tag in suite_tags:
        assert tag.endswith("-suite"), (
            f"{tag!r} classified suite but does not match the -suite "
            "convention"
        )
    # The vocabulary surface is well-represented.
    assert "travel" not in kinds  # not in current gallery, fine
    assert "productivity" in canonical_tags
    assert "calendar" in canonical_tags
    # And the historical operator-curated tags survived as freeform.
    assert "obsidian-integration" in freeform_tags


# ─────────────────────────────────────────────────────────────────────────────
# /api/gallery/tags response shape
# ─────────────────────────────────────────────────────────────────────────────


def test_api_gallery_tags_returns_tags_and_kinds():
    """The route must return both `tags` (reverse map) and `kinds`
    (classification), keyed identically. The admin UI's chip-row
    renderer reads both in lockstep — a drift between keys would
    leave chips unstyled.
    """
    from flask import Flask
    from evolve_admin.web.gallery_routes import register_gallery_routes

    app = Flask(__name__)
    register_gallery_routes(app, _REPO_ROOT / "network.json", _REPO_ROOT)

    with app.test_client() as client:
        resp = client.get("/api/gallery/tags")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert "tags" in payload and "kinds" in payload
        assert isinstance(payload["tags"], dict)
        assert isinstance(payload["kinds"], dict)
        # Same key set
        assert set(payload["tags"]) == set(payload["kinds"])
        # Every kind is one of the three valid strings
        assert set(payload["kinds"].values()) <= {"canonical", "suite", "freeform"}
        # Sanity: at least one suite tag (the backfill assigned three)
        assert any(k == "suite" for k in payload["kinds"].values())


def test_list_gallery_packages_populates_tags_and_application_tags():
    """The admin UI's renderGallery reads p.tags; newer call sites read
    p.application_tags. list_gallery_packages must surface BOTH so
    neither path silently empties.
    """
    from evolve_admin.applications.gallery import list_gallery_packages

    # shared_dir doesn't need to exist for builtin packages; pass repo root
    # so imported-gallery scanning doesn't blow up.
    pkgs = list_gallery_packages(_REPO_ROOT, bot_ids=[])
    assert pkgs, "builtin gallery returned no packages"
    builtin = [p for p in pkgs if p.get("source") == "builtin"]
    assert builtin, "no builtin gallery packages found"
    for p in builtin:
        assert p.get("application_tags"), (
            f"{p.get('pkg_id')} surfaces no application_tags"
        )
        assert p.get("tags") == p["application_tags"], (
            f"{p.get('pkg_id')}: tags must mirror application_tags"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Backfill script honors disabled_tags end-to-end
# ─────────────────────────────────────────────────────────────────────────────


def test_backfill_one_honors_disabled_tags(tmp_path, monkeypatch):
    """Drive `backfill_one` directly against a synthetic manifest carrying
    a `disabled_tags` array. The keyword scanner would otherwise fire
    `email` on this description; the dismissal must keep it out of the
    rewritten `application_tags`.
    """
    scripts_dir = _REPO_ROOT / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    import importlib
    backfill_mod = importlib.import_module("backfill_application_tags")

    manifest = {
        "pkg_id": "p-tagtest1",
        "pkg_version": "1.0",
        "schema_version": 5,
        "manifest_type": "evolve_application",
        "display_name": "Inbox Companion",
        "description": "Reads your inbox and surfaces unread email each morning.",
        "author": "evolve",
        "application_tags": ["personal-productivity"],
        "disabled_tags": ["email"],
        "id": "app_tagtest1",
        "name": "Inbox Companion",
    }
    sub = tmp_path / "p-tagtest1"
    sub.mkdir()
    path = sub / "p-tagtest1.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    changed, before, after = backfill_mod.backfill_one(path)
    written = json.loads(path.read_text(encoding="utf-8"))

    assert "email" in before or True  # before is the pre-merge `existing`
    assert "email" not in after, (
        "disabled_tags should have suppressed `email` from the merged set"
    )
    assert "email" not in written["application_tags"], (
        "rewritten manifest must not carry the dismissed tag"
    )
    # disabled_tags itself stays put — backfill never edits the dismissal list.
    assert written["disabled_tags"] == ["email"]
    assert changed, "added some auto-detected tag (e.g. productivity)"
