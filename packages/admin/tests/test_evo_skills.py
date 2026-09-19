"""tests/test_evo_skills.py — `evo skills` handler.

Mocks the inventory module rather than wiring full openclaw.json
fixtures; the inventory module has its own tests.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for path in (str(_ADMIN_DIR),):
    if path not in sys.path:
        sys.path.insert(0, path)


@dataclass
class FakeSkill:
    id: str
    display: str
    category: str
    status: str = "configured"
    format_compliance: str = "proprietary"
    enabled: bool = True


@dataclass
class FakeInventory:
    bot_id: str
    skills: list = field(default_factory=list)
    read_error: str | None = None


@pytest.fixture
def patched(monkeypatch):
    inventories: dict[str, FakeInventory] = {}

    def fake_get(bot_id, bot_user, network=None):
        return inventories.get(bot_id, FakeInventory(bot_id=bot_id, read_error="missing"))

    # Patch inside the handler's late-import
    import evolve_admin.skills.inventory as inv_mod
    monkeypatch.setattr(inv_mod, "get_bot_skills", fake_get)
    return inventories


def _call(bot_id: str, members: list[str] | None = None):
    from evolve_admin.evo.handlers.skills import render
    network = {"sharedDir": "/tmp/x", "members": members or [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


def test_bot_with_no_skills(patched):
    patched["admin_bot"] = FakeInventory(bot_id="admin_bot", skills=[])
    r = _call("admin_bot")
    body = r.direct_send_message or ""
    assert "No skills installed" in body


def test_bot_with_read_error(patched):
    patched["admin_bot"] = FakeInventory(bot_id="admin_bot", read_error="bad json")
    r = _call("admin_bot")
    body = r.direct_send_message or ""
    assert "Read error: bad json" in body


def test_bot_lists_skills_grouped_by_category(patched):
    patched["admin_bot"] = FakeInventory(bot_id="admin_bot", skills=[
        FakeSkill(id="brave", display="Brave Search", category="search"),
        FakeSkill(id="google", display="Google Workspace", category="workspace"),
        FakeSkill(id="telegram", display="Telegram", category="messaging"),
    ])
    r = _call("admin_bot")
    body = r.direct_send_message or ""
    assert "(3 installed)" in body
    assert "messaging: Telegram" in body
    assert "search: Brave Search" in body
    assert "workspace: Google Workspace" in body


def test_pod_summary_per_bot_counts(patched):
    patched["admin_bot"] = FakeInventory(bot_id="admin_bot", skills=[
        FakeSkill("a", "A", "x"), FakeSkill("b", "B", "y"),
    ])
    patched["team_bot_a"] = FakeInventory(bot_id="team_bot_a", skills=[FakeSkill("c", "C", "z")])
    # team_bot_b missing → unreadable
    r = _call("evolve", members=["admin_bot", "team_bot_a", "team_bot_b", "evolve"])
    body = r.direct_send_message or ""
    assert "**Skills — pod** (3 total" in body
    assert body.index("admin_bot: 2") < body.index("team_bot_a: 1")
    assert "team_bot_b: (missing)" in body
