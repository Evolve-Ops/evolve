"""tests/test_backup_run_state.py — backup.write_run_state semantics.

The state file fed to backup_signal.py is load-bearing for visibility —
its counter logic decides whether failures stay invisible or surface
as red badges / Signals. These tests pin the transition rules.

Per-bot daemons run as the bot user and write state to
``{workspace}/evolve-backup/state.json``. Tests pass ``tmp_path`` as
the workspace root via the ``workspace`` kwarg.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup  # noqa: E402


def _write(tmp: Path, *, status: str, error: str | None = None, bot: str = "evolve") -> dict:
    return backup.write_run_state(bot, status=status, error=error, workspace=tmp)


def _read(tmp: Path) -> dict:
    return json.loads((tmp / "evolve-backup" / "state.json").read_text())


def test_first_success_writes_clean_state(tmp_path):
    state = _write(tmp_path, status="ok")
    assert state["consecutive_failures"] == 0
    assert state["last_attempt_status"] == "ok"
    assert state["last_attempt_at"]
    assert state["last_success_at"] == state["last_attempt_at"]
    assert state["last_error"] is None
    # Round-trips to disk.
    assert _read(tmp_path) == state


def test_no_changes_treated_as_success(tmp_path):
    """``no-changes`` (commit clean, backlog push fine) is still a healthy
    attempt — it must reset the failure counter."""
    _write(tmp_path, status="failed", error="boom")
    state = _write(tmp_path, status="no-changes")
    assert state["consecutive_failures"] == 0
    assert state["last_attempt_status"] == "no-changes"
    assert state["last_success_at"] == state["last_attempt_at"]


def test_failure_increments_counter_and_preserves_success_at(tmp_path):
    s1 = _write(tmp_path, status="ok")
    success_ts = s1["last_success_at"]

    s2 = _write(tmp_path, status="failed", error="push failed: x")
    assert s2["consecutive_failures"] == 1
    assert s2["last_error"] == "push failed: x"
    # last_success_at must NOT advance on a failure — that's the whole
    # point of capturing it separately from last_attempt_at.
    assert s2["last_success_at"] == success_ts

    s3 = _write(tmp_path, status="failed", error="push failed: y")
    assert s3["consecutive_failures"] == 2
    assert s3["last_error"] == "push failed: y"
    assert s3["last_success_at"] == success_ts


def test_recovery_after_streak_resets_counter(tmp_path):
    for _ in range(5):
        _write(tmp_path, status="failed", error="x")
    assert _read(tmp_path)["consecutive_failures"] == 5

    _write(tmp_path, status="ok")
    final = _read(tmp_path)
    assert final["consecutive_failures"] == 0
    assert final["last_error"] is None
    assert final["last_attempt_status"] == "ok"


def test_skipped_does_not_touch_counter_or_success(tmp_path):
    """``skipped`` (workspace missing, no URL) isn't a real attempt — it
    must not bump the failure counter or invalidate prior success."""
    s_ok = _write(tmp_path, status="ok")
    success_ts = s_ok["last_success_at"]

    s_skip = _write(tmp_path, status="skipped", error="workspace not found")
    assert s_skip["consecutive_failures"] == 0
    assert s_skip["last_success_at"] == success_ts
    assert s_skip["last_attempt_status"] == "skipped"


def test_error_truncation(tmp_path):
    huge = "X" * 5000
    state = _write(tmp_path, status="failed", error=huge)
    # 2000-char cap on last_error keeps state files small.
    assert len(state["last_error"]) == 2000


def test_atomic_write(tmp_path):
    """The state file must always be valid JSON — temp + rename means a
    crash mid-write can't leave a partial file behind."""
    _write(tmp_path, status="ok")
    path = tmp_path / "evolve-backup" / "state.json"
    # No leftover .tmp file in the dir.
    assert not (tmp_path / "evolve-backup" / "state.json.tmp").exists()
    json.loads(path.read_text())  # parses cleanly
