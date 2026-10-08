"""tests/test_skills_obsidian_install.py — Obsidian filesystem skill installer.

Tests for V1.5-2 obsidian_install.py:
  - resolve_status routes by (vault_config, path validity) into the state machine.
  - build_install_plan returns correct ordered steps for each state.
  - validate_vault_path enforces the exec-approval boundary (absolute path,
    existing dir, readable, not too broad).
  - The access panel describes read-only by default; write opt-in is explicit.
  - The install plan includes the access panel on the set_vault_path step.

All filesystem calls are tested via tmpdir fixtures — no mock required for
most tests because the helpers operate on actual paths. The resolve_status
function uses injected callables so it can be tested without touching disk.
"""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import obsidian_install  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def safe_tmp_path():
    """tmp_path replacement that lives outside the vault blocklist.

    obsidian_install.validate_vault_path rejects /tmp and /private/tmp
    (production blocklist — vaults stored there would be wiped). pytest's
    built-in tmp_path follows ``$TMPDIR``; under harness/CI environments
    that set ``TMPDIR=/tmp/...`` (Claude Code does, many CI runners do)
    tmp_path lands inside the blocklist and tests that exercise the
    validator's happy path falsely fail with vault_path_reserved_location.

    Scratch dirs under ~/.cache/evolve-pytest stay outside every blocklist
    prefix on every host. Cleaned up per-test.
    """
    base = Path.home() / ".cache" / "evolve-pytest" / "obsidian"
    base.mkdir(parents=True, exist_ok=True)
    d = base / uuid.uuid4().hex[:12]
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def vault_dir(safe_tmp_path):
    """A minimal Obsidian vault directory with a few markdown files."""
    vault = safe_tmp_path / "MyVault"
    vault.mkdir()
    (vault / "Meeting notes.md").write_text("# Meeting notes\n\nDiscussed Q3 targets.\n")
    (vault / "Ideas.md").write_text("# Ideas\n\nBuild something useful.\n")
    daily = vault / "daily"
    daily.mkdir()
    (daily / "2026-05-12.md").write_text("# 2026-05-12\n\nToday I worked on V1.5.\n")
    return vault


# ── validate_vault_path ───────────────────────────────────────────────────────


def test_validate_vault_path_empty():
    ok, reason = obsidian_install.validate_vault_path("")
    assert not ok
    assert reason == "vault_path_empty"


def test_validate_vault_path_whitespace():
    ok, reason = obsidian_install.validate_vault_path("   ")
    assert not ok
    assert reason == "vault_path_empty"


def test_validate_vault_path_nonexistent(safe_tmp_path):
    ok, reason = obsidian_install.validate_vault_path(str(safe_tmp_path / "nonexistent"))
    assert not ok
    assert reason == "vault_not_found"


def test_validate_vault_path_file_not_dir(safe_tmp_path):
    f = safe_tmp_path / "notadirectory.md"
    f.write_text("hello")
    ok, reason = obsidian_install.validate_vault_path(str(f))
    assert not ok
    assert reason == "vault_not_a_directory"


def test_validate_vault_path_too_broad():
    ok, reason = obsidian_install.validate_vault_path("/")
    assert not ok
    assert reason == "vault_path_too_broad"


def test_validate_vault_path_too_broad_users():
    ok, reason = obsidian_install.validate_vault_path("/Users")
    assert not ok
    assert reason == "vault_path_too_broad"


def test_validate_vault_path_valid(vault_dir):
    ok, reason = obsidian_install.validate_vault_path(str(vault_dir))
    assert ok
    assert reason is None


# ── resolve_status ────────────────────────────────────────────────────────────


def _make_readers(vault_path=None, write_daily_note=False, path_ok=True, path_err=None, suggested=None):
    """Build the three injectable callables for resolve_status."""
    def read_vault_config(bot_id):
        if vault_path is None:
            return {}
        return {"vault_path": vault_path, "write_daily_note": write_daily_note}

    def check_path_readable(path_str):
        return (path_ok, path_err if not path_ok else None)

    def find_suggested_vault():
        return suggested

    return read_vault_config, check_path_readable, find_suggested_vault


def test_resolve_status_no_vault_configured():
    read_vc, check_pr, find_sv = _make_readers(vault_path=None, suggested="/Users/alex/Documents/Obsidian")
    status = obsidian_install.resolve_status(
        "admin_bot",
        read_vault_config=read_vc,
        check_path_readable=check_pr,
        find_suggested_vault=find_sv,
    )
    assert status.status == "no_vault_configured"
    assert status.vault_path is None
    assert status.suggested_path == "/Users/alex/Documents/Obsidian"
    assert not status.write_daily_note_enabled


