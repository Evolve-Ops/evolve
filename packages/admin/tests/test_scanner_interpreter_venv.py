"""Regression guard: the application scanner runs under the VENV python, and
the §7a sudoers grant names that exact same interpreter.

Production incident 2026-06-11: every bot's app scan failed with
``ModuleNotFoundError: No module named 'evolve_admin'``. Root cause — the
scanner subprocess was launched with ``/usr/bin/python3`` (macOS system
Python 3.9.6), but ``evolve_admin``/``evolve_analyzer`` became real packages
(6.1, #2592) installed only in the deploy venv (3.14). System python can't
import them, so the scanner died at its first import on every bot.

The fix routes both the runtime invocation (web/server.py) and the §7a
sudoers grant (setup_wizard._render_evolve_sudoers) through the SAME profile
value — ``platform_profile.get_profile().venv_python``, surfaced via
``config.scanner_python()``. sudo matches the command argv as a literal
string, so the two MUST be byte-identical; this test pins that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import setup_wizard  # noqa: E402
from evolve_admin.config import scanner_python  # noqa: E402

# The system python that broke production — must NEVER be the scanner argv.
_SYSTEM_PYTHON = "/usr/bin/python3"


def _seventh_a_interpreter(content: str) -> str:
    """Extract the interpreter argv from the §7a application_scanner grant."""
    for line in content.splitlines():
        if line.startswith("evolve ALL=") and "application_scanner.py" in line:
            _, _, argv = line.partition("NOPASSWD:")
            tokens = argv.split()
            if tokens and tokens[0] == "SETENV:":
                tokens = tokens[1:]
            return tokens[0] if tokens else ""
    raise AssertionError("no §7a application_scanner grant line found")


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=lambda p: p.name)
def test_scanner_python_is_the_venv_not_system(profile) -> None:
    set_profile(profile)
    interp = scanner_python()
    # The exact regression: scanner must NOT run under system python.
    assert interp != _SYSTEM_PYTHON
    # It is the deploy venv python (has the packaged evolve_admin).
    assert interp == profile.venv_python
    assert interp.endswith("/bin/python3")
    assert "evolve-venv" in interp


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=lambda p: p.name)
def test_grant_interpreter_matches_runtime_interpreter(monkeypatch, profile) -> None:
    """The §7a grant argv and config.scanner_python() must be byte-identical —
    sudo matches the command as a literal string. A single-char drift makes
    sudo demand a password and every scan fails silently."""
    set_profile(profile)
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path", lambda: None)
    content = setup_wizard._render_evolve_sudoers()
    assert content is not None
    assert _seventh_a_interpreter(content) == scanner_python()


def test_grant_interpreter_is_not_system_python(monkeypatch) -> None:
    """Belt-and-suspenders on the macOS render: the live pod is macOS, and the
    system-python argv is exactly what broke it."""
    set_profile(MACOS)
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path", lambda: None)
    content = setup_wizard._render_evolve_sudoers()
    assert content is not None
    assert _seventh_a_interpreter(content) != _SYSTEM_PYTHON
