"""Recovery of an orphaned / set-aside app manifest.

Pins restore_orphaned_manifest (incident-atlas-app-conflation-2026-06-22): un-hide
a manifest set aside as ``<name>.json.bak-YYYY-MM-DD`` and conservatively
de-conflate a sibling that absorbed its files WITHOUT stripping shared substrate.
The end-to-end test mirrors the real Atlas state (daily-digest set aside,
article-capture conflated) and proves the restored pair stays split under the
fixed dedup.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402
from evolve_admin.applications.manifest_recovery import (  # noqa: E402
    restore_orphaned_manifest,
    plan_unmerge_files,
    unmerge_safety_issues,
)

_LIB = [f"scripts/atlas_lib/{x}.py" for x in
        ("__init__", "classifier", "fetchers", "archive", "composer")]
_SHARED = _LIB + ["atlas/sources.json", "archive/index.json"]
_DIGEST_OWN = ["scripts/atlas_digest.py", "scripts/atlas-digest-cron.sh",
               "digest/2026-06-05.md",
               "plist-templates/com.bot_id.atlas-daily-digest.plist"]
_CAPTURE_OWN = ["scripts/atlas_capture.py", "atlas/optout.json", "atlas/.capture-salt"]


def _write(caps, name, payload):
    p = caps / name
    p.write_text(json.dumps(payload, indent=2))
    return p


def _digest_manifest():
    return {
        "id": "atlas-daily-digest", "name": "Atlas Daily Digest",
        "objective": "A single concise morning digest of ecosystem changes.",
        "description": "Daily classified news digest to a Telegram group.",
        "identity": {"purpose": "Surface ecosystem developments daily."},
        "evidence_files": _SHARED + _DIGEST_OWN,
        "created_at": "2026-06-07T20:34:00Z", "status": "active",
    }


def _conflated_capture():
    # article-capture after the over-merge: its OWN files + the digest's files
    # + the shared substrate.
    return {
        "id": "app_atlas_article_capture", "name": "Atlas Article Capture",
        "objective": "Turn every shared URL into a classified archive entry.",
        "description": "Distribute a daily curated news digest every morning.",  # bled in
        "identity": {"purpose": "Capture and classify member URLs."},
        "realized_files": [{"path": p} for p in
                           (_SHARED + _CAPTURE_OWN + _DIGEST_OWN)],
        "merged_from": ["atlas-capture", "atlas-content-guard"],
        "created_at": "2026-06-16T07:57:41Z", "status": "active",
    }


def _setup(tmp_path):
    """Mirror the real Atlas state: a set-aside daily-digest, a conflated
    article-capture, AND surviving sibling apps (recap, research) that reference
    the shared substrate — so the de-conflation has voters protecting the
    library/data files (the conflation absorbed some apps but not all)."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write(caps, "atlas-daily-digest.json.bak-2026-06-16", _digest_manifest())
    _write(caps, "atlas-article-capture.json", _conflated_capture())
    # Surviving apps that legitimately use the shared substrate.
    _write(caps, "atlas-weekly-recap.json", {
        "id": "atlas-weekly-recap", "name": "Atlas Weekly Recap",
        "objective": "Weekly longform recap built from the shared archive.",
        "realized_files": [{"path": p} for p in
                           _LIB + ["archive/index.json", "scripts/atlas_recap.py"]],
        "status": "active",
    })
    _write(caps, "atlas-on-demand-research.json", {
        "id": "atlas-on-demand-research", "name": "Atlas On-Demand Research",
        "objective": "Answer @-mentions with Brave + Haiku synthesis.",
        "realized_files": [{"path": p} for p in
                           _LIB + ["atlas/sources.json", "scripts/atlas_research.py"]],
        "status": "active",
    })
    return caps


# ── core restore ──────────────────────────────────────────────────────────────


def test_restore_unhides_the_orphaned_manifest(tmp_path):
    caps = _setup(tmp_path)
    assert not (caps / "atlas-daily-digest.json").exists()
    rep = restore_orphaned_manifest(caps, "atlas-daily-digest.json.bak-2026-06-16")
    assert rep["restored"] == "atlas-daily-digest.json"
    assert rep["id"] == "atlas-daily-digest"
    restored = json.loads((caps / "atlas-daily-digest.json").read_text())
    assert restored["name"] == "Atlas Daily Digest"
    # set-aside file moved into _history (forensic trail), not left in the grid dir
    assert not list(caps.glob("*.bak-*"))
    assert list((caps / "_history").glob("*.restored-*"))


def test_dry_run_writes_nothing(tmp_path):
    caps = _setup(tmp_path)
    rep = restore_orphaned_manifest(
        caps, "atlas-daily-digest.json.bak-2026-06-16",
        unmerge_from="atlas-article-capture", dry_run=True)
    assert rep["dry_run"] is True
    assert not (caps / "atlas-daily-digest.json").exists()
    assert (caps / "atlas-daily-digest.json.bak-2026-06-16").exists()
    # plan still computed
    assert set(rep["removed_files"]) == {p.lower() for p in _DIGEST_OWN}


