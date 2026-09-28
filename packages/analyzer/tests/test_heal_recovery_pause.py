"""heal.py honors the B2.e recovery pause flag.

The pillar's load-bearing invariant: when the operator hits the panic
button (writing ``{shared_dir}/recovery/pause-state.json``), heal MUST
NOT restart any bot gateway on its next cycle — otherwise the pause is
effectively a 5-minute pause, after which heal undoes it.

These tests pin that contract at the heal layer (recovery.py is tested
separately in packages/admin/tests/test_recovery.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import heal  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Build a hermetic environment in tmp_path with one fake bot.

    Stubs:
      - check_gateway → bot is always DOWN (so a restart would normally fire)
      - restart_gateway → records its call (we use this as the canary)
      - _record_incident → no-op
      - _alert_failure → no-op
      - _suspend_guard → never suspended
      - check_pod_conduct_injection → False (don't go down that branch)
      - _check_patterns / detect_backup_drift → no-op
      - _write_status_file → no-op
    """
    restarts: list[str] = []

    monkeypatch.setattr(heal, "_suspend_guard", lambda *a, **k: False)
    monkeypatch.setattr(
        heal, "check_gateway",
        lambda bot_id, port, heal_cfg, **kw: heal.BotStatus(
            bot_id=bot_id, port=port, healthy=False,
            response_time_ms=None, error="stub-down",
        ),
    )

    def fake_restart(bot_id, os_user=None):
        restarts.append(bot_id)
        return True

    monkeypatch.setattr(heal, "restart_gateway", fake_restart)
    monkeypatch.setattr(heal, "_record_incident", lambda *a, **k: None)
    monkeypatch.setattr(heal, "_alert_failure", lambda *a, **k: None)
    monkeypatch.setattr(heal, "_check_patterns", lambda *a, **k: None)
    monkeypatch.setattr(heal, "detect_backup_drift", lambda *a, **k: [])
    monkeypatch.setattr(heal, "_write_status_file", lambda *a, **k: None)
    monkeypatch.setattr(heal, "check_pod_conduct_injection", lambda *a, **k: False)
    monkeypatch.setattr(heal, "_in_restart_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(heal, "_get_port", lambda *a, **k: 9001)

    # POD_CONDUCT.md absence is fine for these tests; heal handles it.
    # heal looks at the literal path /Users/Shared/evolve/POD_CONDUCT.md;
    # we can't easily monkeypatch that constant, so just accept the warning.

    config = {
        "primary": "team_bot_a",
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
    }
    heal_cfg = {"restartCooldownMin": 0}

    return {
        "config": config,
        "heal_cfg": heal_cfg,
        "shared_dir": tmp_path,
        "restarts": restarts,
    }


def test_heal_restarts_when_not_paused(env):
    """Sanity: with no pause flag, a down bot triggers restart."""
    heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
    assert env["restarts"] == ["team_bot_a"]


def test_heal_skips_restart_when_pause_flag_set(env):
    """B2.e contract: pause flag → no restart."""
    pause_flag = env["shared_dir"] / "recovery" / "pause-state.json"
    pause_flag.parent.mkdir(parents=True, exist_ok=True)
    pause_flag.write_text(json.dumps({
        "paused": True,
        "paused_at": "2026-05-12T00:00:00+00:00",
        "reason": "test pause",
        "initiated_by": "pytest",
    }))

    heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
    assert env["restarts"] == [], "heal must not restart while paused"


def test_heal_treats_paused_false_as_not_paused(env):
    """A flag with ``paused: false`` is the post-resume cleared-state shape."""
    pause_flag = env["shared_dir"] / "recovery" / "pause-state.json"
    pause_flag.parent.mkdir(parents=True, exist_ok=True)
    pause_flag.write_text(json.dumps({"paused": False, "cleared_at": "x"}))

    heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
    assert env["restarts"] == ["team_bot_a"], "false-paused flag must not block restart"


def test_heal_corrupt_pause_flag_fails_open(env):
    """A corrupt flag should fail-open (normal operation continues)."""
    pause_flag = env["shared_dir"] / "recovery" / "pause-state.json"
    pause_flag.parent.mkdir(parents=True, exist_ok=True)
    pause_flag.write_text("not-json}}")

    heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
    # fail-open: restart proceeds
    assert env["restarts"] == ["team_bot_a"]
