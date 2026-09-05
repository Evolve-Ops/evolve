"""Identity-stability guarantee for DEFINED apps — Bite 2 of the
Defined/Discovered manifest lifecycle.

Spec: internal/spec-apps-meta-2026-06-13.md §9.2 (guarantee 2), §9.4.

Bite 1 (#3228) shipped the ``definition_status`` axis + the EXISTENCE shield
(a defined app is never L3-archived). It deliberately did NOT guard the
dedup/merge path. THIS bite does: a ``definition_status == "defined"`` app is
**never merged, split, renamed, or re-identified** by the scanner. It stays one
coherent app with a stable id + anchored identity (name + canonical
description). At most a *discovered* duplicate is dropped or kept separate.

These fixtures reuse the REAL Atlas conflation footprint
(incident-atlas-app-conflation-2026-06-22): four Atlas apps share
``scripts/atlas_lib/`` + ``archive/index.json`` by design, which the old
file-overlap dedup read as "same app". Bite 1's promote machinery
(``promote_to_defined``) builds the authored anchored-identity marks so the
fixtures exercise the real Bite-1↔Bite-2 seam, not a hand-rolled one.

The auditor-grade claim being pinned: **the merge/delete path can never delete,
rename, or re-identify a defined app.** test_AUDIT_* construct the actual
"defined app gets deleted / renamed" failure and prove it cannot happen.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402
from evolve_admin.applications.coherence_actions import (  # noqa: E402
    promote_to_defined,
)
from evolve_admin.applications.reconciliation import is_field_authored  # noqa: E402


# ── Atlas-derived file lists (real paths; shared substrate is deliberate) ─────

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
    "scripts/atlas_digest.py", "scripts/atlas-digest-cron.sh", "digest/2026-06-05.md",
]
_CAPTURE_FILES = _SHARED + [
    "scripts/atlas_capture.py", "atlas/optout.json", "atlas/.capture-salt",
]


def _write(caps_dir: Path, app_id: str, **fields) -> Path:
    payload = {"id": app_id, **fields}
    p = caps_dir / f"{app_id}.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


def _defined_capture(caps_dir: Path, app_id: str = "atlas-article-capture") -> Path:
    """An operator-VOUCHED Article Capture, marked defined via the real Bite-1
    promote (so name + description carry authored field_origins)."""
    mf = {
        "id": app_id,
        "name": "Atlas Article Capture",
        "objective": "Make every URL a member shares a classified archive entry.",
        "description": "When a member posts a URL: fetch, summarize, classify, archive.",
        "identity": {"purpose": "Capture and classify member-shared URLs."},
        "evidence_files": list(_CAPTURE_FILES),
        "created_at": "2026-06-11T19:29:26Z",
        "confidence": 0.85,
    }
    promote_to_defined(mf)  # → definition_status=defined + authored name/description
    assert mf["definition_status"] == "defined"
    assert is_field_authored(mf, "name") and is_field_authored(mf, "description")
    p = caps_dir / f"{app_id}.json"
    p.write_text(json.dumps(mf, indent=2))
    return p


def _discovered_dup(caps_dir: Path, app_id: str, *, name: str, files, **extra) -> Path:
    """A scanner-discovered manifest (definition_status defaults discovered)."""
    return _write(
        caps_dir, app_id, name=name, evidence_files=list(files),
        definition_status="discovered", created_at="2026-06-12T00:00:00Z", **extra,
    )


def _snapshot(caps_dir: Path) -> dict:
    out = {}
    for p in caps_dir.glob("*.json"):
        if p.name.startswith("."):
            continue
        d = json.loads(p.read_text())
        out[d["id"]] = d
    return out


# ── Edge (c): defined ↔ discovered overlap → defined survives intact ──────────


def test_defined_survives_discovered_duplicate_and_absorbs_it(tmp_path):
    """A discovered duplicate of a defined app (same name, same files) is the one
    dropped; the defined app survives with id + name + description intact and
    stays `defined`."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _defined_capture(caps)
    # id sorts BEFORE the defined one alphabetically → WITHOUT the defined-first
    # ordering this discovered dup would be the merge winner and the DEFINED app
    # would be the deleted loser. That is the exact failure this bite forbids.
    _discovered_dup(caps, "aaa-capture-dup", name="Atlas Article Capture",
                    files=_CAPTURE_FILES + ["atlas/extra.json"])

    merged = _scanner._dedup_manifests(caps)
    assert merged == 1, "a true discovered duplicate of a defined app should collapse"

    snap = _snapshot(caps)
    # The DEFINED manifest survived; the discovered dup is gone.
    assert "atlas-article-capture" in snap
    assert "aaa-capture-dup" not in snap
    survivor = snap["atlas-article-capture"]
    assert survivor["definition_status"] == "defined"
    assert survivor["name"] == "Atlas Article Capture"
    assert survivor["description"].startswith("When a member posts a URL")
    # Its files were widened by the absorb (fluid scope is allowed, §9.3).
    assert "atlas/extra.json" in _scanner._ev_path_keys(survivor)


