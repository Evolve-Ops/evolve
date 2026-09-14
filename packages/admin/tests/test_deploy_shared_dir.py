"""tests/test_deploy_shared_dir.py — proposal-lifecycle dir invariants.

The 2026-05-12 dismiss-failure bug was rooted in ``proposals/applied/``
being sticky-world-writable (``drwxrwxrwt``) with foreign-owned files
inside: ``evolve``'s atomic move (``os.replace(src, dest)``) hit
EACCES because the sticky bit lets only the file owner unlink. These
tests pin the deploy-time invariant: each lifecycle subdir under
``proposals/`` is created with mode ``0o777`` (no sticky) so any system
writer can transition a proposal regardless of which daemon wrote the
source file.

Mocks all subprocess calls — these tests don't need to actually
shell out for chown/chmod. The Python-level ``os.chmod`` work is what
sets the mode bits that matter here.
"""

from __future__ import annotations

import os
import stat
from unittest.mock import patch

import pytest


@pytest.fixture
def shared(tmp_path):
    """A tmp_path-rooted shared dir for deploy_shared_dir to populate."""
    return tmp_path / "evolve"


def _mode_perms(p):
    """Return the lower 9 bits + sticky/setuid/setgid as a 4-digit octal int."""
    return stat.S_IMODE(p.stat().st_mode)


