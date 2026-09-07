"""Unit tests for install_helpers — privileged install operations for forge.

PR 4 of spec-forge-side-effects-2026-06-02.md §5, plus the v17 pivot
in spec-heartbeat-instruction-2026-06-03.md.

Tests mock the subprocess + safe_write_bot_config seams so no actual
filesystem writes or launchctl calls happen.

Coverage:
  - install_oc_hook: deprecation wrapper returns clear error (v17);
    legacy implementation pinned via _install_oc_hook_legacy_impl
    (read+patch openclaw.json, idempotency on duplicate command,
    refuse-to-clobber when hooks.{event} is a bare bool, kickstart
    after successful write)
  - install_launch_agent: /tmp staging + sudo cp + chown + chmod +
    bootstrap, label sanity checks
  - install_crontab_entry: NotImplementedError path (deferred, returns err)

install_heartbeat_instruction is tested separately in
``test_install_heartbeat_instruction.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.install_helpers import (  # noqa: E402
    _install_oc_hook_legacy_impl as install_oc_hook,   # legacy tests run against the pre-v17 impl
    install_crontab_entry,
    install_launch_agent,
)
from evolve_admin.applications import install_helpers  # noqa: E402


# ── Shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def fake_network():
    """Minimal network.json with a single bot mapping bot_id → user."""
    return {
        "bots": {
            "personal-bot": {"user": "personal-bot"},
            "team-bot-a": {"user": "personal-bot-user-account"},
        },
    }


@pytest.fixture
def oc_config():
    """A canonical openclaw.json with no hooks yet."""
    return {
        "version": "2026.06.01",
        "agents": {"defaults": {"workspace": "/Users/personal-bot/.openclaw/workspace"}},
        "hooks": {},
    }


# ── install_oc_hook ─────────────────────────────────────────────────────────


def test_install_oc_hook_appends_new_hook_entry(tmp_path: Path, fake_network: dict, oc_config: dict) -> None:
    """Happy path: hook is appended to hooks.heartbeat[] and the patched
    config is sent through safe_write_bot_config."""
    # Stand up a fake openclaw.json so Path.read_text() succeeds.
    oc_dir = tmp_path / "personal-bot" / ".openclaw"
    oc_dir.mkdir(parents=True)
    (oc_dir / "openclaw.json").write_text(json.dumps(oc_config))

    written_config: dict = {}

    def fake_safe_write(bot_id, new_config, **kwargs):
        written_config.update(new_config)
        return True, ""

    with patch("evolve_admin.applications.install_helpers.safe_write_bot_config",
               side_effect=fake_safe_write) as mock_write, \
         patch("evolve_admin.applications.install_helpers.subprocess.run") as mock_sp, \
         patch("evolve_admin.applications.install_helpers.Path") as mock_path:
        # Path() in the helper is used only for the config_path; route reads
        # through the mocked tmp_path location.
        mock_path.return_value = oc_dir / "openclaw.json"
        result = install_oc_hook(
            "personal-bot", "heartbeat", "python3 scripts/tasks.py check",
            network=fake_network,
        )

    assert result["ok"] is True
    assert result["artifact"] == "openclaw.json#hooks.heartbeat[0]"
    assert result["restart_required"] is True
    assert result["already_present"] is False
    # The patched config sent to safe_write_bot_config has the new hook.
    assert written_config["hooks"]["heartbeat"][0]["command"] == \
           "python3 scripts/tasks.py check"
    mock_write.assert_called_once()
    # Kickstart was attempted (mocked subprocess.run was called at least once
    # — that's the launchctl kickstart -k call).
    assert mock_sp.called


def test_install_oc_hook_is_idempotent_on_duplicate_command(
    tmp_path: Path, fake_network: dict
) -> None:
    """Re-installing an already-registered command is a no-op — no
    safe_write_bot_config, no kickstart, returns ok with already_present."""
    oc_with_hook = {
        "version": "2026.06.01",
        "hooks": {
            "heartbeat": [{"command": "python3 scripts/tasks.py check"}],
        },
    }
    oc_dir = tmp_path / "personal-bot" / ".openclaw"
    oc_dir.mkdir(parents=True)
    (oc_dir / "openclaw.json").write_text(json.dumps(oc_with_hook))

    with patch("evolve_admin.applications.install_helpers.safe_write_bot_config") as mock_write, \
         patch("evolve_admin.applications.install_helpers.subprocess.run") as mock_sp, \
         patch("evolve_admin.applications.install_helpers.Path") as mock_path:
        mock_path.return_value = oc_dir / "openclaw.json"
        result = install_oc_hook(
            "personal-bot", "heartbeat", "python3 scripts/tasks.py check",
            network=fake_network,
        )

    assert result["ok"] is True
    assert result["already_present"] is True
    assert result["restart_required"] is False
    # Idempotent → no write attempt, no kickstart.
    mock_write.assert_not_called()
    mock_sp.assert_not_called()


def test_install_oc_hook_refuses_to_clobber_bare_bool(
    tmp_path: Path, fake_network: dict
) -> None:
    """Some OC versions used bare bool flags under hooks (e.g. allow-
    ConversationAccess: true). install_oc_hook MUST NOT clobber such
    operator-set values."""
    oc_bool = {
        "version": "2026.06.01",
        "hooks": {"heartbeat": True},  # bare bool, not a list
    }
    oc_dir = tmp_path / "personal-bot" / ".openclaw"
    oc_dir.mkdir(parents=True)
    (oc_dir / "openclaw.json").write_text(json.dumps(oc_bool))

    with patch("evolve_admin.applications.install_helpers.safe_write_bot_config") as mock_write, \
         patch("evolve_admin.applications.install_helpers.Path") as mock_path:
        mock_path.return_value = oc_dir / "openclaw.json"
        result = install_oc_hook(
            "personal-bot", "heartbeat", "python3 scripts/tasks.py check",
            network=fake_network,
        )

    assert result["ok"] is False
    assert "is not a list" in result["error"]
    mock_write.assert_not_called()


def test_install_oc_hook_propagates_safe_write_validation_failure(
    tmp_path: Path, fake_network: dict, oc_config: dict
) -> None:
    """safe_write_bot_config rejects the patched config → helper surfaces err."""
    oc_dir = tmp_path / "personal-bot" / ".openclaw"
    oc_dir.mkdir(parents=True)
    (oc_dir / "openclaw.json").write_text(json.dumps(oc_config))

    with patch("evolve_admin.applications.install_helpers.safe_write_bot_config",
               return_value=(False, "OC schema validation failed: hooks.heartbeat[0].command type")), \
         patch("evolve_admin.applications.install_helpers.subprocess.run") as mock_sp, \
         patch("evolve_admin.applications.install_helpers.Path") as mock_path:
        mock_path.return_value = oc_dir / "openclaw.json"
        result = install_oc_hook(
            "personal-bot", "heartbeat", "python3 scripts/tasks.py check",
            network=fake_network,
        )

    assert result["ok"] is False
    assert "schema validation" in result["error"]
    # safe_write returned err → no kickstart attempted.
    mock_sp.assert_not_called()


# ── install_oc_hook deprecation wrapper (v17) ───────────────────────────────


def test_install_oc_hook_public_returns_deprecation_error() -> None:
    """The PUBLIC ``install_oc_hook`` (not the _legacy_impl alias) returns a
    clear deprecation diagnostic pointing at install_heartbeat_instruction.

    spec-heartbeat-instruction-2026-06-03.md §1 documents this. The
    diagnostic must mention the replacement so future maintainers grepping
    "install_oc_hook" find the migration path.
    """
    result = install_helpers.install_oc_hook("personal-bot", "heartbeat", "echo hi")
    assert result["ok"] is False
    assert "deprecated" in result["error"].lower()
    assert "install_heartbeat_instruction" in result["error"]
    assert "HEARTBEAT.md" in result["error"]


def test_install_oc_hook_public_does_not_touch_safe_write() -> None:
    """The deprecation wrapper must NOT call safe_write_bot_config — it's
    a fast-fail path, not a partial-attempt."""
    with patch("evolve_admin.applications.install_helpers.safe_write_bot_config") as mock_write, \
         patch("evolve_admin.applications.install_helpers.subprocess.run") as mock_sp:
        install_helpers.install_oc_hook("personal-bot", "heartbeat", "echo hi")
    mock_write.assert_not_called()
    mock_sp.assert_not_called()


def test_install_oc_hook_rejects_empty_required_fields(fake_network: dict) -> None:
    for kwargs in (
        {"bot_id": "", "hook_event": "heartbeat", "command": "x"},
        {"bot_id": "personal-bot", "hook_event": "", "command": "x"},
        {"bot_id": "personal-bot", "hook_event": "heartbeat", "command": ""},
    ):
        r = install_oc_hook(network=fake_network, **kwargs)
        assert r["ok"] is False
        assert "requires" in r["error"]


# ── install_launch_agent ────────────────────────────────────────────────────


def test_install_launch_agent_writes_plist_and_bootstraps(
    tmp_path: Path, fake_network: dict
) -> None:
    """Happy path: plist is staged, copied, chowned, chmodded, bootout +
    bootstrap are called in order. Returns ok with path + loaded=True."""
    with patch("evolve_admin.applications.install_helpers.subprocess.run") as mock_sp, \
         patch("evolve_admin.applications.install_helpers._bot_uid", return_value=502):
        # All subprocess.run calls return successful (rc=0).
        mock_sp.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = install_launch_agent(
            "personal-bot",
            "com.personal-bot.task-manager.check",
            "<?xml version='1.0'?><plist><dict><key>Label</key>"
            "<string>com.personal-bot.task-manager.check</string></dict></plist>",
            network=fake_network,
        )

    assert result["ok"] is True
    assert result["artifact"].endswith("com.personal-bot.task-manager.check.plist")
    assert result["loaded"] is True
    # Confirm the launchctl bootstrap was the last subprocess call against
    # the target domain.
    call_args = [c.args[0] for c in mock_sp.call_args_list]
    assert any(a == ["sudo", "/bin/launchctl", "bootstrap", "gui/502",
                     "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-manager.check.plist"]
               for a in call_args)


def test_install_launch_agent_returns_loaded_false_on_bootstrap_failure(
    tmp_path: Path, fake_network: dict
) -> None:
    """The plist still lands on disk even if bootstrap fails — loaded=False,
    artifact populated. Non-fatal because a sleeping user-domain can't be
    bootstrapped until next login."""
    with patch("evolve_admin.applications.install_helpers.subprocess.run") as mock_sp, \
         patch("evolve_admin.applications.install_helpers._bot_uid", return_value=502):
        def fake_run(cmd, **_kwargs):
            # Only the bootstrap call fails.
            if cmd[:2] == ["sudo", "/bin/launchctl"] and "bootstrap" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="bootstrap failed")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_sp.side_effect = fake_run
        result = install_launch_agent(
            "personal-bot", "com.personal-bot.task-manager.check",
            "<plist/>", network=fake_network,
        )

    assert result["ok"] is True   # Plist was written successfully
    assert result["loaded"] is False
    assert result["artifact"].endswith(".plist")


def test_install_launch_agent_rejects_label_with_slash(fake_network: dict) -> None:
    """Defense-in-depth: label shape sanity check before any subprocess fires."""
    r = install_launch_agent(
        "personal-bot", "com.personal-bot/evil", "<plist/>", network=fake_network,
    )
    assert r["ok"] is False
    assert "invalid label" in r["error"]


def test_install_launch_agent_rejects_label_with_plist_suffix(fake_network: dict) -> None:
    r = install_launch_agent(
        "personal-bot", "com.personal-bot.task-manager.check.plist",
        "<plist/>", network=fake_network,
    )
    assert r["ok"] is False
    assert "invalid label" in r["error"]


def test_install_launch_agent_rejects_empty_required_fields(fake_network: dict) -> None:
    for kwargs in (
        {"bot_id": "", "label": "com.x", "plist_xml": "<plist/>"},
        {"bot_id": "personal-bot", "label": "", "plist_xml": "<plist/>"},
        {"bot_id": "personal-bot", "label": "com.x", "plist_xml": ""},
    ):
        r = install_launch_agent(network=fake_network, **kwargs)
        assert r["ok"] is False


def test_install_launch_agent_skips_bootstrap_when_flag_false(
    tmp_path: Path, fake_network: dict
) -> None:
    """bootstrap=False writes the plist but never invokes launchctl."""
    with patch("evolve_admin.applications.install_helpers.subprocess.run") as mock_sp, \
         patch("evolve_admin.applications.install_helpers._bot_uid", return_value=502):
        mock_sp.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = install_launch_agent(
            "personal-bot", "com.personal-bot.task-manager.check",
            "<plist/>", network=fake_network, bootstrap=False,
        )

    assert result["ok"] is True
    assert result["loaded"] is False
    # No launchctl invocations.
    call_args = [c.args[0] for c in mock_sp.call_args_list]
    assert not any("/bin/launchctl" in (a[1] if len(a) > 1 else "")
                   for a in call_args)


# ── install_crontab_entry ───────────────────────────────────────────────────


def test_install_crontab_entry_returns_deferred_error() -> None:
    """PR 4 leaves crontab install unimplemented — caller sees a clear
    not-implemented error pointing at the next steps."""
    r = install_crontab_entry("personal-bot", "0 */4 * * *", "task-check.sh", "task-check")
    assert r["ok"] is False
    assert "not implemented" in r["error"].lower()
    assert "sudoers" in r["error"]
