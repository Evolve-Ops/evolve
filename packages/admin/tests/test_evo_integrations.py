"""tests/test_evo_integrations.py — `evo integrations` handler."""

from __future__ import annotations

import json
import sys
from collections import namedtuple
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for path in (str(_ADMIN_DIR),):
    if path not in sys.path:
        sys.path.insert(0, path)


_PwResult = namedtuple("_PwResult", ["pw_dir"])


def _setup_fake_home(tmp_path: Path, bot_id: str, openclaw: dict) -> Path:
    """Write a fake openclaw.json under {tmp_path}/{bot_id}/.openclaw/."""
    home = tmp_path / bot_id
    oc = home / ".openclaw"
    oc.mkdir(parents=True, exist_ok=True)
    (oc / "openclaw.json").write_text(json.dumps(openclaw))
    return home


@pytest.fixture
def fake_homes(tmp_path, monkeypatch):
    """Redirect pwd.getpwnam to tmp_path-based fake homes."""
    homes: dict[str, Path] = {}

    def fake_pwnam(user: str):
        if user in homes:
            return _PwResult(pw_dir=str(homes[user]))
        raise KeyError(user)

    from evolve_admin.evo.handlers import integrations
    monkeypatch.setattr(integrations.pwd, "getpwnam", fake_pwnam)
    return homes, tmp_path


def _call(fake_homes, bot_id: str, network: dict | None = None):
    _, tmp_path = fake_homes
    from evolve_admin.evo.handlers.integrations import render
    network = network or {"sharedDir": str(tmp_path),
                          "members": [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


def test_bot_shows_channels_and_plugins(fake_homes):
    homes, tmp_path = fake_homes
    homes["admin_bot"] = _setup_fake_home(tmp_path, "admin_bot", {
        "channels": {"telegram": {}, "slack": {}},
        "plugins": {"entries": [
            {"name": "brave"}, {"name": "telegram"},
            {"name": "anthropic"},  # NOT in integration list
            "github",  # bare string form
        ]},
        "mcp": {"servers": {"linear": {}, "notion": {}}},
    })
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "**Integrations — admin_bot**" in body
    assert "Channels: slack, telegram" in body
    assert "brave" in body and "github" in body and "telegram" in body
    assert "anthropic" not in body  # excluded
    assert "linear, notion" in body  # MCP servers


def test_bot_missing_openclaw_json(fake_homes):
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "Couldn't read this bot's OpenClaw config" in body


def test_bot_with_no_channels(fake_homes):
    homes, tmp_path = fake_homes
    homes["admin_bot"] = _setup_fake_home(tmp_path, "admin_bot", {
        "plugins": {"entries": [{"name": "brave"}]},
    })
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "Channels: (none)" in body
    assert "brave" in body


def test_pod_lists_per_bot_compact(fake_homes):
    homes, tmp_path = fake_homes
    homes["admin_bot"] = _setup_fake_home(tmp_path, "admin_bot", {
        "channels": {"telegram": {}},
        "plugins": {"entries": [{"name": "brave"}]},
    })
    homes["team_bot_a"] = _setup_fake_home(tmp_path, "team_bot_a", {
        "channels": {"slack": {}},
        "plugins": {"entries": [{"name": "github"}]},
    })
    network = {"sharedDir": str(tmp_path),
               "members": ["admin_bot", "team_bot_a", "evolve"]}
    r = _call(fake_homes, "evolve", network=network)
    body = r.direct_send_message or ""
    assert "**Integrations — pod**" in body
    assert "admin_bot:" in body and "team_bot_a:" in body
    assert "ch:telegram" in body
    assert "ix:brave" in body or "ix:github" in body


def test_dispatch_envelope(fake_homes):
    r = _call(fake_homes, "admin_bot")
    assert r.subcommand == "integrations"
    assert r.mode == "speak"
