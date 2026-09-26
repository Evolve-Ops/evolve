"""tests/test_manifest_write_fnf_fallback.py — manifest write fallback bug fix.

The atlas-daily-digest forge run on 2026-05-28 logged "Could not save
updated manifest.files" 4 times during the bot dispatch, proceeded as
if everything was fine, then failed at Step 10:
  "Cannot approve: manifest 'atlas-daily-digest' not found for bot 'atlas'"

Root cause: _write_manifest_bytes caught PermissionError but the actual
exception path raised FileNotFoundError. The sequence on a fresh bot
whose `.openclaw/workspace/` exists but `.openclaw/workspace/manifests/`
does NOT (because the scanner hasn't run yet):

  1. save_manifest() calls d.mkdir(parents=True, exist_ok=True)
  2. mkdir fails silently with PermissionError (evolve user can't
     write to bot-owned workspace/)
  3. d is now missing; path.write_bytes() raises FileNotFoundError
     because the parent directory doesn't exist
  4. except PermissionError doesn't match → exception propagates
  5. forge_engine catches it as "non-fatal" and logs
  6. 4 such silent failures during the run
  7. Approve step finally tries load_manifest() and gets None
  8. approve_forge_job dies with "manifest not found"

Coverage:
  - Direct write succeeds → no sudo fallback called (happy path,
    regression guard)
  - PermissionError on direct write → sudo fallback runs (existing
    behavior preserved)
  - FileNotFoundError on direct write → sudo fallback runs (THE FIX)
  - FileNotFoundError on parent mkdir → sudo mkdir -p runs before
    sudo cp (the atlas scenario)
  - sudo mkdir failure → raises informative error
  - sudo cp failure → raises informative error
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))

from evolve_admin.applications.manifest import _write_manifest_bytes  # noqa: E402


def _fake_completed(rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["fake"], returncode=rc, stdout="", stderr=stderr,
    )


def test_direct_write_succeeds_no_sudo_called(tmp_path):
    """Happy path: parent exists and is writable → direct write returns,
    no subprocess.run involvement."""
    target = tmp_path / "manifest.json"
    with patch("subprocess.run") as m_run:
        _write_manifest_bytes(target, b'{"id": "test"}')
    assert target.read_bytes() == b'{"id": "test"}'
    m_run.assert_not_called()


def test_permission_error_triggers_sudo_fallback(tmp_path):
    """Pre-existing behavior: PermissionError on direct write → /tmp + sudo cp."""
    target = tmp_path / "manifests" / "manifest.json"
    # Pre-create the parent so the fallback's mkdir succeeds non-sudo
    target.parent.mkdir(parents=True)

    cp_calls: list = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "sudo" and cmd[1] == "/bin/cp":
            src, dst = cmd[2], cmd[3]
            Path(dst).write_bytes(Path(src).read_bytes())
            cp_calls.append((src, dst))
            return _fake_completed(0)
        return _fake_completed(0)

    # Make path.write_bytes raise PermissionError on the FIRST call only —
    # so the mock's cp emulation (which also calls write_bytes) doesn't
    # loop. Production code only calls write_bytes once before fallback.
    real_write_bytes = Path.write_bytes
    call_count = [0]
    def fake_write_bytes(self, data):
        call_count[0] += 1
        if call_count[0] == 1 and "manifests" in str(self):
            raise PermissionError("no ACL")
        return real_write_bytes(self, data)

    with patch.object(Path, "write_bytes", fake_write_bytes), \
         patch("subprocess.run", side_effect=fake_run):
        _write_manifest_bytes(target, b'{"id": "test"}')

    assert len(cp_calls) == 1
    assert target.read_bytes() == b'{"id": "test"}'


def test_file_not_found_error_triggers_sudo_fallback(tmp_path):
    """THE FIX: FileNotFoundError on direct write (parent dir missing)
    must trigger the sudo fallback, not propagate. Atlas hit exactly
    this — workspace/ existed but workspace/manifests/ didn't, mkdir
    silently failed, write_bytes raised FileNotFoundError, exception
    propagated to forge_engine which logged "non-fatal" and continued.

    Pre-fix: this test would fail with FileNotFoundError raised from
    _write_manifest_bytes (the except clause only caught PermissionError).
    Post-fix: the FNF is caught, the fallback runs, the file gets written.
    """
    # Parent dir is missing — direct write will raise FileNotFoundError
    target = tmp_path / "missing_parent_dir" / "manifest.json"
    assert not target.parent.exists()

    cp_calls: list = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "sudo" and cmd[1] == "/bin/cp":
            src, dst = cmd[2], cmd[3]
            Path(dst).write_bytes(Path(src).read_bytes())
            cp_calls.append((src, dst))
            return _fake_completed(0)
        return _fake_completed(0)

    # Must NOT raise — the fix means FNF triggers fallback
    with patch("subprocess.run", side_effect=fake_run):
        _write_manifest_bytes(target, b'{"id": "test"}')

    # The fallback used sudo cp (after the fallback's own mkdir
    # succeeded for the tmp_path-based parent without needing sudo)
    assert len(cp_calls) == 1
    assert target.exists()
    assert target.read_bytes() == b'{"id": "test"}'


def test_sudo_mkdir_failure_raises_informative(tmp_path):
    """If sudo /bin/mkdir -p fails (sudoers misconfigured), raise a
    PermissionError with the rc + stderr tail so the operator can
    debug. Silent fall-through to sudo cp would just produce a
    cp-can't-find-target message which is less actionable."""
    target = tmp_path / "nonexistent_dir" / "manifest.json"

    # Force BOTH the initial write and the fallback's non-sudo mkdir
    # to fail, so the sudo mkdir path is actually exercised.
    real_mkdir = Path.mkdir
    def fake_mkdir(self, *args, **kwargs):
        if str(self).endswith("nonexistent_dir"):
            raise PermissionError("simulated ACL denial")
        return real_mkdir(self, *args, **kwargs)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "sudo" and cmd[1] == "/bin/mkdir":
            return _fake_completed(1, stderr="sorry, user not in sudoers")
        return _fake_completed(0)

    with patch.object(Path, "mkdir", fake_mkdir), \
         patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(PermissionError) as exc_info:
            _write_manifest_bytes(target, b'{"id": "test"}')
    msg = str(exc_info.value)
    assert "mkdir failed" in msg
    assert "rc=1" in msg
    assert "sudoers" in msg