def test_defined_kept_separate_when_distinct_discovered_overlaps(tmp_path):
    """A DISTINCT discovered app sharing the defined app's substrate is neither
    merged into it nor able to delete it — both survive (the keep-bias path)."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _defined_capture(caps)
    _discovered_dup(
        caps, "atlas-daily-digest", name="Atlas Daily Digest", files=_DIGEST_FILES,
        objective="Post a single concise morning digest to the community.",
        description="Distribute a daily curated news digest every morning.",
    )
    assert _scanner._dedup_manifests(caps) == 0
    snap = _snapshot(caps)
    assert set(snap) == {"atlas-article-capture", "atlas-daily-digest"}
    assert snap["atlas-article-capture"]["definition_status"] == "defined"


# ── Edge (a): two DEFINED apps sharing files → neither merged ─────────────────


def test_two_defined_apps_sharing_files_are_never_merged(tmp_path):
    """Two vouched apps that genuinely share a substrate are both kept — even if
    the dedup heuristics would otherwise collapse them. Deletion of either is
    forbidden; a coherence note is fine (we only assert non-deletion)."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _defined_capture(caps, "atlas-article-capture")
    # A second defined app whose name is similar enough that, absent the veto,
    # cond 2/3 (name+overlap) would merge it. Mark it defined too.
    mf2 = {
        "id": "atlas-article-capture-pro",
        "name": "Atlas Article Capture",  # same display name → sim 1.0
        "objective": "Pro capture variant with richer summaries.",
        "description": "A second vouched capture app on the shared substrate.",
        "evidence_files": list(_CAPTURE_FILES),
        "created_at": "2026-06-13T00:00:00Z",
    }
    promote_to_defined(mf2)
    (caps / "atlas-article-capture-pro.json").write_text(json.dumps(mf2, indent=2))

    assert _scanner._dedup_manifests(caps) == 0, "two DEFINED apps must never merge"
    snap = _snapshot(caps)
    assert set(snap) == {"atlas-article-capture", "atlas-article-capture-pro"}
    assert all(d["definition_status"] == "defined" for d in snap.values())


# ── Edge (b): a defined app whose files all vanished is never merged-away ──────


def test_defined_zero_file_app_is_never_merged_away(tmp_path):
    """A defined app whose evidence has all vanished (zero files) keeps existing
    — and is not absorbed by some other app it incidentally name-resembles.
    (Existence vs L3 archival is Bite 1; here we pin the DEDUP path.)"""
    caps = tmp_path / "manifests"
    caps.mkdir()
    mf = {
        "id": "atlas-recap", "name": "Atlas Weekly Recap",
        "objective": "A weekly recap the operator vouched for.",
        "description": "Weekly recap digest.", "evidence_files": [],
        "created_at": "2026-06-09T00:00:00Z",
    }
    promote_to_defined(mf)
    (caps / "atlas-recap.json").write_text(json.dumps(mf, indent=2))
    # A discovered app with a near-identical name (would trip cond 2 sim>=0.85).
    _discovered_dup(caps, "atlas-recap-job", name="Atlas Weekly Recap",
                    files=["scripts/recap.py"], objective="Recap job.")

    _scanner._dedup_manifests(caps)
    snap = _snapshot(caps)
    assert "atlas-recap" in snap, "the zero-file DEFINED app must never be deleted"
    assert snap["atlas-recap"]["definition_status"] == "defined"


# ── Over-merge regression guard: discovered ↔ discovered still dedups ──────────


def test_discovered_duplicates_still_merge(tmp_path):
    """The legitimate dedup that fixed the Atlas Frankenstein must still fire for
    DISCOVERED apps — no over-freeze."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _discovered_dup(caps, "atlas-daily-digest", name="Atlas Daily Digest",
                    files=_DIGEST_FILES, objective="Morning digest.")
    _discovered_dup(caps, "atlas-daily-digest-v2", name="Atlas Daily Digest",
                    files=["digest/2026-06-08.md"], objective="Morning digest.")
    assert _scanner._dedup_manifests(caps) == 1, "discovered duplicates must still collapse"


def test_discovered_distinct_apps_still_not_merged(tmp_path):
    """The #3095 distinct-app veto is unchanged for discovered apps."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _discovered_dup(caps, "atlas-daily-digest", name="Atlas Daily Digest",
                    files=_DIGEST_FILES, objective="Post a daily news digest.",
                    description="Daily digest.")
    _discovered_dup(caps, "atlas-article-capture", name="Atlas Article Capture",
                    files=_CAPTURE_FILES, objective="Capture member URLs.",
                    description="Capture URLs.")
    assert _scanner._dedup_manifests(caps) == 0


