"""Tests for the macOS Application Firewall allow-rule (firewall_allow.py).

WHAT THESE PIN — the 2026-09-04 phone test, in the three places the fix lives:

  * **Resolution reaches the binary the kernel actually runs.** The plist's
    program is a venv console script; the firewall judges the interpreter on
    its shebang, and that interpreter is a symlink into the Homebrew Cellar
    whose own chain ends at the framework ``Python.app/Contents/MacOS/Python``.
    Stopping anywhere short of the end of that chain allows the wrong path —
    which is indistinguishable from allowing nothing.
  * **"Blocked" and "not listed" are both drift, "off" and "Linux" are not.**
    A Homebrew upgrade moves the binary, so *absent* is the state the next
    re-block arrives in; an operator who never turned the firewall on needs
    no rule at all.
  * **The fix text names the whole repair.** The two commands AND the daemon
    restart — the firewall caches its verdict per process, so a rule added
    without a restart leaves the pod exactly as broken as before.

No live firewall is touched: ``socketfilterfw`` is replaced by a recorder, so
every test asserts on argv rather than on this machine's state.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import firewall_allow as fw  # noqa: E402

GLOBAL_ON = "Firewall is enabled. (State = 1)\n"
GLOBAL_OFF = "Firewall is disabled. (State = 0)\n"


def _listapps(*entries: "tuple[str, str]") -> str:
    """Render ``--listapps`` output for ``(path, "Allow"|"Block")`` pairs."""
    lines = [f"Total number of apps = {len(entries)} "]
    for i, (path, verdict) in enumerate(entries, start=1):
        lines.append(f"{i} : {path} ")
        lines.append(f"             ({verdict} incoming connections)")
    return "\n".join(lines) + "\n"


class _Firewall:
    """Recording stand-in for the ``socketfilterfw`` binary."""

    def __init__(self, *, glob: str = GLOBAL_ON, apps: str = "", rc: int = 0):
        self.glob = glob
        self.apps = apps
        self.rc = rc
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> "tuple[int, str]":
        self.calls.append(args)
        if self.rc:
            return self.rc, "boom"
        if args[:1] == ("--getglobalstate",):
            return 0, self.glob
        if args[:1] == ("--listapps",):
            return 0, self.apps
        return 0, ""

    @property
    def writes(self) -> "list[tuple[str, ...]]":
        return [c for c in self.calls if c[0] in ("--add", "--unblockapp")]


@pytest.fixture()
def macos(monkeypatch):
    """A macOS pod that really has an Application Firewall, on any host.

    Both halves are needed, and they are different facts: ``conftest.py``
    already pins the MACOS profile so macOS path shapes are deterministic on
    Linux CI, but the runner has no ``socketfilterfw`` on disk — so a test
    that only pinned the profile would exercise the absent-CLI branch instead
    of the one it means to.
    """
    import platform_profile

    monkeypatch.setattr(fw, "_get_profile", lambda: platform_profile.MACOS)
    monkeypatch.setattr(fw, "_cli_available", lambda: True)


@pytest.fixture()
def binary(monkeypatch, tmp_path) -> Path:
    """A fixed daemon interpreter, so tests never depend on the real venv."""
    b = tmp_path / "Python.app" / "Contents" / "MacOS" / "Python"
    b.parent.mkdir(parents=True)
    b.write_text("")
    monkeypatch.setattr(fw, "daemon_interpreter", lambda: b)
    return b


def _install(monkeypatch, firewall: _Firewall) -> _Firewall:
    monkeypatch.setattr(fw, "_run", firewall)
    return firewall


# ── resolving the binary the daemon actually runs ───────────────────────────


def test_resolution_follows_the_shebang_and_the_whole_symlink_chain(tmp_path):
    """venv console script → venv python3 → Cellar → framework Python.app.

    Each hop is one the reference pod really has, and the operator's own
    repair named the last of them. A resolver that stops at the venv would
    add an allow rule for a symlink the firewall does not judge.
    """
    framework = (tmp_path / "Cellar" / "python@3.14" / "3.14.0" / "Frameworks"
                 / "Python.framework" / "Versions" / "3.14" / "Resources"
                 / "Python.app" / "Contents" / "MacOS" / "Python")
    framework.parent.mkdir(parents=True)
    framework.write_text("")

    cellar_bin = tmp_path / "Cellar" / "python@3.14" / "3.14.0" / "bin"
    cellar_bin.mkdir(parents=True)
    (cellar_bin / "python3.14").symlink_to(framework)

    venv_bin = tmp_path / "evolve-venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").symlink_to(cellar_bin / "python3.14")

    console = venv_bin / "evolve-admin"
    console.write_text(f"#!{venv_bin / 'python3'}\nimport sys\n")

    assert fw.resolve_daemon_interpreter(console) == framework.resolve()


def test_resolution_of_a_plain_interpreter_needs_no_shebang(tmp_path):
    """The MCP-bridge shape (argv[0] IS the interpreter) resolves too."""
    real = tmp_path / "python3.14"
    real.write_bytes(b"\xcf\xfa\xed\xfe not text")
    link = tmp_path / "python3"
    link.symlink_to(real)
    assert fw.resolve_daemon_interpreter(link) == real.resolve()


def test_an_env_shebang_resolves_beside_the_script_or_not_at_all(tmp_path):
    """``#!/usr/bin/env python3`` is honoured only in the venv layout.

    Guessing at a ``PATH`` we are not the ones executing under would produce
    an allow rule for some other machine's interpreter, which is worse than
    reporting the script itself.
    """
    beside = tmp_path / "python3"
    beside.write_text("")
    script = tmp_path / "evolve-admin"
    script.write_text("#!/usr/bin/env python3\n")
    assert fw.resolve_daemon_interpreter(script) == beside.resolve()

    lonely_dir = tmp_path / "elsewhere"
    lonely_dir.mkdir()
    lonely = lonely_dir / "evolve-admin"
    lonely.write_text("#!/usr/bin/env python3\n")
    assert fw.resolve_daemon_interpreter(lonely) == lonely.resolve()


# ── reading the firewall ────────────────────────────────────────────────────


def test_app_state_reads_listapps_and_matches_through_symlinks(
    monkeypatch, tmp_path
):
    real = tmp_path / "Python"
    real.write_text("")
    link = tmp_path / "python3"
    link.symlink_to(real)
    _install(monkeypatch, _Firewall(apps=_listapps(
        ("/usr/bin/python3", "Allow"), (str(link), "Block"),
    )))
    assert fw.app_state(real) == "block"


def test_an_unlisted_app_is_none_not_allowed(monkeypatch, tmp_path):
    """The distinction ``--getappblocked`` cannot make.

    That subcommand answers "permitted" for a path the firewall has never
    heard of, so it reads a post-Homebrew-upgrade binary — the exact drift
    this module exists for — as fine.
    """
    _install(monkeypatch, _Firewall(apps=_listapps(("/usr/bin/python3", "Allow"))))
    assert fw.app_state(tmp_path / "Python") is None


@pytest.mark.parametrize("state,expected", [
    ("Firewall is enabled. (State = 1)", True),
    ("Firewall is enabled. (State = 2)", True),
    ("Firewall is disabled. (State = 0)", False),
])
def test_global_state_parsing(monkeypatch, state, expected):
    _install(monkeypatch, _Firewall(glob=state))
    assert fw.firewall_enabled() is expected


def test_an_unreadable_firewall_is_none_not_false(monkeypatch):
    _install(monkeypatch, _Firewall(rc=1))
    assert fw.firewall_enabled() is None


# ── the drift check ─────────────────────────────────────────────────────────


def test_a_blocked_interpreter_is_drift_naming_both_commands_and_the_restart(
    monkeypatch, macos, binary
):
    _install(monkeypatch, _Firewall(apps=_listapps((str(binary), "Block"))))
    (check,) = fw.check_daemon_firewall_allow()
    assert check.ok is False
    assert check.category == fw.PERM_CHECK_CATEGORY
    assert "blocked" in check.detail
    assert f"{fw.SOCKETFILTERFW} --add {binary}" in check.fix_description
    assert f"{fw.SOCKETFILTERFW} --unblockapp {binary}" in check.fix_description
    assert "restart the admin daemon" in check.fix_description
    assert fw.REPAIR_COMMAND in check.fix_description
    assert check.apply is not None


def test_an_interpreter_absent_from_the_list_is_the_same_drift(
    monkeypatch, macos, binary
):
    """A Homebrew Python upgrade re-blocks by MOVING the binary, so the
    post-upgrade state is 'not listed' — it must not read as healthy."""
    _install(monkeypatch, _Firewall(apps=_listapps(("/usr/bin/python3", "Allow"))))
    (check,) = fw.check_daemon_firewall_allow()
    assert check.ok is False
    assert "not in its app list" in check.detail


def test_an_allowed_interpreter_passes(monkeypatch, macos, binary):
    fwall = _install(monkeypatch, _Firewall(apps=_listapps((str(binary), "Allow"))))
    (check,) = fw.check_daemon_firewall_allow()
    assert check.ok is True
    assert fwall.writes == []


def test_a_disabled_firewall_is_an_informational_pass(monkeypatch, macos, binary):
    _install(monkeypatch, _Firewall(glob=GLOBAL_OFF))
    (check,) = fw.check_daemon_firewall_allow()
    assert check.ok is True
    assert "off" in check.detail
    assert check.apply is None


def test_an_unreadable_firewall_is_drift_with_no_automatic_repair(
    monkeypatch, macos, binary
):
    """"We could not check" must never render as "checked and fine" — and
    there is nothing to apply when we do not know what is wrong."""
    _install(monkeypatch, _Firewall(rc=1))
    (check,) = fw.check_daemon_firewall_allow()
    assert check.ok is False
    assert check.apply is None
    assert "cannot read" in check.detail


def test_linux_is_an_explicit_pass_naming_the_platform(monkeypatch):
    import platform_profile

    monkeypatch.setattr(fw, "_get_profile", lambda: platform_profile.LINUX)
    fwall = _install(monkeypatch, _Firewall())
    (check,) = fw.check_daemon_firewall_allow()
    assert check.ok is True
    assert "linux" in check.detail
    assert fwall.calls == []


# ── the repair ──────────────────────────────────────────────────────────────


def test_the_repair_runs_add_then_unblock_as_root(monkeypatch, macos, binary):
    fwall = _install(monkeypatch, _Firewall(apps=_listapps((str(binary), "Block"))))
    monkeypatch.setattr(fw, "_geteuid", lambda: 0)
    assert fw.allow_daemon_interpreter(binary) is True
    assert fwall.writes == [("--add", str(binary)), ("--unblockapp", str(binary))]


def test_a_non_root_pass_reports_instead_of_shelling_out(
    monkeypatch, macos, binary, caplog
):
    """The daemon (``evolve``) has no grant for socketfilterfw, and this
    module deliberately never asks for one — it names the operator command."""
    fwall = _install(monkeypatch, _Firewall())
    monkeypatch.setattr(fw, "_geteuid", lambda: 501)
    with caplog.at_level("WARNING"):
        assert fw.allow_daemon_interpreter(binary) is False
    assert fwall.writes == []
    assert fw.REPAIR_COMMAND in caplog.text


def test_the_check_apply_is_the_repair_the_fix_text_describes(
    monkeypatch, macos, binary
):
    fwall = _install(monkeypatch, _Firewall(apps=_listapps((str(binary), "Block"))))
    monkeypatch.setattr(fw, "_geteuid", lambda: 0)
    (check,) = fw.check_daemon_firewall_allow()
    assert check.apply() is True
    assert fwall.writes == [("--add", str(binary)), ("--unblockapp", str(binary))]


# ── the deploy step ─────────────────────────────────────────────────────────


def test_the_deploy_step_is_a_noop_when_already_allowed(monkeypatch, macos, binary):
    fwall = _install(monkeypatch, _Firewall(apps=_listapps((str(binary), "Allow"))))
    lines: list[str] = []
    assert fw.ensure_daemon_firewall_allow(lines.append) is True
    assert fwall.writes == []
    assert len(lines) == 1 and str(binary) in lines[0]


def test_the_deploy_step_adds_the_rule_when_blocked(monkeypatch, macos, binary):
    fwall = _install(monkeypatch, _Firewall(apps=_listapps((str(binary), "Block"))))
    monkeypatch.setattr(fw, "_geteuid", lambda: 0)
    lines: list[str] = []
    assert fw.ensure_daemon_firewall_allow(lines.append) is True
    assert fwall.writes == [("--add", str(binary)), ("--unblockapp", str(binary))]
    assert len(lines) == 1 and "allowed incoming connections" in lines[0]


def test_the_deploy_step_never_touches_the_global_state(monkeypatch, macos, binary):
    """An operator who turned the firewall on keeps it on — and a pod whose
    firewall is off gets no rule, not a switch flipped for it."""
    fwall = _install(monkeypatch, _Firewall(
        glob=GLOBAL_OFF, apps=_listapps((str(binary), "Block")),
    ))
    monkeypatch.setattr(fw, "_geteuid", lambda: 0)
    assert fw.ensure_daemon_firewall_allow(lambda _m: None) is True
    assert fwall.writes == []
    assert all(c[0] != "--setglobalstate" for c in fwall.calls)


def test_the_deploy_step_says_so_on_linux_and_runs_nothing(monkeypatch):
    import platform_profile

    monkeypatch.setattr(fw, "_get_profile", lambda: platform_profile.LINUX)
    fwall = _install(monkeypatch, _Firewall())
    lines: list[str] = []
    assert fw.ensure_daemon_firewall_allow(lines.append) is True
    assert fwall.calls == []
    assert len(lines) == 1 and "linux" in lines[0]


# ── a macOS profile on a host with no firewall ──────────────────────────────
#
# The admin suite pins the MACOS profile on Linux runners (conftest.py) so
# macOS path shapes are deterministic. That makes "the profile says macOS" and
# "this host has an Application Firewall" two different facts, and conflating
# them turned every CI run into a firewall drift report on a machine that has
# no firewall. A missing CLI is not a broken firewall.


def test_an_absent_firewall_cli_is_an_informational_pass(monkeypatch):
    import platform_profile

    monkeypatch.setattr(fw, "_get_profile", lambda: platform_profile.MACOS)
    monkeypatch.setattr(fw, "_cli_available", lambda: False)
    fwall = _install(monkeypatch, _Firewall())

    (check,) = fw.check_daemon_firewall_allow()
    assert check.ok is True
    assert fw.SOCKETFILTERFW in check.detail
    assert check.apply is None
    assert fwall.calls == []


def test_an_absent_cli_is_not_the_unreadable_state_drift(monkeypatch):
    """The two look alike from a failed subprocess and mean opposite things:
    "there is no filter here" versus "the filter is right there and would not
    answer". Only the second is drift."""
    import platform_profile

    monkeypatch.setattr(fw, "_get_profile", lambda: platform_profile.MACOS)
    monkeypatch.setattr(fw, "_cli_available", lambda: True)
    _install(monkeypatch, _Firewall(rc=1))
    (present_but_mute,) = fw.check_daemon_firewall_allow()
    assert present_but_mute.ok is False

    monkeypatch.setattr(fw, "_cli_available", lambda: False)
    (absent,) = fw.check_daemon_firewall_allow()
    assert absent.ok is True


def test_the_deploy_step_and_the_repair_both_stand_down_with_no_cli(monkeypatch):
    import platform_profile

    monkeypatch.setattr(fw, "_get_profile", lambda: platform_profile.MACOS)
    monkeypatch.setattr(fw, "_cli_available", lambda: False)
    monkeypatch.setattr(fw, "_geteuid", lambda: 0)
    fwall = _install(monkeypatch, _Firewall())

    lines: list[str] = []
    assert fw.ensure_daemon_firewall_allow(lines.append) is True
    assert fw.allow_daemon_interpreter(Path("/some/Python")) is False
    assert fwall.calls == []
    assert len(lines) == 1 and fw.SOCKETFILTERFW in lines[0]


def test_the_cli_gate_reads_the_real_path(monkeypatch, tmp_path):
    """``_cli_available`` must test the binary this module actually invokes —
    a gate pointed at some other path would pass on a host where the firewall
    call still cannot run."""
    monkeypatch.setattr(fw, "SOCKETFILTERFW", str(tmp_path / "nope"))
    assert fw._cli_available() is False
    present = tmp_path / "socketfilterfw"
    present.write_text("")
    monkeypatch.setattr(fw, "SOCKETFILTERFW", str(present))
    assert fw._cli_available() is True
