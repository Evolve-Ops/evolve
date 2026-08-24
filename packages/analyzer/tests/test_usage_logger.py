"""tests/test_usage_logger.py — mtime-sweep activity logger."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import usage_logger  # noqa: E402
from usage_logger import (  # noqa: E402
    SCHEMA_VERSION,
    collect_evidence_paths,
    sweep_app,
    run_usage_logger,
    _strip_evidence_prefix,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _set_mtime(path: Path, days_ago: float) -> None:
    """Set both atime and mtime of `path` to `days_ago` days before now."""
    target = time.time() - days_ago * 86400
    os.utime(path, (target, target))


def _write_manifest(manifest_dir: Path, manifest: dict) -> Path:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    p = manifest_dir / f"{manifest['id']}.json"
    p.write_text(json.dumps(manifest))
    return p


def _patch_bot_home(monkeypatch, bot_id: str, home: Path) -> None:
    """Redirect usage_logger's bot_home() to a tmp dir."""
    monkeypatch.setattr(usage_logger, "_bot_home", lambda b: home if b == bot_id else Path("/nonexistent"))


# ── Evidence-prefix parsing ──────────────────────────────────────────────────

def test_strip_evidence_prefix_handles_known_kinds():
    assert _strip_evidence_prefix("directory: home/") == "home"
    assert _strip_evidence_prefix("memory: notes/foo.md") == "memory: notes/foo.md".split(": ", 1)[1].rstrip("/")
    assert _strip_evidence_prefix("script: tools/run.py") == "tools/run.py"
    assert _strip_evidence_prefix("json: data/x.json") == "data/x.json"


def test_strip_evidence_prefix_passthrough_when_no_kind():
    assert _strip_evidence_prefix("plain/path.md") == "plain/path.md"
    assert _strip_evidence_prefix("/abs/path.md") == "/abs/path.md"


# ── collect_evidence_paths ────────────────────────────────────────────────────

def test_collect_paths_dedupes_across_fields(tmp_path):
    workspace = tmp_path / "ws"
    manifest = {
        "files": [{"path": "shared/file.md"}],
        "evidence_files": ["memory: shared/file.md", "directory: tools/"],
        "crons": [{"script": "tools/cron.py"}],
    }
    paths = collect_evidence_paths(manifest, workspace)
    # shared/file.md appears once (both files and evidence_files); tools/ once; cron.py once
    rels = sorted(str(p.relative_to(workspace)) for p in paths)
    assert rels == ["shared/file.md", "tools", "tools/cron.py"]


def test_collect_paths_handles_legacy_string_lists(tmp_path):
    workspace = tmp_path / "ws"
    manifest = {
        "files": ["v4_legacy.md"],
        "crons": ["v4_cron.py"],
    }
    paths = collect_evidence_paths(manifest, workspace)
    assert {p.name for p in paths} == {"v4_legacy.md", "v4_cron.py"}


def test_collect_paths_handles_absolute_paths(tmp_path):
    workspace = tmp_path / "ws"
    abs_target = tmp_path / "outside" / "abs.md"
    manifest = {"evidence_files": [f"memory: {abs_target}"]}
    paths = collect_evidence_paths(manifest, workspace)
    assert paths == [abs_target]


def test_collect_paths_reads_v7_arc_realized_files(tmp_path):
    """v7-arc Instances store paths under realized_files, not files."""
    workspace = tmp_path / "ws"
    manifest = {
        "manifest_shape": "v7-arc",
        "files": [],
        "realized_files": [
            {"path": "procedures/security-cve-scan.md",
             "logical_name": "procedure", "file_id": "f-abc"},
            {"path": "data/cves.json",
             "logical_name": "output", "file_id": "f-def"},
        ],
    }
    paths = collect_evidence_paths(manifest, workspace)
    rels = sorted(str(p.relative_to(workspace)) for p in paths)
    assert rels == ["data/cves.json", "procedures/security-cve-scan.md"]


