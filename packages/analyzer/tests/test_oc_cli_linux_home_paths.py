"""tests/test_oc_cli_linux_home_paths.py — oc_cli's bot-home cwd is platform-aware.

[META:platform] regression for the VPS pod failure:

    ✗ Could not restart 'ai.openclaw.evo-gateway'. Plist not found in standard
      locations. Tried: scheduler unavailable: ... requires the LaunchdScheduler
      adapter ...; openclaw gateway restart: [Errno 2] No such file or
      directory: '/Users/evo'

Two hardcoded-macOS-path bugs stacked. This file covers the second: every
``openclaw`` subprocess oc_cli spawns runs with ``cwd=<bot home>``, and the
gateway-restart fallback built that literally as ``/Users/{user}``. On a Linux
pod the bot's home is ``/home/{user}``, so ``subprocess.run`` raised
FileNotFoundError on the cwd BEFORE the command ran — the error names a path
that has nothing to do with what failed.

The cwd is load-bearing, not cosmetic: ``openclaw`` is a Node binary and Node
calls ``uv_cwd()`` during startup, so a missing or untraversable cwd kills the
CLI before it emits any output (the same shape as the ``sudo -H -u <bot>
openclaw`` over-SSH gotcha in CLAUDE.md). Hence the ``/tmp`` floor — every
account can traverse it.

Same family as the admin-daemon-socket (#3160), primary-gateway-label, and
usage-analytics turn-reader bugs: macOS-first code on a Linux pod.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import oc_cli  # noqa: E402
from platform_profile import LINUX, MACOS, set_profile  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_profile():
    yield
    set_profile(None)


def _no_passwd_entry(monkeypatch):
    """Force the profile-keyed branch: no passwd entry for this account."""
    import pwd as _pwd

    def _boom(_user):
        raise KeyError("no such user")

    monkeypatch.setattr(_pwd, "getpwnam", _boom)


class _AlwaysDir:
    """Minimal Path stand-in whose is_dir() is always True — lets the
    profile-keyed branch be asserted on a macOS CI box, where /home/<bot> does
    not exist and the /tmp floor would otherwise mask which branch ran.
    Deliberately not a Path subclass (those need a private ``_flavour`` before
    3.12)."""

    def __init__(self, p) -> None:
        self._p = str(p)

    def __truediv__(self, other) -> "_AlwaysDir":
        return _AlwaysDir(f"{self._p.rstrip('/')}/{other}")

    def __str__(self) -> str:
        return self._p

    def is_dir(self) -> bool:
        return True


@pytest.mark.parametrize(
    "profile, expected_root",
    [(LINUX, "/home"), (MACOS, "/Users")],
)
def test_home_falls_back_to_the_profile_home_root(
    monkeypatch, profile, expected_root
):
    """With no passwd entry, the fallback follows the PROFILE's home root.
    On Linux that is /home — the macOS ``/Users`` literal is what produced
    ``[Errno 2] No such file or directory: '/Users/evo'`` on the VPS pod."""
    _no_passwd_entry(monkeypatch)
    set_profile(profile)
    monkeypatch.setattr(oc_cli, "Path", _AlwaysDir)

    assert oc_cli._resolve_home_dir("zzzbot") == f"{expected_root}/zzzbot"


def test_home_uses_passwd_entry_on_any_platform(monkeypatch, tmp_path):
    """pwd wins over the profile default — a Linux bot home is /home/<bot>,
    and the resolver must report exactly what the OS says."""
    import pwd as _pwd
    from types import SimpleNamespace

    real_home = tmp_path / "home" / "evo"
    real_home.mkdir(parents=True)
    monkeypatch.setattr(
        _pwd, "getpwnam", lambda u: SimpleNamespace(pw_dir=str(real_home))
    )
    set_profile(LINUX)
    assert oc_cli._resolve_home_dir("evo") == str(real_home)


def test_home_floors_at_tmp_when_unresolvable(monkeypatch):
    """A home dir that does not exist must NOT be handed to subprocess.run —
    Node's uv_cwd() would die on it. Floor at /tmp, which anyone can traverse.
    This is the exact ``[Errno 2] ... '/Users/evo'`` failure, inverted."""
    _no_passwd_entry(monkeypatch)
    set_profile(MACOS)
    assert oc_cli._resolve_home_dir("zzz-nonexistent-bot") == "/tmp"


def test_gateway_restart_fallback_cwd_goes_through_the_seam(monkeypatch, tmp_path):
    """The openclaw-CLI fallback inside oc_gateway_restart must use the
    resolver, not a ``/Users/{user}`` f-string — that literal is what raised
    ``[Errno 2] ... '/Users/evo'`` on the VPS pod, from the cwd rather than
    from anything the command did.

    Reached the way a Linux pod would: a unit that IS installed but whose
    restart fails, so the fallback is the genuine last resort.
    """
    from runtime.scheduler import SystemdScheduler, set_scheduler

    net = tmp_path / "network.json"
    net.write_text('{"bots": {"zzzbot": {"user": "zzzuser"}}}')

    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    (unit_dir / "ai.openclaw.zzzbot-gateway.service").write_text("[Unit]\n")

    resolved = tmp_path / "home" / "zzzuser"
    resolved.mkdir(parents=True)
    monkeypatch.setattr(oc_cli, "_resolve_home_dir", lambda u: str(resolved))
    monkeypatch.setattr(oc_cli, "_find_openclaw", lambda: "/usr/bin/openclaw")

    def _reply(argv):
        if "show" in argv:
            return 0, "LoadState=loaded\nActiveState=failed\nMainPID=0\n", ""
        return 1, "", "Job for ... failed"

    set_scheduler(SystemdScheduler(unit_dir=unit_dir, use_sudo=False, runner=_reply))
    set_profile(LINUX)

    seen: dict = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["cwd"] = kwargs.get("cwd")
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(oc_cli.subprocess, "run", _fake_run)

    try:
        res = oc_cli.oc_gateway_restart("zzzbot", network_path=str(net))
        assert res["ok"] is True
        assert res["method"] == "openclaw-gateway-restart"
        assert seen["cwd"] == str(resolved)
        assert "/Users/zzzuser" not in " ".join(str(x) for x in seen["argv"])
    finally:
        set_scheduler(None)
