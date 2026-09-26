"""Unit tests for the tri-state file/stat read primitive.

Guards the invariant at the heart of the 2026-06-20 Evo confabulation
incident: a permission denial (EACCES/EPERM) on a bot-owned file under a
0700 home must NEVER be reported as "absent". The three states are
``exists`` / ``absent`` / ``indeterminate`` and they must stay distinct.

See ``feedback_exists_check_lies_under_0700_parents`` and
``project_evo_confabulation_failure_mode``.
"""
from __future__ import annotations

import errno
import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.evo.tools.fs_tristate import (  # noqa: E402
    ReadState,
    classify_privileged_stderr,
    read_json,
    read_text,
    stat_status,
)


# ── stat_status ──────────────────────────────────────────────────────────────


def test_stat_status_exists(tmp_path):
    target = tmp_path / "real_dir"
    target.mkdir()
    assert stat_status(target) is ReadState.EXISTS


def test_stat_status_exists_for_file(tmp_path):
    f = tmp_path / "f.json"
    f.write_text("{}")
    assert stat_status(f) is ReadState.EXISTS


def test_stat_status_absent_is_enoent(tmp_path):
    assert stat_status(tmp_path / "missing") is ReadState.ABSENT


def test_stat_status_eacces_is_indeterminate_not_absent(tmp_path):
    """The core bug: EACCES must NOT collapse to absent."""
    target = tmp_path / "denied"

    def fake_stat(*a, **kw):
        raise PermissionError(errno.EACCES, "Permission denied")

    with patch.object(Path, "stat", fake_stat):
        result = stat_status(target)
    assert result is ReadState.INDETERMINATE
    assert result is not ReadState.ABSENT


def test_stat_status_eperm_is_indeterminate(tmp_path):
    target = tmp_path / "eperm"

    def fake_stat(*a, **kw):
        raise OSError(errno.EPERM, "Operation not permitted")

    with patch.object(Path, "stat", fake_stat):
        assert stat_status(target) is ReadState.INDETERMINATE


def test_stat_status_generic_oserror_is_indeterminate(tmp_path):
    """Any non-ENOENT OSError (EIO etc.) is conservatively indeterminate —
    we can't prove the file is absent, so we never claim it."""
    target = tmp_path / "io"

    def fake_stat(*a, **kw):
        raise OSError(errno.EIO, "I/O error")

    with patch.object(Path, "stat", fake_stat):
        assert stat_status(target) is ReadState.INDETERMINATE


# ── classify_privileged_stderr ───────────────────────────────────────────────


def test_classify_privileged_returncode_zero_exists():
    assert classify_privileged_stderr(0, "") is ReadState.EXISTS


def test_classify_privileged_enoent_is_absent():
    assert (
        classify_privileged_stderr(1, "/bin/cat: x: No such file or directory")
        is ReadState.ABSENT
    )


def test_classify_privileged_permission_is_indeterminate():
    assert (
        classify_privileged_stderr(1, "/bin/cat: x: Permission denied")
        is ReadState.INDETERMINATE
    )


def test_classify_privileged_missing_sudoers_grant_is_indeterminate():
    """A missing sudoers grant → 'a password is required' → indeterminate,
    NOT absent. A read that can never succeed is a tooling failure."""
    assert (
        classify_privileged_stderr(1, "sudo: a password is required")
        is ReadState.INDETERMINATE
    )


# ── read_text ────────────────────────────────────────────────────────────────