def test_collect_paths_falls_back_to_realized_when_files_empty(tmp_path):
    """Even without an explicit manifest_shape tag, empty files[] +
    populated realized_files[] should be treated as v7-arc."""
    workspace = tmp_path / "ws"
    manifest = {
        "files": [],
        "realized_files": [{"path": "notes.md", "file_id": "f-1"}],
    }
    paths = collect_evidence_paths(manifest, workspace)
    assert [p.relative_to(workspace) for p in paths] == [Path("notes.md")]


def test_collect_paths_prefers_files_when_both_populated(tmp_path):
    """If files[] has entries, don't double-add realized_files[] (they're
    typically the same paths — the v7-arc fallback only fires when files[]
    is empty or shape is explicitly v7-arc, but dedup handles overlap)."""
    workspace = tmp_path / "ws"
    manifest = {
        "files": [{"path": "a.md"}],
        "realized_files": [{"path": "a.md"}, {"path": "b.md"}],
    }
    paths = collect_evidence_paths(manifest, workspace)
    # files[] was non-empty so realized_files[] is skipped — a.md only.
    rels = sorted(str(p.relative_to(workspace)) for p in paths)
    assert rels == ["a.md"]


def test_collect_paths_v7_arc_explicit_shape_reads_realized_even_with_files(tmp_path):
    """When manifest_shape=='v7-arc', read realized_files[] even if files[]
    happens to be populated (defensive — mirrors reconciler behavior)."""
    workspace = tmp_path / "ws"
    manifest = {
        "manifest_shape": "v7-arc",
        "files": [{"path": "hydrated.md"}],   # would come from hydrator
        "realized_files": [{"path": "hydrated.md"}, {"path": "extra.md"}],
    }
    paths = collect_evidence_paths(manifest, workspace)
    rels = sorted(str(p.relative_to(workspace)) for p in paths)
    assert rels == ["extra.md", "hydrated.md"]


# ── sweep_app: file/directory mtime resolution ────────────────────────────────

def test_sweep_app_no_evidence_marks_evidence_absent(tmp_path):
    now = datetime.now(timezone.utc)
    stats = sweep_app({}, tmp_path, now)
    assert stats == {
        "last_modified_ts": None,
        "days_since_modified": None,
        "active_files_30d": 0,
        "active_files_60d": 0,
        "total_files": 0,
        "evidence_present": False,
    }


def test_sweep_app_with_evidence_but_missing_files_distinguishes_from_no_evidence(tmp_path):
    now = datetime.now(timezone.utc)
    manifest = {"evidence_files": ["directory: ghost/"]}  # path declared but doesn't exist
    stats = sweep_app(manifest, tmp_path, now)
    assert stats["evidence_present"] is True
    assert stats["total_files"] == 0
    assert stats["last_modified_ts"] is None
    assert stats["days_since_modified"] is None


def test_sweep_app_picks_up_recent_file(tmp_path):
    now = datetime.now(timezone.utc)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    f = workspace / "notes.md"
    f.write_text("hello")
    _set_mtime(f, days_ago=3)

    manifest = {"files": [{"path": "notes.md"}]}
    stats = sweep_app(manifest, workspace, now)

    assert stats["evidence_present"] is True
    assert stats["total_files"] == 1
    assert stats["active_files_30d"] == 1
    assert stats["active_files_60d"] == 1
    assert 2 <= stats["days_since_modified"] <= 4
    assert stats["last_modified_ts"] is not None


def test_sweep_app_directory_takes_max_mtime_across_files(tmp_path):
    now = datetime.now(timezone.utc)
    workspace = tmp_path / "ws"
    d = workspace / "data"
    d.mkdir(parents=True)
    old = d / "old.md"
    fresh = d / "fresh.md"
    old.write_text("o")
    fresh.write_text("f")
    _set_mtime(old, days_ago=120)
    _set_mtime(fresh, days_ago=2)

    manifest = {"evidence_files": ["directory: data/"]}
    stats = sweep_app(manifest, workspace, now)

    assert stats["total_files"] == 2
    # last_modified should reflect the FRESH file, not the old one
    assert stats["days_since_modified"] <= 3
    # only fresh file is within 30d
    assert stats["active_files_30d"] == 1
    assert stats["active_files_60d"] == 1


