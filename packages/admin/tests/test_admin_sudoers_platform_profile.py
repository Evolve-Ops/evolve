"""Golden tests — `_render_admin_sudoers` on the platform command table (8.3 W5A).

The /etc/sudoers.d/evolve-admin writer was the second of the two deferred
W4b sudoers items: it hardcoded /bin/cat, /Users/<bot>, and launchctl. Now
it renders from ``platform_profile.get_profile()`` like
``_render_evolve_sudoers`` (design-linux-port §5 — one writer per file, two
command tables).

Same layering as test_sudoers_platform_profile.py:

1. **macOS byte-identity golden**: ``evolve_admin_macos.sudoers`` was
   captured from the writer BEFORE parameterization, so equality proves the
   refactor changed zero bytes of the production file. Exact-argv grants
   mean a drifted byte is a silently dead grant.
2. **Linux render snapshot + leakage**: ``evolve_admin_linux.sudoers`` pins
   the first blessed Linux rendering; no macOS-only marker may appear.
3. **visudo syntax check** for both renders, on whichever host runs this.
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import setup_wizard  # noqa: E402

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "sudoers_golden"

# The exact inputs the goldens were captured with.
ADMIN_USER = "pod-admin"
BOT_NAMES = ["team-bot-a", "team-bot-b", "evolve"]


class _Bot:
    def __init__(self, name: str) -> None:
        self.name = name


def _render() -> str:
    return setup_wizard._render_admin_sudoers(
        ADMIN_USER, [_Bot(n) for n in BOT_NAMES]
    )


def _assert_bytes_equal(rendered: str, golden_name: str) -> None:
    golden_path = GOLDEN_DIR / golden_name
    golden = golden_path.read_bytes()
    got = rendered.encode("utf-8")
    if got == golden:
        return
    diff = "".join(
        difflib.unified_diff(
            golden.decode("utf-8").splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"golden/{golden_name}",
            tofile="rendered",
            n=2,
        )
    )
    raise AssertionError(
        f"admin sudoers render drifted from {golden_name} — exact-match "
        f"grants mean every changed byte is a dead grant in production. If "
        f"the change is intentional, regenerate the fixture in the same "
        f"PR.\n{diff}"
    )


# ── 1. macOS byte-identity (the kill-risk gate) ───────────────────────────────


def test_macos_admin_render_byte_identical_to_pre_refactor() -> None:
    set_profile(MACOS)
    _assert_bytes_equal(_render(), "evolve_admin_macos.sudoers")


# ── 2. Linux render snapshot + leakage ────────────────────────────────────────


def test_linux_admin_render_matches_golden() -> None:
    set_profile(LINUX)
    _assert_bytes_equal(_render(), "evolve_admin_linux.sudoers")


_MACOS_ONLY_MARKERS = [
    "/Users/",
    "launchctl",
    "kickstart",  # launchctl-only verb; systemd's equivalent is restart
    "/opt/homebrew",
]


def test_linux_admin_render_has_no_macos_leakage() -> None:
    set_profile(LINUX)
    content = _render()
    leaks = {m: True for m in _MACOS_ONLY_MARKERS if m in content}
    assert not leaks, f"macOS-only markers leaked into the Linux render: {leaks}"


def test_linux_admin_render_grants_systemd_restart_per_bot_reads() -> None:
    set_profile(LINUX)
    content = _render()
    # Per-bot config reads come from the profile home root + cat.
    assert "pod-admin ALL=(team-bot-a) NOPASSWD: /bin/cat /home/team-bot-a/.openclaw/openclaw.json" in content
    # Gateway control is the systemctl verb family.
    assert "pod-admin ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ai.openclaw.*" in content


# ── 3. Both renders validate under visudo ─────────────────────────────────────


def _visudo() -> "str | None":
    found = shutil.which("visudo")
    if found:
        return found
    fallback = Path("/usr/sbin/visudo")
    return str(fallback) if fallback.exists() else None


@pytest.mark.parametrize(
    "profile", [pytest.param(MACOS, id="macos"), pytest.param(LINUX, id="linux")]
)
def test_admin_render_passes_visudo_syntax_check(tmp_path, profile) -> None:
    visudo = _visudo()
    if visudo is None:
        pytest.skip("no visudo on this host")
    set_profile(profile)
    f = tmp_path / "rendered.sudoers"
    f.write_text(_render())
    r = subprocess.run([visudo, "-c", "-f", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, f"visudo rejected the {profile.name} render: {r.stderr or r.stdout}"
