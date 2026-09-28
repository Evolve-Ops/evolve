"""Regression tests for heal POD_CONDUCT.md injection — pins the data-safety
behavior of `_read_with_sudo_fallback` and `check_pod_conduct_injection`.

The pre-fix bug (2026-04-27): heal used bare `sudo cat` against a path with
no matching sudoers grant, so the read silently failed (rc != 0). The
inject path then treated empty stdout as "file is empty" and would have
overwritten existing AGENTS.md content with just the POD_CONDUCT.md
reference if the write had succeeded. The write also failed (no terminal
for password) so AGENTS.md was preserved by accident — but the heal log
filled with "Failed to inject" errors on every cycle for every bot.

These tests pin the post-fix invariants:
- Direct read is preferred (works via macOS ACL)
- sudo cat fallback only runs if direct read fails with PermissionError
- BOTH paths failing returns (None, "failed") — caller MUST abort the
  write rather than treat it as empty content
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import heal  # noqa: E402


# ── _read_with_sudo_fallback ──────────────────────────────────────────────


def test_read_prefers_direct_when_path_is_readable(tmp_path: Path):
    """Direct read works → no sudo needed."""
    f = tmp_path / "agents.md"
    f.write_text("hello world")
    text, source = heal._read_with_sudo_fallback(f)
    assert text == "hello world"
    assert source == "direct"


def test_read_falls_back_to_sudo_cat_on_permission_error(tmp_path: Path):
    """When direct read raises PermissionError, sudo cat fallback runs."""
    f = tmp_path / "agents.md"   # path doesn't need to exist for the mock

    def fake_read_text(self):
        raise PermissionError(f"no read access to {self}")

    fake_run_result = type("R", (), {"returncode": 0, "stdout": "via sudo"})()

    with patch.object(Path, "read_text", fake_read_text), \
         patch("heal.subprocess.run", return_value=fake_run_result) as run_mock:
        text, source = heal._read_with_sudo_fallback(f)

    assert text == "via sudo"
    assert source == "sudo"
    # Verify sudo was called with full /bin/cat path (not bare 'cat' — fixed
    # because bare 'cat' resolves via secure_path but we want explicit calls)
    assert run_mock.call_args.args[0][0:2] == ["sudo", "/bin/cat"]


def test_read_returns_failed_when_both_paths_fail(tmp_path: Path):
    """CRITICAL: when both direct AND sudo cat fail, return (None, 'failed').
    Caller must distinguish this from "empty file" or it'll destroy data."""
    f = tmp_path / "agents.md"

    def fake_read_text(self):
        raise PermissionError("no read")

    fake_run_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "denied"})()

    with patch.object(Path, "read_text", fake_read_text), \
         patch("heal.subprocess.run", return_value=fake_run_result):
        text, source = heal._read_with_sudo_fallback(f)

    assert text is None
    assert source == "failed"


def test_read_returns_failed_on_file_not_found(tmp_path: Path):
    """Missing file → direct raises FileNotFoundError; if sudo cat also
    fails, returns (None, 'failed')."""
    f = tmp_path / "does-not-exist.md"

    fake_run_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "no such file"})()

    with patch("heal.subprocess.run", return_value=fake_run_result):
        text, source = heal._read_with_sudo_fallback(f)

    assert text is None
    assert source == "failed"


# ── check_pod_conduct_injection — data safety ─────────────────────────────


def test_inject_aborts_when_agents_md_unreadable(tmp_path: Path, capsys):
    """The whole reason this fix exists: if we can't read AGENTS.md, we
    must NOT proceed to write it. Pre-fix code treated unreadable as
    empty and would have destroyed content."""
    # Function computes workspace as config_path.parent / "workspace".
    # config_path is ~/.openclaw/openclaw.json on real bots; tests
    # mirror that layout so workspace resolves to ~/.openclaw/workspace.
    openclaw_dir = tmp_path / ".openclaw"
    openclaw_dir.mkdir()
    config_path = openclaw_dir / "openclaw.json"
    config_path.write_text("{}")
    workspace = openclaw_dir / "workspace"
    workspace.mkdir()
    # AGENTS.md does NOT exist; sudo cat will also fail in this fake setup

    fake_failed = type("R", (), {"returncode": 1, "stdout": "", "stderr": "denied"})()

    # Mock sudo subprocess calls to fail; mock conduct_src as missing so we
    # don't actually try to copy anything either.
    with patch("heal.subprocess.run", return_value=fake_failed), \
         patch("heal.Path.exists", return_value=False):
        result = heal.check_pod_conduct_injection("team_bot_a", config_path, os_user="team_bot_a")

    assert result is False
    captured = capsys.readouterr()
    # Verify the abort message landed (not the "Failed to inject" panic)
    assert "Failed to read" in captured.out
    assert "skipping POD_CONDUCT injection" in captured.out


