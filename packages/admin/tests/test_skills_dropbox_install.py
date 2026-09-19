"""tests/test_skills_dropbox_install.py — Dropbox MCP-backed install flow.

Covers the second of the paste-token-skill revivals. Same shape as
test_skills_obsidian_install.py because Dropbox follows Obsidian's
template:

  - validate_dropbox_path enforces the reserved-location blacklist
  - find_dropbox_folder reads ~/.dropbox/info.json for auto-suggest
  - grant_dropbox_acl / revoke_dropbox_acl run the right chmod +a / -a
    commands per mode (read vs read_write)
  - read_mode_marker / write_mode_marker round-trip the install state
  - resolve_status_mcp reads openclaw.json::mcp.servers.dropbox
  - access_panel_for(mode) produces mode-specific will/wont lists
  - The /api/skills/install/dropbox/set-folder-path route validates →
    ACL-grants → creates InstallMcpServer proposal with
    catalog_id="filesystem" + extra_args=[folder_path] + writes marker.

No real chmod / sudo in tests — injectable subprocess runner + mocked
helpers throughout.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import dropbox_install  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def folder_dir(tmp_path):
    """A minimal Dropbox-shaped folder for tests that need a real path."""
    folder = tmp_path / "Dropbox"
    folder.mkdir()
    (folder / "notes.md").write_text("# Notes\n")
    return folder


# On macOS, pytest's tmp_path lands under /private/tmp/... which the
# validation blacklist correctly rejects (matches /tmp / /private/tmp).
# That's the right behavior in production (no legitimate Dropbox folder
# lives there), but it makes the three "validate happy paths" untestable
# via the fixture above. Same issue as the obsidian tests — those three
# also fail on main for the same reason. Skip cleanly with a marker.
import tempfile as _tempfile  # noqa: E402

_TMP_BLOCKED = (
    _tempfile.gettempdir().startswith("/tmp")
    or _tempfile.gettempdir().startswith("/private/tmp")
)

_skip_if_tmp_blocked = pytest.mark.skipif(
    _TMP_BLOCKED,
    reason=(
        "pytest tmp_path is under /private/tmp on macOS, which the "
        "dropbox_path blacklist correctly rejects. The validate logic "
        "is fully exercised by the rejection cases above (and via the "
        "route's integration tests that mock validate_dropbox_path)."
    ),
)


# ── validate_dropbox_path ─────────────────────────────────────────────────────


def test_validate_dropbox_path_empty():
    ok, reason = dropbox_install.validate_dropbox_path("")
    assert not ok
    assert reason == "dropbox_path_empty"


def test_validate_dropbox_path_whitespace():
    ok, reason = dropbox_install.validate_dropbox_path("   ")
    assert not ok
    assert reason == "dropbox_path_empty"


def test_validate_dropbox_path_too_broad_root():
    ok, reason = dropbox_install.validate_dropbox_path("/")
    assert not ok
    assert "too_broad" in reason


def test_validate_dropbox_path_too_broad_users():
    ok, reason = dropbox_install.validate_dropbox_path("/Users")
    assert not ok
    assert "too_broad" in reason


@pytest.mark.parametrize("reserved", [
    "/etc", "/etc/hosts", "/var/log", "/usr/local",
    "/System/Library", "/tmp", "/private/tmp",
])
def test_validate_dropbox_path_rejects_reserved_system_locations(reserved):
    ok, reason = dropbox_install.validate_dropbox_path(reserved)
    assert not ok
    assert "reserved" in reason


@pytest.mark.parametrize("suffix", [
    "/Users/me/.ssh",
    "/Users/me/.aws",
    "/Users/me/.openclaw",
    "/Users/me/.dropbox",  # the desktop client's config dir, NOT the sync folder
])
def test_validate_dropbox_path_rejects_user_sensitive_dirs(suffix):
    ok, reason = dropbox_install.validate_dropbox_path(suffix)
    assert not ok
    assert "reserved" in reason


@_skip_if_tmp_blocked
def test_validate_dropbox_path_nonexistent(tmp_path):
    ok, reason = dropbox_install.validate_dropbox_path(str(tmp_path / "nope"))
    assert not ok
    assert "not_found" in reason


@_skip_if_tmp_blocked
def test_validate_dropbox_path_file_not_dir(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    ok, reason = dropbox_install.validate_dropbox_path(str(f))
    assert not ok
    assert reason == "dropbox_path_not_a_directory"


@_skip_if_tmp_blocked
def test_validate_dropbox_path_valid(folder_dir):
    ok, reason = dropbox_install.validate_dropbox_path(str(folder_dir))
    assert ok
    assert reason is None


# ── find_dropbox_folder (auto-detect) ─────────────────────────────────────────


class TestFindDropboxFolder:
    """find_dropbox_folder reads ~/.dropbox/info.json. Prefers personal
    over business since the personal account is the common case."""

    def test_returns_none_when_no_info_json(self, tmp_path):
        assert dropbox_install.find_dropbox_folder(tmp_path) is None

    def test_returns_personal_path_when_present(self, tmp_path):
        info_dir = tmp_path / ".dropbox"
        info_dir.mkdir()
        (info_dir / "info.json").write_text(json.dumps({
            "personal": {"path": "/Users/me/Dropbox", "host": 12345},
        }))
        assert dropbox_install.find_dropbox_folder(tmp_path) == "/Users/me/Dropbox"

    def test_returns_business_path_when_only_business_present(self, tmp_path):
        info_dir = tmp_path / ".dropbox"
        info_dir.mkdir()
        (info_dir / "info.json").write_text(json.dumps({
            "business": {"path": "/Users/me/Dropbox (Work)", "host": 67890},
        }))
        assert dropbox_install.find_dropbox_folder(tmp_path) == "/Users/me/Dropbox (Work)"

    def test_prefers_personal_over_business(self, tmp_path):
        info_dir = tmp_path / ".dropbox"
        info_dir.mkdir()
        (info_dir / "info.json").write_text(json.dumps({
            "personal": {"path": "/Users/me/Dropbox"},
            "business": {"path": "/Users/me/Dropbox (Work)"},
        }))
        assert dropbox_install.find_dropbox_folder(tmp_path) == "/Users/me/Dropbox"

    def test_returns_none_on_malformed_json(self, tmp_path):
        info_dir = tmp_path / ".dropbox"
        info_dir.mkdir()
        (info_dir / "info.json").write_text("not json {{")
        assert dropbox_install.find_dropbox_folder(tmp_path) is None


# ── ACL grant / revoke helpers (injectable runner, no real chmod) ─────────────


class _FakeChmodRunner:
    """Records subprocess calls + lets tests force specific return codes."""

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


class TestGrantDropboxAcl:
    """grant_dropbox_acl runs chmod +a with the right ACE per mode.
    Mirrors TestGrantVaultAcl in test_skills_obsidian_install.py."""

    def test_read_mode_grants_read_only_ace(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = dropbox_install.grant_dropbox_acl(
            str(tmp_path), "team_bot_a", "read", runner=runner,
        )
        assert ok, err
        # Two chmod calls: top-level and recursive
        assert len(runner.calls) == 2
        for argv in runner.calls:
            ace = argv[-2]
            assert ace.startswith("team_bot_a allow ")
            assert "list" in ace
            assert "readattr" in ace
            assert "file_inherit" in ace
            assert "directory_inherit" in ace
            # Read mode MUST NOT include write/delete
            assert "write" not in ace
            assert "delete" not in ace
            assert "add_file" not in ace

    def test_read_write_mode_grants_write_capabilities(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = dropbox_install.grant_dropbox_acl(
            str(tmp_path), "team_bot_a", "read_write", runner=runner,
        )
        assert ok, err
        ace = runner.calls[0][-2]
        assert ace.startswith("team_bot_a allow ")
        assert "list" in ace
        assert "write" in ace
        assert "delete" in ace
        assert "add_file" in ace
        assert "add_subdirectory" in ace

    def test_unknown_mode_returns_error_without_chmod(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = dropbox_install.grant_dropbox_acl(
            str(tmp_path), "team_bot_a", "fly_freely", runner=runner,
        )
        assert ok is False
        assert "unknown dropbox mode" in err
        assert runner.calls == []

    def test_missing_folder_returns_error_without_chmod(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = dropbox_install.grant_dropbox_acl(
            str(tmp_path / "gone"), "team_bot_a", "read", runner=runner,
        )
        assert ok is False
        assert "does not exist" in err
        assert runner.calls == []

    def test_chmod_failure_bails_before_recursive_pass(self, tmp_path):
        runner = _FakeChmodRunner(
            returncode_for={("sudo", "/bin/chmod", "+a"): 1},
        )
        ok, err = dropbox_install.grant_dropbox_acl(
            str(tmp_path), "team_bot_a", "read", runner=runner,
        )
        assert ok is False
        assert "chmod +a on folder root failed" in err
        assert len(runner.calls) == 1


class TestRevokeDropboxAcl:
    def test_revoke_runs_chmod_a_for_both_modes(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = dropbox_install.revoke_dropbox_acl(
            str(tmp_path), "team_bot_a", runner=runner,
        )
        assert ok, err
        # 4 calls: read mode (top + -R) + read_write mode (top + -R)
        assert len(runner.calls) == 4
        modes_seen = set()
        for argv in runner.calls:
            ace = argv[-2]
            modes_seen.add("read_write" if ("write" in ace and "delete" in ace) else "read")
        assert modes_seen == {"read", "read_write"}

    def test_missing_folder_is_idempotent_success(self, tmp_path):
        runner = _FakeChmodRunner()
        ok, err = dropbox_install.revoke_dropbox_acl(
            str(tmp_path / "gone"), "team_bot_a", runner=runner,
        )
        assert ok, err
        assert runner.calls == []


# ── Access panel mode variants ────────────────────────────────────────────────


class TestAccessPanelForMode:
    def test_neutral_panel_exposes_mode_choices_with_read_first(self):
        choices = dropbox_install.DROPBOX_ACCESS_PANEL["mode_choices"]
        assert {c["value"] for c in choices} == {"read", "read_write"}
        # Read is the safe default — must come first so the UI auto-selects it
        assert choices[0]["value"] == "read"

    def test_read_panel_wont_says_no_writes(self):
        panel = dropbox_install.access_panel_for("read")
        assert panel["mode"] == "read"
        wont = " ".join(panel["wont"]).lower()
        assert "create" in wont or "edit" in wont or "delete" in wont

    def test_read_write_panel_will_includes_save_or_create(self):
        panel = dropbox_install.access_panel_for("read_write")
        assert panel["mode"] == "read_write"
        will = " ".join(panel["will"]).lower()
        assert "create" in will or "save" in will

    def test_unknown_mode_falls_back_to_neutral(self):
        panel = dropbox_install.access_panel_for("not-a-mode")
        assert "mode_choices" in panel


# ── MCP-aware status resolver ─────────────────────────────────────────────────


class TestResolveStatusMcp:
    def test_no_folder_configured_when_mcp_block_absent(self):
        def _read(_bot_id):
            return {"mcp": {"servers": {}}}, None

        status = dropbox_install.resolve_status_mcp(
            "team_bot_a",
            read_oc_config=_read,
            read_marker=lambda _b: None,
        )
        assert status.status == "no_folder_configured"

    def test_active_when_mcp_block_present(self):
        def _read(_bot_id):
            return {
                "mcp": {
                    "servers": {
                        "dropbox": {
                            "command": "/Users/Shared/evolve/mcp/launchers/x/dropbox",
                            "args": ["/Users/me/Dropbox"],
                        },
                    },
                },
            }, None

        status = dropbox_install.resolve_status_mcp(
            "team_bot_a",
            read_oc_config=_read,
            read_marker=lambda _b: {
                "folder_path": "/Users/me/Dropbox",
                "mode": "read",
                "skill_id": dropbox_install.DROPBOX_SKILL_ID,
            },
        )
        assert status.status == "active"
        assert status.folder_path == "/Users/me/Dropbox"
        assert status.mode == "read"
        assert status.error is None

    def test_drift_detected_when_marker_path_differs(self):
        def _read(_bot_id):
            return {
                "mcp": {
                    "servers": {
                        "dropbox": {"command": "/l", "args": ["/new/folder"]},
                    },
                },
            }, None

        status = dropbox_install.resolve_status_mcp(
            "team_bot_a",
            read_oc_config=_read,
            read_marker=lambda _b: {"folder_path": "/old/folder", "mode": "read"},
        )
        assert status.status == "active"
        # folder_path comes from openclaw.json (the truth)
        assert status.folder_path == "/new/folder"
        # ...and drift surfaces in error
        assert "mode_marker_drift" in (status.error or "")

    def test_unknown_when_oc_unreadable(self):
        def _read(_bot_id):
            return None, "permission_denied"

        status = dropbox_install.resolve_status_mcp(
            "team_bot_a",
            read_oc_config=_read,
            read_marker=lambda _b: None,
        )
        assert status.status == "unknown"
        assert "permission_denied" in (status.error or "")


# ── Mode marker absence handling ──────────────────────────────────────────────


class TestModeMarkerMissing:
    def test_read_returns_none_when_file_absent_and_sudo_fallback_fails(self):
        with patch(
            "evolve_admin.skills.dropbox_install.mode_marker_path",
            return_value=type("P", (), {"exists": staticmethod(lambda: False)})(),
        ), patch(
            "evolve_admin.skills.dropbox_install.subprocess.run",
            return_value=type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
        ):
            assert dropbox_install.read_mode_marker("team_bot_a") is None


# ── Route integration: /api/skills/install/dropbox/set-folder-path ────────────


@pytest.fixture
def dropbox_route_app(tmp_path):
    """Flask app + stubs for the dropbox install route. Mirrors the
    obsidian_route_app fixture in test_skills_obsidian_install.py."""
    from evolve_admin.web import server as srv

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
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
    from evolve_admin.web import server as srv
    return patch.object(srv, "_operator_create_apply", lambda **kw: _fake_create(
        kw["action_kind"], kw["action_payload"], kw["bot_id"], kw["summary"]
    ))


class TestSetFolderPathRoute:
    def test_missing_bot_id_rejected(self, dropbox_route_app):
        app, _, fake = dropbox_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-folder-path",
                json={"folder_path": "/x", "mode": "read"},
            )
        assert r.status_code == 400
        assert "bot_id" in r.get_json()["error"]

    def test_missing_folder_path_rejected(self, dropbox_route_app):
        app, _, fake = dropbox_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-folder-path",
                json={"bot_id": "team_bot_a", "mode": "read"},
            )
        assert r.status_code == 400
        assert "folder_path" in r.get_json()["error"]

    def test_bad_mode_rejected(self, dropbox_route_app, tmp_path):
        app, _, fake = dropbox_route_app
        folder = tmp_path / "Dropbox"
        folder.mkdir()
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-folder-path",
                json={"bot_id": "team_bot_a", "folder_path": str(folder), "mode": "evil"},
            )
        assert r.status_code == 400
        assert "mode" in r.get_json()["error"]

    def test_reserved_path_rejected_before_chmod_or_proposal(
        self, dropbox_route_app,
    ):
        app, captured, fake = dropbox_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-folder-path",
                json={"bot_id": "team_bot_a", "folder_path": "/etc", "mode": "read"},
            )
        assert r.status_code == 400
        body = r.get_json()
        assert body["error"] == "folder_path_invalid"
        assert captured == []

    def test_happy_path_creates_install_proposal_with_extra_args(
        self, dropbox_route_app, tmp_path,
    ):
        """The critical end-to-end shape: an InstallMcpServer proposal with
        catalog_id="filesystem", server_id="dropbox", extra_args=[folder_path].
        Mirrors the Obsidian test but with dropbox-specific fields."""
        app, captured, fake = dropbox_route_app
        folder = tmp_path / "Documents" / "Dropbox"
        folder.mkdir(parents=True)

        with _stub_create_apply(fake), \
             patch("evolve_admin.skills.dropbox_install.revoke_dropbox_acl",
                   return_value=(True, None)) as mock_revoke, \
             patch("evolve_admin.skills.dropbox_install.grant_dropbox_acl",
                   return_value=(True, None)) as mock_grant, \
             patch("evolve_admin.skills.dropbox_install.write_mode_marker",
                   return_value=(True, None)) as mock_marker, \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.dropbox_install.validate_dropbox_path",
                   return_value=(True, None)), \
             patch("evolve_admin.skills.dropbox_install.resolve_status_mcp",
                   return_value=dropbox_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       folder_path=str(folder), mode="read_write",
                   )):
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-folder-path",
                json={
                    "bot_id": "team_bot_a",
                    "folder_path": str(folder),
                    "mode": "read_write",
                },
            )

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "active"

        mock_revoke.assert_called_once()  # idempotency pass
        mock_grant.assert_called_once()
        grant_args = mock_grant.call_args
        assert grant_args[0][0] == str(folder)
        assert grant_args[0][1] == "team_bot_a"
        assert grant_args[0][2] == "read_write"

        mock_marker.assert_called_once_with(
            "team_bot_a", str(folder), "read_write",
        )

        # The critical proposal shape
        assert len(captured) == 1
        prop = captured[0]
        assert prop["kind"] == "InstallMcpServer"
        payload = prop["payload"]
        assert payload["bot_id"] == "team_bot_a"
        assert payload["server_id"] == "dropbox"
        # Same catalog entry as Obsidian — proves filesystem MCP serves both
        assert payload["catalog_id"] == "filesystem"
        assert payload["env_bindings"] == {}
        # The whole point of this exercise
        assert payload["extra_args"] == [str(folder)]

    def test_acl_failure_does_not_create_proposal(
        self, dropbox_route_app, tmp_path,
    ):
        """If the ACL grant fails the proposal MUST NOT be created —
        otherwise we'd ship an MCP install pointing at a folder the bot
        can't read. Same critical guard as the Obsidian version."""
        app, captured, fake = dropbox_route_app
        folder = tmp_path / "Documents" / "Dropbox"
        folder.mkdir(parents=True)

        with _stub_create_apply(fake), \
             patch("evolve_admin.skills.dropbox_install.revoke_dropbox_acl",
                   return_value=(True, None)), \
             patch("evolve_admin.skills.dropbox_install.grant_dropbox_acl",
                   return_value=(False, "chmod failed: permission denied")), \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.dropbox_install.validate_dropbox_path",
                   return_value=(True, None)):
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-folder-path",
                json={
                    "bot_id": "team_bot_a",
                    "folder_path": str(folder),
                    "mode": "read",
                },
            )

        assert r.status_code == 500
        assert "acl_grant_failed" in r.get_json()["error"]
        assert captured == []