# ── de-conflation protects shared substrate ───────────────────────────────────


def test_unmerge_strips_only_unique_files_keeps_shared_substrate(tmp_path):
    caps = _setup(tmp_path)
    rep = restore_orphaned_manifest(
        caps, "atlas-daily-digest.json.bak-2026-06-16",
        unmerge_from="atlas-article-capture")
    cap = json.loads((caps / "atlas-article-capture.json").read_text())
    cap_paths = {e["path"] for e in cap["realized_files"]}
    # digest-OWN files removed from the sibling
    for p in _DIGEST_OWN:
        assert p not in cap_paths, f"{p} should have been stripped"
    # shared substrate + capture's own files RETAINED
    for p in _SHARED + _CAPTURE_OWN:
        assert p in cap_paths, f"{p} must NOT be stripped (shared/own)"
    assert set(rep["removed_files"]) == {p.lower() for p in _DIGEST_OWN}


def test_plan_unmerge_protects_files_shared_with_a_third_app(tmp_path):
    """A digest 'own' file that a THIRD active app also claims is not stripped."""
    caps = _setup(tmp_path)
    # weekly-recap legitimately also reads the digest archive file.
    _write(caps, "atlas-weekly-recap.json", {
        "id": "atlas-weekly-recap", "name": "Atlas Weekly Recap",
        "objective": "Weekly longform recap from the archive.",
        "realized_files": [{"path": "digest/2026-06-05.md"},
                           {"path": "scripts/atlas_recap.py"}],
        "status": "active",
    })
    plan = plan_unmerge_files(
        caps, _digest_manifest(), caps / "atlas-article-capture.json")
    assert "digest/2026-06-05.md" not in plan  # shared with recap → kept
    assert "scripts/atlas_digest.py" in plan   # uniquely digest's → stripped


# ── safety: refuse unsafe overwrite ───────────────────────────────────────────


def test_refuses_to_clobber_different_active_id(tmp_path):
    caps = _setup(tmp_path)
    # An active atlas-daily-digest.json already exists for a DIFFERENT app.
    _write(caps, "atlas-daily-digest.json",
           {"id": "something-else", "name": "Other", "status": "active"})
    with pytest.raises(ValueError, match="refusing to overwrite"):
        restore_orphaned_manifest(caps, "atlas-daily-digest.json.bak-2026-06-16")
    # force overrides
    rep = restore_orphaned_manifest(
        caps, "atlas-daily-digest.json.bak-2026-06-16", force=True)
    assert rep["id"] == "atlas-daily-digest"


def test_refuses_already_active_same_id(tmp_path):
    caps = _setup(tmp_path)
    _write(caps, "atlas-daily-digest.json", _digest_manifest())
    with pytest.raises(ValueError, match="already active"):
        restore_orphaned_manifest(caps, "atlas-daily-digest.json.bak-2026-06-16")