# ── AUDIT: construct the "defined app deleted / renamed" failure, prove absence ─


def test_AUDIT_defined_app_is_never_the_deleted_loser(tmp_path):
    """Auditor-grade: enumerate every dedup pair and assert the deleted file is
    never a defined manifest. We run a worst case — a defined app sandwiched
    between two discovered duplicates that all share files — and confirm the
    defined manifest's PATH still exists after dedup."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    defined_path = _defined_capture(caps, "mmm-defined-capture")
    # Two discovered dups, one sorting before and one after the defined id, so
    # neither index position can make the defined app a loser.
    _discovered_dup(caps, "aaa-capture-dup", name="Atlas Article Capture",
                    files=_CAPTURE_FILES)
    _discovered_dup(caps, "zzz-capture-dup", name="Atlas Article Capture",
                    files=_CAPTURE_FILES)

    _scanner._dedup_manifests(caps)
    assert defined_path.exists(), "the DEFINED manifest file must never be unlinked"
    survivor = json.loads(defined_path.read_text())
    assert survivor["definition_status"] == "defined"
    assert survivor["name"] == "Atlas Article Capture"


def test_AUDIT_merge_never_renames_or_reidentifies_a_defined_winner():
    """_merge_two_manifests must never rewrite a defined winner's anchored
    identity — name is structurally preserved, and an AUTHORED description is
    not filled even when empty (the authored-but-blank edge §9.2 calls out)."""
    winner = {
        "id": "cap", "name": "Atlas Article Capture",
        "description": "",  # authored-but-empty anchored line
        "definition_status": "defined",
        "provenance": {"field_origins": {
            "name": {"source": "confirmed", "via": "definition_status:promote"},
            "description": {"source": "confirmed", "via": "definition_status:promote"},
        }},
    }
    loser = {
        "id": "dig", "name": "Atlas Daily Digest",
        "description": "A long sibling description that must NOT become the "
                       "vouched app's identity line.",
    }
    merged = _scanner._merge_two_manifests(winner, loser)
    assert merged["name"] == "Atlas Article Capture", "name must never be renamed"
    assert merged["description"] == "", "authored anchored description must not be filled"
    assert merged["definition_status"] == "defined"


def test_merge_sticky_definition_never_demotes():
    """A merge whose LOSER is defined (reachable on the same_spec path) must keep
    the survivor `defined` — dedup never silently un-promotes a vouched app."""
    winner = {"id": "i-1", "name": "Cap", "definition_status": "discovered"}
    loser = {"id": "i-2", "name": "Cap", "definition_status": "defined"}
    merged = _scanner._merge_two_manifests(winner, loser)
    assert merged["definition_status"] == "defined"


def test_merge_discovered_winner_keeps_filling_description():
    """No over-freeze: a DISCOVERED winner with an empty description still fills
    from the loser (the legacy fill-only behavior is unchanged)."""
    winner = {"id": "a", "name": "A", "description": "", "definition_status": "discovered"}
    loser = {"id": "b", "name": "B", "description": "From loser."}
    merged = _scanner._merge_two_manifests(winner, loser)
    assert merged["description"] == "From loser."


def test_merge_defined_winner_with_unauthored_empty_description_still_fills():
    """Precision: the lock is keyed on AUTHORED. A defined winner whose
    description is empty AND not authored (e.g. promoted on a v7-arc shape that
    skips field-marking) keeps the fill-only behavior — we only freeze the
    operator-vouched anchored line, not every empty field on a defined app."""
    winner = {"id": "a", "name": "A", "description": "", "definition_status": "defined"}
    loser = {"id": "b", "name": "B", "description": "From loser."}
    merged = _scanner._merge_two_manifests(winner, loser)
    assert merged["description"] == "From loser."


# ── --dedup-existing cleanup mode obeys the same guarantee ────────────────────


def test_dedup_existing_archives_discovered_not_defined(tmp_path):
    """In the operator-invoked cleanup path, a defined manifest outranks a
    discovered one as winner, so the DISCOVERED duplicate is the one archived —
    even though it is the older manifest (created_at would otherwise win)."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    # Discovered is OLDER (would win the created_at rule) but must still lose to
    # the defined app.
    _discovered_dup(caps, "atlas-capture-old", name="Atlas Article Capture",
                    files=_CAPTURE_FILES)
    p_old = caps / "atlas-capture-old.json"
    d = json.loads(p_old.read_text())
    d["created_at"] = "2026-06-01T00:00:00Z"
    p_old.write_text(json.dumps(d))
    _defined_capture(caps, "atlas-article-capture")

    res = _scanner.dedup_existing_manifests(caps, None)
    assert res["merged"] == 1
    assert "atlas-capture-old" in res["archived"]
    assert "atlas-article-capture" in res["winners"]
    snap = _snapshot(caps)
    assert "atlas-article-capture" in snap
    assert snap["atlas-article-capture"]["definition_status"] == "defined"
    assert snap["atlas-article-capture"]["name"] == "Atlas Article Capture"