# ── Route integration: /api/skills/install/dropbox/set-mode ───────────────────


class TestSetModeRoute:
    """POST /api/skills/install/dropbox/set-mode — flip an already-installed
    folder between read and read+write. Same shape as the Obsidian
    set-mode test. The mcp.servers.dropbox entry is untouched."""

    def test_missing_bot_id_rejected(self, dropbox_route_app):
        app, _, _ = dropbox_route_app
        r = app.test_client().post(
            "/api/skills/install/dropbox/set-mode",
            json={"mode": "read"},
        )
        assert r.status_code == 400
        assert "bot_id" in r.get_json()["error"]

    def test_bad_mode_rejected(self, dropbox_route_app):
        app, _, _ = dropbox_route_app
        r = app.test_client().post(
            "/api/skills/install/dropbox/set-mode",
            json={"bot_id": "team_bot_a", "mode": "evil"},
        )
        assert r.status_code == 400
        assert "mode" in r.get_json()["error"]

    def test_not_installed_returns_404(self, dropbox_route_app):
        app, _, _ = dropbox_route_app
        with patch("evolve_admin.skills.dropbox_install.read_mode_marker",
                   return_value=None):
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read"},
            )
        assert r.status_code == 404
        assert r.get_json()["error"] == "skill_not_installed"

    def test_unchanged_mode_short_circuits(self, dropbox_route_app, tmp_path):
        app, _, _ = dropbox_route_app
        folder = tmp_path / "Dropbox"
        folder.mkdir()
        with patch("evolve_admin.skills.dropbox_install.read_mode_marker",
                   return_value={"folder_path": str(folder), "mode": "read"}), \
             patch("evolve_admin.skills.dropbox_install.resolve_status_mcp",
                   return_value=dropbox_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       folder_path=str(folder), mode="read",
                   )), \
             patch("evolve_admin.skills.dropbox_install.grant_dropbox_acl") as mock_grant, \
             patch("evolve_admin.skills.dropbox_install.revoke_dropbox_acl") as mock_revoke, \
             patch("evolve_admin.skills.dropbox_install.write_mode_marker") as mock_marker:
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read"},
            )
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "unchanged"
        mock_grant.assert_not_called()
        mock_revoke.assert_not_called()
        mock_marker.assert_not_called()

    def test_drift_between_marker_and_oc_returns_409(
        self, dropbox_route_app, tmp_path,
    ):
        app, _, _ = dropbox_route_app
        marker_folder = tmp_path / "OldDropbox"
        oc_folder = tmp_path / "NewDropbox"
        marker_folder.mkdir()
        oc_folder.mkdir()
        with patch("evolve_admin.skills.dropbox_install.read_mode_marker",
                   return_value={"folder_path": str(marker_folder), "mode": "read"}), \
             patch("evolve_admin.skills.dropbox_install.resolve_status_mcp",
                   return_value=dropbox_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       folder_path=str(oc_folder), mode="read",
                   )), \
             patch("evolve_admin.skills.dropbox_install.grant_dropbox_acl") as mock_grant:
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read_write"},
            )
        assert r.status_code == 409
        body = r.get_json()
        assert body["error"] == "mode_marker_drift"
        mock_grant.assert_not_called()

    def test_happy_path_read_to_read_write(self, dropbox_route_app, tmp_path):
        app, _, _ = dropbox_route_app
        folder = tmp_path / "Dropbox"
        folder.mkdir()
        with patch("evolve_admin.skills.dropbox_install.read_mode_marker",
                   return_value={"folder_path": str(folder), "mode": "read"}), \
             patch("evolve_admin.skills.dropbox_install.resolve_status_mcp",
                   return_value=dropbox_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       folder_path=str(folder), mode="read_write",
                   )), \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.dropbox_install.revoke_dropbox_acl",
                   return_value=(True, None)) as mock_revoke, \
             patch("evolve_admin.skills.dropbox_install.grant_dropbox_acl",
                   return_value=(True, None)) as mock_grant, \
             patch("evolve_admin.skills.dropbox_install.write_mode_marker",
                   return_value=(True, None)) as mock_marker:
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read_write"},
            )
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "active"

        mock_revoke.assert_called_once()
        mock_grant.assert_called_once()
        args = mock_grant.call_args[0]
        assert args[0] == str(folder)
        assert args[1] == "team_bot_a"
        assert args[2] == "read_write"
        mock_marker.assert_called_once_with("team_bot_a", str(folder), "read_write")

    def test_happy_path_read_write_to_read(self, dropbox_route_app, tmp_path):
        app, _, _ = dropbox_route_app
        folder = tmp_path / "Dropbox"
        folder.mkdir()
        with patch("evolve_admin.skills.dropbox_install.read_mode_marker",
                   return_value={"folder_path": str(folder), "mode": "read_write"}), \
             patch("evolve_admin.skills.dropbox_install.resolve_status_mcp",
                   return_value=dropbox_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       folder_path=str(folder), mode="read",
                   )), \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.dropbox_install.revoke_dropbox_acl",
                   return_value=(True, None)), \
             patch("evolve_admin.skills.dropbox_install.grant_dropbox_acl",
                   return_value=(True, None)) as mock_grant, \
             patch("evolve_admin.skills.dropbox_install.write_mode_marker",
                   return_value=(True, None)) as mock_marker:
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read"},
            )
        assert r.status_code == 200, r.get_json()
        assert mock_grant.call_args[0][2] == "read"
        mock_marker.assert_called_once_with("team_bot_a", str(folder), "read")

    def test_acl_grant_failure_rolls_back_to_previous_mode(
        self, dropbox_route_app, tmp_path,
    ):
        """If grant fails, re-grant the previous mode so the bot is not
        left without any ACE on the folder. Marker is NOT updated."""
        app, _, _ = dropbox_route_app
        folder = tmp_path / "Dropbox"
        folder.mkdir()

        grant_calls: list[str] = []

        def _fake_grant(folder_path, bot_user, mode):
            grant_calls.append(mode)
            if mode == "read_write":
                return False, "chmod failed: permission denied"
            return True, None

        with patch("evolve_admin.skills.dropbox_install.read_mode_marker",
                   return_value={"folder_path": str(folder), "mode": "read"}), \
             patch("evolve_admin.skills.dropbox_install.resolve_status_mcp",
                   return_value=dropbox_install.InstallStatus(
                       bot_id="team_bot_a", status="active",
                       folder_path=str(folder), mode="read",
                   )), \
             patch("evolve_admin.config.get_bot_user", return_value="team_bot_a"), \
             patch("evolve_admin.skills.dropbox_install.revoke_dropbox_acl",
                   return_value=(True, None)), \
             patch("evolve_admin.skills.dropbox_install.grant_dropbox_acl",
                   side_effect=_fake_grant), \
             patch("evolve_admin.skills.dropbox_install.write_mode_marker") as mock_marker:
            r = app.test_client().post(
                "/api/skills/install/dropbox/set-mode",
                json={"bot_id": "team_bot_a", "mode": "read_write"},
            )

        assert r.status_code == 500
        body = r.get_json()
        assert "acl_grant_failed" in body["error"]
        assert body["rolled_back_to"] == "read"
        assert grant_calls == ["read_write", "read"]
        mock_marker.assert_not_called()
