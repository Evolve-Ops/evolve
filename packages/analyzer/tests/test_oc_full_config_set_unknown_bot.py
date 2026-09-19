"""Tests for oc_cli.oc_full_config_set_with_error unknown-bot guard.

Regression for the 2026-06-03 ``bot=forge`` opaque sudo crash: a caller
PUT /api/admin/config/forge/cascade and the writer naively shelled out
to ``sudo -u forge python3 …`` because ``_resolve_user`` falls back to
``bot_id`` itself when the bot isn't in network.json. The HTTP layer
now rejects unknown bot_ids with 400; the writer is the second line of
defense for non-HTTP callers (CLI, arbiter, evo handlers).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_cli  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_bots_cache():
    """The bots dict is module-cached by network_path; tests use a
    fresh tmp_path each run, so reset between tests to avoid bleed."""
    oc_cli._bots_cache = None
    oc_cli._bots_cache_path = None
    yield
    oc_cli._bots_cache = None
    oc_cli._bots_cache_path = None


def _write_network(tmp_path: Path, bots: dict) -> str:
    network = {"bots": bots, "sharedDir": str(tmp_path / "shared")}
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    return str(p)


def test_unknown_bot_id_returns_structured_error(tmp_path, monkeypatch):
    """When bot_id isn't in network.json::bots, the writer must refuse
    BEFORE invoking the sudo subprocess. Returns (None, msg) where the
    msg names the unknown bot and lists known bots — operators reading
    admin-ui.err.log should see exactly what's wrong without grepping."""
    network_path = _write_network(tmp_path, {"admin_bot": {"role": "member"}})

    # If the guard were absent, the writer would shell out — fail loudly
    # on any subprocess.run call so a regression here is unmissable.
    def _explode(*a, **kw):
        raise AssertionError(
            f"subprocess.run should not run for unknown bot_id; args={a!r}"
        )

    monkeypatch.setattr(oc_cli.subprocess, "run", _explode)

    result, err = oc_cli.oc_full_config_set_with_error(
        "forge", {"cascade": {"enabled": True}}, network_path=network_path,
    )

    assert result is None
    assert err is not None
    assert "forge" in err
    assert "network.json::bots" in err
    assert "admin_bot" in err  # known bots listed in the message


def test_known_bot_id_passes_through_to_subprocess(tmp_path, monkeypatch):
    """Positive control: a registered bot_id reaches the subprocess
    path. We don't care what the subprocess does — only that the guard
    didn't reject prematurely."""
    network_path = _write_network(tmp_path, {"admin_bot": {"role": "member"}})

    monkeypatch.setattr(oc_cli, "_should_sudo", lambda u: False)
    monkeypatch.setattr(oc_cli, "_resolve_user", lambda b, n=None: "admin_user")

    called: dict = {}

    class _StubProc:
        returncode = 0
        stdout = json.dumps({"ok": True})
        stderr = ""

    def _fake_run(cmd, **kw):
        called["cmd"] = cmd
        return _StubProc()

    monkeypatch.setattr(oc_cli.subprocess, "run", _fake_run)

    result, err = oc_cli.oc_full_config_set_with_error(
        "admin_bot", {"cascade": {"enabled": True}}, network_path=network_path,
    )

    assert err is None
    assert result == {"ok": True}
    assert called.get("cmd") is not None
