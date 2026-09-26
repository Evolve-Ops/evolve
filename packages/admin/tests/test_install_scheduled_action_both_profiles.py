"""Both-profile proof for the gallery app-cron materializer (#3392).

The ``mechanism: "launchd"`` scheduled-action install path
(``forge_engine._materialize_scheduled_actions`` →
``install_launchd_command_action``) routes through the scheduler seam:

- **LINUX profile** — the SAME manifest entry renders a systemd
  service+timer unit set named ``ai.evolve.<bot>.<app>`` via
  ``render_systemd_units``, with **no /Users path anywhere in the unit
  bodies** (the seam-routing-passes-but-unit-body-leaks-paths capstone:
  routing through the seam is not enough; assert on the rendered body).
  Before #3392 this path hardcoded ``/Users/<bot>/...`` and called
  ``get_launchd_scheduler()``, which raises off macOS — the app installed
  but never swept (silent-dead-app).
- **MACOS profile** — the rendered plist is byte-identical to what the
  pre-seam installer wrote (golden pinned below), so existing pods'
  app-cron plists cannot move.

Also pins the seam-factory contract the both-profile behavior depends on:
``get_scheduler()``'s default is keyed by the ACTIVE profile (get_perms
shape), not first-call-wins.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

import platform_profile  # noqa: E402
from evolve_admin.applications import install_helpers  # noqa: E402
from evolve_admin.applications.install_helpers import (  # noqa: E402
    install_launchd_command_action,
)
from evolve_admin.runtime import (  # noqa: E402
    LaunchdScheduler,
    SystemdScheduler,
    get_scheduler,
    set_scheduler,
)


BOT = "atlas"
LABEL = "ai.evolve.atlas.pm-inbox"


@pytest.fixture(autouse=True)
def _reset_seams():
    """Every test here leaves the profile AND scheduler override as the
    suite default (conftest pins MACOS; the scheduler override must not
    leak a fake into later tests)."""
    yield
    set_scheduler(None)
    platform_profile.set_profile(platform_profile.MACOS)


def _patch_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        install_helpers, "load_network",
        lambda: {"bots": {BOT: {"user": BOT}}},
    )
    monkeypatch.setattr(install_helpers, "get_bot_user", lambda bot_id, net: BOT)
    monkeypatch.setattr(install_helpers, "_bot_uid", lambda u: 1001)


def _ok_runner(calls: list):
    """Recording fake for the adapter's subprocess chokepoint — every
    systemctl / mkdir / chown the install issues, no process spawned."""
    def run(argv):
        calls.append(list(argv))
        return 0, "", ""
    return run


def _install_pm_inbox(**kwargs):
    """The pm-inbox-shaped manifest entry (internal/spec-darwin-pm-2026-07-02.md):
    hourly cron, ${workspace}-relative helper, TZ env, ${bot_id} label."""
    return install_launchd_command_action(
        bot_id=BOT,
        action_id="pm-inbox-sweep",
        label="ai.evolve.${bot_id}.pm-inbox",
        command="/bin/bash ${workspace}/scripts/pm-inbox-cron.sh",
        schedule={"cron": {"Minute": 7}},
        cwd="${workspace}",
        env={"TZ": "America/Los_Angeles"},
        **kwargs,
    )


# ── LINUX profile — systemd units from the same manifest entry ───────────────


def _linux_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_network(monkeypatch)
    platform_profile.set_profile(platform_profile.LINUX)
    calls: list = []
    sched = SystemdScheduler(
        unit_dir=tmp_path, use_sudo=False, runner=_ok_runner(calls),
    )
    set_scheduler(sched)
    result = _install_pm_inbox()
    return result, calls


def test_linux_profile_renders_systemd_service_and_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _calls = _linux_install(tmp_path, monkeypatch)
    assert result["ok"] is True, result.get("error")
    assert result["loaded"] is True
    # The artifact stamp is the primary systemd unit, not a plist.
    assert result["artifact"] == str(tmp_path / f"{LABEL}.service")
    assert (tmp_path / f"{LABEL}.service").is_file()
    assert (tmp_path / f"{LABEL}.timer").is_file()


def test_linux_unit_bodies_carry_no_users_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capstone assertion: seam routing is not enough — the rendered
    BODY must resolve every path for the Linux pod (/home workspace,
    granted per-bot log root, no Homebrew PATH)."""
    result, _calls = _linux_install(tmp_path, monkeypatch)
    assert result["ok"] is True, result.get("error")

    service = (tmp_path / f"{LABEL}.service").read_text()
    timer = (tmp_path / f"{LABEL}.timer").read_text()
    for body in (service, timer):
        assert "/Users" not in body
        assert "/opt/homebrew" not in body

    # The substituted workspace resolved to the Linux bot home.
    assert (
        "ExecStart=/bin/bash /home/atlas/.openclaw/workspace/scripts/pm-inbox-cron.sh"
        in service
    )
    assert "WorkingDirectory=/home/atlas/.openclaw/workspace" in service
    assert f"User={BOT}" in service
    # Logs land under the sudoers-granted per-bot root, not /tmp.
    assert (
        f"StandardOutput=append:/home/atlas/.openclaw/logs/{LABEL}.out.log"
        in service
    )
    assert (
        f"StandardError=append:/home/atlas/.openclaw/logs/{LABEL}.err.log"
        in service
    )
    # PATH tracks the LINUX profile's exec dirs.
    assert '"PATH=/usr/local/bin:/usr/bin:/bin"' in service
    # Hourly cron → OnCalendar on the timer.
    assert "OnCalendar=" in timer


