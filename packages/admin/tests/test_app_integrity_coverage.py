"""Tests for app_integrity_coverage — the harness coverage reader (Bite 2a).

The load-bearing property under test is CONSERVATISM: a badge may only clear on
PROVEN coverage. Every absence / staleness / schema drift / ambiguous id must
fail toward NOT-covered (badge kept). Falsely clearing is the dishonesty the
just-works arc fights, so these tests attack that direction hard.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.app_integrity_coverage import (  # noqa: E402
    COVERAGE_FILENAME,
    COVERAGE_FRESH_S,
    id_is_covered,
    is_app_covered,
    load_covered_ids,
    resolve_app_id,
)


def _write_coverage(shared: Path, bot: str, covered_apps: list, *, version=1) -> Path:
    d = shared / bot
    d.mkdir(parents=True, exist_ok=True)
    p = d / COVERAGE_FILENAME
    p.write_text(json.dumps({
        "version": version,
        "bot_id": bot,
        "generated_by": "evolve-plugin/app-integrity-middleware",
        "covered_apps": covered_apps,
    }))
    return p


# ── resolve_app_id mirrors the middleware's appIdOf field priority ───────────

def test_resolve_prefers_pkg_id_then_id_then_spec_then_instance():
    assert resolve_app_id({"pkg_id": "P", "id": "I", "spec_id": "S", "instance_id": "N"}) == "P"
    assert resolve_app_id({"id": "I", "spec_id": "S", "instance_id": "N"}) == "I"
    assert resolve_app_id({"spec_id": "S", "instance_id": "N"}) == "S"
    assert resolve_app_id({"instance_id": "N"}) == "N"


def test_resolve_v7arc_raw_instance_keys_on_instance_id():
    # A raw v7-arc instance has NO top-level pkg_id/id/spec_id (spec_id lives
    # under provenance) — so it keys on instance_id, exactly as the middleware
    # does when it writes coverage. This is why callers must resolve from the
    # RAW (un-hydrated) manifest.
    raw = {
        "manifest_shape": "v7-arc",
        "instance_id": "inst_task_mgr_001",
        "provenance": {"spec_id": "app_task_manager", "spec_version": "v3"},
        "realized_files": [{"path": "scripts/tasks.py"}],
    }
    assert resolve_app_id(raw) == "inst_task_mgr_001"


def test_resolve_blank_and_nondict_are_empty():
    assert resolve_app_id({}) == ""
    assert resolve_app_id({"pkg_id": "   "}) == ""
    assert resolve_app_id(None) == ""
    assert resolve_app_id("nope") == ""


# ── load_covered_ids: the happy path ─────────────────────────────────────────

def test_load_returns_covered_ids(tmp_path: Path):
    shared = tmp_path / "shared"
    _write_coverage(shared, "bot-a", [
        {"appId": "app_task_manager", "scripts": ["/x/tasks.py"]},
        {"appId": "inst_notes_002", "scripts": ["/x/notes.py"]},
    ])
    ids = load_covered_ids(shared, "bot-a")
    assert ids == {"app_task_manager", "inst_notes_002"}


# ── Conservative failures: every one must yield an EMPTY set ──────────────────

def test_missing_file_is_empty(tmp_path: Path):
    assert load_covered_ids(tmp_path / "shared", "bot-a") == set()


def test_unparseable_json_is_empty(tmp_path: Path):
    shared = tmp_path / "shared"
    d = shared / "bot-a"
    d.mkdir(parents=True)
    (d / COVERAGE_FILENAME).write_text("{not json")
    assert load_covered_ids(shared, "bot-a") == set()


def test_wrong_version_is_empty(tmp_path: Path):
    shared = tmp_path / "shared"
    _write_coverage(shared, "bot-a", [{"appId": "x", "scripts": []}], version=2)
    assert load_covered_ids(shared, "bot-a") == set()


def test_missing_version_is_empty(tmp_path: Path):
    shared = tmp_path / "shared"
    d = shared / "bot-a"
    d.mkdir(parents=True)
    (d / COVERAGE_FILENAME).write_text(json.dumps({"covered_apps": [{"appId": "x"}]}))
    assert load_covered_ids(shared, "bot-a") == set()


def test_covered_apps_not_a_list_is_empty(tmp_path: Path):
    shared = tmp_path / "shared"
    d = shared / "bot-a"
    d.mkdir(parents=True)
    (d / COVERAGE_FILENAME).write_text(json.dumps({"version": 1, "covered_apps": {}}))
    assert load_covered_ids(shared, "bot-a") == set()


def test_unknown_sentinel_never_enters_the_set(tmp_path: Path):
    shared = tmp_path / "shared"
    _write_coverage(shared, "bot-a", [
        {"appId": "unknown", "scripts": ["/x/a.py"]},
        {"appId": "real_app", "scripts": ["/x/b.py"]},
    ])
    assert load_covered_ids(shared, "bot-a") == {"real_app"}


def test_malformed_entries_are_skipped(tmp_path: Path):
    shared = tmp_path / "shared"
    _write_coverage(shared, "bot-a", [
        "not-a-dict",
        {"noAppId": True},
        {"appId": 123},
        {"appId": "  "},
        {"appId": "good"},
    ])
    assert load_covered_ids(shared, "bot-a") == {"good"}


# ── Unreadable vs absent: same conservative answer, different legibility ─────
# An ABSENT file is the normal pre-middleware state and stays silent. An
# UNREADABLE file that EXISTS (e.g. minted 0600 bot-owned so the evolve-user
# admin server can't read it) is a perms regression: still NOT covered — the
# clear semantics never change — but it must warn so the fleet-wide re-badge
# is visible instead of silent.

_LOGGER_NAME = "evolve_admin.applications.app_integrity_coverage"


def _unreadable_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.name == _LOGGER_NAME and "unreadable" in r.getMessage()
    ]


def test_absent_file_is_empty_and_silent(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        assert load_covered_ids(tmp_path / "shared", "bot-a") == set()
    assert _unreadable_warnings(caplog) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through chmod 000")
def test_unreadable_file_is_empty_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    shared = tmp_path / "shared"
    p = _write_coverage(shared, "bot-a", [{"appId": "x", "scripts": []}])
    p.chmod(0o000)  # the live bug: file exists, evolve-user reader gets EACCES
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        assert load_covered_ids(shared, "bot-a") == set()  # conservative: KEPT
        assert load_covered_ids(shared, "bot-a") == set()  # repeat sweep
    warnings = _unreadable_warnings(caplog)
    assert len(warnings) == 1  # deduped — the sweep re-reads constantly
    msg = warnings[0].getMessage()
    assert str(p) in msg  # names the path so an operator can go fix it
    assert "NOT covered" in msg


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through chmod 000")
def test_unreadable_warning_rearms_after_successful_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    shared = tmp_path / "shared"
    p = _write_coverage(shared, "bot-a", [{"appId": "x", "scripts": []}])
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        p.chmod(0o000)
        assert load_covered_ids(shared, "bot-a") == set()
        p.chmod(0o644)  # regression fixed → reads fine, dedup re-arms
        assert load_covered_ids(shared, "bot-a") == {"x"}
        p.chmod(0o000)  # a NEW regression must warn again
        assert load_covered_ids(shared, "bot-a") == set()
    assert len(_unreadable_warnings(caplog)) == 2


# ── Staleness: a frozen file from a dead/downgraded gateway must not certify ──

def test_stale_file_is_empty(tmp_path: Path):
    shared = tmp_path / "shared"
    p = _write_coverage(shared, "bot-a", [{"appId": "x", "scripts": []}])
    old = time.time() - COVERAGE_FRESH_S - 60
    os.utime(p, (old, old))
    assert load_covered_ids(shared, "bot-a") == set()


def test_fresh_file_within_window_is_trusted(tmp_path: Path):
    shared = tmp_path / "shared"
    p = _write_coverage(shared, "bot-a", [{"appId": "x", "scripts": []}])
    recent = time.time() - COVERAGE_FRESH_S + 3600  # 1h inside the window
    os.utime(p, (recent, recent))
    assert load_covered_ids(shared, "bot-a") == {"x"}


def test_now_injection_controls_staleness(tmp_path: Path):
    shared = tmp_path / "shared"
    p = _write_coverage(shared, "bot-a", [{"appId": "x", "scripts": []}])
    st = p.stat()
    # now just inside the window → trusted; just past → empty.
    assert load_covered_ids(shared, "bot-a", now=st.st_mtime + COVERAGE_FRESH_S - 1) == {"x"}
    assert load_covered_ids(shared, "bot-a", now=st.st_mtime + COVERAGE_FRESH_S + 1) == set()


# ── id_is_covered / is_app_covered: the match guards ─────────────────────────

def test_id_is_covered_membership():
    ids = {"a", "b"}
    assert id_is_covered("a", ids) is True
    assert id_is_covered("c", ids) is False


def test_id_is_covered_rejects_empty_and_unknown():
    assert id_is_covered("", {"a"}) is False
    assert id_is_covered("   ", {"a"}) is False
    assert id_is_covered("unknown", {"unknown", "a"}) is False  # sentinel never matches
    assert id_is_covered(None, {"a"}) is False  # type: ignore[arg-type]


def test_is_app_covered_uses_raw_manifest_id(tmp_path: Path):
    shared = tmp_path / "shared"
    _write_coverage(shared, "bot-a", [{"appId": "inst_task_mgr_001", "scripts": []}])
    ids = load_covered_ids(shared, "bot-a")
    raw_v7 = {
        "manifest_shape": "v7-arc",
        "instance_id": "inst_task_mgr_001",
        "provenance": {"spec_id": "app_task_manager"},
    }
    assert is_app_covered(raw_v7, ids) is True
    # The HYDRATED shape (id=instance_id, pkg_id=spec_id) would resolve to the
    # spec_id and miss — proving why callers must pass the RAW manifest.
    hydrated = {"id": "inst_task_mgr_001", "pkg_id": "app_task_manager"}
    assert is_app_covered(hydrated, ids) is False


def test_is_app_covered_legacy_keys_on_pkg_id(tmp_path: Path):
    shared = tmp_path / "shared"
    _write_coverage(shared, "bot-a", [{"appId": "app_reporter", "scripts": []}])
    ids = load_covered_ids(shared, "bot-a")
    legacy = {"pkg_id": "app_reporter", "id": "reporter", "name": "Reporter"}
    assert is_app_covered(legacy, ids) is True
