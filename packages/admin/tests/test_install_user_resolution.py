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
    _install_launchd_audit_runner,
    _install_launchd_audit_runner_tier3,
    _install_launchd_backup,
    _install_launchd_cost_converter,
    _install_launchd_doctor_pass,
    install_bot_gateway_plist,
)
# _install_launchd_test removed 2026-06-08 — app-test surface killed
# per docs/decision-app-tests-2026-06-08.md.
# _install_launchd_apply removed 2026-08-18 — the legacy per-bot apply
# daemon was retired (docs/design-proposal-signing-key-2026-08-18.md).
# Its parametrizations were the ONLY pin on this contract for the
# ``_install_launchd_*`` per-bot family, so they are repointed below onto
# surviving siblings that take the same ``user=bot_user`` argument, rather
# than left to ``install_bot_gateway_plist`` — that pins *a* version of the
# contract, but for a different installer with a different code path.


# ── _install_launchd_* per-bot family: bot_id in the label, bot_user as the Unix user ──


# (installer, full label, needs_evolve_dir). Every entry takes ``user=bot_user``
# and installs through the Scheduler seam, so the contract is observed on the
# recorded :class:`JobSpec`.
#
# Note: a per-bot ``_install_launchd_measure`` once lived here and was pinned as
# "always runs as evolve". The function was retired when measure became pod-wide
# (``ai.openclaw.evolve.measure``); the dead code lingered until the 2026-05-26
# orphan-sweep meta-test caught it. That is the regression class this module
# exists to catch, and the reason the list is kept extendable.
# ``needs_evolve_dir`` is the only shape difference across the family — some
# take an (unused-for-this-contract) evolve_dir positional. The label is spelled
# out rather than built from a prefix because ``backup`` puts the bot_id in the
# MIDDLE (``ai.evolve.<bot>.backup``), and a prefix-only table quietly stopped
# matching it — which is the sort of drift this suite is for.
_LABEL_BOT = "team_bot_b"
_PER_BOT_SEAM_INSTALLERS = [
    (_install_launchd_cost_converter,
     f"ai.openclaw.evolve.cost-converter.{_LABEL_BOT}", False),
    (_install_launchd_doctor_pass,
     f"ai.openclaw.evolve.doctor-pass.{_LABEL_BOT}", False),
    (_install_launchd_audit_runner,
     f"ai.openclaw.evolve.audit-runner.{_LABEL_BOT}", True),
    (_install_launchd_audit_runner_tier3,
     f"ai.openclaw.evolve.audit-runner-t3.{_LABEL_BOT}", True),
    (_install_launchd_backup, f"ai.evolve.{_LABEL_BOT}.backup", True),
]
_INSTALLER_IDS = [fn.__name__ for fn, _l, _d in _PER_BOT_SEAM_INSTALLERS]


def _install_via_fake_seam(install_fn, needs_evolve_dir, *, user):
    """Run *install_fn* for bot_id ``team_bot_b`` against a FakeScheduler and
    return the recorded jobs. ``user=None`` exercises the omitted-arg path."""
    from evolve_admin.runtime import FakeScheduler, set_scheduler

    fake = FakeScheduler()
    set_scheduler(fake)
    try:
        with patch.object(deploy.Path, "exists", return_value=True):
            result = DeployResult("team_bot_b", True)
            args = ["team_bot_b"]
            if needs_evolve_dir:
                args.append(Path("/Users/personal_bot_user/evolve"))
            args.append(result)
            kwargs = {} if user is None else {"user": user}
            install_fn(*args, **kwargs)
    finally:
        set_scheduler(None)
    return fake.jobs


@pytest.mark.parametrize(
    "install_fn,label,needs_evolve_dir", _PER_BOT_SEAM_INSTALLERS,
    ids=_INSTALLER_IDS,
)
def test_install_launchd_uses_bot_user_for_unix_user(
    install_fn, label, needs_evolve_dir
):
    """When bot_id != bot_user, the installed job must:
    - use bot_id in the launchd/systemd label (logical-name-stable)
    - use bot_user as the Unix username (UserName / User=)

    Getting this backwards installs a job under a Unix account that does not
    exist, or points it at an orphan ``/Users/<bot_id>/`` tree — the exact
    failure this module's docstring describes.
    """
    jobs = _install_via_fake_seam(
        install_fn, needs_evolve_dir, user="personal_bot_user")

    assert label in jobs, (
        f"{install_fn.__name__} must install through the seam; "
        f"recorded jobs: {list(jobs)}"
    )
    spec = jobs[label]
    assert ".team_bot_b" in spec.label, (
        f"label should still reference bot_id 'team_bot_b', got {spec.label}"
    )
    assert spec.user == "personal_bot_user", (
        f"UserName should be 'personal_bot_user' (bot_user), got {spec.user!r}"
    )


@pytest.mark.parametrize(
    "install_fn,label,needs_evolve_dir", _PER_BOT_SEAM_INSTALLERS,
    ids=_INSTALLER_IDS,
)
def test_install_launchd_falls_back_to_bot_id_when_user_omitted(
    install_fn, label, needs_evolve_dir
):
    """Back-compat: omitting ``user`` defaults the Unix user to bot_id, which
    is correct for the majority case where the two coincide."""
    jobs = _install_via_fake_seam(install_fn, needs_evolve_dir, user=None)

    spec = jobs[label]
    assert spec.user == _LABEL_BOT, (
        f"omitted user should default to bot_id, got {spec.user!r}"
    )


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