def test_linux_install_registers_the_timer_within_granted_verbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every subprocess the Linux install issues must stay inside the
    already-granted evolve sudoers surface: daemon-reload, enable/restart
    of ai.evolve.* units, and mkdir/chown of {home}/*/.openclaw/logs —
    a shape outside those grants dies at sudo on a real pod."""
    result, calls = _linux_install(tmp_path, monkeypatch)
    assert result["ok"] is True, result.get("error")

    joined = [" ".join(argv) for argv in calls]
    assert any(a.endswith("systemctl daemon-reload") for a in joined)
    assert any(f"systemctl enable {LABEL}.timer" in a for a in joined)
    assert any(f"systemctl restart {LABEL}.timer" in a for a in joined)
    # Log-dir creation targets the granted per-bot root, chowned to the bot.
    # -h (no-dereference) stays inside the `chown * <path>` grant glob —
    # sudoers arg wildcards span whitespace.
    assert any(a.endswith("mkdir -p /home/atlas/.openclaw/logs") for a in joined)
    assert any(a.endswith(f"chown -h {BOT} /home/atlas/.openclaw/logs") for a in joined)
    # Nothing may touch launchctl on a Linux pod.
    assert not any("launchctl" in a for a in joined)


def test_linux_install_failure_is_loud_not_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed registration must FAIL the install (ok=False) — the old
    path returned ok=True with loaded=False and forge stamped the action
    installed: the exact silent-dead-app shape #3392 kills."""
    _patch_network(monkeypatch)
    platform_profile.set_profile(platform_profile.LINUX)

    def failing_runner(argv):
        if "daemon-reload" in argv:
            return 1, "", "simulated systemctl failure"
        return 0, "", ""

    set_scheduler(SystemdScheduler(
        unit_dir=tmp_path, use_sudo=False, runner=failing_runner,
    ))
    result = _install_pm_inbox()
    assert result["ok"] is False
    assert "daemon-reload" in result["error"]


# ── MACOS profile — byte-identity with the pre-seam renderer ─────────────────

# Rendered by the PRE-#3392 installer (_build_command_plist_xml +
# _ensure_launchd_openclaw_path on the MACOS profile) for the exact
# install call `_install_pm_inbox` makes. The seam's launchd adapter must
# write these bytes verbatim — if this golden ever needs editing, every
# app-cron plist already installed on a macOS pod moves with it.
_GOLDEN_MACOS_PLIST = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>ai.evolve.atlas.pm-inbox</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>/bin/bash</string>
\t\t<string>/Users/atlas/.openclaw/workspace/scripts/pm-inbox-cron.sh</string>
\t</array>
\t<key>UserName</key>
\t<string>atlas</string>
\t<key>GroupName</key>
\t<string>staff</string>
\t<key>WorkingDirectory</key>
\t<string>/Users/atlas/.openclaw/workspace</string>
\t<key>RunAtLoad</key>
\t<false/>
\t<key>StartCalendarInterval</key>
\t<dict>
\t\t<key>Minute</key>
\t\t<integer>7</integer>
\t</dict>
\t<key>StandardOutPath</key>
\t<string>/tmp/ai.evolve.atlas.pm-inbox.out.log</string>
\t<key>StandardErrorPath</key>
\t<string>/tmp/ai.evolve.atlas.pm-inbox.err.log</string>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>TZ</key>
\t\t<string>America/Los_Angeles</string>
\t\t<key>PATH</key>
\t\t<string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
\t</dict>
</dict>
</plist>
'''


def test_macos_profile_plist_is_byte_identical_to_pre_seam_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_network(monkeypatch)
    platform_profile.set_profile(platform_profile.MACOS)
    calls: list = []
    set_scheduler(LaunchdScheduler(
        plist_dir=tmp_path, use_sudo=False, runner=_ok_runner(calls),
    ))

    result = _install_pm_inbox()
    assert result["ok"] is True, result.get("error")
    assert result["loaded"] is True
    assert result["artifact"] == str(tmp_path / f"{LABEL}.plist")

    written = (tmp_path / f"{LABEL}.plist").read_text()
    assert written == _GOLDEN_MACOS_PLIST

    # macOS registration still bootouts + bootstraps the system domain.
    joined = [" ".join(argv) for argv in calls]
    assert any(f"launchctl bootout system/{LABEL}" in a for a in joined)
    assert any("launchctl bootstrap system" in a for a in joined)
    assert not any("systemctl" in a for a in joined)


def test_macos_byte_identical_reinstall_is_skipped_not_bounced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second install of the unchanged action: the seam's idempotent-skip
    contract (same as every deploy.py infra daemon) — no bootout/bootstrap
    bounce, envelope still ok+loaded."""
    _patch_network(monkeypatch)
    platform_profile.set_profile(platform_profile.MACOS)
    calls: list = []
    set_scheduler(LaunchdScheduler(
        plist_dir=tmp_path, use_sudo=False, runner=_ok_runner(calls),
    ))
    assert _install_pm_inbox()["ok"] is True
    calls.clear()

    again = _install_pm_inbox()
    assert again["ok"] is True
    assert again["loaded"] is True
    assert again["skipped"] is True
    assert calls == []  # no launchctl traffic on the skip


# ── seam-factory contract ─────────────────────────────────────────────────────


def test_get_scheduler_default_is_profile_keyed_not_first_call_wins() -> None:
    """A profile pinned AFTER an earlier get_scheduler() call still selects
    the matching adapter (the get_perms cache shape). First-call-wins here
    silently kept launchd for the whole process on a Linux pod."""
    set_scheduler(None)
    platform_profile.set_profile(platform_profile.MACOS)
    assert isinstance(get_scheduler(), LaunchdScheduler)
    platform_profile.set_profile(platform_profile.LINUX)
    assert isinstance(get_scheduler(), SystemdScheduler)
    platform_profile.set_profile(platform_profile.MACOS)
    assert isinstance(get_scheduler(), LaunchdScheduler)


def test_bootstrap_false_is_a_dry_run_touching_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bootstrap=False (test-only flag): resolve + validate, write nothing,
    register nothing."""
    _patch_network(monkeypatch)
    platform_profile.set_profile(platform_profile.LINUX)
    calls: list = []
    set_scheduler(SystemdScheduler(
        unit_dir=tmp_path, use_sudo=False, runner=_ok_runner(calls),
    ))
    result = _install_pm_inbox(bootstrap=False)
    assert result["ok"] is True
    assert result["loaded"] is False
    assert result["artifact"] == str(tmp_path / f"{LABEL}.service")
    assert list(tmp_path.iterdir()) == []
    assert calls == []