def test_resolve_status_vault_not_found():
    read_vc, _, find_sv = _make_readers(
        vault_path="/tmp/missing-vault",
        path_ok=False,
        path_err="vault_not_found",
        suggested=None,
    )
    status = obsidian_install.resolve_status(
        "admin_bot",
        read_vault_config=read_vc,
        check_path_readable=lambda p: (False, "vault_not_found"),
        find_suggested_vault=find_sv,
    )
    assert status.status == "vault_not_found"
    assert status.vault_path == "/tmp/missing-vault"
    assert status.error == "vault_not_found"


def test_resolve_status_vault_not_readable():
    read_vc, _, find_sv = _make_readers(
        vault_path="/tmp/some-vault",
        path_ok=False,
        path_err="vault_not_readable",
    )
    status = obsidian_install.resolve_status(
        "team_bot_a",
        read_vault_config=read_vc,
        check_path_readable=lambda p: (False, "vault_not_readable"),
        find_suggested_vault=find_sv,
    )
    assert status.status == "vault_not_readable"


def test_resolve_status_active(vault_dir):
    vault_str = str(vault_dir)
    read_vc, check_pr, find_sv = _make_readers(vault_path=vault_str, path_ok=True)
    status = obsidian_install.resolve_status(
        "admin_bot",
        read_vault_config=read_vc,
        check_path_readable=check_pr,
        find_suggested_vault=find_sv,
    )
    assert status.status == "active"
    assert status.vault_path == vault_str
    assert status.note_count is not None
    assert status.note_count >= 3  # 3 .md files in fixture


def test_resolve_status_active_with_write_enabled(vault_dir):
    vault_str = str(vault_dir)
    read_vc, check_pr, find_sv = _make_readers(
        vault_path=vault_str, write_daily_note=True, path_ok=True
    )
    status = obsidian_install.resolve_status(
        "team_bot_a",
        read_vault_config=read_vc,
        check_path_readable=check_pr,
        find_suggested_vault=find_sv,
    )
    assert status.status == "active"
    assert status.write_daily_note_enabled is True


def test_resolve_status_config_read_error():
    def bad_reader(bot_id):
        raise OSError("disk full")

    status = obsidian_install.resolve_status(
        "team_bot_a",
        read_vault_config=bad_reader,
        check_path_readable=lambda p: (True, None),
        find_suggested_vault=lambda: None,
    )
    assert status.status == "unknown"
    assert "OSError" in status.error


# ── build_install_plan ────────────────────────────────────────────────────────


def _status(st, vault_path=None, suggested=None):
    return obsidian_install.InstallStatus(
        bot_id="admin_bot",
        status=st,
        vault_path=vault_path,
        suggested_path=suggested,
    )


def test_plan_active():
    plan = obsidian_install.build_install_plan(_status("active", vault_path="/tmp/v"))
    assert plan == []


def test_plan_unknown():
    plan = obsidian_install.build_install_plan(_status("unknown"))
    assert plan == []


def test_plan_no_vault_configured():
    plan = obsidian_install.build_install_plan(
        _status("no_vault_configured", suggested="/Users/alex/Documents/Obsidian")
    )
    assert len(plan) == 2
    step0 = plan[0]
    assert step0.id == "set_vault_path"
    assert step0.access_panel is not None
    assert "vault_path" in step0.payload or step0.payload.get("suggested_path") is not None
    step1 = plan[1]
    assert step1.id == "confirm"
    assert step1.endpoint is not None


def test_plan_vault_not_found():
    plan = obsidian_install.build_install_plan(_status("vault_not_found", vault_path="/tmp/gone"))
    assert len(plan) == 2
    assert plan[0].id == "set_vault_path"
    assert plan[1].id == "confirm"


def test_plan_vault_not_readable():
    plan = obsidian_install.build_install_plan(_status("vault_not_readable"))
    assert len(plan) == 2
    assert plan[0].id == "set_vault_path"


# ── Access panel content ──────────────────────────────────────────────────────


def test_access_panel_has_will_wont():
    panel = obsidian_install.VAULT_ACCESS_PANEL
    assert "will" in panel
    assert "wont" in panel
    assert len(panel["will"]) >= 2
    assert len(panel["wont"]) >= 2
    # No jargon in the summary (Plex test: no "OAuth", "scopes", "API key")
    summary = panel["summary"]
    for jargon in ("OAuth", "scope", "API key", "endpoint"):
        assert jargon not in summary, f"Jargon found in summary: {jargon!r}"


def test_access_panel_write_opt_in_documented():
    """The neutral install panel must signal that write access is an opt-in
    via the read+write mode (not the default). The exact opt-in phrasing
    moved from "daily note" → mode_choices after the 2026-05-30 rewire."""
    panel = obsidian_install.VAULT_ACCESS_PANEL
    # The mode_choices radio is the structural opt-in: the UI shows read
    # selected by default and the operator must explicitly flip to
    # read_write to grant writes.
    choices = panel.get("mode_choices") or []
    assert len(choices) == 2
    assert choices[0]["value"] == "read", (
        "read must be the first (default-selected) choice — see "
        "internal/design/paste-token-skills-future-2026-05-30.md "
        "open question #2 on safe defaults"
    )
    # And the summary copy must mention that the user chooses
    assert "choose" in panel["summary"].lower()


