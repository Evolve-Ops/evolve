"""Tests for heal's live repair of retired openclaw.json keys
(breaker-notify-survives-openclaw-config-validation item 2, "how the fix
reaches the live pod").

``deploy.strip_retired_openclaw_keys`` stops NEW deploys from writing the
key; this is the matching heal-cycle repair that reaches an ALREADY-
deployed bot faster than waiting on the next full ``evolve-admin deploy`` —
same shape as heal's existing ``agents.main`` strip just above it in
``check_pod_conduct_injection``, and it calls the SAME deploy.py function
the deploy-time fix uses, so the two paths can never disagree about which
keys are retired.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import heal  # noqa: E402


def _oc_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".openclaw"
    d.mkdir(parents=True, exist_ok=True)
    (d / "workspace").mkdir(exist_ok=True)
    (d / "workspace" / "AGENTS.md").write_text("existing content\nPOD_CONDUCT.md\n")
    return d


def test_retired_key_present_is_stripped_and_written_back(tmp_path, monkeypatch):
    oc_dir = _oc_dir(tmp_path)
    config_path = oc_dir / "openclaw.json"
    config_path.write_text(json.dumps({
        "agents": {
            "defaults": {
                "contextPruning": {
                    "mode": "cache-ttl", "ttl": "5m", "keepLastAssistants": 5,
                },
            },
        },
    }))

    written = {}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["sudo", "/bin/cp"]:
            staged = Path(cmd[2])
            dest = Path(cmd[3])
            if dest == config_path:
                written["config"] = json.loads(staged.read_text())
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(heal.subprocess, "run", fake_run)
    fake_runtime = MagicMock()
    monkeypatch.setattr(
        "runtime.agent_runtime.get_runtime", lambda: fake_runtime,
    )

    heal.check_pod_conduct_injection("team_bot_a", config_path, os_user="team_bot_a")

    assert "config" in written, "the stripped config was never written back"
    cp = written["config"]["agents"]["defaults"]["contextPruning"]
    assert "keepLastAssistants" not in cp
    assert cp["mode"] == "cache-ttl"
    assert cp["ttl"] == "5m"
    # A config-shape change that fixes CLI startup is worth a gateway
    # restart, same as the agents.main repair right above it.
    fake_runtime.gateway_restart.assert_called_once_with("team_bot_a")


def test_clean_config_is_left_untouched(tmp_path, monkeypatch):
    oc_dir = _oc_dir(tmp_path)
    config_path = oc_dir / "openclaw.json"
    config_path.write_text(json.dumps({
        "agents": {
            "defaults": {"contextPruning": {"mode": "cache-ttl", "ttl": "5m"}},
        },
    }))

    config_writes = []

    def fake_run(cmd, **kw):
        if cmd[:2] == ["sudo", "/bin/cp"] and Path(cmd[3]) == config_path:
            config_writes.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(heal.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "runtime.agent_runtime.get_runtime", lambda: MagicMock(),
    )

    heal.check_pod_conduct_injection("team_bot_a", config_path, os_user="team_bot_a")

    assert config_writes == []


def test_unreadable_config_does_not_crash_the_repair(tmp_path, monkeypatch):
    """No openclaw.json at all (or unreadable) → the repair step is a
    no-op, not a crash; POD_CONDUCT injection still runs its own course."""
    oc_dir = _oc_dir(tmp_path)
    config_path = oc_dir / "openclaw.json"  # never created

    def fake_run(cmd, **kw):
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(heal.subprocess, "run", fake_run)

    # Must not raise.
    result = heal.check_pod_conduct_injection(
        "team_bot_a", config_path, os_user="team_bot_a",
    )
    assert result is True  # AGENTS.md already had the POD_CONDUCT reference
