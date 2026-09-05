"""Regression tests for the I/O safety wrapper around inject_handoff_check
and inject_session_surface_check in deploy.py.

These tests pin the data-safety invariant introduced in the May 2026 fix:
when read_text() raises PermissionError or OSError (i.e. the file EXISTS but
is unreadable), both inject functions MUST abort without writing — never treat
a read failure as "file is empty" and overwrite with a minimal template.

Coverage gap that hid the original bug
---------------------------------------
``tests/test_inject_block_delimiters.py`` exercises only the pure helper
functions ``_build_handoff_agents_md`` / ``_build_surface_agents_md`` and
passes ``""`` as the existing content, which is indistinguishable from the
silent-failure path. This file covers the I/O wrapper layer that the pure-
builder tests deliberately skip.

Mirrors the structure of packages/analyzer/tests/test_heal_pod_conduct.py
which added the equivalent regression coverage for heal.py after the April
2026 incident.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Point at the worktree's copy of evolve_admin (conftest.py handles the
# editable-install shadow; these explicit inserts are belt-and-suspenders).
_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402
from evolve_admin.deploy import (  # noqa: E402
    EVOLVE_HANDOFF_BEGIN,
    EVOLVE_HANDOFF_END,
    inject_handoff_check,
    inject_session_surface_check,
)


# ── helpers ──────────────────────────────────────────────────────────────────

RICH_CONTENT = (
    "# team_bot_a\n\n"
    "## Identity\nI am team_bot_a. I help Pod_admin run the evolve-ops pod.\n\n"
    "## Session Start\n- Read SOUL.md and USER.md first.\n\n"
    "## Capability Deployment Protocol\n"
    "- Run deploy_checklist.py before declaring anything built.\n\n"
    "## Message Processing Protocol\n"
    "- Prefer Slack Socket Mode. No polling scripts.\n"
) * 30  # ~15 KB of realistic rich content


def _make_workspace(tmp_path: Path, content: str | None = None) -> tuple[Path, Path]:
    """Create a minimal workspace layout and return (workspace, agents_md)."""
    workspace = tmp_path / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    agents_md = workspace / "AGENTS.md"
    if content is not None:
        agents_md.write_text(content)
    return workspace, agents_md


# ── inject_handoff_check: abort-on-read-failure ───────────────────────────────


class TestInjectHandoffCheckAbortOnReadFailure:
    """inject_handoff_check MUST NOT clobber AGENTS.md when read fails."""

    def _run(self, tmp_path: Path, agents_md: Path, monkeypatch):
        """Wire bot_user resolution to tmp_path and run the function."""
        # Monkeypatch _bot_user_for to return a predictable string, and rehome
        # the bot's home dir onto tmp_path. Post-platform_profile sweep the
        # workspace path is built via the evolve_config.user_home seam (deploy
        # imports it as _user_home), so patch that binding — Path()
        # interception can no longer see the construction.
        monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "fake_user")
        monkeypatch.setattr(deploy, "_user_home", lambda u: tmp_path)
        inject_handoff_check("fake_bot")

    def test_permission_error_aborts_write(self, tmp_path: Path, monkeypatch, capsys):
        """PermissionError on read → file unchanged, abort message logged."""
        _, agents_md = _make_workspace(tmp_path, RICH_CONTENT)
        original_size = agents_md.stat().st_size

        real_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self == agents_md:
                raise PermissionError(f"[Errno 13] Permission denied: '{self}'")
            return real_read_text(self, *args, **kwargs)

        with patch.object(Path, "read_text", fake_read_text):
            self._run(tmp_path, agents_md, monkeypatch)

        # File must be unchanged
        assert agents_md.stat().st_size == original_size, (
            f"AGENTS.md was written ({agents_md.stat().st_size} B) "
            f"despite PermissionError on read (was {original_size} B)"
        )
        assert RICH_CONTENT[:50] in agents_md.read_text(), \
            "Rich content was overwritten on PermissionError read failure"

        captured = capsys.readouterr()
        assert "inject_handoff_check" in captured.out
        assert "skipping injection" in captured.out
        assert "file preserved" in captured.out

    def test_oserror_aborts_write(self, tmp_path: Path, monkeypatch, capsys):
        """Generic OSError on read → file unchanged, abort message logged."""
        _, agents_md = _make_workspace(tmp_path, RICH_CONTENT)
        original_size = agents_md.stat().st_size

        real_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self == agents_md:
                raise OSError("I/O error reading file")
            return real_read_text(self, *args, **kwargs)

        with patch.object(Path, "read_text", fake_read_text):
            self._run(tmp_path, agents_md, monkeypatch)

        assert agents_md.stat().st_size == original_size, \
            "AGENTS.md was written despite OSError on read"
        assert RICH_CONTENT[:50] in agents_md.read_text(), \
            "Rich content was overwritten on OSError read failure"

        captured = capsys.readouterr()
        assert "skipping injection" in captured.out

    def test_file_not_found_proceeds_with_empty(self, tmp_path: Path, monkeypatch):
        """FileNotFoundError → file doesn't exist yet; inject should proceed and create it."""
        workspace = tmp_path / ".openclaw" / "workspace"
        workspace.mkdir(parents=True)
        agents_md = workspace / "AGENTS.md"
        # Deliberately NOT creating agents_md — FileNotFoundError is the
        # legitimate "new bot" case.

        monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "fake_user")
        monkeypatch.setattr(deploy, "_user_home", lambda u: tmp_path)

        # Mock the write path so we don't need sudo; capture what would be written
        written: list[str] = []

        real_write_text = Path.write_text

        def fake_write_text(self, content, *args, **kwargs):
            if self == agents_md:
                written.append(content)
                # Actually write so the file exists for assertions
                real_write_text(self, content, *args, **kwargs)
            else:
                real_write_text(self, content, *args, **kwargs)

        with patch.object(Path, "write_text", fake_write_text):
            inject_handoff_check("fake_bot")

        # The handoff block should have been written since the file didn't exist
        assert len(written) >= 1 or agents_md.exists(), \
            "inject should have created AGENTS.md when it didn't exist"
        if written:
            assert EVOLVE_HANDOFF_BEGIN in written[0], \
                "Written content should contain the handoff block"

    def test_existing_rich_content_preserved_on_success(self, tmp_path: Path, monkeypatch):
        """When read succeeds with rich content, the handoff block is injected
        and ALL existing content is preserved (not replaced)."""
        _, agents_md = _make_workspace(tmp_path, RICH_CONTENT)

        monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "fake_user")
        monkeypatch.setattr(deploy, "_user_home", lambda u: tmp_path)
        inject_handoff_check("fake_bot")

        result = agents_md.read_text()
        assert EVOLVE_HANDOFF_BEGIN in result, "Handoff block should be present"
        assert EVOLVE_HANDOFF_END in result, "Handoff block end marker should be present"
        assert "I am team_bot_a." in result, "Rich identity content should survive inject"
        assert "deploy_checklist.py" in result, "Protocol content should survive inject"
        assert "No polling scripts." in result, "Tactic notes should survive inject"


