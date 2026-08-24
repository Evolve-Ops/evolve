"""Regression cover: ``BotWorkspace.stat`` shells out a portable ``stat`` flag.

The original implementation hardcoded the BSD/macOS form
``stat -f "%z %m"``. On GNU/Linux ``-f`` means ``--file-system`` and the
format string becomes a bogus path operand, so the call ALWAYS failed and the
``except WorkspaceError: pass`` guard silently returned ``{}`` on every Linux
pod — the same class of bug PR #3259 fixed in ``analyzer/audit.py``
(``internal/diag-readable-findings-linux-2026-06-24.md``).

The fix keys the stat flag/format on the platform profile: GNU
``stat -c "%s %Y"`` on Linux, BSD ``stat -f "%z %m"`` on macOS. Both yield
``"size mtime_epoch"``. These tests assert the right flags are issued per
platform and that the Linux path returns a populated dict, not ``{}``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin.mcp_bridge import workspace as ws_mod  # noqa: E402


def _make_workspace(monkeypatch, captured: list) -> "ws_mod.BotWorkspace":
    """A BotWorkspace whose ``_sudo`` is replaced by a recorder returning a
    canned ``"size mtime"`` line, so no real subprocess (or sudo grant) runs."""
    monkeypatch.setattr(ws_mod, "get_bot_user", lambda bot_id, network: bot_id)
    monkeypatch.setattr(ws_mod, "load_network", lambda: {})
    w = ws_mod.BotWorkspace(
        "team-bot-a", Path("/home/team-bot-a/.openclaw/workspace/evolve")
    )

    def _fake_sudo(*args, input_text=None):
        captured.append(list(args))
        return "4096 1750000000\n"  # %z %m / %s %Y → size mtime_epoch

    monkeypatch.setattr(w, "_sudo", _fake_sudo)
    return w


def test_stat_uses_gnu_flag_on_linux(monkeypatch):
    """On Linux the stat shell-out uses GNU ``-c "%s %Y"`` and returns a
    populated dict — NOT the empty {} the BSD form silently produced."""
    set_profile(LINUX)
    try:
        captured: list = []
        w = _make_workspace(monkeypatch, captured)
        result = asyncio.run(w.stat("memory/MEMORY.md"))
    finally:
        set_profile(MACOS)  # restore conftest's pin for the rest of the suite

    assert captured, "stat never shelled out"
    argv = captured[0]
    assert argv[0] == "stat"
    assert "-c" in argv and "%s %Y" in argv, f"expected GNU flag, got {argv!r}"
    assert "-f" not in argv, f"BSD flag leaked onto Linux: {argv!r}"

    # The whole point of the bug: the dict must be populated, not {}.
    assert result.get("size_bytes") == 4096
    assert result.get("last_modified")  # an ISO timestamp string
    assert result["last_modified"].endswith("Z")


def test_stat_uses_bsd_flag_on_macos(monkeypatch):
    """macOS keeps the BSD ``-f "%z %m"`` form (byte-identical to pre-fix)."""
    set_profile(MACOS)
    captured: list = []
    w = _make_workspace(monkeypatch, captured)
    result = asyncio.run(w.stat("memory/MEMORY.md"))

    assert captured, "stat never shelled out"
    argv = captured[0]
    assert argv[0] == "stat"
    assert "-f" in argv and "%z %m" in argv, f"expected BSD flag, got {argv!r}"
    assert "-c" not in argv, f"GNU flag leaked onto macOS: {argv!r}"
    assert result.get("size_bytes") == 4096
    assert result.get("last_modified")