def test_skill_kind_is_filesystem():
    assert obsidian_install.OBSIDIAN_SKILL_KIND == "filesystem"
    assert obsidian_install.SKILL_REGISTRY_ENTRY["kind"] == "filesystem"


def test_to_dict_roundtrip(vault_dir):
    read_vc, check_pr, find_sv = _make_readers(vault_path=str(vault_dir), path_ok=True)
    status = obsidian_install.resolve_status(
        "admin_bot",
        read_vault_config=read_vc,
        check_path_readable=check_pr,
        find_suggested_vault=find_sv,
    )
    d = status.to_dict()
    assert d["skill_id"] == "obsidian_vault"
    assert d["kind"] == "filesystem"
    assert d["status"] == "active"
    assert d["vault_path"] == str(vault_dir)


# ── Security: reserved-location blacklist (V1.5-2 fix-up) ────────────────────
# These tests pin the fix for the reviewer's finding:
#   "validate_vault_path accepts /etc, ~/.ssh, /tmp, /var/log, ~/Library
#    as valid Obsidian vaults. Combined with write_daily_note=true the bot
#    can write into ~/.ssh/2026-05-13.md."
#
# The blacklist is a HARD reject — any path under a reserved prefix returns
# (False, "vault_path_reserved_location: ...") before the path existence or
# readability checks run. This means the test is purely logic-based and does
# not require the paths to exist on disk.


@pytest.mark.parametrize("bad_path", [
    "/etc",
    "/etc/",
    "/var/log",
    "/tmp",
    "/Library",
    "/private/etc",
    "/dev",
    "/System",
    "/usr/local/lib",
    "/bin",
])
def test_validate_vault_path_rejects_reserved_system_locations(bad_path):
    """System-level paths are unconditionally rejected.

    The test supplies paths directly (without expanduser) since they are
    already absolute. validate_vault_path resolves them internally; the
    blacklist check runs on both the expanded and resolved forms to defeat
    symlink bypasses.
    """
    ok, err = obsidian_install.validate_vault_path(bad_path)
    assert not ok, f"{bad_path!r} should be rejected"
    assert err is not None
    assert "reserved" in err.lower() or "system" in err.lower(), (
        f"error message should mention reserved/system for {bad_path!r}, got {err!r}"
    )


@pytest.mark.parametrize("bad_suffix", [
    "/.ssh",
    "/.gnupg",
    "/.aws",
    "/.config",
    "/Library",
])
def test_validate_vault_path_rejects_user_sensitive_dirs(bad_suffix):
    """Per-user sensitive dirs are rejected for the running user's home.

    expand ~ relative to the current test runner's home. The goal is to
    confirm that whatever home the evolve user has, its .ssh / .gnupg /
    .aws / .config / Library are all off-limits.
    """
    expanded = str(Path("~" + bad_suffix).expanduser())
    ok, err = obsidian_install.validate_vault_path(expanded)
    assert not ok, f"{expanded!r} (from ~{bad_suffix}) should be rejected"
    assert err is not None
    assert "reserved" in err.lower() or "system" in err.lower(), (
        f"error message should mention reserved/system for ~{bad_suffix!r}, got {err!r}"
    )


def test_validate_vault_path_ssh_keys_attack():
    """Explicitly verify the ~/.ssh attack path from the reviewer finding.

    Combined with write_daily_note=True, accepting ~/.ssh would allow the
    bot to write ~/.ssh/2026-05-13.md. This test pins that it's rejected.
    """
    ssh_path = str(Path("~/.ssh").expanduser())
    ok, err = obsidian_install.validate_vault_path(ssh_path)
    assert not ok
    assert "reserved" in (err or "").lower() or "system" in (err or "").lower()


def test_validate_vault_path_ssh_subdir_also_rejected():
    """A path under ~/.ssh/ (not just ~/.ssh itself) is also rejected."""
    subpath = str(Path("~/.ssh/evil_subdir").expanduser())
    ok, err = obsidian_install.validate_vault_path(subpath)
    assert not ok
    assert "reserved" in (err or "").lower() or "system" in (err or "").lower()


# ── Route integration tests for the Obsidian install flow ────────────────────
# These tests verify that the Flask route layer validates vault_path before
# writing config — specifically that POSTing a reserved path gets a 400,
# not a 200 or an attempt to write the config.



# Pure unit tests above (validate_vault_path, resolve_status, build_install_plan,
# access panel shape, to_dict roundtrip) stay because the helpers are reused
# by the MCP-backed install path (rewired same day as the withdrawal).
#
# Tests for the new MCP-backed install path follow.

