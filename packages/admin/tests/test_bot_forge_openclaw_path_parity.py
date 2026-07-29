"""Forge dispatch argv ↔ sudoers grant: the openclaw path contract.

sudoers grants are exact-argv matches. The forge dispatcher runs
``/usr/bin/sudo -H -u <bot> <openclaw> agent …`` as the TTY-less ``evolve``
daemon, so the ``<openclaw>`` it invokes and the path in the platform's
rendered ``evolve ALL=(ALL) NOPASSWD: SETENV: <openclaw>`` grant must be the
SAME string — a divergence is "sudo: a password is required" and a dead
forge, not a test failure.

Live incident (Gate-2 gallery re-sweep, Linux VPS pod, 2026-07-11): 4/4
forge installs died at the build step because ``bot_forge.OPENCLAW_BIN`` was
the hardcoded macOS Homebrew path ``/opt/homebrew/bin/openclaw`` while the
Linux grant named ``/usr/bin/openclaw``. bot_forge was the consumer the
#3358 single-source-resolver consolidation missed.

These tests pin the contract per platform: both sides now flow through
``platform_profile.find_openclaw_cli()`` (dispatcher via
``deploy._openclaw_bin()``, grant via ``setup_wizard._find_openclaw_path()``),
so for any filesystem state the dispatch argv path equals the rendered grant
path. Each platform case is anchored to its sudoers golden fixture — the
grant line asserted here is byte-identical to the one in the golden.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import platform_profile as pp  # noqa: E402
from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import deploy, setup_wizard  # noqa: E402
from evolve_admin.applications import bot_forge  # noqa: E402

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "sudoers_golden"

# The discovered-openclaw inputs the sudoers goldens were captured with
# (see test_sudoers_platform_profile.py — MAC_OC_PATH / LINUX_OC_PATH).
_GOLDEN_CASES = [
    pytest.param(
        MACOS,
        "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw",
        "evolve_macos.sudoers",
        id="macos-golden",
    ),
    pytest.param(
        LINUX,
        "/usr/lib/node_modules/openclaw/bin/openclaw",
        "evolve_linux.sudoers",
        id="linux-golden",
    ),
]

_GRANT_RE = re.compile(
    r"^evolve ALL=\(ALL\) NOPASSWD: SETENV: (\S+openclaw)$", re.MULTILINE
)


def _patch_fs(monkeypatch, *, existing: set[str]) -> None:
    """Make the shared resolver see exactly ``existing`` on disk, nothing on
    PATH, and no stale cache in deploy's process-lifetime memo."""
    monkeypatch.setattr(pp.shutil, "which", lambda name: None)
    monkeypatch.setattr(deploy, "_OPENCLAW_BIN", None)

    real_exists = Path.exists
    candidates = set(pp.OPENCLAW_CLI_CANDIDATES)

    def fake_exists(self):
        s = str(self)
        if s in candidates:
            return s in existing
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)


def _dispatch_openclaw_argv() -> str:
    """The openclaw path the forge dispatcher would exec under sudo."""
    cmd = bot_forge._build_agent_cmd(
        bot_id="atlas", prompt="x", timeout_sec=60, model=None,
    )
    assert cmd[:4] == ["/usr/bin/sudo", "-H", "-u", "atlas"]
    return cmd[4]


def _rendered_grant_path() -> str:
    """The openclaw path in the SETENV grant of a freshly rendered sudoers
    (REAL ``_find_openclaw_path`` — this is deliberately not stubbed; the
    whole point is that both sides consult the same resolver)."""
    content = setup_wizard._render_evolve_sudoers()
    assert content is not None
    m = _GRANT_RE.search(content)
    assert m, "rendered sudoers has no openclaw SETENV grant"
    return m.group(1)


@pytest.mark.parametrize("profile,oc_path,golden_name", _GOLDEN_CASES)
def test_dispatch_argv_matches_rendered_grant_and_golden(
    monkeypatch, profile, oc_path, golden_name
) -> None:
    """Per platform: dispatcher argv == rendered SETENV grant == the grant
    line frozen in that platform's sudoers golden."""
    set_profile(profile)
    _patch_fs(monkeypatch, existing={oc_path})

    grant = _rendered_grant_path()
    assert grant == oc_path
    assert _dispatch_openclaw_argv() == grant

    golden = (GOLDEN_DIR / golden_name).read_text()
    assert f"evolve ALL=(ALL) NOPASSWD: SETENV: {grant}\n" in golden


def test_linux_vps_symlink_dispatch_matches_grant(monkeypatch) -> None:
    """The live VPS shape: only ``/usr/bin/openclaw`` exists. The old
    hardcoded ``/opt/homebrew/bin/openclaw`` argv could never match the
    grant here — this is the 4/4 forge-install failure, pinned."""
    set_profile(LINUX)
    _patch_fs(monkeypatch, existing={"/usr/bin/openclaw"})

    argv_path = _dispatch_openclaw_argv()
    assert argv_path == "/usr/bin/openclaw"
    assert argv_path == _rendered_grant_path()


def test_macos_homebrew_symlink_dispatch_unchanged(monkeypatch) -> None:
    """Normal mini install: the Homebrew symlink exists → the dispatch argv
    is the exact path the old constant hardcoded, so macOS behavior is
    byte-identical after the resolver routing."""
    set_profile(MACOS)
    _patch_fs(
        monkeypatch,
        existing={
            "/opt/homebrew/bin/openclaw",
            "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw",
        },
    )

    argv_path = _dispatch_openclaw_argv()
    assert argv_path == "/opt/homebrew/bin/openclaw"
    assert argv_path == _rendered_grant_path()