# ── inject_session_surface_check: abort-on-read-failure ──────────────────────


class TestInjectSessionSurfaceCheckAbortOnReadFailure:
    """inject_session_surface_check MUST NOT clobber AGENTS.md when read fails."""

    def _run(self, tmp_path: Path, agents_md: Path, monkeypatch):
        """Wire bot_user resolution and run inject_session_surface_check."""
        # Same seam as above: workspace is built via deploy._user_home.
        monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "fake_user")
        monkeypatch.setattr(deploy, "_user_home", lambda u: tmp_path)
        inject_session_surface_check("fake_bot")

    def test_permission_error_and_sudo_cat_fail_aborts_write(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """PermissionError on direct read + sudo cat failure → file unchanged."""
        _, agents_md = _make_workspace(tmp_path, RICH_CONTENT)
        original_size = agents_md.stat().st_size

        real_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self == agents_md:
                raise PermissionError("Permission denied")
            return real_read_text(self, *args, **kwargs)

        fake_failed_run = MagicMock()
        fake_failed_run.returncode = 1
        fake_failed_run.stdout = ""
        fake_failed_run.stderr = "Permission denied"

        with patch.object(Path, "read_text", fake_read_text), \
             patch("evolve_admin.deploy.subprocess.run", return_value=fake_failed_run):
            self._run(tmp_path, agents_md, monkeypatch)

        assert agents_md.stat().st_size == original_size, \
            "AGENTS.md was written despite read+sudo-cat failure"
        assert RICH_CONTENT[:50] in agents_md.read_text(), \
            "Rich content was overwritten on read failure"

        captured = capsys.readouterr()
        assert "inject_session_surface_check" in captured.out
        assert "skipping scrub" in captured.out
        assert "file preserved" in captured.out

    def test_oserror_and_sudo_cat_fail_aborts_write(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """OSError on direct read + sudo cat failure → file unchanged."""
        _, agents_md = _make_workspace(tmp_path, RICH_CONTENT)
        original_size = agents_md.stat().st_size

        real_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self == agents_md:
                raise OSError("I/O error")
            return real_read_text(self, *args, **kwargs)

        fake_failed_run = MagicMock()
        fake_failed_run.returncode = 1
        fake_failed_run.stdout = ""

        with patch.object(Path, "read_text", fake_read_text), \
             patch("evolve_admin.deploy.subprocess.run", return_value=fake_failed_run):
            self._run(tmp_path, agents_md, monkeypatch)

        assert agents_md.stat().st_size == original_size, \
            "AGENTS.md was written despite OSError + sudo cat failure"

        captured = capsys.readouterr()
        assert "skipping scrub" in captured.out

    def test_permission_error_but_sudo_cat_succeeds_proceeds(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """PermissionError on direct read + sudo cat succeeds → scrub proceeds normally.
        This is the legitimate fallback path: direct ACL not yet applied, sudo grant works."""
        content_without_surface_block = RICH_CONTENT  # no surface block to scrub
        _, agents_md = _make_workspace(tmp_path, content_without_surface_block)

        real_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self == agents_md:
                raise PermissionError("Permission denied")
            return real_read_text(self, *args, **kwargs)

        # sudo cat "succeeds" and returns the rich content (no surface block)
        fake_ok_run = MagicMock()
        fake_ok_run.returncode = 0
        fake_ok_run.stdout = content_without_surface_block

        calls: list = []

        def tracking_run(cmd, **kwargs):
            calls.append(cmd)
            return fake_ok_run

        with patch.object(Path, "read_text", fake_read_text), \
             patch("evolve_admin.deploy.subprocess.run", side_effect=tracking_run):
            self._run(tmp_path, agents_md, monkeypatch)

        # Content had no surface block, so the scrub is a no-op (no write)
        captured = capsys.readouterr()
        assert "already absent" in captured.out or len(calls) <= 1, \
            "Expected no-op since content has no surface block to scrub"

    def test_file_not_found_is_noop(self, tmp_path: Path, monkeypatch, capsys):
        """FileNotFoundError → file doesn't exist, nothing to scrub, no write."""
        workspace = tmp_path / ".openclaw" / "workspace"
        workspace.mkdir(parents=True)
        agents_md = workspace / "AGENTS.md"
        # Do NOT create agents_md

        monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "fake_user")
        monkeypatch.setattr(deploy, "_user_home", lambda u: tmp_path)
        inject_session_surface_check("fake_bot")

        # File should still not exist (nothing to scrub → no-op write)
        assert not agents_md.exists(), \
            "AGENTS.md should not be created when it didn't exist"

        captured = capsys.readouterr()
        assert "already absent" in captured.out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
