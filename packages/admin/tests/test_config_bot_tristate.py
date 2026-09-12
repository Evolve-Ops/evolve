"""config.bot must surface a permission wall as 'indeterminate', not as
'not found'. Regression for the Evo confabulation incident: a 0700-shielded
openclaw.json that can't be read must NOT be reported as absent.
"""
from __future__ import annotations

import errno
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.evo.tools import config_bot  # noqa: E402
from evolve_admin.evo.tools.fs_tristate import ReadState  # noqa: E402


@pytest.fixture
def _daemon_down(monkeypatch):
    """Force the admin-daemon path to fall through to the direct-fs read."""
    from evolve_admin.evo import admin_client

    def raise_unavailable(*a, **kw):
        raise admin_client.AdminDaemonUnavailable("socket down")

    monkeypatch.setattr(admin_client, "get_json", raise_unavailable)


def _point_at(monkeypatch, oc_json_path: Path):
    """Make _read_openclaw_json resolve to our temp openclaw.json."""
    import evolve_admin.config as cfg

    monkeypatch.setattr(cfg, "load_network", lambda p: {"bots": {}})
    monkeypatch.setattr(cfg, "bot_home", lambda bot_id, net=None: oc_json_path.parent.parent)


def test_config_bot_genuinely_absent(tmp_path, monkeypatch, _daemon_down):
    home = tmp_path / "Users" / "newbie"
    (home / ".openclaw").mkdir(parents=True)
    oc = home / ".openclaw" / "openclaw.json"  # NOT created
    _point_at(monkeypatch, oc)

    out = config_bot._handler(tmp_path / "network.json", bot_id="newbie")
    assert out["available"] is False
    assert out["read_state"] == ReadState.ABSENT.value
    assert "absent" in out["error"].lower()


def test_config_bot_permission_wall_is_indeterminate(tmp_path, monkeypatch, _daemon_down):
    """The incident shape: read denied (EACCES) + sudo grant missing →
    indeterminate. The handler must NOT say 'not found'."""
    home = tmp_path / "Users" / "atlas"
    (home / ".openclaw").mkdir(parents=True)
    oc = home / ".openclaw" / "openclaw.json"
    oc.write_text('{"gateway": {"port": 1}}')  # file EXISTS
    _point_at(monkeypatch, oc)

    def fake_read(*a, **kw):
        raise PermissionError(errno.EACCES, "Permission denied")

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "sudo: a password is required"

    with patch.object(Path, "read_text", fake_read), patch(
        "evolve_admin.evo.tools.fs_tristate.subprocess.run", return_value=FakeProc()
    ):
        out = config_bot._handler(tmp_path / "network.json", bot_id="atlas")

    assert out["available"] is False
    assert out["read_state"] == ReadState.INDETERMINATE.value
    # Must not falsely claim absence; must flag the uncertainty explicitly.
    err = out["error"].lower()
    assert "not found" not in err
    assert "permission" in err
    assert "not confirmed absent" in err or "do not conclude" in err


def test_config_bot_present_but_malformed_is_not_permission_error(
    tmp_path, monkeypatch, _daemon_down
):
    """A readable-but-unparseable config is reported as present-and-malformed,
    NOT as a permission wall — the headline must not imply 'read denied'."""
    home = tmp_path / "Users" / "broken"
    (home / ".openclaw").mkdir(parents=True)
    oc = home / ".openclaw" / "openclaw.json"
    oc.write_text("{not valid json")
    _point_at(monkeypatch, oc)

    out = config_bot._handler(tmp_path / "network.json", bot_id="broken")
    assert out["available"] is False
    assert out["read_state"] == ReadState.EXISTS.value
    assert "could not be parsed" in out["error"].lower()
    assert "permission" not in out["error"].lower()


def test_config_bot_happy_path(tmp_path, monkeypatch, _daemon_down):
    home = tmp_path / "Users" / "ok"
    (home / ".openclaw").mkdir(parents=True)
    oc = home / ".openclaw" / "openclaw.json"
    oc.write_text('{"gateway": {"port": 8080, "bind": "127.0.0.1"}}')
    _point_at(monkeypatch, oc)

    out = config_bot._handler(tmp_path / "network.json", bot_id="ok")
    assert out["available"] is True
    assert out["read_state"] == ReadState.EXISTS.value
    assert out["config"]["gateway"]["port"] == 8080