def _run_deploy(shared):
    """Invoke deploy_shared_dir with subprocess fully mocked.

    Tests care about the Python-level mkdir/chmod calls; the subprocess
    calls (sudo chown / chmod fallbacks) are mocked so an inconvenient
    PermissionError on tmp_path doesn't make the test rely on `sudo`.
    """
    from evolve_admin import deploy

    # The fallback paths in deploy_shared_dir use subprocess via _run_sudo
    # or directly; they fire only when the Python-level call raises
    # PermissionError. tmp_path is writable so we expect them not to fire
    # — but mock subprocess.run anyway so an unexpected sudo call doesn't
    # actually escalate during the test.
    with patch.object(deploy.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        result = deploy.deploy_shared_dir(shared)
    return result


def test_deploy_shared_dir_creates_all_four_lifecycle_subdirs(shared):
    """pending, snoozed, applied, archived all exist after deploy_shared_dir."""
    _run_deploy(shared)
    for subdir in ("pending", "snoozed", "applied", "archived"):
        p = shared / "proposals" / subdir
        assert p.is_dir(), f"missing proposals/{subdir}"


def test_lifecycle_subdirs_have_no_sticky_bit(shared):
    """Regression for the dismiss EACCES bug: every lifecycle subdir
    must end up at mode 0o777 (no sticky). Sticky-bit world-writable
    (1777) is what trapped the cross-user unlink in production."""
    _run_deploy(shared)
    for subdir in ("pending", "snoozed", "applied", "archived"):
        p = shared / "proposals" / subdir
        mode = _mode_perms(p)
        assert mode & 0o1000 == 0, (
            f"proposals/{subdir} has sticky bit set (mode={oct(mode)}); "
            "this is the dismiss-EACCES regression — sticky lets only "
            "the file owner unlink, breaking cross-daemon proposal moves"
        )


def test_lifecycle_subdirs_world_writable(shared):
    """Mode 0o777 — readable, writable, executable by everyone so every
    producer (bot appliers, admin UI, verify daemon) can transition
    proposals through this dir without coordination."""
    _run_deploy(shared)
    for subdir in ("pending", "snoozed", "applied", "archived"):
        p = shared / "proposals" / subdir
        mode = _mode_perms(p)
        assert mode == 0o777, (
            f"proposals/{subdir} mode is {oct(mode)}, want 0o777"
        )


def test_lifecycle_subdirs_normalize_existing_sticky_dir(shared, tmp_path):
    """Idempotency: if a previous on-demand writer created the dir as
    1777 (sticky), deploy_shared_dir must strip the sticky bit on the
    next run rather than leave it.

    Simulates the exact production state we found on the mini.
    """
    # Pre-seed: create applied/ as 1777 sticky, the bad state.
    applied = shared / "proposals" / "applied"
    applied.mkdir(parents=True, exist_ok=True)
    os.chmod(applied, 0o1777)
    assert _mode_perms(applied) & 0o1000, "fixture setup failed"

    _run_deploy(shared)

    mode = _mode_perms(applied)
    assert mode == 0o777, (
        f"deploy_shared_dir did not normalize sticky bit; final mode={oct(mode)}"
    )


def test_lifecycle_subdirs_preserve_existing_files(shared):
    """Re-running deploy_shared_dir must not delete files in the
    proposal lifecycle subdirs. The mkdir(exist_ok=True) +
    chmod sequence should be content-preserving.
    """
    _run_deploy(shared)
    applied = shared / "proposals" / "applied"
    fake_proposal = applied / "deadbeef-fake.json"
    fake_proposal.write_text('{"id": "deadbeef-fake"}')
    assert fake_proposal.exists()

    # Second invocation — should be a no-op for existing content.
    _run_deploy(shared)
    assert fake_proposal.exists(), (
        "deploy_shared_dir destroyed existing proposal content on re-run"
    )
    assert fake_proposal.read_text() == '{"id": "deadbeef-fake"}'


# ── fix_shared_dir_permissions — multi-writer dir exclusion ──────────────────
#
# Regression for the 2026-06-07 pod_perms_drift cycle: per-bot deploys
# were doing `sudo chown -R <bot>:wheel /Users/Shared/evolve/proposals`,
# which made every redeploy of every bot reset the dir-owner contract
# the same ensure_pod_perms pass was trying to enforce. repo_puller's
# version-bump sweep called deploy_bot for each lagging bot, so the
# drift cycled multiple times per day on the canary pod. The fix is
# to keep this function's chown loop strictly per-bot (applications/
# {bot_id}) and route every pod-wide multi-writer dir through
# deploy_shared_dir + ensure_pod_perms instead.


@pytest.fixture
def shared_with_pod_dirs(shared):
    """Pre-create the pod-wide dirs that the 2026-06-07 bug would have
    chowned to the bot user. Mimics a shared dir after an initial
    deploy_shared_dir run."""
    (shared / "proposals").mkdir(parents=True, exist_ok=True)
    (shared / "scoreboard").mkdir(parents=True, exist_ok=True)
    (shared / "feedback").mkdir(parents=True, exist_ok=True)
    (shared / "applications" / "bot-a").mkdir(parents=True, exist_ok=True)
    return shared


def test_fix_shared_dir_permissions_does_not_chown_proposals(shared_with_pod_dirs):
    """fix_shared_dir_permissions must NOT touch ownership of
    {sharedDir}/proposals/. It's a pod-wide multi-writer dir owned by
    evolve and managed by deploy_shared_dir + ensure_pod_perms. The
    canonical bug shape that motivated this test: a `chown -R
    {bot_user}:wheel /Users/Shared/evolve/proposals` on every deploy_bot
    flipped dir-owner away from evolve, triggering the
    pod_perms_drift_monitor every hour."""
    from evolve_admin import deploy

    with patch.object(deploy.subprocess, "run") as mock_run, \
         patch.object(deploy, "_bot_user_for", return_value="bot-a"):
        mock_run.return_value.returncode = 0
        deploy.fix_shared_dir_permissions("bot-a", shared_with_pod_dirs)

    proposals_path = str(shared_with_pod_dirs / "proposals")
    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else call.kwargs.get("args", [])
        if not argv:
            continue
        # Any chown command whose path is /Users/Shared/.../proposals
        # (or a subpath of it) would re-trigger the 2026-06-07 drift class.
        if "chown" in argv[0] or (len(argv) > 1 and "chown" in argv[1]):
            for token in argv:
                assert not (
                    isinstance(token, str)
                    and token.startswith(proposals_path)
                ), (
                    f"fix_shared_dir_permissions ran a chown on "
                    f"{token!r} — the pod-wide proposals/ dir must not "
                    f"be chowned per-bot (pod_perms_drift_monitor will "
                    f"fire every hour). Full argv: {argv!r}"
                )


def test_fix_shared_dir_permissions_does_not_chown_pod_multi_writer_dirs(
    shared_with_pod_dirs,
):
    """Same invariant as above, generalized: NONE of the pod-wide
    multi-writer dirs (proposals/, scoreboard/, feedback/, alerts/,
    signals/) may appear as a chown target inside
    fix_shared_dir_permissions. Only `applications/{bot_id}` is allowed
    because that one IS per-bot by design."""
    from evolve_admin import deploy

    with patch.object(deploy.subprocess, "run") as mock_run, \
         patch.object(deploy, "_bot_user_for", return_value="bot-a"):
        mock_run.return_value.returncode = 0
        deploy.fix_shared_dir_permissions("bot-a", shared_with_pod_dirs)

    forbidden = [
        str(shared_with_pod_dirs / d)
        for d in ("proposals", "scoreboard", "feedback", "alerts", "signals")
    ]
    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else call.kwargs.get("args", [])
        if not argv:
            continue
        if "chown" in argv[0] or (len(argv) > 1 and "chown" in argv[1]):
            for token in argv:
                if not isinstance(token, str):
                    continue
                for f in forbidden:
                    assert not token.startswith(f), (
                        f"fix_shared_dir_permissions chowned a pod-wide "
                        f"multi-writer dir: {token!r}. These dirs are "
                        f"managed by deploy_shared_dir + ensure_pod_perms "
                        f"and must NOT be chowned per-bot. argv: {argv!r}"
                    )


def test_fix_shared_dir_permissions_still_chowns_per_bot_applications(
    shared_with_pod_dirs,
):
    """The legitimate per-bot subdir (applications/{bot_id}) must still
    get its bot:wheel chown — that's the design intent the loop is
    supposed to serve."""
    from evolve_admin import deploy

    with patch.object(deploy.subprocess, "run") as mock_run, \
         patch.object(deploy, "_bot_user_for", return_value="bot-a"):
        mock_run.return_value.returncode = 0
        deploy.fix_shared_dir_permissions("bot-a", shared_with_pod_dirs)

    apps_path = str(shared_with_pod_dirs / "applications" / "bot-a")
    saw_apps_chown = False
    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else call.kwargs.get("args", [])
        if not argv:
            continue
        # Match `sudo /usr/sbin/chown -R bot-a:wheel <apps_path>` exactly.
        if (
            "chown" in argv[0] or (len(argv) > 1 and "chown" in argv[1])
        ) and apps_path in argv:
            saw_apps_chown = True
            break
    assert saw_apps_chown, (
        f"fix_shared_dir_permissions did not chown {apps_path} — the "
        f"per-bot applications dir is the one legitimate target of this "
        f"loop and must still be chowned to bot:wheel"
    )


def test_write_install_json_chowns_to_evolve_on_permission_fallback(shared):
    """Regression (2026-06-23 Linux upgrade incident): when the evolve daemon
    can't write install.json directly (``setup --fresh`` run as root leaves it
    root-owned), ``write_install_json`` must not stop at the sudo /bin/cp — it
    must also chown install.json to the evolve service user so the *fast*
    ``write_text()`` path wins on the next run. The missing chown was the
    failure: install.json stayed root-owned, every subsequent evolve write fell
    back to a sudo cp, and the upgrade failed ("0 of N bot(s) upgraded").

    NOTE on the kept design: #3141 implements this inside ``write_install_json``'s
    ``PermissionError`` fallback (stage → sudo cp → sudo chmod → sudo chown
    evolve), NOT inside ``deploy_shared_dir`` — the latter was a parallel earlier
    design (closed #3139). This pins #3141's actual code path. The on-Linux
    real-sudo / §10a-grant behaviour is covered end-to-end by
    ``tests/e2e_linux/test_ubuntu_e2e.py::test_step6c2_upgrade_reclamp_keeps_evolve_access_and_records_install``;
    this is the fast, platform-agnostic branch test that runs in the macOS admin
    shard."""
    from evolve_admin import deploy

    # Force the PermissionError branch: a read-only install.json makes the fast
    # ``path.write_text()`` raise (an un-writable same-owner file reproduces the
    # same EACCES the pod hits on a root-owned file, without a second uid).
    shared.mkdir(parents=True, exist_ok=True)
    install_path = shared / "install.json"
    install_path.write_text("{}")
    os.chmod(install_path, 0o444)

    with patch.object(deploy.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        deploy.write_install_json(shared, network_id="test-net", bots=["bot-a"])

    install_str = str(install_path)
    saw = False
    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else call.kwargs.get("args", [])
        if not argv:
            continue
        is_chown = "chown" in argv[0] or (len(argv) > 1 and "chown" in argv[1])
        if is_chown and install_str in argv:
            # owner spec must target the evolve service user
            assert any(
                isinstance(a, str) and a.startswith(f"{deploy.EVOLVE_SERVICE_USER}:")
                for a in argv
            ), f"install.json chown does not target evolve: {argv!r}"
            saw = True
            break
    assert saw, (
        f"write_install_json did not chown {install_str} to evolve on the "
        f"PermissionError fallback — the 2026-06-23 upgrade-path install.json "
        f"EACCES regression. argv seen: "
        f"{[c.args[0] if c.args else c.kwargs.get('args') for c in mock_run.call_args_list]}"
    )
