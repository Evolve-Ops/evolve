"""Tests for the launchd-install template-honesty gate.

``install_launchd_command_action`` must refuse to bootstrap a launchd job
whose referenced helper script still carries unsubstituted ``{var}`` /
``${var}`` placeholders — the Atlas Daily Digest failure mode (a wrapper
shipped with literal ``{bot_id}`` etc. that failed every morning while its
``exit 0`` reported success). See docs/spec-apps-meta-2026-06-13.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import install_helpers  # noqa: E402
from evolve_admin.applications.install_helpers import (  # noqa: E402
    _check_resolved_command_and_helper,
    install_launchd_command_action,
)

_BROKEN_WRAPPER = (
    "#!/bin/bash\n"
    'WORKSPACE="/Users/{bot_id}/.openclaw/workspace"\n'
    'python3 "$WORKSPACE/scripts/atlas_digest.py" send '
    '--chat-id "{telegram_chat_id}" >> "/tmp/{bot_id}-digest.log" 2>&1\n'
    "exit 0\n"
)

_CLEAN_WRAPPER = (
    "#!/bin/bash\n"
    'WORKSPACE="/Users/atlas/.openclaw/workspace"\n'
    'python3 "$WORKSPACE/scripts/atlas_digest.py" send '
    '--chat-id "-5223931757" >> "/tmp/atlas-digest.log" 2>&1\n'
    "exit $?\n"
)


def _write_wrapper(tmp_path: Path, body: str) -> tuple[str, str]:
    """Write *body* to <tmp>/scripts/cron.sh; return (command, workspace)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    wrapper = scripts / "cron.sh"
    wrapper.write_text(body, encoding="utf-8")
    return f"/bin/bash {wrapper}", str(tmp_path)


# ── _check_resolved_command_and_helper (the guard) ───────────────────────────


def test_guard_rejects_residual_placeholder_wrapper(tmp_path: Path) -> None:
    command, workspace = _write_wrapper(tmp_path, _BROKEN_WRAPPER)
    err = _check_resolved_command_and_helper(command, workspace)
    assert err  # non-empty → install would fail
    assert "{bot_id}" in err
    assert "{telegram_chat_id}" in err
    assert str(tmp_path / "scripts" / "cron.sh") in err


def test_guard_passes_clean_wrapper(tmp_path: Path) -> None:
    command, workspace = _write_wrapper(tmp_path, _CLEAN_WRAPPER)
    assert _check_resolved_command_and_helper(command, workspace) == ""


def test_guard_does_not_block_when_helper_missing(tmp_path: Path) -> None:
    # No file on disk → install ordering may write it later; don't block.
    command = f"/bin/bash {tmp_path}/scripts/not-written-yet.sh"
    assert _check_resolved_command_and_helper(command, str(tmp_path)) == ""


def test_guard_flags_unresolved_command_itself(tmp_path: Path) -> None:
    # A command still carrying a known install var after substitution.
    err = _check_resolved_command_and_helper(
        "/bin/bash ${shared_dir}/x.sh", str(tmp_path),
    )
    assert "${shared_dir}" in err


def test_guard_ignores_bare_binary(tmp_path: Path) -> None:
    assert _check_resolved_command_and_helper("/usr/bin/true", str(tmp_path)) == ""


# ── install_launchd_command_action end-to-end with the guard ─────────────────


def _patch_offline(monkeypatch: pytest.MonkeyPatch, bot_user: str = "atlas") -> None:
    monkeypatch.setattr(
        install_helpers, "load_network",
        lambda: {"bots": {bot_user: {"user": bot_user}}},
    )
    monkeypatch.setattr(
        install_helpers, "get_bot_user", lambda bot_id, net: bot_user,
    )
    monkeypatch.setattr(
        install_helpers, "install_launchd_system_daemon",
        lambda *a, **k: {"ok": True, "artifact": "/x.plist", "error": "", "loaded": True},
    )


def test_install_refuses_unresolved_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_offline(monkeypatch)
    wrapper = tmp_path / "cron.sh"
    wrapper.write_text(_BROKEN_WRAPPER, encoding="utf-8")
    # Point the guard's path resolution at our tmp wrapper regardless of the
    # /Users/<bot> workspace the installer computes.
    monkeypatch.setattr(
        install_helpers, "helper_script_from_command",
        lambda command, *, workspace_root: (wrapper, True),
    )

    result = install_launchd_command_action(
        bot_id="atlas",
        action_id="daily-digest",
        label="ai.evolve.${bot_id}.atlas-daily-digest",
        command="/bin/bash ${workspace}/scripts/atlas-digest-cron.sh",
        schedule={"cron": {"Hour": 7, "Minute": 0}},
    )
    assert result["ok"] is False
    assert "placeholder" in result["error"].lower()


def test_install_accepts_clean_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_offline(monkeypatch)
    wrapper = tmp_path / "cron.sh"
    wrapper.write_text(_CLEAN_WRAPPER, encoding="utf-8")
    monkeypatch.setattr(
        install_helpers, "helper_script_from_command",
        lambda command, *, workspace_root: (wrapper, True),
    )

    result = install_launchd_command_action(
        bot_id="atlas",
        action_id="daily-digest",
        label="ai.evolve.${bot_id}.atlas-daily-digest",
        command="/bin/bash ${workspace}/scripts/atlas-digest-cron.sh",
        schedule={"cron": {"Hour": 7, "Minute": 0}},
    )
    assert result["ok"] is True