import json as _json
import os as _os
import tempfile as _tempfile
from unittest.mock import patch


# ── ACL grant / revoke helpers (unit-level; injectable runner — no real chmod) ──


class _FakeChmodRunner:
    """Records every (sudo /bin/chmod ...) call so we can assert the right
    ACE got passed to chmod for each mode. ``returncode_for`` overrides the
    default 0 return code for specific argv prefixes — used to simulate
    a chmod failure on the recursive pass."""

    def __init__(self, returncode_for=None):
        self.calls: list[list[str]] = []
        self.returncode_for = returncode_for or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        rc = self.returncode_for.get(tuple(argv[:3]))
        if rc is not None:
            _R.returncode = rc
            _R.stderr = "fake chmod failure"
        return _R()


class TestGrantVaultAcl:
    """grant_vault_acl(vault_path, bot_user, mode) — runs the right chmod
    commands per mode. The mode toggle is enforced at the OS-permission
    layer: in read mode the bot user has no write ACE, so the filesystem
    MCP's write_file call returns EACCES even though the tool is advertised."""

    def test_read_mode_grants_read_only_ace(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = obsidian_install.grant_vault_acl(
            str(tmp_path), "team_bot_a", "read", runner=runner,
        )
        assert ok, err
        # Two chmod +a calls: top-level and recursive
        assert len(runner.calls) == 2
        for argv in runner.calls:
            assert argv[:3] == ["sudo", "/bin/chmod", "+a"] or argv[:4] == ["sudo", "/bin/chmod", "-R", "+a"]
            # ACE includes the bot user + read perms
            ace = argv[-2]
            assert ace.startswith("team_bot_a allow ")
            assert "list" in ace
            assert "readattr" in ace
            assert "file_inherit" in ace
            assert "directory_inherit" in ace
            # Read mode MUST NOT include write/delete in the ACE
            assert "write" not in ace
            assert "delete" not in ace
            assert "add_file" not in ace

    def test_read_write_mode_grants_write_capabilities(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = obsidian_install.grant_vault_acl(
            str(tmp_path), "team_bot_a", "read_write", runner=runner,
        )
        assert ok, err
        ace = runner.calls[0][-2]
        assert ace.startswith("team_bot_a allow ")
        # Read perms from read mode
        assert "list" in ace
        assert "readattr" in ace
        # Plus write-side perms
        assert "write" in ace
        assert "delete" in ace
        assert "add_file" in ace
        assert "add_subdirectory" in ace

    def test_unknown_mode_returns_error_without_running_chmod(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = obsidian_install.grant_vault_acl(
            str(tmp_path), "team_bot_a", "fly_freely", runner=runner,
        )
        assert ok is False
        assert "unknown vault mode" in err
        assert runner.calls == [], "should not run chmod for unknown mode"

    def test_missing_vault_returns_error_without_running_chmod(self, tmp_path):
        runner = _FakeChmodRunner()
        missing = tmp_path / "does-not-exist"
        ok, err = obsidian_install.grant_vault_acl(
            str(missing), "team_bot_a", "read", runner=runner,
        )
        assert ok is False
        assert "does not exist" in err
        assert runner.calls == []

    def test_chmod_failure_propagates(self, tmp_path):
        # Make the top-level chmod fail; recursive should not be tried
        runner = _FakeChmodRunner(
            returncode_for={("sudo", "/bin/chmod", "+a"): 1},
        )
        ok, err = obsidian_install.grant_vault_acl(
            str(tmp_path), "team_bot_a", "read", runner=runner,
        )
        assert ok is False
        assert "chmod +a on vault root failed" in err
        assert len(runner.calls) == 1  # bailed before the -R pass


class TestRevokeVaultAcl:
    """revoke_vault_acl removes both read AND read_write ACEs so a re-install
    with a different mode starts clean. ACL-not-found errors are treated as
    success (idempotent)."""

    def test_revoke_runs_chmod_a_for_both_modes(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = obsidian_install.revoke_vault_acl(
            str(tmp_path), "team_bot_a", runner=runner,
        )
        assert ok, err
        # 4 calls total: read mode (top + -R) + read_write mode (top + -R)
        assert len(runner.calls) == 4
        modes_seen = set()
        for argv in runner.calls:
            ace = argv[-2]
            if "write" in ace and "delete" in ace:
                modes_seen.add("read_write")
            else:
                modes_seen.add("read")
        assert modes_seen == {"read", "read_write"}

    def test_missing_vault_is_idempotent_success(self, tmp_path):
        runner = _FakeChmodRunner()
        missing = tmp_path / "does-not-exist"
        ok, err = obsidian_install.revoke_vault_acl(
            str(missing), "team_bot_a", runner=runner,
        )
        assert ok, err
        assert runner.calls == []  # nothing to do


# ── Mode marker roundtrip ─────────────────────────────────────────────────────


class TestModeMarkerRoundtrip:
    """write_mode_marker → read_mode_marker should round-trip the install
    metadata. The marker is the source of truth for "what mode am I in?"
    in the admin UI; mcp.servers.obsidian is the source of truth for
    "am I installed at all?"."""

    def test_read_returns_none_when_file_absent(self):
        with patch(
            "evolve_admin.skills.obsidian_install.mode_marker_path",
            return_value=type("P", (), {"exists": staticmethod(lambda: False)})(),
        ), patch(
            "evolve_admin.skills.obsidian_install.subprocess.run",
            return_value=type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
        ):
            assert obsidian_install.read_mode_marker("team_bot_a") is None


# ── Access panel mode variants ────────────────────────────────────────────────


class TestAccessPanelForMode:
    """access_panel_for(mode) → mode-specific will/wont lists. The neutral
    panel (used at the FIRST install step before the user picks a mode)
    exposes ``mode_choices`` so the UI can render the radio."""

    def test_neutral_panel_exposes_mode_choices(self):
        choices = obsidian_install.VAULT_ACCESS_PANEL["mode_choices"]
        assert isinstance(choices, list)
        ids = {c["value"] for c in choices}
        assert ids == {"read", "read_write"}
        # Read is the recommended/safe default — must come first so the UI
        # auto-selects it
        assert choices[0]["value"] == "read"

    def test_read_panel_wont_says_no_writes(self):
        panel = obsidian_install.access_panel_for("read")
        assert panel["mode"] == "read"
        wont = " ".join(panel["wont"]).lower()
        # Some phrasing of "won't write" must appear
        assert "create" in wont or "edit" in wont or "delete" in wont

    def test_read_write_panel_will_includes_create_and_edit(self):
        panel = obsidian_install.access_panel_for("read_write")
        assert panel["mode"] == "read_write"
        will = " ".join(panel["will"]).lower()
        assert "create" in will
        assert "edit" in will or "modif" in will

    def test_unknown_mode_falls_back_to_neutral(self):
        panel = obsidian_install.access_panel_for("not-a-mode")
        # No mode field set, but mode_choices still present (it's the
        # neutral panel)
        assert "mode_choices" in panel


# ── MCP-aware status resolver ─────────────────────────────────────────────────


class TestResolveStatusMcp:
    """resolve_status_mcp reads openclaw.json + the mode marker. Active when
    mcp.servers.obsidian is present in the bot's openclaw.json (the §2 OC
    loader signal — see audit-plugins-page-2026-05-29.md)."""

    def test_no_vault_configured_when_mcp_block_absent(self):
        def _read(_bot_id):
            return {"mcp": {"servers": {}}}, None

        status = obsidian_install.resolve_status_mcp(
            "team_bot_a",
            read_oc_config=_read,
            read_marker=lambda _b: None,
        )
        assert status.status == "no_vault_configured"

    def test_active_when_mcp_block_present(self):
        def _read(_bot_id):
            return {
                "mcp": {
                    "servers": {
                        "obsidian": {
                            "command": "/Users/Shared/evolve/mcp/launchers/team_bot_a/obsidian",
                            "args": ["/Users/team_bot_a/Documents/Vault"],
                        }
                    }
                }
            }, None

        status = obsidian_install.resolve_status_mcp(
            "team_bot_a",
            read_oc_config=_read,
            read_marker=lambda _b: {
                "vault_path": "/Users/team_bot_a/Documents/Vault",
                "mode": "read",
                "skill_id": obsidian_install.OBSIDIAN_SKILL_ID,
            },
        )
        assert status.status == "active"
        assert status.vault_path == "/Users/team_bot_a/Documents/Vault"
        # read mode → write_daily_note_enabled stays False
        assert status.write_daily_note_enabled is False
        assert status.error is None

    def test_active_in_read_write_mode_flips_write_flag(self):
        def _read(_bot_id):
            return {
                "mcp": {
                    "servers": {
                        "obsidian": {
                            "command": "/launcher",
                            "args": ["/v"],
                        }
                    }
                }
            }, None

        status = obsidian_install.resolve_status_mcp(
            "team_bot_a",
            read_oc_config=_read,
            read_marker=lambda _b: {
                "vault_path": "/v", "mode": "read_write",
            },
        )
        assert status.status == "active"
        assert status.write_daily_note_enabled is True

    def test_drift_detected_when_marker_path_differs_from_oc(self):
        def _read(_bot_id):
            return {
                "mcp": {
                    "servers": {
                        "obsidian": {"command": "/l", "args": ["/new/vault"]},
                    }
                }
            }, None

        status = obsidian_install.resolve_status_mcp(
            "team_bot_a",
            read_oc_config=_read,
            read_marker=lambda _b: {"vault_path": "/old/vault", "mode": "read"},
        )
        assert status.status == "active"
        # vault_path is the openclaw.json truth, not the stale marker
        assert status.vault_path == "/new/vault"
        # ...and the drift is surfaced in error so the UI can warn
        assert "mode_marker_drift" in (status.error or "")

    def test_unknown_when_oc_unreadable(self):
        def _read(_bot_id):
            return None, "permission_denied"

        status = obsidian_install.resolve_status_mcp(
            "team_bot_a",
            read_oc_config=_read,
            read_marker=lambda _b: None,
        )
        assert status.status == "unknown"
        assert "permission_denied" in (status.error or "")


# ── Route integration: /api/skills/install/obsidian/set-vault-path ────────────


@pytest.fixture
def obsidian_route_app(tmp_path):
    """Flask app + stubs for the obsidian install route. Mirrors the
    pattern in test_mcp_install_inline_token.py: real Flask app, stubbed
    _create_mcp_proposal so the test doesn't need the proposals pipeline."""
    from evolve_admin.web import server as srv

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(_json.dumps(network))
    app = srv.create_app(network_path=network_path)
    app.config["TESTING"] = True

    proposals_captured: list[dict] = []

    def _fake_create(action_kind, action_payload, bot_id, summary):
        proposals_captured.append({
            "kind": action_kind, "payload": action_payload,
            "bot_id": bot_id, "summary": summary,
        })
        return {
            "id": "fake-prop-id",
            "status": "applied",
            "kind": action_kind,
            "summary": summary,
            "payload": action_payload,
        }, None

    return app, proposals_captured, _fake_create


def _stub_create_apply(_fake_create):
    """Patch the underlying _operator_create_apply to use the fake. The
    obsidian route reaches _create_mcp_proposal which wraps that helper."""
    from evolve_admin.web import server as srv
    return patch.object(srv, "_operator_create_apply", lambda **kw: _fake_create(
        kw["action_kind"], kw["action_payload"], kw["bot_id"], kw["summary"]
    ))


class TestSetVaultPathRoute:
    """POST /api/skills/install/obsidian/set-vault-path — the wrapper that
    glues path validation + ACL grant + InstallMcpServer proposal."""

    def test_missing_bot_id_rejected(self, obsidian_route_app):
        app, _, fake = obsidian_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-vault-path",
                json={"vault_path": "/x", "mode": "read"},
            )
        assert r.status_code == 400
        assert "bot_id" in r.get_json()["error"]

    def test_missing_vault_path_rejected(self, obsidian_route_app):
        app, _, fake = obsidian_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-vault-path",
                json={"bot_id": "team_bot_a", "mode": "read"},
            )
        assert r.status_code == 400
        assert "vault_path" in r.get_json()["error"]

    def test_bad_mode_rejected(self, obsidian_route_app, tmp_path):
        app, _, fake = obsidian_route_app
        vault = tmp_path / "Vault"
        vault.mkdir()
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-vault-path",
                json={"bot_id": "team_bot_a", "vault_path": str(vault), "mode": "evil"},
            )
        assert r.status_code == 400
        assert "mode" in r.get_json()["error"]

    def test_reserved_path_rejected(self, obsidian_route_app):
        """Reserved-system-path validation must trigger before any ACL grant
        or proposal create. /etc, /tmp, ~/.ssh, etc. are blacklisted."""
        app, captured, fake = obsidian_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-vault-path",
                json={"bot_id": "team_bot_a", "vault_path": "/etc", "mode": "read"},
            )
        assert r.status_code == 400
        body = r.get_json()
        assert body["error"] == "vault_path_invalid"
        # No proposal should have been created — validation runs first
        assert captured == []

    def test_happy_path_creates_install_proposal_with_extra_args(
        self, obsidian_route_app, tmp_path,
    ):
        """Submitting a valid vault_path + mode results in an InstallMcpServer
        proposal with catalog_id=filesystem, server_id=obsidian, and
        extra_args=[vault_path]. This is the critical end-to-end shape."""
        app, captured, fake = obsidian_route_app
        # Use a vault path under Documents so validate_vault_path accepts it
        vault = tmp_path / "Documents" / "Vault"
        vault.mkdir(parents=True)

        # Patch the ACL helpers + mode-marker write so the test doesn't run
        # real chmod / sudo — we only care about the route plumbing here.
        with _stub_create_apply(fake), \
             patch("evolve_admin.skills.obsidian_install.revoke_vault_acl",
                   return_value=(True, None)) as mock_revoke, \
             patch("evolve_admin.skills.obsidian_install.grant_vault_acl",
                   return_value=(True, None)) as mock_grant, \
             patch("evolve_admin.skills.obsidian_install.write_mode_marker",
                   return_value=(True, None)) as mock_marker, \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.obsidian_install.validate_vault_path",
                   return_value=(True, None)), \
             patch("evolve_admin.skills.obsidian_install.resolve_status_mcp",
                   return_value=obsidian_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       vault_path=str(vault),
                   )):
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-vault-path",
                json={
                    "bot_id": "team_bot_a",
                    "vault_path": str(vault),
                    "mode": "read_write",
                },
            )

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "active"

        # ACL grant called with the right mode
        mock_revoke.assert_called_once()  # idempotency pass before grant
        mock_grant.assert_called_once()
        grant_args = mock_grant.call_args
        assert grant_args[0][0] == str(vault)
        assert grant_args[0][1] == "team_bot_a"
        assert grant_args[0][2] == "read_write"

        # Mode marker persisted
        mock_marker.assert_called_once_with(
            "team_bot_a", str(vault), "read_write",
        )

        # Proposal shape — this is the critical contract with the applier
        assert len(captured) == 1
        prop = captured[0]
        assert prop["kind"] == "InstallMcpServer"
        payload = prop["payload"]
        assert payload["bot_id"] == "team_bot_a"
        assert payload["server_id"] == "obsidian"
        assert payload["catalog_id"] == "filesystem"
        assert payload["env_bindings"] == {}
        # extra_args is THE point of this whole exercise
        assert payload["extra_args"] == [str(vault)]

    def test_acl_failure_does_not_create_proposal(
        self, obsidian_route_app, tmp_path,
    ):
        """If the ACL grant fails, the proposal must NOT be created —
        otherwise we'd ship an MCP install pointing at a vault the bot
        can't read. Better to surface the ACL error and have the user
        retry."""
        app, captured, fake = obsidian_route_app
        vault = tmp_path / "Documents" / "Vault"
        vault.mkdir(parents=True)

        with _stub_create_apply(fake), \
             patch("evolve_admin.skills.obsidian_install.revoke_vault_acl",
                   return_value=(True, None)), \
             patch("evolve_admin.skills.obsidian_install.grant_vault_acl",
                   return_value=(False, "chmod failed: permission denied")), \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.obsidian_install.validate_vault_path",
                   return_value=(True, None)):
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-vault-path",
                json={
                    "bot_id": "team_bot_a",
                    "vault_path": str(vault),
                    "mode": "read",
                },
            )

        assert r.status_code == 500
        assert "acl_grant_failed" in r.get_json()["error"]
        assert captured == []  # critical: no proposal got through


