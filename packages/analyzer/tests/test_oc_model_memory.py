"""tests/test_oc_model_memory.py — oc_model.py memorySearch read/write.

Locks the JSON contract for the new ``memory`` subcommand and the in-process
helpers that read/write ``agents.defaults.memorySearch`` in a bot's
openclaw.json. The CLI surface is invoked via ``sudo -u <bot>`` from the
admin server (oc_cli.oc_memory_get/oc_memory_set) so any drift in field
names or shape would silently break the per-bot writer.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402
from oc_model import (  # noqa: E402
    get_memory_search_config,
    json_memory,
    json_memory_set,
    set_memory_search_config,
)


class _OkValidate:
    """Pretend ``openclaw config validate`` succeeded (binary may not exist in CI)."""
    returncode = 0
    stdout = '{"valid": true, "issues": []}'
    stderr = ""


@pytest.fixture(autouse=True)
def _stub_openclaw_validate(monkeypatch):
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "openclaw" and cmd[1] == "config":
            return _OkValidate()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(oc_model.subprocess, "run", fake_run)


def _seed_oc_json(tmp_path: Path, contents: dict) -> Path:
    p = tmp_path / "openclaw.json"
    p.write_text(json.dumps(contents, indent=2))
    return p


# ── Accessor helpers ────────────────────────────────────────────────────────


def test_get_returns_empty_for_missing_block():
    assert get_memory_search_config({}) == {}


def test_get_returns_block_when_present():
    data = {"agents": {"defaults": {"memorySearch": {"provider": "gemini", "fallback": "openai"}}}}
    assert get_memory_search_config(data) == {"provider": "gemini", "fallback": "openai"}


def test_set_creates_nested_path():
    data: dict = {}
    set_memory_search_config(data, {"provider": "openai"})
    assert data == {"agents": {"defaults": {"memorySearch": {"provider": "openai"}}}}


def test_set_with_empty_dict_removes_block():
    data = {"agents": {"defaults": {"memorySearch": {"provider": "openai"}}}}
    set_memory_search_config(data, {})
    assert "memorySearch" not in data["agents"]["defaults"]


def test_set_preserves_sibling_fields():
    """memorySearch write must not stomp agents.defaults.model or other siblings."""
    data = {
        "agents": {
            "defaults": {
                "model": {"primary": "anthropic/claude-sonnet-4-6", "fallbacks": []},
                "workspace": "/Users/x/.openclaw/workspace",
            }
        }
    }
    set_memory_search_config(data, {"provider": "openai"})
    defaults = data["agents"]["defaults"]
    assert defaults["model"] == {"primary": "anthropic/claude-sonnet-4-6", "fallbacks": []}
    assert defaults["workspace"] == "/Users/x/.openclaw/workspace"
    assert defaults["memorySearch"] == {"provider": "openai"}


# ── JSON CLI shape ──────────────────────────────────────────────────────────


def test_json_memory_returns_current_block(tmp_path):
    p = _seed_oc_json(tmp_path, {
        "agents": {"defaults": {"memorySearch": {"provider": "gemini", "fallback": "openai"}}}
    })
    result = json_memory("team_bot_a", oc_json_path=p)
    assert result["ok"] is True
    assert result["bot"] == "team_bot_a"
    assert result["provider"] == "gemini"
    assert result["fallback"] == "openai"


def test_json_memory_returns_nones_when_unset(tmp_path):
    p = _seed_oc_json(tmp_path, {})
    result = json_memory("team_bot_a", oc_json_path=p)
    assert result["ok"] is True
    assert result["provider"] is None
    assert result["fallback"] is None


def test_json_memory_set_writes_provider_and_fallback(tmp_path):
    p = _seed_oc_json(tmp_path, {})
    result = json_memory_set("team_bot_a", "gemini", "openai", oc_json_path=p)
    assert result["ok"] is True
    assert result["provider"] == "gemini"
    assert result["fallback"] == "openai"
    on_disk = json.loads(p.read_text())
    assert on_disk["agents"]["defaults"]["memorySearch"] == {"provider": "gemini", "fallback": "openai"}


def test_json_memory_set_omits_fallback_when_none(tmp_path):
    p = _seed_oc_json(tmp_path, {})
    json_memory_set("team_bot_a", "openai", None, oc_json_path=p)
    on_disk = json.loads(p.read_text())
    assert on_disk["agents"]["defaults"]["memorySearch"] == {"provider": "openai"}


def test_json_memory_set_with_empty_provider_clears_block(tmp_path):
    p = _seed_oc_json(tmp_path, {
        "agents": {"defaults": {"memorySearch": {"provider": "openai"}}}
    })
    result = json_memory_set("team_bot_a", "", None, oc_json_path=p)
    assert result["ok"] is True
    assert result["provider"] is None
    on_disk = json.loads(p.read_text())
    assert "memorySearch" not in on_disk.get("agents", {}).get("defaults", {})


def test_json_memory_set_preserves_other_agents_defaults(tmp_path):
    p = _seed_oc_json(tmp_path, {
        "agents": {"defaults": {"model": {"primary": "anthropic/claude-sonnet-4-6"}}}
    })
    json_memory_set("team_bot_a", "gemini", "openai", oc_json_path=p)
    on_disk = json.loads(p.read_text())
    assert on_disk["agents"]["defaults"]["model"] == {"primary": "anthropic/claude-sonnet-4-6"}
    assert on_disk["agents"]["defaults"]["memorySearch"] == {"provider": "gemini", "fallback": "openai"}
