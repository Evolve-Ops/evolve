"""test_accept_drift_acl.py — `set_evolve_read_acl` grants evolve write on
`workspace/evolve-backup/`.

Regression guard for the 2026-05-21 accept_drift failure across six bots.
Diagnosis: docs/diagnosis-accept-drift-regression-2026-05-21.md.

Root cause was that ``deploy_bot`` materialized
``/Users/<bot>/.openclaw/workspace/evolve-backup/`` as root, so the
admin-server (running as evolve) couldn't subsequently write the new
baseline file there. The fix lives in ``set_evolve_read_acl`` and grants
evolve full read+write ACL on the ``evolve-backup/`` directory, with
``-R`` to backfill the ACE onto existing root-owned files.

Since the W4a Perms seam, the ACL ritual is emitted by
``runtime.perms.MacOSPerms`` rather than open-coded subprocess calls, so
these tests assert two layers:

- the deploy flow still routes evolve-backup/ through the seam's
  recursive write grant with the canonical perm set, and
- the argv the seam emits for it is byte-identical to the pre-seam
  ``sudo /bin/chmod +a`` / ``-R +a`` pair (sudoers matches on these
  exact shapes — drift here fails silently in production).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from evolve_admin import deploy
from evolve_admin.runtime import MacOSPerms, set_perms

# The canonical evolve-write contract for workspace subdirs — must keep
# every verb the 2026-05-21 diagnosis called out.
_REQUIRED_VERBS = ("write", "add_file", "delete", "file_inherit", "directory_inherit")


@pytest.fixture(autouse=True)
def _reset_perms():
    yield
    set_perms(None)


def test_set_evolve_read_acl_grants_write_on_evolve_backup_dir():
    """``set_evolve_read_acl`` must include an ACL block granting evolve
    write on ``workspace/evolve-backup/``. Without this, the
    2026-05-21 accept_drift regression returns the next time
    ``deploy_bot`` materializes evolve-backup/ as root.
    """
    # The bot-private read contract — incl. the evolve-backup write grant — now
    # lives in the _apply_openclaw_read_contract helper that set_evolve_read_acl
    # (and the _add_acl drift repair) both route through (#3198 sibling-call-site
    # consolidation). Inspect the helper, which is what set_evolve_read_acl reaches.
    src = inspect.getsource(deploy._apply_openclaw_read_contract)
    assert "workspace/evolve-backup" in src, (
        "the .openclaw read-contract helper no longer references "
        "workspace/evolve-backup. The 2026-05-21 accept_drift regression "
        "(diagnosis-accept-drift-regression-2026-05-21.md) will recur."
    )
    # The block must ride the seam's recursive write grant (dir ACE +
    # -R backfill) with the canonical workspace write perm set.
    idx = src.find("workspace/evolve-backup")
    window = src[idx: idx + 1500]
    assert "grant_write_recursive" in window, (
        "evolve-backup block no longer uses the seam's recursive write "
        "grant — pre-existing root-owned files won't get the backfilled "
        "ACE and the six bots from the 2026-05-21 regression stay broken."
    )
    assert "EVOLVE_WS_WRITE_ACL_PERMS" in window, (
        "evolve-backup block doesn't use the canonical workspace write "
        "perm constant."
    )


@pytest.mark.parametrize("verb", _REQUIRED_VERBS)
def test_workspace_write_acl_constant_keeps_required_verbs(verb):
    """The perm constant must keep every verb the diagnosis requires:
    ``write`` (the failing op), ``add_file`` (create new baseline files),
    ``delete`` (replace via write+truncate), and both inherit flags
    (grants propagate to files created inside evolve-backup/)."""
    perms = {p.strip() for p in deploy.EVOLVE_WS_WRITE_ACL_PERMS.split(",")}
    assert verb in perms, (
        f"EVOLVE_WS_WRITE_ACL_PERMS lost {verb!r} — the 2026-05-21 "
        "accept_drift fix no longer applies in full."
    )


def test_set_evolve_read_acl_emits_presseam_chmod_argv_for_evolve_backup(monkeypatch):
    """Behavioral parity pin: with a recorded-runner MacOSPerms injected
    through the seam, running set_evolve_read_acl emits the EXACT
    pre-seam command pair for evolve-backup —

        sudo /bin/chmod +a "evolve allow <perms>" <dir>
        sudo /bin/chmod -R +a "evolve allow <perms>" <dir>

    Sudoers grants match on these shapes; any re-render breaks silently.
    """
    captured: list[list[str]] = []

    def _seam_run(args, **kwargs):
        captured.append(list(args))
        class _R:
            returncode = 0
            stderr = ""
        return _R()

    set_perms(MacOSPerms(runner=_seam_run))

    # Residual non-ACL subprocess calls in the flow (chmod 700, mkdir,
    # chown) — record only; nothing real runs.
    deploy_calls: list[list[str]] = []

    def _deploy_run(args, **kwargs):
        deploy_calls.append(list(args))
        class _R:
            returncode = 0
            stderr = ""
        return _R()

    monkeypatch.setattr(deploy.subprocess, "run", _deploy_run)
    # Pin the resolved bot user so the function doesn't touch /Users.
    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id, *a, **kw: "team_bot_a")
    # Force every existence gate True so the evolve-backup branch runs.
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "iterdir", lambda self: iter([]))
    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter([]))

    deploy.set_evolve_read_acl("team_bot_a")

    backup_calls = [
        cmd for cmd in captured
        if any("workspace/evolve-backup" in str(arg) for arg in cmd)
    ]
    assert backup_calls, (
        "No seam command targeted workspace/evolve-backup — the branch "
        "was skipped or the path no longer matches."
    )
    expected_ace = f"evolve allow {deploy.EVOLVE_WS_WRITE_ACL_PERMS}"
    backup_dir = next(
        arg for cmd in backup_calls for arg in cmd
        if "workspace/evolve-backup" in str(arg)
    )
    assert backup_calls == [
        ["sudo", "/bin/chmod", "+a", expected_ace, backup_dir],
        ["sudo", "/bin/chmod", "-R", "+a", expected_ace, backup_dir],
    ], (
        "evolve-backup argv drifted from the pre-seam chmod +a / -R +a "
        f"pair: {backup_calls!r}"
    )
    # The deploy module itself must no longer emit ACL commands — all
    # +a / -N rides the seam.
    assert not any("+a" in c or "-N" in c for c in deploy_calls), (
        f"deploy.py still open-codes ACL chmod calls: {deploy_calls!r}"
    )