def test_dedup_existing_keeps_distinct_discovered_neighbor_of_defined(tmp_path):
    """A DISTINCT discovered app that merely shares a defined app's substrate
    must not be archived in cleanup mode just because file overlap is high —
    the #3095 distinct-apps veto now gates the cleanup ev_overlap branch too."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _defined_capture(caps, "atlas-article-capture")
    # Distinct discovered app (Daily Digest) sharing atlas_lib/ + data dir.
    _discovered_dup(
        caps, "atlas-daily-digest", name="Atlas Daily Digest", files=_DIGEST_FILES,
        objective="Post a single concise morning digest.",
        description="Distribute a daily curated news digest every morning.",
    )
    res = _scanner.dedup_existing_manifests(caps, None)
    assert res["merged"] == 0, "distinct apps must not collapse on shared files alone"
    assert set(_snapshot(caps)) == {"atlas-article-capture", "atlas-daily-digest"}


def test_dedup_existing_keeps_two_defined_apps(tmp_path):
    """Two matched DEFINED manifests are both vouched — the cleanup mode archives
    neither."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _defined_capture(caps, "atlas-article-capture")
    mf2 = {
        "id": "atlas-article-capture-2", "name": "Atlas Article Capture",
        "objective": "Second vouched capture.", "description": "Second.",
        "evidence_files": list(_CAPTURE_FILES), "created_at": "2026-06-14T00:00:00Z",
    }
    promote_to_defined(mf2)
    (caps / "atlas-article-capture-2.json").write_text(json.dumps(mf2, indent=2))

    res = _scanner.dedup_existing_manifests(caps, None)
    assert res["merged"] == 0, "two defined apps must never be deduped away"
    assert set(_snapshot(caps)) == {"atlas-article-capture", "atlas-article-capture-2"}


def test_dedup_existing_defined_winner_anchored_identity_not_filled(tmp_path):
    """The --dedup-existing fill-only step must not write a defined winner's
    authored anchored identity (name/description) from the loser."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    # Defined winner with an authored-but-EMPTY description.
    mf = {
        "id": "atlas-capture", "name": "Atlas Article Capture",
        "objective": "Capture URLs.", "description": "",
        "evidence_files": list(_CAPTURE_FILES), "created_at": "2026-06-11T00:00:00Z",
    }
    promote_to_defined(mf)
    assert is_field_authored(mf, "description")
    (caps / "atlas-capture.json").write_text(json.dumps(mf, indent=2))
    # Discovered loser carrying a description that must NOT bleed into the winner.
    _discovered_dup(caps, "zzz-capture-dup", name="Atlas Article Capture",
                    files=_CAPTURE_FILES, description="Loser description.")

    _scanner.dedup_existing_manifests(caps, None)
    snap = _snapshot(caps)
    assert snap["atlas-capture"]["description"] == "", \
        "authored anchored description on a defined winner must stay untouched"


# ── same_spec (cond 0) stays authoritative for genuine same-app instances ──────


def test_same_spec_defined_instances_still_collapse(tmp_path):
    """Two v7-arc Instances bound to the same Spec are the SAME app — collapsing
    them is identity-preserving, so the both-defined veto must NOT block cond 0.
    The survivor stays defined (sticky)."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    for inst, ds in (("i-1", "defined"), ("i-2", "discovered")):
        (caps / f"{inst}.json").write_text(json.dumps({
            "instance_id": inst, "manifest_shape": "v7-arc",
            "provenance": {"spec_id": "p-aaa", "spec_version": "1.0"},
            "realized_files": [{"path": p} for p in _CAPTURE_FILES],
            "status": "active", "definition_status": ds,
        }))
    assert _scanner._dedup_manifests(caps) == 1
    # Exactly one survivor, and the vouch is preserved (sticky-up).
    survivors = [json.loads(p.read_text()) for p in caps.glob("*.json")
                 if not p.name.startswith(".")]
    assert len(survivors) == 1
    assert survivors[0]["definition_status"] == "defined"
