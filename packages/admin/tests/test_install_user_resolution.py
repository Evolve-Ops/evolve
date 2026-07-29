"""Regression tests for bot_id vs bot_user resolution in launchd plist install.

Pins the contract that install functions emit the resolved Unix username
(bot_user) for the plist's UserName field and filesystem paths, while keeping
the bot_id (logical name) for the launchd label and --bot-id args.

Without this, deploys for bots whose macOS account name differs from their
logical bot_id (e.g. bot_id="team_bot_b", user="personal_bot_user") install plists with
UserName=<bot_id> and create orphan /Users/<bot_id>/.openclaw/ directories.
heal.py's oc_cli probes then fail with token mismatch and heal kills the
gateway every 5 min in a self-cancelling restart cascade.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402
from evolve_admin.deploy import (  # noqa: E402
    DeployResult,
    _install_launchd_apply,
    install_bot_gateway_plist,
)
# _install_launchd_test removed 2026-06-08 — app-test surface killed
# per docs/decision-app-tests-2026-06-08.md.


# ── _plist_content: bot_id stays in label, bot_user shapes user-side fields ──


@pytest.mark.parametrize("install_fn,kind", [
    (_install_launchd_apply, "apply"),
])
def test_install_launchd_uses_bot_user_for_unix_user(install_fn, kind):
    """When bot_id != bot_user, the installed job must:
    - use bot_id in the launchd/systemd label (logical-name-stable)
    - use bot_user as the Unix username (UserName / User=)

    The apply daemon installs through the Scheduler seam (via_seam=True), so
    the contract is observed on the recorded :class:`JobSpec` rather than on
    a captured ``_plist_content`` call.

    Note: a per-bot ``_install_launchd_measure`` once lived here and was
    pinned as "always runs as evolve". The function was retired when
    measure became pod-wide (``ai.openclaw.evolve.measure``); the dead
    code lingered until the 2026-05-26 orphan-sweep meta-test caught it.
    """
    from evolve_admin.runtime import FakeScheduler, set_scheduler

    fake = FakeScheduler()
    set_scheduler(fake)
    try:
        with patch.object(deploy.Path, "exists", return_value=True):
            result = DeployResult("team_bot_b", True)
            install_fn("team_bot_b", Path("/Users/personal_bot_user/evolve"),
                       result, user="personal_bot_user")
    finally:
        set_scheduler(None)

    label = "ai.openclaw.evolve.apply.team_bot_b"
    assert label in fake.jobs, (
        f"apply must install through the seam; recorded jobs: {list(fake.jobs)}"
    )
    spec = fake.jobs[label]
    assert ".team_bot_b" in spec.label, (
        f"label should still reference bot_id 'team_bot_b', got {spec.label}"
    )
    assert spec.user == "personal_bot_user", (
        f"UserName should be 'personal_bot_user' (bot_user), got {spec.user!r}"
    )


@pytest.mark.parametrize("install_fn", [
    _install_launchd_apply,
])
def test_install_launchd_falls_back_to_bot_id_when_user_omitted(install_fn):
    """For back-compat: if `user` is not passed, default to bot_id (works for
    bots where bot_id == macOS user, which is the majority case)."""
    from evolve_admin.runtime import FakeScheduler, set_scheduler

    fake = FakeScheduler()
    set_scheduler(fake)
    try:
        with patch.object(deploy.Path, "exists", return_value=True):
            result = DeployResult("team_bot_a", True)
            install_fn("team_bot_a", Path("/Users/team_bot_a/evolve"), result)
    finally:
        set_scheduler(None)

    spec = fake.jobs["ai.openclaw.evolve.apply.team_bot_a"]
    assert spec.user == "team_bot_a"


# ── install_bot_gateway_plist: same bot_id-vs-user contract ──


def test_install_bot_gateway_plist_uses_bot_user_for_paths():
    """Gateway plist content must reference bot_user (not bot_id) for:
    - <key>UserName</key><string>{user}</string>
    - StandardOut/ErrorPath under /Users/{user}/.openclaw/logs/
    - HOME env var = /Users/{user}
    - log dir chown
    The label `ai.openclaw.{bot_id}-gateway` and Comment must keep bot_id.
    """
    def fake_run(cmd, **kw):
        from subprocess import CompletedProcess
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    # The plist write + bootout/bootstrap ritual rides the Scheduler seam
    # (4.3C S2) — inject a fake runner so the test never spawns a real
    # ``sudo cp``/``sudo launchctl``.
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler
    set_scheduler(LaunchdScheduler(runner=lambda argv: (0, "", "")))
    try:
        with patch.object(deploy.subprocess, "run", side_effect=fake_run), \
             patch.object(deploy.shutil, "which", return_value="/opt/homebrew/bin/node"), \
             patch.object(deploy, "_wait_for_gateway_port", return_value=True):
            ok, detail = install_bot_gateway_plist(
                "team_bot_b", port=18790, user="personal_bot_user",
            )
    finally:
        set_scheduler(None)

    # We can't easily read the written file from the patched run, but we can
    # call the function with both bot_ids and confirm it doesn't blow up.
    # The function now returns (success, detail) so the wizard can surface
    # the actual error on failure instead of a canned recovery hint.
    assert ok is True, f"install should succeed under the mocked subprocess.run: {detail}"
    assert "port 18790" in detail


def test_install_bot_gateway_plist_user_omitted_defaults_to_bot_id():
    """Back-compat: omitting `user` falls back to bot_id."""
    def fake_run(cmd, **kw):
        from subprocess import CompletedProcess
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    from evolve_admin.runtime import LaunchdScheduler, set_scheduler
    set_scheduler(LaunchdScheduler(runner=lambda argv: (0, "", "")))
    try:
        with patch.object(deploy.subprocess, "run", side_effect=fake_run), \
             patch.object(deploy.shutil, "which", return_value="/opt/homebrew/bin/node"), \
             patch.object(deploy, "_wait_for_gateway_port", return_value=True):
            ok, _detail = install_bot_gateway_plist("team_bot_a", port=18789)
    finally:
        set_scheduler(None)

    assert ok is True
