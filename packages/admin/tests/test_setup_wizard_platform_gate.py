"""The wizard's platform gate (design-linux-port-2026-06-10.md §9).

Gate semantics under test (post-GA un-gate):

- **darwin proceeds as today** — no profile pin, no adapter swap, and an
  explicit ``--platform linux`` on a Mac refuses loudly (the host cannot be
  that platform).
- **linux is GA** — it auto-detects and proceeds with NO opt-in, pinning the
  LINUX profile and activating LinuxUserIsolation + SystemdScheduler. The
  explicit forms (``--platform linux`` / ``EVOLVE_PLATFORM=linux``) stay
  accepted as no-ops; ``--platform macos`` on a Linux host refuses.
- **anything else hard-fails.**

The gate is the wizard's ONE platform-detection site; ``host``/``env`` are
its test injection points, so nothing here touches the real ``sys.platform``
or process environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, get_profile, set_profile  # noqa: E402
from runtime.isolation import (  # noqa: E402
    LinuxUserIsolation,
    MacOSIsolation,
    get_isolation,
    set_isolation,
)
from runtime.scheduler import (  # noqa: E402
    LaunchdScheduler,
    SystemdScheduler,
    get_scheduler,
    set_scheduler,
)

from evolve_admin import setup_wizard  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_seams():
    """Activating Linux mutates process-wide seam singletons — restore the
    defaults around every test (the conftest re-pins the MACOS profile)."""
    yield
    set_isolation(None)
    set_scheduler(None)
    set_profile(MACOS)


def _gate(flag=None, *, host, env=None):
    return setup_wizard._resolve_platform_gate(flag, host=host, env=env or {})


# ── darwin: unchanged, and an explicit linux flag still refuses ───────────────


def test_darwin_proceeds_without_any_flag():
    assert _gate(None, host="darwin") == "macos"


@pytest.mark.parametrize("flag", ["macos", "darwin", "auto", ""])
def test_darwin_accepts_macos_spellings(flag):
    assert _gate(flag, host="darwin") == "macos"


def test_darwin_does_not_touch_profile_or_seams():
    set_isolation(None)
    set_scheduler(None)
    _gate(None, host="darwin")
    assert get_profile().name == "macos"
    assert isinstance(get_isolation(), MacOSIsolation)
    assert isinstance(get_scheduler(), LaunchdScheduler)


def test_darwin_refuses_platform_linux_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        _gate("linux", host="darwin")
    assert exc.value.code == 1
    assert "not valid on a macOS host" in capsys.readouterr().out


# ── linux: GA — auto-detects and proceeds with no opt-in ──────────────────────


def test_linux_proceeds_without_optin():
    """The GA un-gate: a bare invocation on a Linux host proceeds and pins the
    Linux profile + adapters — no EVOLVE_PLATFORM / --platform required."""
    assert _gate(None, host="linux") == "linux"
    assert get_profile() is LINUX
    assert isinstance(get_isolation(), LinuxUserIsolation)
    assert isinstance(get_scheduler(), SystemdScheduler)


def test_linux_proceeds_no_experimental_warning(capsys):
    """The old 'EXPERIMENTAL / pre-parity' banner is gone now that Linux is GA."""
    _gate(None, host="linux")
    out = capsys.readouterr().out
    assert "EXPERIMENTAL" not in out
    assert "experimental" not in out


@pytest.mark.parametrize("env_val", ["", "darwin", "macos", "yes", "linux"])
def test_linux_proceeds_regardless_of_env(env_val):
    """EVOLVE_PLATFORM no longer gates anything on a Linux host — accepted as a
    no-op whatever its value."""
    assert _gate(None, host="linux", env={"EVOLVE_PLATFORM": env_val}) == "linux"
    assert get_profile() is LINUX


def test_linux_explicit_flag_optin_still_works():
    assert _gate("linux", host="linux") == "linux"
    assert get_profile() is LINUX
    assert isinstance(get_isolation(), LinuxUserIsolation)
    assert isinstance(get_scheduler(), SystemdScheduler)


def test_linux2_host_string_is_recognized():
    """sys.platform can carry a version suffix on old kernels."""
    assert _gate(None, host="linux2") == "linux"


def test_linux_refuses_macos_flag():
    """An explicit ``--platform macos`` on a Linux host is a mistake — refuse."""
    with pytest.raises(SystemExit):
        _gate("macos", host="linux")


def test_linux_macos_flag_refusal_leaves_seams_untouched():
    set_isolation(None)
    set_scheduler(None)
    set_profile(MACOS)
    with pytest.raises(SystemExit):
        _gate("macos", host="linux")
    assert get_profile().name == "macos"
    assert isinstance(get_isolation(), MacOSIsolation)
    assert isinstance(get_scheduler(), LaunchdScheduler)


# ── everything else: hard-fail ────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["win32", "cygwin", "freebsd14", "sunos5"])
def test_other_platforms_hard_fail(host):
    with pytest.raises(SystemExit) as exc:
        _gate(None, host=host)
    assert exc.value.code == 1


def test_other_platforms_hard_fail_even_with_linux_flag():
    with pytest.raises(SystemExit):
        _gate("linux", host="win32")
    assert get_profile().name == "macos"  # nothing was activated