def test_workspace_path_includes_openclaw_dir(tmp_path: Path, capsys):
    """Regression for the path-calculation bug found 2026-04-27 after PR #384.
    The function had `config_path.parent.parent / "workspace"` which gave
    `/Users/{bot}/workspace` (missing `.openclaw/`). All reads/writes
    silently routed to a non-existent path. Reverting to .parent.parent
    here should make this test fail."""
    openclaw_dir = tmp_path / ".openclaw"
    openclaw_dir.mkdir()
    config_path = openclaw_dir / "openclaw.json"
    config_path.write_text("{}")
    # Deliberately do NOT create the workspace dir — heal must look for
    # AGENTS.md at .openclaw/workspace/AGENTS.md (not at /workspace/AGENTS.md).
    # The abort message will include the path heal actually tried, which
    # we assert on below.

    # Mock subprocess so direct read returns FileNotFoundError (workspace
    # dir doesn't exist) and sudo cat fallback also fails.
    fake_failed = type("R", (), {"returncode": 1, "stdout": "", "stderr": "no such file"})()

    with patch("heal.subprocess.run", return_value=fake_failed):
        result = heal.check_pod_conduct_injection("team_bot_a", config_path, os_user="team_bot_a")

    assert result is False
    captured = capsys.readouterr()
    # The path in the abort message MUST include `.openclaw/workspace/`.
    # Pre-fix this would say `/{tmp}/workspace/AGENTS.md` (no .openclaw).
    assert ".openclaw/workspace/AGENTS.md" in captured.out, \
        f"workspace path missing .openclaw/ — got: {captured.out!r}"


def test_inject_returns_true_when_pod_conduct_already_present(tmp_path: Path):
    """If AGENTS.md already references POD_CONDUCT.md, no work needed."""
    # Function computes workspace as config_path.parent / "workspace".
    # config_path is ~/.openclaw/openclaw.json on real bots; tests
    # mirror that layout so workspace resolves to ~/.openclaw/workspace.
    openclaw_dir = tmp_path / ".openclaw"
    openclaw_dir.mkdir()
    config_path = openclaw_dir / "openclaw.json"
    config_path.write_text("{}")
    workspace = openclaw_dir / "workspace"
    workspace.mkdir()
    agents_md = workspace / "AGENTS.md"
    agents_md.write_text("# AGENTS\n\nSome content\n\n## Pod Conduct\nSee POD_CONDUCT.md\n")

    result = heal.check_pod_conduct_injection("team_bot_a", config_path, os_user="team_bot_a")
    assert result is True


def test_inject_preserves_existing_content_when_appending(tmp_path: Path, monkeypatch):
    """When AGENTS.md exists but lacks POD_CONDUCT.md reference, the inject
    should APPEND the reference, not replace the file. Mock the sudo cp
    to capture what would be written."""
    # Function computes workspace as config_path.parent / "workspace".
    # config_path is ~/.openclaw/openclaw.json on real bots; tests
    # mirror that layout so workspace resolves to ~/.openclaw/workspace.
    openclaw_dir = tmp_path / ".openclaw"
    openclaw_dir.mkdir()
    config_path = openclaw_dir / "openclaw.json"
    config_path.write_text("{}")
    workspace = openclaw_dir / "workspace"
    workspace.mkdir()
    agents_md = workspace / "AGENTS.md"
    original_content = "# AGENTS\n\nImportant existing content that must survive\n"
    agents_md.write_text(original_content)

    captured_writes: list[str] = []

    real_run = heal.subprocess.run

    def fake_run(cmd, **kwargs):
        # Capture sudo cp's source content
        if isinstance(cmd, list) and len(cmd) >= 4 and cmd[0:2] == ["sudo", "/bin/cp"]:
            src = Path(cmd[2])
            if src.exists():
                captured_writes.append(src.read_text())
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        # Other subprocess calls (chown etc) — succeed quietly
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(heal.subprocess, "run", fake_run)

    # /Users/Shared/evolve/POD_CONDUCT.md may or may not exist on the test
    # host. If it does, the inject path will try to copy it (mocked above
    # to succeed); if not, the copy is skipped. Either way the AGENTS.md
    # write captured below is what we're asserting on.
    result = heal.check_pod_conduct_injection("team_bot_a", config_path, os_user="team_bot_a")

    assert result is True
    # Verify the captured AGENTS.md write contains BOTH the original content
    # AND the new POD_CONDUCT reference. This is the data-safety property.
    agents_writes = [w for w in captured_writes if "AGENTS" in w or "Pod Conduct" in w]
    assert len(agents_writes) >= 1
    written = agents_writes[-1]
    assert "Important existing content that must survive" in written
    assert "POD_CONDUCT.md" in written
    assert "## Pod Conduct" in written
