"""tests/test_backup_preflight.py — Phase 4d pre-flight estimate."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup_preflight as bp  # noqa: E402
from data_classification import build_resolver  # noqa: E402


def _mkfile(path: Path, size: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


# ─── Empty / missing workspace ────────────────────────────────────────────

def test_summary_missing_workspace_returns_zeros(tmp_path):
    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path / "nope", resolver)
    assert out["total"] == {"files": 0, "bytes": 0}
    assert out["truncated"] is False
    assert out["walked_files"] == 0


def test_summary_empty_workspace_returns_zeros(tmp_path):
    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path, resolver)
    assert out["total"] == {"files": 0, "bytes": 0}


# ─── Classification bucketing ─────────────────────────────────────────────

def test_summary_buckets_by_classification(tmp_path):
    _mkfile(tmp_path / "SOUL.md",          size=10)
    _mkfile(tmp_path / "notes" / "a.md",   size=20)
    _mkfile(tmp_path / "notes" / "b.md",   size=30)
    _mkfile(tmp_path / "index" / "data.json", size=40)

    manifest = {
        "id": "notes-app",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }
    resolver = build_resolver(manifests=[manifest])
    out = bp.compute_preflight_summary(tmp_path, resolver)

    assert out["by_classification"]["cloud"]["files"] == 2     # SOUL + index
    assert out["by_classification"]["cloud"]["bytes"] == 50    # 10 + 40
    assert out["by_classification"]["local"]["files"] == 2     # notes/a + notes/b
    assert out["by_classification"]["local"]["bytes"] == 50    # 20 + 30
    assert out["by_classification"]["ephemeral"]["files"] == 0
    assert out["total"] == {"files": 4, "bytes": 100}


def test_summary_skips_git_dir(tmp_path):
    """``.git`` is never part of a backup commit — walking it would
    inflate the estimate by the whole git object store size."""
    _mkfile(tmp_path / "SOUL.md", size=10)
    _mkfile(tmp_path / ".git" / "objects" / "ab" / "huge.pack", size=10_000_000)
    _mkfile(tmp_path / ".git" / "HEAD",                         size=23)

    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path, resolver)
    assert out["total"] == {"files": 1, "bytes": 10}


def test_summary_skips_evolve_backup_dir(tmp_path):
    """``evolve-backup/`` is the staging area for the current backup —
    walking it would double-count every commit."""
    _mkfile(tmp_path / "SOUL.md", size=10)
    _mkfile(tmp_path / "evolve-backup" / "openclaw.json", size=5)
    _mkfile(tmp_path / "evolve-backup" / "state.json",    size=5)

    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path, resolver)
    assert out["total"] == {"files": 1, "bytes": 10}


def test_summary_nested_directories(tmp_path):
    """Deep nesting works — the walk recurses normally."""
    _mkfile(tmp_path / "a" / "b" / "c" / "leaf.md", size=42)
    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path, resolver)
    assert out["total"]["files"] == 1
    assert out["total"]["bytes"] == 42


# ─── Truncation cap ───────────────────────────────────────────────────────

def test_summary_truncates_at_max_files(tmp_path):
    """When the cap is hit, ``truncated=true`` and walked_files == max."""
    for i in range(120):
        _mkfile(tmp_path / f"file-{i}.md", size=1)
    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path, resolver, max_files=100)
    assert out["truncated"] is True
    assert out["walked_files"] == 100
    assert out["total"]["files"] == 100


def test_summary_no_truncation_under_cap(tmp_path):
    for i in range(5):
        _mkfile(tmp_path / f"f-{i}.md", size=1)
    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path, resolver, max_files=100)
    assert out["truncated"] is False
    assert out["walked_files"] == 5


# ─── Errors don't crash the walk ──────────────────────────────────────────

def test_summary_stat_failure_is_counted_as_skipped(tmp_path, monkeypatch):
    """A path whose stat() fails is counted in skipped_errors, not in any bucket."""
    _mkfile(tmp_path / "good.md", size=10)
    _mkfile(tmp_path / "bad.md",  size=20)
    resolver = build_resolver()

    real_stat = Path.stat
    def angry_stat(self, *a, **kw):
        if self.name == "bad.md":
            raise PermissionError("synthetic stat failure")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", angry_stat)
    out = bp.compute_preflight_summary(tmp_path, resolver)
    assert out["total"]["files"] == 1  # only good.md counted
    assert out["total"]["bytes"] == 10
    assert out["skipped_errors"] == 1


# ─── Result shape sanity ──────────────────────────────────────────────────

def test_summary_has_all_three_classification_keys(tmp_path):
    """Even when no files of a given class exist, the bucket appears with zeros."""
    _mkfile(tmp_path / "x.md", size=1)
    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path, resolver)
    assert set(out["by_classification"].keys()) == {"cloud", "local", "ephemeral"}
    assert out["by_classification"]["local"] == {"files": 0, "bytes": 0}
    assert out["by_classification"]["ephemeral"] == {"files": 0, "bytes": 0}


def test_summary_returns_jsonable_shape(tmp_path):
    import json
    _mkfile(tmp_path / "x.md", size=1)
    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path, resolver)
    blob = json.dumps(out)
    assert '"truncated": false' in blob
    assert '"cloud"' in blob


# ─── Built-in recursion guard rule applies ────────────────────────────────

def test_summary_evolve_backup_is_skipped_not_classified_ephemeral(tmp_path):
    """The ``_NEVER_WALK_DIRS`` skip beats the ephemeral classification —
    we don't even open the directory, so files there don't show up in the
    ephemeral bucket either."""
    _mkfile(tmp_path / "evolve-backup" / "x.json", size=99)
    resolver = build_resolver()
    out = bp.compute_preflight_summary(tmp_path, resolver)
    assert out["by_classification"]["ephemeral"] == {"files": 0, "bytes": 0}


# ─── End-to-end with realistic mix ────────────────────────────────────────

def test_summary_realistic_mix(tmp_path):
    """A workspace mirroring a real bot: code + notes + cache + git internals."""
    # Code (cloud)
    _mkfile(tmp_path / "notes-app" / "notes_app.py", size=100)
    _mkfile(tmp_path / "notes-app" / "AGENTS.md",    size=50)
    # User-authored data (local)
    _mkfile(tmp_path / "notes" / "2026-05-28.md", size=200)
    _mkfile(tmp_path / "notes" / "secret.md",     size=80)
    # Regenerable cache (ephemeral via manifest)
    _mkfile(tmp_path / "cache" / "embeddings.bin", size=10_000)
    # Should be skipped entirely
    _mkfile(tmp_path / ".git" / "HEAD", size=23)
    _mkfile(tmp_path / "evolve-backup" / "state.json", size=42)

    manifest = {
        "id": "notes-app",
        "data_paths": [
            {"path": "notes/", "privacy": "local"},
            {"path": "cache/", "privacy": "ephemeral"},
        ],
    }
    resolver = build_resolver(manifests=[manifest])
    out = bp.compute_preflight_summary(tmp_path, resolver)
    assert out["by_classification"]["cloud"]["files"] == 2
    assert out["by_classification"]["cloud"]["bytes"] == 150
    assert out["by_classification"]["local"]["files"] == 2
    assert out["by_classification"]["local"]["bytes"] == 280
    assert out["by_classification"]["ephemeral"]["files"] == 1
    assert out["by_classification"]["ephemeral"]["bytes"] == 10_000
