"""tests/test_connect_cli.py — `evolve-admin connect` laptop-side tunnel CLI.

The runtime side effects (launchctl, ssh) are mocked. We're checking that
the command wires options through correctly, branches on --status/--uninstall/
--once, and renders the right surface text.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


def _invoke(*args: str):
    from evolve_admin.cli import main
    runner = CliRunner()
    return runner.invoke(main, ["connect", *args], catch_exceptions=False)


def test_connect_status_branch_prints_panel():
    fake_status = {
        "installed": False,
        "loaded": False,
        "pid": None,
        "listening_on_local": False,
        "plist_path": "/tmp/fake.plist",
        "log_path": "/tmp/fake.log",
    }
    with patch("evolve_admin.tunnel.tunnel_status", return_value=fake_status):
        r = _invoke("--status")
    assert r.exit_code == 0
    assert "not installed" in r.output
    assert "/tmp/fake.plist" in r.output


def test_connect_uninstall_when_nothing_installed():
    with patch(
        "evolve_admin.tunnel.uninstall_persistent_tunnel",
        return_value={"removed": False, "plist_path": "/tmp/fake.plist"},
    ):
        r = _invoke("--uninstall")
    assert r.exit_code == 0
    assert "nothing to remove" in r.output


def test_connect_uninstall_after_removal_reports_path():
    with patch(
        "evolve_admin.tunnel.uninstall_persistent_tunnel",
        return_value={"removed": True, "plist_path": "/tmp/fake.plist"},
    ):
        r = _invoke("--uninstall")
    assert r.exit_code == 0
    assert "Removed" in r.output
    assert "/tmp/fake.plist" in r.output


def test_connect_install_default_path_calls_installer_and_open(tmp_path: Path):
    fake_info = {
        "plist_path": "/tmp/fake.plist",
        "log_path": "/tmp/fake.log",
        "used_autossh": True,
        "label": "com.evolve.tunnel",
        "remote_host": "mini",
        "remote_user": "pod_admin_user",
        "remote_port": 5050,
        "local_port": 5050,
        "ssh_key_present": True,
    }
    with patch("evolve_admin.tunnel.install_persistent_tunnel", return_value=fake_info) as inst, \
         patch("subprocess.run") as sp_run:
        r = _invoke("--host", "mini")
    assert r.exit_code == 0
    inst.assert_called_once()
    cfg = inst.call_args[0][0]
    assert cfg["remote_host"] == "mini"
    # autossh path is reported
    assert "autossh" in r.output
    # browser-open issued via `open` on darwin (sys.platform=='darwin' in test env)
    open_calls = [
        c for c in sp_run.call_args_list
        if c.args and c.args[0] and c.args[0][0] == "open"
    ]
    assert open_calls, "expected `open` to be invoked for browser auto-open"


def test_connect_install_no_open_flag_skips_browser():
    fake_info = {
        "plist_path": "/tmp/fake.plist",
        "log_path": "/tmp/fake.log",
        "used_autossh": False,
        "label": "com.evolve.tunnel",
        "remote_host": "mini",
        "remote_user": "pod_admin_user",
        "remote_port": 5050,
        "local_port": 5050,
        "ssh_key_present": True,
    }
    with patch("evolve_admin.tunnel.install_persistent_tunnel", return_value=fake_info), \
         patch("subprocess.run") as sp_run:
        r = _invoke("--host", "mini", "--no-open")
    assert r.exit_code == 0
    open_calls = [
        c for c in sp_run.call_args_list
        if c.args and c.args[0] and c.args[0][0] == "open"
    ]
    assert not open_calls


def test_connect_install_warns_when_ssh_key_missing():
    fake_info = {
        "plist_path": "/tmp/fake.plist",
        "log_path": "/tmp/fake.log",
        "used_autossh": True,
        "label": "com.evolve.tunnel",
        "remote_host": "mini",
        "remote_user": "pod_admin_user",
        "remote_port": 5050,
        "local_port": 5050,
        "ssh_key_present": False,
    }
    with patch("evolve_admin.tunnel.install_persistent_tunnel", return_value=fake_info), \
         patch("subprocess.run"):
        r = _invoke("--host", "mini", "--no-open")
    assert r.exit_code == 0
    assert "SSH key not found" in r.output


def test_connect_install_reports_friendly_error_when_host_missing():
    with patch(
        "evolve_admin.tunnel.install_persistent_tunnel",
        side_effect=RuntimeError("remote_host is required — pass --host or set adminBaseUrl"),
    ):
        r = _invoke()
    assert r.exit_code == 1
    assert "remote_host is required" in r.output


def test_connect_once_runs_one_shot_and_propagates_exit_code():
    with patch("evolve_admin.tunnel.run_one_shot_tunnel", return_value=42):
        r = _invoke("--host", "mini", "--once")
    # Click maps explicit sys.exit() to exit_code
    assert r.exit_code == 42


def test_oc_alias_still_dispatches_to_menu_commands():
    """`evolve-admin oc <subcommand>` keeps working as a hidden alias."""
    from evolve_admin.ocadmin import menu_group, oc_group
    # Same commands dict — adding/removing on one shows on the other.
    assert menu_group.commands is oc_group.commands
    # Both groups expose the same subcommand set.
    assert set(menu_group.commands.keys()) == set(oc_group.commands.keys())
    # The alias is hidden from --help so `menu` is the canonical name.
    assert oc_group.hidden is True