def test_rejects_non_manifest_bak(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write(caps, "junk.json.bak-2026-06-16", {"no": "id"})
    with pytest.raises(ValueError, match="no id"):
        restore_orphaned_manifest(caps, "junk.json.bak-2026-06-16")


def test_rejects_path_traversal(tmp_path):
    caps = _setup(tmp_path)
    for bad in ("../../etc/passwd", "x/../../escape.json.bak", "/etc/hosts"):
        with pytest.raises(ValueError, match="escapes the manifests directory"):
            restore_orphaned_manifest(caps, bad)
    # traversal via --unmerge-from is rejected too
    with pytest.raises(ValueError, match="escapes the manifests directory"):
        restore_orphaned_manifest(
            caps, "atlas-daily-digest.json.bak-2026-06-16",
            unmerge_from="../../../tmp/evil")


# ── end-to-end: recovery + the fixed scanner keeps the split ──────────────────


def test_recovered_pair_stays_split_under_fixed_dedup(tmp_path):
    caps = _setup(tmp_path)
    restore_orphaned_manifest(
        caps, "atlas-daily-digest.json.bak-2026-06-16",
        unmerge_from="atlas-article-capture")
    # All four apps active now; the fixed dedup must NOT recombine any of them.
    before = sorted(p.stem for p in caps.glob("*.json"))
    assert "atlas-daily-digest" in before and "atlas-article-capture" in before
    merged = _scanner._dedup_manifests(caps)
    assert merged == 0
    after = sorted(p.stem for p in caps.glob("*.json"))
    assert after == before
    # identity intact and un-bled on both
    dig = json.loads((caps / "atlas-daily-digest.json").read_text())
    cap = json.loads((caps / "atlas-article-capture.json").read_text())
    assert "morning digest" in dig["objective"].lower()
    assert "url" in cap["objective"].lower()


# ── HARDENING: contaminated-footprint guard (live Atlas regression) ───────────

# Mirror of the LIVE daily-digest .bak footprint (23 files): it is itself
# contaminated — it claims article-capture's own script + the shared atlas_lib +
# the sibling's manifest. Set-subtraction de-conflation would gut article-capture.
_CONTAMINATED_DIGEST_FP = (
    _SHARED + [
        "scripts/atlas_digest.py", "scripts/atlas-digest-cron.sh",
        "plist-templates/com.bot_id.atlas-daily-digest.plist",
        # contamination — these belong to article-capture, not digest:
        "scripts/atlas_capture.py", "atlas/optout.json", "atlas/.capture-salt",
        "atlas/member-hash-salt.bin", "manifests/atlas-article-capture.json",
        "scripts/atlas_guard.py",
    ]
)


def _contaminated_digest():
    return {
        "id": "atlas-daily-digest", "name": "Atlas Daily Digest",
        "objective": "A single concise morning digest of ecosystem changes.",
        "evidence_files": list(_CONTAMINATED_DIGEST_FP), "status": "active",
        "created_at": "2026-06-07T20:34:00Z",
    }


def _setup_live_shapes(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write(caps, "atlas-daily-digest.json.bak-2026-06-16", _contaminated_digest())
    _write(caps, "atlas-article-capture.json", _conflated_capture())
    # recap survives but (like the live pod) does NOT list atlas_lib — so there
    # is no third-app vote protecting the shared library.
    _write(caps, "atlas_recap_generator.json", {
        "id": "atlas_recap_generator", "name": "Atlas Recap Generator",
        "objective": "Weekly recap.",
        "realized_files": [{"path": p} for p in
                           ["scripts/atlas_recap.py", "scripts/atlas-recap-cron.sh"]],
        "status": "active",
    })
    return caps


def test_unmerge_safety_issues_flags_contaminated_footprint():
    issues = unmerge_safety_issues(
        _contaminated_digest(), _conflated_capture(), "atlas-article-capture")
    assert issues, "contaminated restored footprint must produce safety issues"
    blob = " ".join(issues).lower()
    assert "atlas-article-capture" in blob  # names the sibling's own manifest
    assert "capture" in blob                # names the sibling-named files


def test_restore_refuses_unmerge_on_contaminated_footprint(tmp_path):
    caps = _setup_live_shapes(tmp_path)
    with pytest.raises(ValueError, match="refused"):
        restore_orphaned_manifest(
            caps, "atlas-daily-digest.json.bak-2026-06-16",
            unmerge_from="atlas-article-capture")
    # article-capture untouched; daily-digest not yet restored
    cap = json.loads((caps / "atlas-article-capture.json").read_text())
    assert any(e["path"] == "scripts/atlas_capture.py" for e in cap["realized_files"])
    assert any("atlas_lib" in e["path"] for e in cap["realized_files"])


def test_plain_restore_still_works_when_unmerge_would_refuse(tmp_path):
    caps = _setup_live_shapes(tmp_path)
    rep = restore_orphaned_manifest(caps, "atlas-daily-digest.json.bak-2026-06-16")
    assert rep["id"] == "atlas-daily-digest"
    assert (caps / "atlas-daily-digest.json").exists()
    # sibling untouched (no unmerge requested)
    cap = json.loads((caps / "atlas-article-capture.json").read_text())
    assert any("atlas_lib" in e["path"] for e in cap["realized_files"])


def test_positive_attribution_protects_shared_lib_without_a_third_voter(tmp_path):
    """Two apps only, no app votes for atlas_lib, but the restored (digest)
    footprint is CLEAN. Old set-subtraction would strip the shared lib; positive
    attribution strips only the digest-named files and keeps atlas_lib + the
    sibling's own capture files."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write(caps, "atlas-daily-digest.json.bak-2026-06-16", {
        "id": "atlas-daily-digest", "name": "Atlas Daily Digest",
        "objective": "Morning digest.",
        "evidence_files": _SHARED + [
            "scripts/atlas_digest.py", "scripts/atlas-digest-cron.sh"],
        "created_at": "2026-06-07T00:00:00Z", "status": "active",
    })
    _write(caps, "atlas-article-capture.json", _conflated_capture())
    plan = plan_unmerge_files(
        caps, json.loads((caps / "atlas-daily-digest.json.bak-2026-06-16").read_text()),
        caps / "atlas-article-capture.json")
    assert "scripts/atlas_digest.py" in plan
    assert "scripts/atlas-digest-cron.sh" in plan
    for keep in ("scripts/atlas_capture.py", "atlas/optout.json",
                 *[p for p in _LIB]):
        assert keep not in plan, f"{keep} must be protected (lib / sibling-own)"