def test_sudo_cp_failure_raises_informative(tmp_path):
    """If sudo /bin/cp fails after a successful mkdir, raise with the
    rc + stderr tail. Same actionability rationale as mkdir failure."""
    target = tmp_path / "manifests" / "manifest.json"
    target.parent.mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "sudo" and cmd[1] == "/bin/cp":
            return _fake_completed(1, stderr="cp: cannot create regular file")
        return _fake_completed(0)

    real_write_bytes = Path.write_bytes
    call_count = [0]
    def fake_write_bytes(self, data):
        call_count[0] += 1
        if call_count[0] == 1 and "manifests" in str(self):
            raise PermissionError("no ACL")
        return real_write_bytes(self, data)

    with patch.object(Path, "write_bytes", fake_write_bytes), \
         patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(PermissionError) as exc_info:
            _write_manifest_bytes(target, b'{"id": "test"}')
    msg = str(exc_info.value)
    assert "rc=1" in msg
    assert "cp: cannot create" in msg


def test_save_manifest_swallows_file_not_found_on_mkdir(tmp_path, monkeypatch):
    """save_manifest's try/except around the initial d.mkdir() must
    catch FileNotFoundError too, not just PermissionError. The atlas
    fresh-bot case: workspace/ exists but its parent .openclaw/ might
    not be evolve-writable for nested mkdir — both PermissionError
    AND FileNotFoundError must fall through to the sudo path inside
    _write_manifest_bytes, not propagate up and abort the save."""
    from evolve_admin.applications import manifest as _mod
    from evolve_admin.applications.manifest import (
        ApplicationManifest, save_manifest,
    )

    # Point applications_dir at a path we'll force mkdir failures for
    fake_apps_dir = tmp_path / "manifests"

    def fake_apps_dir_fn(shared_dir, bot_id):
        return fake_apps_dir

    monkeypatch.setattr(_mod, "applications_dir", fake_apps_dir_fn)

    # Patch Path.mkdir to raise FileNotFoundError ONLY on the first
    # save_manifest-driven call. The fallback's own mkdir uses the
    # same path; we want THAT to succeed so the test can verify the
    # save path completed end-to-end.
    real_mkdir = Path.mkdir
    mkdir_call_count = [0]
    def fake_mkdir(self, *args, **kwargs):
        if str(self) == str(fake_apps_dir):
            mkdir_call_count[0] += 1
            if mkdir_call_count[0] == 1:
                raise FileNotFoundError("simulated nested-parent missing")
        return real_mkdir(self, *args, **kwargs)

    cp_done: list = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "sudo" and cmd[1] == "/bin/mkdir":
            Path(cmd[3]).mkdir(parents=True, exist_ok=True)
            return _fake_completed(0)
        if cmd[0] == "sudo" and cmd[1] == "/bin/cp":
            src, dst = cmd[2], cmd[3]
            Path(dst).write_bytes(Path(src).read_bytes())
            cp_done.append(dst)
            return _fake_completed(0)
        return _fake_completed(0)

    m = ApplicationManifest(id="test-app", name="Test", bot_id="bot_test")

    # Patch BOTH Path.mkdir and subprocess.run; save_manifest must
    # complete cleanly because the underlying write helper handles the
    # FileNotFoundError via sudo mkdir + cp.
    with patch.object(Path, "mkdir", fake_mkdir), \
         patch("subprocess.run", side_effect=fake_run):
        result_path = save_manifest(m, tmp_path)

    assert result_path.exists()
    assert len(cp_done) == 1
