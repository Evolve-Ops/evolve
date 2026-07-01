"""Unit tests for install_heartbeat_instruction — v17 replacement for install_oc_hook.

Spec: docs/spec-heartbeat-instruction-2026-06-03.md §3.

These tests exercise the helper against real ``tmp_path`` workspaces
(no mocking of file I/O — the helper does direct writes, evolve has
write ACL on workspace, no sudo path to mock).

The ``get_bot_user`` lookup IS mocked to redirect ``/Users/{bot_user}/
.openclaw/workspace`` to a tmp-path equivalent.

Coverage:
  * Append-new-section: file doesn't have the anchor → append at end
    with the evolve-managed marker
  * File-doesn't-exist: helper creates HEARTBEAT.md/AGENTS.md with the
    documented default header before appending
  * Idempotent on identical re-install: returns ok with
    already_present=True; file unchanged
  * Replace-existing-managed-section: same anchor with the marker →
    body replaced atomically
  * Refuse-to-clobber-operator-content: same anchor WITHOUT the marker
    → returns error, file unchanged
  * Section bounds: helper writes only between the heading and the next
    heading at same/higher level
  * Required-field validation (bot_id, file, section_anchor, body)
  * section_anchor must start with `#` (markdown heading)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.install_helpers import (  # noqa: E402
    install_heartbeat_instruction,
    _find_section,
    _make_artifact,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_workspace(tmp_path: Path, monkeypatch):
    """Tmp dir that pretends to be /Users/{bot}/.openclaw/workspace.

    Patches ``get_bot_user`` so the helper resolves the bot to a user
    whose ``/Users/{user}/.openclaw/workspace`` IS this tmp_path. The
    simplest way: pick a bot_user name, then patch
    ``Path(f"/Users/{bot_user}/.openclaw/workspace")`` to return tmp_path
    via a wrapper that overrides Path's __new__ for that exact prefix.

    Easier in practice: just patch the helper module's Path constructor
    for the target path.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()

    # The helper does Path(f"/Users/{bot_user}/.openclaw/workspace").
    # We patch Path inside the helper to redirect that exact pattern.
    real_path = Path
    target_prefix = "/Users/personal-bot/.openclaw/workspace"

    def fake_path(p, *args, **kwargs):
        s = str(p)
        if s == target_prefix:
            return ws
        if s.startswith(target_prefix + "/"):
            return ws / s[len(target_prefix) + 1:]
        return real_path(p, *args, **kwargs)

    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.Path", fake_path,
    )
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.get_bot_user",
        lambda bot_id, network: "personal-bot",
    )
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.load_network",
        lambda: {},
    )
    return ws


# ── Required-field validation ────────────────────────────────────────────────


def test_rejects_empty_required_fields() -> None:
    for kwargs in (
        {"bot_id": "", "file": "HEARTBEAT.md", "section_anchor": "## X", "body": "y"},
        {"bot_id": "x", "file": "", "section_anchor": "## X", "body": "y"},
        {"bot_id": "x", "file": "HEARTBEAT.md", "section_anchor": "", "body": "y"},
        {"bot_id": "x", "file": "HEARTBEAT.md", "section_anchor": "## X", "body": ""},
    ):
        r = install_heartbeat_instruction(**kwargs)
        assert r["ok"] is False
        assert "requires" in r["error"]


def test_section_anchor_must_be_markdown_heading() -> None:
    """A section anchor without leading ``#``s isn't a markdown heading."""
    r = install_heartbeat_instruction(
        bot_id="x", file="HEARTBEAT.md", section_anchor="Task Manager", body="y",
    )
    assert r["ok"] is False
    assert "markdown heading" in r["error"]


# ── Append a new section to an existing file ─────────────────────────────────


def test_appends_new_section_to_existing_file(fake_workspace: Path) -> None:
    (fake_workspace / "HEARTBEAT.md").write_text(
        "# Heartbeat instructions\n\nThis is the operator note.\n"
    )

    result = install_heartbeat_instruction(
        bot_id="personal-bot",
        file="HEARTBEAT.md",
        section_anchor="## Task Manager — Heartbeat Check",
        body="Every heartbeat, run `python3 scripts/tasks.py check`.",
        pkg_id="p-9bfa1c84",
        job_id="j-abcdef",
    )

    assert result["ok"] is True
    assert result["artifact"] == "HEARTBEAT.md#Task Manager — Heartbeat Check"
    assert result["already_present"] is False
    assert result["created_file"] is False

    text = (fake_workspace / "HEARTBEAT.md").read_text()
    # Original content preserved.
    assert "This is the operator note." in text
    # New section appended with marker and pkg/job attribution.
    assert "## Task Manager — Heartbeat Check" in text
    assert "<!-- evolve-managed: pkg=p-9bfa1c84 job=j-abcdef -->" in text
    assert "python3 scripts/tasks.py check" in text