def test_sweep_app_skips_ignored_dir_names(tmp_path):
    now = datetime.now(timezone.utc)
    workspace = tmp_path / "ws"
    real = workspace / "code" / "main.py"
    real.parent.mkdir(parents=True)
    real.write_text("x")
    cache = workspace / "code" / "__pycache__" / "main.cpython.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_text("x")

    manifest = {"evidence_files": ["directory: code/"]}
    stats = sweep_app(manifest, workspace, now)

    # __pycache__ contents excluded; .pyc would be excluded by suffix anyway
    assert stats["total_files"] == 1


def test_sweep_app_distinguishes_active_window_thresholds(tmp_path):
    now = datetime.now(timezone.utc)
    workspace = tmp_path / "ws"
    d = workspace / "files"
    d.mkdir(parents=True)
    very_old = d / "very_old.md"  # >60d
    medium   = d / "medium.md"    # 30–60d window
    fresh    = d / "fresh.md"     # <30d
    for p in (very_old, medium, fresh):
        p.write_text("x")
    _set_mtime(very_old, days_ago=120)
    _set_mtime(medium, days_ago=45)
    _set_mtime(fresh, days_ago=5)

    manifest = {"evidence_files": ["directory: files/"]}
    stats = sweep_app(manifest, workspace, now)

    assert stats["total_files"] == 3
    assert stats["active_files_30d"] == 1   # only fresh
    assert stats["active_files_60d"] == 2   # fresh + medium


# ── run_usage_logger end-to-end ───────────────────────────────────────────────

def test_run_usage_logger_writes_per_app_stats(tmp_path, monkeypatch):
    bot_id = "admin_bot"
    bot_home = tmp_path / "bots" / bot_id
    workspace = bot_home / ".openclaw" / "workspace"
    manifests_dir = workspace / "manifests"

    # Active app: file modified 3d ago
    active_dir = workspace / "active_app"
    active_dir.mkdir(parents=True)
    active_file = active_dir / "data.md"
    active_file.write_text("data")
    _set_mtime(active_file, days_ago=3)
    _write_manifest(manifests_dir, {
        "id": "active-app",
        "name": "Active App",
        "status": "active",
        "evidence_files": ["directory: active_app/"],
    })

    # Stale app: file modified 90d ago
    stale_dir = workspace / "stale_app"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "old.md"
    stale_file.write_text("old")
    _set_mtime(stale_file, days_ago=90)
    _write_manifest(manifests_dir, {
        "id": "stale-app",
        "name": "Stale App",
        "status": "active",
        "evidence_files": ["directory: stale_app/"],
    })

    # Manifest with no resolvable files
    _write_manifest(manifests_dir, {
        "id": "empty-app",
        "name": "Empty App",
        "status": "active",
        "evidence_files": [],
    })

    # Paused manifest — should be skipped
    _write_manifest(manifests_dir, {
        "id": "paused-app",
        "name": "Paused App",
        "status": "paused",
        "evidence_files": ["directory: active_app/"],
    })

    _patch_bot_home(monkeypatch, bot_id, bot_home)

    shared = tmp_path / "shared"
    stats = run_usage_logger(bot_id, shared)

    assert set(stats.keys()) == {"active-app", "stale-app", "empty-app"}

    a = stats["active-app"]
    assert a["evidence_present"] is True
    assert a["active_files_30d"] == 1
    assert a["days_since_modified"] <= 4

    s = stats["stale-app"]
    assert s["evidence_present"] is True
    assert s["active_files_30d"] == 0
    assert s["active_files_60d"] == 0
    assert s["days_since_modified"] >= 80

    e = stats["empty-app"]
    assert e["evidence_present"] is False
    assert e["total_files"] == 0
    assert e["last_modified_ts"] is None

    out = shared / bot_id / "recommendations" / "usage-stats.json"
    payload = json.loads(out.read_text())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["bot_id"] == bot_id
    assert set(payload["apps"].keys()) == {"active-app", "stale-app", "empty-app"}