# ── Route integration: /api/skills/install/obsidian/set-mode ──────────────────


class TestSetModeRoute:
    """POST /api/skills/install/obsidian/set-mode — flips an already-installed
    vault between read and read+write by re-running the ACL grant and
    updating the mode marker. No new MCP proposal; no gateway kickstart."""

    def test_missing_bot_id_rejected(self, obsidian_route_app):
        app, _, _ = obsidian_route_app
        r = app.test_client().post(
            "/api/skills/install/obsidian/set-mode",
            json={"mode": "read"},
        )
        assert r.status_code == 400
        assert "bot_id" in r.get_json()["error"]

    def test_bad_mode_rejected(self, obsidian_route_app):
        app, _, _ = obsidian_route_app
        r = app.test_client().post(
            "/api/skills/install/obsidian/set-mode",
            json={"bot_id": "team_bot_a", "mode": "evil"},
        )
        assert r.status_code == 400
        assert "mode" in r.get_json()["error"]

    def test_not_installed_returns_404(self, obsidian_route_app):
        app, _, _ = obsidian_route_app
        with patch("evolve_admin.skills.obsidian_install.read_mode_marker",
                   return_value=None):
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read"},
            )
        assert r.status_code == 404
        assert r.get_json()["error"] == "skill_not_installed"

    def test_unchanged_mode_short_circuits(self, obsidian_route_app, tmp_path):
        app, _, _ = obsidian_route_app
        vault = tmp_path / "Vault"
        vault.mkdir()
        with patch("evolve_admin.skills.obsidian_install.read_mode_marker",
                   return_value={"vault_path": str(vault), "mode": "read"}), \
             patch("evolve_admin.skills.obsidian_install.resolve_status_mcp",
                   return_value=obsidian_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       vault_path=str(vault),
                   )), \
             patch("evolve_admin.skills.obsidian_install.grant_vault_acl") as mock_grant, \
             patch("evolve_admin.skills.obsidian_install.revoke_vault_acl") as mock_revoke, \
             patch("evolve_admin.skills.obsidian_install.write_mode_marker") as mock_marker:
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read"},
            )
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "unchanged"
        # No ACL or marker writes for a no-op
        mock_grant.assert_not_called()
        mock_revoke.assert_not_called()
        mock_marker.assert_not_called()

    def test_drift_between_marker_and_oc_returns_409(
        self, obsidian_route_app, tmp_path,
    ):
        app, _, _ = obsidian_route_app
        marker_vault = tmp_path / "OldVault"
        oc_vault = tmp_path / "NewVault"
        marker_vault.mkdir()
        oc_vault.mkdir()
        with patch("evolve_admin.skills.obsidian_install.read_mode_marker",
                   return_value={"vault_path": str(marker_vault), "mode": "read"}), \
             patch("evolve_admin.skills.obsidian_install.resolve_status_mcp",
                   return_value=obsidian_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       vault_path=str(oc_vault),
                   )), \
             patch("evolve_admin.skills.obsidian_install.grant_vault_acl") as mock_grant:
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read_write"},
            )
        assert r.status_code == 409
        body = r.get_json()
        assert body["error"] == "mode_marker_drift"
        mock_grant.assert_not_called()

    def test_happy_path_read_to_read_write(self, obsidian_route_app, tmp_path):
        app, _, _ = obsidian_route_app
        vault = tmp_path / "Vault"
        vault.mkdir()
        with patch("evolve_admin.skills.obsidian_install.read_mode_marker",
                   return_value={"vault_path": str(vault), "mode": "read"}), \
             patch("evolve_admin.skills.obsidian_install.resolve_status_mcp",
                   return_value=obsidian_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       vault_path=str(vault),
                       write_daily_note_enabled=True,
                   )), \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.obsidian_install.revoke_vault_acl",
                   return_value=(True, None)) as mock_revoke, \
             patch("evolve_admin.skills.obsidian_install.grant_vault_acl",
                   return_value=(True, None)) as mock_grant, \
             patch("evolve_admin.skills.obsidian_install.write_mode_marker",
                   return_value=(True, None)) as mock_marker:
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read_write"},
            )
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "active"

        mock_revoke.assert_called_once()
        # grant called with new mode
        mock_grant.assert_called_once()
        args = mock_grant.call_args[0]
        assert args[0] == str(vault)
        assert args[1] == "team_bot_a"
        assert args[2] == "read_write"
        # marker updated with new mode
        mock_marker.assert_called_once_with("team_bot_a", str(vault), "read_write")

    def test_happy_path_read_write_to_read(self, obsidian_route_app, tmp_path):
        app, _, _ = obsidian_route_app
        vault = tmp_path / "Vault"
        vault.mkdir()
        with patch("evolve_admin.skills.obsidian_install.read_mode_marker",
                   return_value={"vault_path": str(vault), "mode": "read_write"}), \
             patch("evolve_admin.skills.obsidian_install.resolve_status_mcp",
                   return_value=obsidian_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       vault_path=str(vault),
                   )), \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.obsidian_install.revoke_vault_acl",
                   return_value=(True, None)), \
             patch("evolve_admin.skills.obsidian_install.grant_vault_acl",
                   return_value=(True, None)) as mock_grant, \
             patch("evolve_admin.skills.obsidian_install.write_mode_marker",
                   return_value=(True, None)) as mock_marker:
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read"},
            )
        assert r.status_code == 200, r.get_json()
        assert mock_grant.call_args[0][2] == "read"
        mock_marker.assert_called_once_with("team_bot_a", str(vault), "read")

    def test_acl_grant_failure_rolls_back_to_previous_mode(
        self, obsidian_route_app, tmp_path,
    ):
        """If grant fails, re-grant the previous mode so the bot is not
        left without any ACE on the vault. The marker is NOT updated."""
        app, _, _ = obsidian_route_app
        vault = tmp_path / "Vault"
        vault.mkdir()

        grant_calls: list[str] = []

        def _fake_grant(vault_path, bot_user, mode):
            grant_calls.append(mode)
            if mode == "read_write":
                return False, "chmod failed: permission denied"
            return True, None

        with patch("evolve_admin.skills.obsidian_install.read_mode_marker",
                   return_value={"vault_path": str(vault), "mode": "read"}), \
             patch("evolve_admin.skills.obsidian_install.resolve_status_mcp",
                   return_value=obsidian_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       vault_path=str(vault),
                   )), \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.obsidian_install.revoke_vault_acl",
                   return_value=(True, None)), \
             patch("evolve_admin.skills.obsidian_install.grant_vault_acl",
                   side_effect=_fake_grant), \
             patch("evolve_admin.skills.obsidian_install.write_mode_marker") as mock_marker:
            r = app.test_client().post(
                "/api/skills/install/obsidian/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read_write"},
            )

        assert r.status_code == 500
        body = r.get_json()
        assert "acl_grant_failed" in body["error"]
        assert body["rolled_back_to"] == "read"
        # First grant attempted new mode, second re-granted previous
        assert grant_calls == ["read_write", "read"]
        # Marker MUST NOT be updated when the grant failed
        mock_marker.assert_not_called()