# ── Create the file when it doesn't exist ────────────────────────────────────


def test_creates_heartbeat_md_when_absent(fake_workspace: Path) -> None:
    """Helper auto-creates HEARTBEAT.md with the documented default header."""
    assert not (fake_workspace / "HEARTBEAT.md").exists()

    result = install_heartbeat_instruction(
        bot_id="personal-bot",
        file="HEARTBEAT.md",
        section_anchor="## Task Manager — Heartbeat Check",
        body="Every heartbeat, run task-check.",
        pkg_id="p-9bfa1c84",
    )

    assert result["ok"] is True
    assert result["created_file"] is True
    text = (fake_workspace / "HEARTBEAT.md").read_text()
    # Default header present (so operator knows what evolve-managed means).
    assert "# Heartbeat instructions" in text
    assert "evolve-managed" in text
    # Section appended.
    assert "## Task Manager — Heartbeat Check" in text


def test_creates_agents_md_when_absent(fake_workspace: Path) -> None:
    """Helper supports AGENTS.md too, with its own default header."""
    result = install_heartbeat_instruction(
        bot_id="personal-bot",
        file="AGENTS.md",
        section_anchor="## Session start — task summary",
        body="On session start, show the task summary.",
    )
    assert result["ok"] is True
    assert result["created_file"] is True
    text = (fake_workspace / "AGENTS.md").read_text()
    assert "# Agent instructions" in text


def test_creates_with_generic_default_for_unknown_filename(fake_workspace: Path) -> None:
    """A file the helper doesn't have a custom header for still gets created
    (but with no specialised header)."""
    result = install_heartbeat_instruction(
        bot_id="personal-bot",
        file="CUSTOM.md",
        section_anchor="## X",
        body="x",
    )
    assert result["ok"] is True
    assert result["created_file"] is True
    text = (fake_workspace / "CUSTOM.md").read_text()
    assert "## X" in text


# ── Idempotency ──────────────────────────────────────────────────────────────


def test_idempotent_on_identical_reinstall(fake_workspace: Path) -> None:
    """Calling install twice with the same args is a no-op on the second
    call: returns ok with already_present=True, file unchanged."""
    args = dict(
        bot_id="personal-bot",
        file="HEARTBEAT.md",
        section_anchor="## Task Manager — Heartbeat Check",
        body="Every heartbeat, run task-check.",
        pkg_id="p-9bfa1c84",
        job_id="j-abc",
    )
    first = install_heartbeat_instruction(**args)
    text_after_first = (fake_workspace / "HEARTBEAT.md").read_text()

    second = install_heartbeat_instruction(**args)
    text_after_second = (fake_workspace / "HEARTBEAT.md").read_text()

    assert first["ok"] is True
    assert first["already_present"] is False
    assert second["ok"] is True
    assert second["already_present"] is True
    assert text_after_first == text_after_second


# ── Replace an existing managed section ──────────────────────────────────────


def test_replaces_existing_managed_section(fake_workspace: Path) -> None:
    """Same anchor, marker present → body replaced atomically; other
    sections in the file are not touched."""
    (fake_workspace / "HEARTBEAT.md").write_text(
        "# Heartbeat instructions\n\n"
        "## Task Manager — Heartbeat Check\n"
        "<!-- evolve-managed: pkg=p-9bfa1c84 -->\n\n"
        "Old body.\n\n"
        "## Another App\n\n"
        "Operator-authored content stays intact.\n"
    )
    result = install_heartbeat_instruction(
        bot_id="personal-bot",
        file="HEARTBEAT.md",
        section_anchor="## Task Manager — Heartbeat Check",
        body="New body with `python3 scripts/tasks.py check`.",
        pkg_id="p-9bfa1c84",
    )
    assert result["ok"] is True
    assert result["already_present"] is False
    text = (fake_workspace / "HEARTBEAT.md").read_text()
    assert "Old body." not in text
    assert "New body" in text
    # Other section preserved verbatim.
    assert "## Another App\n\nOperator-authored content stays intact." in text


# ── Refuse to clobber operator content ───────────────────────────────────────


def test_refuses_to_overwrite_unmanaged_section(fake_workspace: Path) -> None:
    """Same anchor, NO marker → operator wrote it; helper refuses."""
    (fake_workspace / "HEARTBEAT.md").write_text(
        "# Heartbeat instructions\n\n"
        "## Task Manager — Heartbeat Check\n\n"
        "Operator-authored. Do not clobber.\n"
    )
    original_text = (fake_workspace / "HEARTBEAT.md").read_text()

    result = install_heartbeat_instruction(
        bot_id="personal-bot",
        file="HEARTBEAT.md",
        section_anchor="## Task Manager — Heartbeat Check",
        body="Forge wants to write this.",
    )
    assert result["ok"] is False
    assert "evolve-managed" in result["error"]
    # File unchanged.
    assert (fake_workspace / "HEARTBEAT.md").read_text() == original_text


