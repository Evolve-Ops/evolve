"""tests/test_app_growth_sweep.py — the growth log's admin-side backstop.

The load-bearing assertions are the ones the brief names: the sweep catches an
out-of-band edit the observer never saw and marks it sweep-grade; it does NOT
re-record a change the plugin already logged with its cause; the first run for
a bot establishes a baseline and emits nothing (history is not backfilled); an
unreadable file is skipped rather than baselined; and retention prunes only
the sweep's own subtree.

The observer half lives in packages/plugin/tests/growthLog.test.mjs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import app_growth_sweep  # noqa: E402
from app_growth_sweep import (  # noqa: E402
    GROWTH_LOG_ROOT,
    GROWTH_LOG_SCHEMA_VERSION,
    UNATTRIBUTED_SEGMENT,
    count_growth_records,
    declared_files,
    digest_file,
    index_key,
    normalize_declared_path,
    observed_bot_dir,
    observed_since,
    prune_sweep_retention,
    state_path,
    sweep_app_dir,
    sweep_bot,
)

BOT = "team_bot_a"


@pytest.fixture()
def pod(tmp_path, monkeypatch):
    """A tmp pod: a shared dir plus one bot workspace with a manifests dir."""
    shared = tmp_path / "shared"
    ws = tmp_path / "bots" / BOT / ".openclaw" / "workspace"
    (ws / "manifests").mkdir(parents=True)
    (ws / "scripts").mkdir(parents=True)
    shared.mkdir()
    monkeypatch.setattr(
        app_growth_sweep, "_bot_home", lambda b: tmp_path / "bots" / b,
    )
    return {"shared": shared, "ws": ws, "tmp": tmp_path}


def write_manifest(pod, name: str, data: dict) -> None:
    (pod["ws"] / "manifests" / f"{name}.json").write_text(json.dumps(data, indent=1))


def write_file(pod, rel: str, body: str) -> Path:
    p = pod["ws"] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def read_sweep(pod, app_id, day) -> list[dict]:
    f = sweep_app_dir(pod["shared"], BOT, app_id) / f"{day}.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def write_observed(pod, app_id, day, rec) -> None:
    d = observed_bot_dir(pod["shared"], BOT) / (app_id or UNATTRIBUTED_SEGMENT)
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{day}.jsonl").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_index_key_folds_case_and_trims_slashes():
    assert index_key("/Scripts/Tasks.py/") == "scripts/tasks.py"
    assert index_key("  scripts/a.py ") == "scripts/a.py"


def test_index_key_agrees_with_the_typescript_twin():
    # The dedup between the two writers is a string compare across a language
    # boundary. If these two ever diverge, every observed change gets
    # double-recorded by the sweep — silently.
    ts_src = (
        Path(__file__).parents[3] / "packages" / "plugin" / "src" / "apps" / "GrowthLog.ts"
    ).read_text()
    assert 'export function indexKey(relPath: string): string {' in ts_src
    assert 'return relPath.replace(/^\\/+|\\/+$/g, "").toLowerCase();' in ts_src


def test_normalize_declared_path_handles_the_shapes_manifests_actually_hold(pod):
    ws = pod["ws"]
    assert normalize_declared_path("scripts/a.py", ws) == "scripts/a.py"
    assert normalize_declared_path({"path": "scripts/b.py"}, ws) == "scripts/b.py"
    assert normalize_declared_path("workspace/scripts/c.py", ws) == "scripts/c.py"
    assert normalize_declared_path(str(ws / "scripts/d.py"), ws) == "scripts/d.py"
    assert normalize_declared_path("code: scripts/e.py", ws) == "scripts/e.py"
    # Escapes and junk are refused, never raised on.
    assert normalize_declared_path("/etc/passwd", ws) is None
    assert normalize_declared_path("../../secrets.json", ws) is None
    assert normalize_declared_path(None, ws) is None
    assert normalize_declared_path(42, ws) is None
    assert normalize_declared_path("", ws) is None


def test_declared_files_dedupes_across_the_three_manifest_keys(pod):
    m = {
        "files": ["scripts/a.py", "scripts/a.py"],
        "realized_files": [{"path": "Scripts/A.py"}, {"path": "scripts/b.py"}],
        "evidence_files": ["scripts/c.py"],
    }
    assert declared_files(m, pod["ws"]) == ["scripts/a.py", "scripts/b.py", "scripts/c.py"]


def test_digest_file_returns_none_for_unreadable_not_a_digest_of_nothing(pod):
    p = write_file(pod, "scripts/a.py", "print('hi')\n")
    assert digest_file(p) is not None
    assert digest_file(pod["ws"] / "scripts" / "missing.py") is None


# ── Baseline ─────────────────────────────────────────────────────────────────


def test_first_run_establishes_a_baseline_and_emits_nothing(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")

    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")

    assert s["baseline_established"] is False, "reports the PRE-run state"
    assert s["changes"] == 0
    assert s["records_written"] == 0
    assert s["files_seen"] == 1
    assert read_sweep(pod, "task-manager", "2026-08-28") == []
    state = json.loads(state_path(pod["shared"], BOT).read_text())
    assert state["baseline_established"] is True
    assert list(state["files"]) == ["scripts/tasks.py"]


def test_an_unchanged_file_after_the_baseline_emits_nothing(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")
    sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")

    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-29T03:40:00Z")
    assert s["changes"] == 0
    assert s["records_written"] == 0


# ── The behaviour the brief names ────────────────────────────────────────────


def test_sweep_catches_an_out_of_band_edit_and_marks_it_sweep_grade(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")
    sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")

    # An edit no bot turn produced — an operator, a cron, a bash heredoc.
    write_file(pod, "scripts/tasks.py", "v2 — edited out of band\n")

    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-29T03:40:00Z")
    assert s["changes"] == 1
    assert s["records_written"] == 1

    (rec,) = read_sweep(pod, "task-manager", "2026-08-29")
    assert rec["schema_version"] == GROWTH_LOG_SCHEMA_VERSION
    assert rec["kind"] == "app_delta"
    assert rec["app_id"] == "task-manager"
    assert rec["files"] == ["scripts/tasks.py"]
    assert rec["attribution"] == "sweep", "second-class, and says so"
    assert rec["cause"] is None, "a sweep record cannot know the cause"
    assert rec["cause_source"] == "none"
    assert rec["session_id"] is None


def test_a_new_declared_file_after_the_baseline_is_a_change(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")
    sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")

    write_manifest(pod, "tm", {
        "app_id": "task-manager", "files": ["scripts/tasks.py", "scripts/helper.py"],
    })
    write_file(pod, "scripts/helper.py", "new\n")

    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-29T03:40:00Z")
    assert s["records_written"] == 1
    (rec,) = read_sweep(pod, "task-manager", "2026-08-29")
    assert rec["files"] == ["scripts/helper.py"]


def test_a_change_the_observer_already_logged_is_not_recorded_twice(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")
    sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")

    write_file(pod, "scripts/tasks.py", "v2\n")
    # The plugin saw this one and recorded it WITH its conversational cause.
    write_observed(pod, "task-manager", "2026-08-28", {
        "schema_version": 1, "kind": "app_delta",
        "ts": "2026-08-28T14:00:00.000Z", "bot_id": BOT,
        "app_id": "task-manager", "files": ["scripts/tasks.py"],
        "cause": "make the overdue list bold", "attribution": "manifest",
    })

    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-29T03:40:00Z")
    assert s["changes"] == 1
    assert s["already_observed"] == 1
    assert s["records_written"] == 0
    assert read_sweep(pod, "task-manager", "2026-08-29") == []


def test_dedup_ignores_observer_records_from_before_the_last_sweep(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")
    sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")

    # An observed record from BEFORE the baseline sweep must not excuse a
    # change made after it — otherwise one old edit suppresses every later one.
    write_observed(pod, "task-manager", "2026-08-27", {
        "schema_version": 1, "kind": "app_delta",
        "ts": "2026-08-27T09:00:00.000Z", "bot_id": BOT,
        "app_id": "task-manager", "files": ["scripts/tasks.py"],
        "cause": "an older change", "attribution": "manifest",
    })
    write_file(pod, "scripts/tasks.py", "v2\n")

    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-29T03:40:00Z")
    assert s["already_observed"] == 0
    assert s["records_written"] == 1


def test_observed_since_is_empty_before_a_baseline_exists(pod):
    write_observed(pod, "task-manager", "2026-08-28", {
        "ts": "2026-08-28T09:00:00.000Z", "app_id": "task-manager",
        "files": ["scripts/tasks.py"],
    })
    assert observed_since(pod["shared"], BOT, None) == set()
    assert observed_since(pod["shared"], BOT, "2026-08-28T00:00:00Z") == {
        ("task-manager", "scripts/tasks.py"),
    }


# ── Fail-safe behaviours ─────────────────────────────────────────────────────


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a 0000 file")
def test_an_unreadable_file_is_skipped_not_baselined(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    p = write_file(pod, "scripts/tasks.py", "v1\n")
    p.chmod(0o000)
    try:
        s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")
    finally:
        p.chmod(0o644)
    assert s["files_unreadable"] == 1
    assert s["files_seen"] == 0
    state = json.loads(state_path(pod["shared"], BOT).read_text())
    assert state["files"] == {}, "an unreadable file must not be stored as a baseline"


def test_a_declared_file_that_does_not_exist_is_counted_not_recorded(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/gone.py"]})
    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")
    assert s["files_missing"] == 1
    assert s["records_written"] == 0


def test_a_corrupt_state_file_reruns_the_baseline_rather_than_reporting_everything(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")
    sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")
    state_path(pod["shared"], BOT).write_text("{not json")
    write_file(pod, "scripts/tasks.py", "v2\n")

    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-29T03:40:00Z")
    assert s["records_written"] == 0, "re-baseline, never a fabricated pod-wide delta"
    assert json.loads(state_path(pod["shared"], BOT).read_text())["baseline_established"]


def test_a_manifest_with_no_resolvable_identity_is_skipped(pod):
    write_manifest(pod, "nameless", {"files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")
    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")
    assert s["apps"] == 0
    assert s["files_seen"] == 0


def test_a_broken_manifest_does_not_cost_the_whole_run(pod):
    (pod["ws"] / "manifests" / "broken.json").write_text("{not json")
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")
    s = sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")
    assert s["apps"] == 1
    assert s["files_seen"] == 1


def test_a_missing_manifests_dir_reports_an_error_and_writes_nothing(pod, tmp_path):
    s = sweep_bot("no_such_bot", pod["shared"], now_iso="2026-08-28T03:40:00Z")
    assert "error" in s
    assert not (pod["shared"] / GROWTH_LOG_ROOT).exists()


def test_dry_run_computes_changes_but_writes_neither_records_nor_state(pod):
    write_manifest(pod, "tm", {"app_id": "task-manager", "files": ["scripts/tasks.py"]})
    write_file(pod, "scripts/tasks.py", "v1\n")
    sweep_bot(BOT, pod["shared"], now_iso="2026-08-28T03:40:00Z")
    before = state_path(pod["shared"], BOT).read_text()
    write_file(pod, "scripts/tasks.py", "v2\n")

    s = sweep_bot(BOT, pod["shared"], dry_run=True, now_iso="2026-08-29T03:40:00Z")
    assert s["would_write"] == 1
    assert s["records_written"] == 0
    assert read_sweep(pod, "task-manager", "2026-08-29") == []
    assert state_path(pod["shared"], BOT).read_text() == before


# ── Retention + census ───────────────────────────────────────────────────────


def test_retention_prunes_only_the_sweeps_own_subtree(pod):
    sweep_dir = sweep_app_dir(pod["shared"], BOT, "task-manager")
    sweep_dir.mkdir(parents=True)
    (sweep_dir / "2026-01-01.jsonl").write_text("{}\n")
    (sweep_dir / "2026-08-28.jsonl").write_text("{}\n")
    observed_dir = observed_bot_dir(pod["shared"], BOT) / "task-manager"
    observed_dir.mkdir(parents=True)
    (observed_dir / "2026-01-01.jsonl").write_text("{}\n")

    pruned = prune_sweep_retention(pod["shared"], BOT, now="2026-08-29T03:40:00Z")

    assert pruned == 1
    assert not (sweep_dir / "2026-01-01.jsonl").exists()
    assert (sweep_dir / "2026-08-28.jsonl").exists()
    assert (observed_dir / "2026-01-01.jsonl").exists(), (
        "the observer's files are bot-owned; the sweep never deletes them"
    )


def test_count_growth_records_censuses_both_subtrees_without_writing(pod):
    write_observed(pod, "task-manager", "2026-08-28", {
        "ts": "2026-08-28T09:00:00.000Z", "kind": "app_delta",
        "app_id": "task-manager", "files": ["scripts/tasks.py"],
    })
    write_observed(pod, None, "2026-08-28", {
        "ts": "2026-08-28T10:00:00.000Z", "kind": "unattributed_change",
        "app_id": None, "files": ["scripts/new.py"],
    })
    d = sweep_app_dir(pod["shared"], BOT, "task-manager")
    d.mkdir(parents=True)
    (d / "2026-08-29.jsonl").write_text(json.dumps({
        "ts": "2026-08-29T03:40:00Z", "kind": "app_delta",
        "app_id": "task-manager", "files": ["scripts/tasks.py"],
    }) + "\n")

    c = count_growth_records(pod["shared"], BOT)
    assert c["observed_records"] == 2
    assert c["sweep_records"] == 1
    assert c["unattributed_records"] == 1
    assert c["apps"] == ["task-manager"]
    assert c["days"] == ["2026-08-28", "2026-08-29"]