def test_read_text_exists_returns_content(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello")
    res = read_text(f)
    assert res.state is ReadState.EXISTS
    assert res.exists is True
    assert res.text == "hello"


def test_read_text_absent_on_enoent_no_fallback(tmp_path):
    res = read_text(tmp_path / "nope.txt", sudo_fallback=False)
    assert res.state is ReadState.ABSENT
    assert res.text is None


def test_read_text_eacces_no_fallback_is_indeterminate(tmp_path):
    """EACCES with fallback disabled → indeterminate, never absent."""
    f = tmp_path / "secret.txt"

    def fake_read(*a, **kw):
        raise PermissionError(errno.EACCES, "Permission denied")

    with patch.object(Path, "read_text", fake_read):
        res = read_text(f, sudo_fallback=False)
    assert res.state is ReadState.INDETERMINATE
    assert res.indeterminate is True
    assert res.text is None


def test_read_text_eacces_then_sudo_cat_succeeds(tmp_path):
    """Direct read EACCES → sudo /bin/cat succeeds → exists + content."""
    f = tmp_path / "owned.json"

    def fake_read(*a, **kw):
        raise PermissionError(errno.EACCES, "Permission denied")

    class FakeProc:
        returncode = 0
        stdout = '{"ok": true}'
        stderr = ""

    with patch.object(Path, "read_text", fake_read), patch(
        "evolve_admin.evo.tools.fs_tristate.subprocess.run", return_value=FakeProc()
    ):
        res = read_text(f)
    assert res.state is ReadState.EXISTS
    assert res.text == '{"ok": true}'


def test_read_text_eacces_then_sudo_cat_enoent_is_absent(tmp_path):
    """Direct EACCES but sudo proves the file is really gone → absent."""
    f = tmp_path / "gone.json"

    def fake_read(*a, **kw):
        raise PermissionError(errno.EACCES, "Permission denied")

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "/bin/cat: gone.json: No such file or directory"

    with patch.object(Path, "read_text", fake_read), patch(
        "evolve_admin.evo.tools.fs_tristate.subprocess.run", return_value=FakeProc()
    ):
        res = read_text(f)
    assert res.state is ReadState.ABSENT


def test_read_text_eacces_then_sudo_grant_missing_is_indeterminate(tmp_path):
    """Direct EACCES + sudo grant missing → indeterminate, NOT absent.

    This is the precise incident shape: the file exists but every read
    path is blocked. The result must be 'cannot determine', so Evo can't
    confabulate 'the file doesn't exist yet'."""
    f = tmp_path / "manifests" / "atlas-article-capture.json"

    def fake_read(*a, **kw):
        raise PermissionError(errno.EACCES, "Permission denied")

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "sudo: a password is required"

    with patch.object(Path, "read_text", fake_read), patch(
        "evolve_admin.evo.tools.fs_tristate.subprocess.run", return_value=FakeProc()
    ):
        res = read_text(f)
    assert res.state is ReadState.INDETERMINATE
    assert res.state is not ReadState.ABSENT


def test_read_text_sudo_timeout_is_indeterminate(tmp_path):
    import subprocess as _sp

    f = tmp_path / "x.json"

    def fake_read(*a, **kw):
        raise PermissionError(errno.EACCES, "Permission denied")

    def fake_run(*a, **kw):
        raise _sp.TimeoutExpired(cmd="cat", timeout=5)

    with patch.object(Path, "read_text", fake_read), patch(
        "evolve_admin.evo.tools.fs_tristate.subprocess.run", fake_run
    ):
        res = read_text(f)
    assert res.state is ReadState.INDETERMINATE


# ── read_json ────────────────────────────────────────────────────────────────


def test_read_json_parses(tmp_path):
    f = tmp_path / "c.json"
    f.write_text('{"a": 1}')
    res, parsed = read_json(f)
    assert res.state is ReadState.EXISTS
    assert parsed == {"a": 1}


def test_read_json_absent(tmp_path):
    res, parsed = read_json(tmp_path / "no.json", sudo_fallback=False)
    assert res.state is ReadState.ABSENT
    assert parsed is None


def test_read_json_exists_but_invalid_stays_exists(tmp_path):
    """A present-but-malformed file is EXISTS with parsed=None — distinct
    from absent/indeterminate. The file IS there; it just won't parse."""
    f = tmp_path / "bad.json"
    f.write_text("{not json")
    res, parsed = read_json(f)
    assert res.state is ReadState.EXISTS
    assert parsed is None
    assert "invalid JSON" in res.detail


def test_as_dict_surfaces_state():
    res, _ = read_json(Path("/definitely/not/here.json"), sudo_fallback=False)
    blob = res.as_dict()
    assert blob["read_state"] == "absent"
    assert "read_detail" in blob