# ── Section-boundary helper (_find_section) ──────────────────────────────────


def test_find_section_returns_correct_bounds() -> None:
    text = (
        "# Heartbeat instructions\n\n"
        "Header note.\n\n"
        "## Task Manager — Check\n"
        "Body 1.\n\n"
        "## Another App\n"
        "Body 2.\n"
    )
    pos = _find_section(text, "## Task Manager — Check")
    assert pos is not None
    start, end = pos
    section = text[start:end]
    assert section.startswith("## Task Manager — Check")
    assert "Body 1." in section
    # Boundary stops BEFORE the next ## heading.
    assert "Body 2." not in section


def test_find_section_returns_none_when_missing() -> None:
    text = "# Top\n\n## A\n\nbody\n"
    assert _find_section(text, "## B") is None


def test_find_section_respects_heading_level() -> None:
    """A ``###`` section ends at the next ``###`` or higher (``##``/``#``),
    not at every nested heading below it."""
    text = (
        "# Top\n\n"
        "### Sub A\n"
        "Sub-A body.\n\n"
        "### Sub B\n"
        "Sub-B body.\n"
    )
    pos = _find_section(text, "### Sub A")
    assert pos is not None
    section = text[pos[0]:pos[1]]
    assert "Sub-A body." in section
    assert "Sub-B body." not in section


def test_find_section_rejects_non_heading_anchor() -> None:
    """An anchor without leading ``#``s isn't a heading and returns None."""
    text = "## Real section\nbody\n"
    assert _find_section(text, "Real section") is None


# ── _make_artifact ───────────────────────────────────────────────────────────


def test_make_artifact_strips_heading_markers() -> None:
    assert _make_artifact("HEARTBEAT.md", "## Task Manager — Check") == \
           "HEARTBEAT.md#Task Manager — Check"
    assert _make_artifact("AGENTS.md", "### Session start") == \
           "AGENTS.md#Session start"


# ── Ownership restore after os.replace ───────────────────────────────────────


def test_restores_bot_ownership_after_write(fake_workspace: Path, monkeypatch) -> None:
    """``os.replace`` leaves the new file owned by the writer (evolve in
    prod). Without the chown, install_integrity_monitor flags ownership
    drift on every write. Regression for atlas's HEARTBEAT.md owned by
    evolve:staff instead of atlas:staff.
    """
    chown_calls: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kwargs):
        chown_calls.append(list(cmd))
        return FakeProc()

    # Pretend the bot user exists so the helper actually issues the chown.
    import pwd as real_pwd
    real_uid = real_pwd.getpwuid(0).pw_uid  # root — any existing uid

    class FakePwent:
        pw_uid = real_uid + 999  # ≠ test runner's uid, forces chown attempt

    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.subprocess.run", fake_run,
    )
    monkeypatch.setattr(
        "pwd.getpwnam",
        lambda name: FakePwent() if name == "personal-bot" else real_pwd.getpwnam(name),
    )

    result = install_heartbeat_instruction(
        bot_id="personal-bot",
        file="HEARTBEAT.md",
        section_anchor="## X",
        body="y",
        pkg_id="p-abc",
    )

    assert result["ok"] is True, result
    # Exactly one chown call, with bot_user:staff and the target path.
    assert len(chown_calls) == 1, chown_calls
    cmd = chown_calls[0]
    assert cmd[:2] == ["sudo", "/usr/sbin/chown"]
    assert cmd[2] == "personal-bot:staff"
    assert cmd[3].endswith("/HEARTBEAT.md")


def test_skips_chown_when_bot_user_missing(fake_workspace: Path, monkeypatch) -> None:
    """Test environments don't have the bot user in /etc/passwd; the helper
    must not blow up — it returns ok and skips the sudo call."""
    chown_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        chown_calls.append(list(cmd))
        raise AssertionError("should not invoke sudo when bot user is missing")

    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.subprocess.run", fake_run,
    )

    # Don't patch pwd.getpwnam — "personal-bot" really isn't a user on the
    # CI host, so getpwnam raises KeyError and the helper short-circuits.
    result = install_heartbeat_instruction(
        bot_id="personal-bot",
        file="HEARTBEAT.md",
        section_anchor="## X",
        body="y",
    )

    assert result["ok"] is True, result
    assert chown_calls == []
