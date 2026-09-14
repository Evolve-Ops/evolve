"""Wizard prereq + OpenClaw-install steps under both platform profiles (8.3 W5A).

The prereq probe and the npm installer are profile-keyed (never
``sys.platform``): Linux rows are os-release / Node-24-via-NodeSource /
ACL tools / systemd / unit-dir; macOS rows are unchanged. Every test runs
with an injected runner and a booby-trapped ``subprocess.run`` — the wizard
steps must spawn ZERO real subprocesses here.

Linux tests pin LINUX inside the test body (the admin conftest re-pins
MACOS around each test); macOS tests pin MACOS explicitly anyway so each
test is self-evident.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import setup_wizard  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """Booby-trap: any un-faked subprocess in these steps is a test failure."""
    def _bomb(*args, **kwargs):
        raise AssertionError(
            f"real subprocess.run escaped the seam: {args[0] if args else kwargs}"
        )
    monkeypatch.setattr(setup_wizard.subprocess, "run", _bomb)
    yield


def _fake_which(table):
    def which(cmd, path=None):
        return table.get(cmd)
    return which


def _recording_runner(node_version="v24.2.0"):
    calls = []

    def runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        stdout = f"{node_version}\n" if argv[1:] == ["--version"] else ""
        return subprocess.CompletedProcess(list(argv), 0, stdout, "")

    return runner, calls


def _row(rows, name):
    matches = [r for r in rows if r.name == name]
    assert matches, f"no prereq row named {name!r} in {[r.name for r in rows]}"
    return matches[0]


# ── booby-trap proof ──────────────────────────────────────────────────────────


def test_trap_fires_without_injected_runner(monkeypatch):
    """Prove the trap is armed: the node probe without a runner explodes."""
    set_profile(LINUX)
    monkeypatch.setattr(
        setup_wizard.shutil, "which", _fake_which({"node": "/usr/bin/node"})
    )
    with pytest.raises(AssertionError, match="real subprocess"):
        setup_wizard._check_prerequisites()


# ── Linux prereq rows ─────────────────────────────────────────────────────────


def test_linux_prereq_rows(monkeypatch):
    set_profile(LINUX)
    monkeypatch.setattr(setup_wizard.shutil, "which", _fake_which({
        "node": "/usr/bin/node",
        "npm": "/usr/bin/npm",
    }))
    runner, calls = _recording_runner("v24.2.0")

    rows = setup_wizard._check_prerequisites(runner=runner)
    names = [r.name for r in rows]

    # Node 24 detected via the injected runner — the ONLY subprocess.
    node_row = _row(rows, "Node.js v24.2.0")
    assert node_row.ok
    assert [c[0] for c in calls] == [["/usr/bin/node", "--version"]]

    assert _row(rows, "npm").ok

    # NodeSource-era Linux rows.
    acl = _row(rows, "POSIX ACL tools (setfacl/getfacl)")
    assert not acl.hard  # detect-and-instruct, like Homebrew on macOS
    if not acl.ok:
        assert "apt-get install -y acl" in acl.detail
    systemd = _row(rows, "systemd (systemctl)")
    assert systemd.hard
    assert any(n == "/etc/systemd/system writable" for n in names)

    # OpenClaw missing → soft, npm install offered later.
    oc = _row(rows, "OpenClaw CLI")
    assert not oc.ok and "will install via npm" in oc.detail

    # Venv follows the Linux shared-dir sibling convention.
    venv = _row(rows, "Evolve venv")
    assert not venv.ok
    assert "/var/lib/evolve-venv/bin/python3" in venv.detail

    # No macOS rows leak in.
    assert not any("Homebrew" in n for n in names)
    assert not any("/Library/LaunchDaemons" in n for n in names)
    assert not any(n.startswith("macOS") for n in names)
    assert not any("Apple Silicon" in n for n in names)


def test_linux_node_below_24_instructs_nodesource(monkeypatch):
    set_profile(LINUX)
    monkeypatch.setattr(
        setup_wizard.shutil, "which", _fake_which({"node": "/usr/bin/node"})
    )
    runner, _ = _recording_runner("v20.11.0")  # fine on macOS, too old on Linux
    rows = setup_wizard._check_prerequisites(runner=runner)
    node_row = _row(rows, "Node.js v20.11.0")
    assert not node_row.ok
    assert "NodeSource" in node_row.detail
    assert "setup_24.x" in node_row.detail


def test_linux_node_missing_instructs_nodesource(monkeypatch):
    set_profile(LINUX)
    monkeypatch.setattr(setup_wizard.shutil, "which", _fake_which({}))
    runner, calls = _recording_runner()
    rows = setup_wizard._check_prerequisites(runner=runner)
    node_row = _row(rows, "Node.js not found")
    assert not node_row.ok
    assert "NodeSource" in node_row.detail
    assert calls == []  # nothing to probe — zero subprocess


# ── macOS prereq rows unchanged ───────────────────────────────────────────────


def test_macos_prereq_rows_unchanged(monkeypatch):
    set_profile(MACOS)
    monkeypatch.setattr(setup_wizard.shutil, "which", _fake_which({
        "node": "/opt/homebrew/bin/node",
        "npm": "/opt/homebrew/bin/npm",
        "brew": "/opt/homebrew/bin/brew",
    }))
    runner, _ = _recording_runner("v20.11.0")
    rows = setup_wizard._check_prerequisites(runner=runner)
    names = [r.name for r in rows]

    node_row = _row(rows, "Node.js v20.11.0")
    assert node_row.ok  # 20 is the macOS floor
    assert _row(rows, "Homebrew").ok
    assert any(n == "/Library/LaunchDaemons writable" for n in names)
    # Linux rows must not appear on macOS.
    assert not any("setfacl" in n for n in names)
    assert not any("systemd" in n for n in names)
    assert not any("/etc/systemd" in n for n in names)


def test_macos_node_missing_hint_is_brew(monkeypatch):
    set_profile(MACOS)
    monkeypatch.setattr(setup_wizard.shutil, "which", _fake_which({}))
    runner, _ = _recording_runner()
    rows = setup_wizard._check_prerequisites(runner=runner)
    node_row = _row(rows, "Node.js not found")
    assert "brew install node" in node_row.detail
    assert "NodeSource" not in node_row.detail


# ── os-release parsing ────────────────────────────────────────────────────────


def _write_os_release(tmp_path, body):
    p = tmp_path / "os-release"
    p.write_text(body)
    return p


def test_os_release_ubuntu_2404_ok(tmp_path):
    p = _write_os_release(
        tmp_path, 'PRETTY_NAME="Ubuntu 24.04.2 LTS"\nID=ubuntu\nVERSION_ID="24.04"\n'
    )
    row = setup_wizard._linux_release_prereq(p)
    assert row.ok and row.name == "Ubuntu 24.04.2 LTS"


def test_os_release_older_ubuntu_warns(tmp_path):
    p = _write_os_release(
        tmp_path, 'PRETTY_NAME="Ubuntu 22.04.5 LTS"\nID=ubuntu\nVERSION_ID="22.04"\n'
    )
    row = setup_wizard._linux_release_prereq(p)
    assert not row.ok and "24.04" in row.detail
    assert not row.hard  # never aborts — the gate already opted in


def test_os_release_debian_warns(tmp_path):
    p = _write_os_release(
        tmp_path, 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\n'
    )
    row = setup_wizard._linux_release_prereq(p)
    assert not row.ok and not row.hard


def test_os_release_missing_file(tmp_path):
    row = setup_wizard._linux_release_prereq(tmp_path / "missing")
    assert not row.ok and row.name == "Linux (unknown distribution)"


# ── OpenClaw npm install: --prefix is macOS-only ──────────────────────────────


def test_linux_npm_install_has_no_homebrew_prefix(monkeypatch):
    set_profile(LINUX)
    monkeypatch.setattr(setup_wizard.shutil, "which", _fake_which({
        "npm": "/usr/bin/npm",
        "openclaw": "/usr/bin/openclaw",  # post-install verify
    }))
    runner, calls = _recording_runner()

    assert setup_wizard._install_openclaw_npm(runner=runner) is True
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/npm", "install", "-g", "openclaw"]
    assert not any(a.startswith("--prefix") for a in argv)
    assert kwargs["env"]["PATH"].startswith("/usr/local/bin:/usr/bin:/bin:")
    assert "/opt/homebrew" not in kwargs["env"]["PATH"].split(":" )[0]


def test_macos_npm_install_keeps_brew_prefix(monkeypatch):
    set_profile(MACOS)
    from evolve_admin import host_power
    monkeypatch.setattr(host_power, "homebrew_prefix", lambda: "/opt/homebrew")
    monkeypatch.setattr(setup_wizard.shutil, "which", _fake_which({
        "npm": "/opt/homebrew/bin/npm",
        "openclaw": "/opt/homebrew/bin/openclaw",
    }))
    runner, calls = _recording_runner()

    assert setup_wizard._install_openclaw_npm(runner=runner) is True
    argv, kwargs = calls[0]
    assert argv == [
        "/opt/homebrew/bin/npm", "install", "-g", "--prefix=/opt/homebrew", "openclaw",
    ]
    assert kwargs["env"]["PATH"].startswith("/opt/homebrew/bin:")


def test_npm_missing_fails_with_platform_hint(monkeypatch, capsys):
    set_profile(LINUX)
    monkeypatch.setattr(setup_wizard.shutil, "which", _fake_which({}))
    runner, calls = _recording_runner()
    assert setup_wizard._install_openclaw_npm(runner=runner) is False
    assert calls == []
    assert "NodeSource" in capsys.readouterr().out
