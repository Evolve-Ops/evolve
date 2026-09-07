"""test_bot_shared_subdirs.py — the per-bot shared-store subdir contract.

The 2026-09-03 incident these tests exist for: ``exec-failures/`` was added
to ``deploy``'s creation list (#3923) and not to the sudoers renderer's grant
list. Its ``chown`` and ``chmod +a`` then had no NOPASSWD grant, both ran
``check=False`` with the output discarded, and eight bots on the reference pod
carried an ``evolve``-owned ``0755`` ledger dir their own gateway could not
append to. Nothing logged it; the armed ``ExecFailureAbsorber`` refused to
absorb (record-or-deliver) and ``exec_failure_monitor`` read an empty ledger,
so the two silences stacked and no Signal ever fired.

Three proofs, one per leg of the fix:

1. **Grant coverage** — every registry entry's four privileged shapes appear
   in BOTH rendered goldens. This is the test that would have caught the bug;
   it fails the moment a subdir exists with no grant behind it.
2. **Failures are on the record** — a chown that fails makes
   ``ensure_bot_subdir`` return False and log a warning, instead of the
   pre-fix silent ``check=False``.
3. **Self-heal** — ``check_bot_shared_subdirs`` reports an already-drifted
   dir (the exact reference-pod state: evolve-owned, no ACE) and its ``apply``
   repairs it. ``ensure_pod_perms`` runs this at deploy time and
   ``pod_perms_drift_monitor`` hourly, so the drift becomes a Signal.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import setup_wizard  # noqa: E402
from evolve_admin.bot_shared_subdirs import (  # noqa: E402
    BOT_SHARED_SUBDIR_READ_ACL_PERMS,
    BOT_SHARED_SUBDIRS,
    LINUX_READ_ACL_ENTRY,
    check_bot_shared_subdirs,
    create_bot_subdirs,
    ensure_bot_subdir,
)
from evolve_admin.runtime import FakePerms, set_perms  # noqa: E402

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "sudoers_golden"
MAC_OC_PATH = "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw"
LINUX_OC_PATH = "/usr/lib/node_modules/openclaw/bin/openclaw"


@pytest.fixture(autouse=True)
def _reset_perms():
    yield
    set_perms(None)


# ── 1. every registered subdir is granted, on both platforms ─────────────────


def _rendered(monkeypatch, profile, oc_path: "str | None") -> str:
    from platform_profile import set_profile

    set_profile(profile)
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path", lambda: oc_path)
    content = setup_wizard._render_evolve_sudoers()
    assert content is not None
    return content


def _grant_lines(text: str) -> "list[str]":
    return [ln for ln in text.splitlines() if ln.startswith("evolve ALL=")]


@pytest.mark.parametrize("plat", ["macos", "linux"])
def test_every_registered_subdir_has_its_privileged_grants(monkeypatch, plat):
    """The class fix: registry entry ⇒ grants, with no second list to forget.

    ``ensure_bot_subdir`` runs up to four privileged shapes per subdir —
    ``mkdir -p``, ``chmod <mode>``, ``chown``, and (when the entry asks for the
    evolve read ACE) the platform's ACL verb. sudoers matches on exact argv, so
    a missing shape is not a degraded grant but a dead one.
    """
    from platform_profile import LINUX, MACOS, get_profile, set_profile

    try:
        profile = MACOS if plat == "macos" else LINUX
        text = _rendered(monkeypatch, profile,
                         MAC_OC_PATH if plat == "macos" else LINUX_OC_PATH)
        lines = set(_grant_lines(text))
        c = get_profile().commands
        shared = profile.shared_dir_default

        missing: list[str] = []
        for entry in BOT_SHARED_SUBDIRS:
            target = f"{shared}/{entry.glob}"
            wanted = [
                f'{c["mkdir"]} -p {target}',
                f'{c["chmod"]} {entry.mode_token} {target}',
                f'{c["chown"]} * {target}',
            ]
            if entry.evolve_read_acl:
                if plat == "macos":
                    wanted.append(f'{c["chmod"]} +a * {target}')
                else:
                    spec = LINUX_READ_ACL_ENTRY.replace(":", "\\:")
                    wanted.append(f'{c["setfacl"]} -m {spec} {target}')
                    wanted.append(f'{c["setfacl"]} -d -m {spec} {target}')
            for argv in wanted:
                if f"evolve ALL=(root) NOPASSWD: {argv}" not in lines:
                    missing.append(argv)
        assert not missing, (
            "per-bot shared subdirs with no NOPASSWD grant — sudo runs these "
            "non-interactively, so each one dies 'a terminal is required' and "
            "the subdir silently keeps the wrong owner/ACL (the 2026-09-03 "
            f"exec-failures incident):\n  " + "\n  ".join(missing)
        )
    finally:
        set_profile(None)


def test_exec_failures_and_app_runs_are_registered():
    """Pin the two entries the incident was about — a registry that quietly
    lost them would make the coverage test above vacuously pass."""
    globs = {e.glob for e in BOT_SHARED_SUBDIRS}
    assert "*/exec-failures" in globs
    assert "*/app-runs" in globs
    ledger = next(e for e in BOT_SHARED_SUBDIRS if e.glob == "*/exec-failures")
    # exec_failure_monitor reads this as evolve; the plugin writes it at
    # umask 077, so without the inheritable ACE the monitor sees nothing.
    assert ledger.evolve_read_acl is True
    assert ledger.mode == 0o755


def test_registry_globs_name_exactly_one_bot_level():
    """visudo globs do not cross '/', so one ``*`` pins exactly the per-bot
    level. Two would make a grant reach a level nobody intended."""
    for entry in BOT_SHARED_SUBDIRS:
        assert entry.glob.count("*") == 1, entry.glob
        assert not entry.glob.startswith("/"), entry.glob


def test_path_substitutes_the_bot_id(tmp_path):
    ledger = next(e for e in BOT_SHARED_SUBDIRS if e.glob == "*/exec-failures")
    metrics = next(e for e in BOT_SHARED_SUBDIRS if e.glob == "metrics/*")
    assert ledger.path(tmp_path, "examplebot") == tmp_path / "examplebot" / "exec-failures"
    assert metrics.path(tmp_path, "examplebot") == tmp_path / "metrics" / "examplebot"


# ── 2. creation: bot-owned + evolve read ACE, and failures are reported ──────


class _Runs:
    """Records argv and answers each call from a caller-supplied verdict."""

    def __init__(self, fail_substr: "str | None" = None):
        self.calls: list[list[str]] = []
        self.fail_substr = fail_substr

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        rc = 1 if self.fail_substr and self.fail_substr in " ".join(argv) else 0
        return subprocess.CompletedProcess(argv, rc, "", "sudo: a password is required")


def test_create_lands_bot_owned_with_the_evolve_read_ace(tmp_path, monkeypatch):
    """The contract every entry must end in: chowned to the bot user, and
    (when the entry asks) carrying the inheritable evolve read ACE."""
    fake = FakePerms()
    set_perms(fake)
    runs = _Runs()
    monkeypatch.setattr("evolve_admin.bot_shared_subdirs._run", runs)

    create_bot_subdirs(tmp_path, "examplebot", "examplebot", "wheel", "/usr/sbin/chown")

    for entry in BOT_SHARED_SUBDIRS:
        target = entry.path(tmp_path, "examplebot")
        assert target.is_dir(), f"{entry.glob} was not created"
        assert ["sudo", "/usr/sbin/chown", "examplebot:wheel", str(target)] in runs.calls, (
            f"{entry.glob} was never chowned to the bot user"
        )
        granted = fake.acl_user_effective(
            target, "evolve", BOT_SHARED_SUBDIR_READ_ACL_PERMS)
        assert granted is entry.evolve_read_acl, (
            f"{entry.glob}: evolve read ACE presence does not match the registry"
        )


def test_chown_failure_is_reported_not_swallowed(tmp_path, monkeypatch, caplog):
    """The heart of the incident: the pre-fix helper ran chown under
    ``check=False`` and discarded the output, so a denied grant left a wrong
    directory and no record anywhere. Now it returns False and logs."""
    set_perms(FakePerms())
    runs = _Runs(fail_substr="chown")
    monkeypatch.setattr("evolve_admin.bot_shared_subdirs._run", runs)
    ledger = next(e for e in BOT_SHARED_SUBDIRS if e.glob == "*/exec-failures")
    target = ledger.path(tmp_path, "examplebot")

    with caplog.at_level(logging.WARNING, logger="evolve_admin.bot_shared_subdirs"):
        ok = ensure_bot_subdir(ledger, target, "examplebot", "wheel", "/usr/sbin/chown")

    assert ok is False
    assert "exec-failures" in caplog.text and "chown failed" in caplog.text, caplog.text
    assert "refresh-sudoers" in caplog.text, (
        "the warning must name the operator's next step — a missing grant is "
        "the cause this failure has had every time it has occurred"
    )


def test_creation_continues_past_one_failing_subdir(tmp_path, monkeypatch):
    """A pod that cannot chown one leaf must still get the other seven — a
    deploy that aborted here would trade a silent subdir for a silent bot."""
    set_perms(FakePerms())
    monkeypatch.setattr("evolve_admin.bot_shared_subdirs._run",
                        _Runs(fail_substr="exec-failures"))

    create_bot_subdirs(tmp_path, "examplebot", "examplebot", "wheel", "/usr/sbin/chown")

    for entry in BOT_SHARED_SUBDIRS:
        assert entry.path(tmp_path, "examplebot").is_dir()


# ── 3. self-heal on an already-drifted pod ──────────────────────────────────


def test_drift_check_flags_the_reference_pod_state(tmp_path, monkeypatch):
    """Reproduce the observed state — dir present, evolve-owned, no ACE — and
    assert it reads as drift with a runnable fix.

    Ownership is faked rather than really chowned: the test suite runs as an
    unprivileged user and the point is the comparison, not the syscall.
    """
    set_perms(FakePerms())  # nothing has been granted → every ACE is missing
    monkeypatch.setattr("evolve_admin.bot_shared_subdirs._owner_of",
                        lambda p: "evolve")
    for entry in BOT_SHARED_SUBDIRS:
        entry.path(tmp_path, "examplebot").mkdir(parents=True)

    checks = check_bot_shared_subdirs(tmp_path, "examplebot", "examplebot")

    by_target = {c.target: c for c in checks}
    ledger = next(e for e in BOT_SHARED_SUBDIRS if e.glob == "*/exec-failures")
    drift = by_target[str(ledger.path(tmp_path, "examplebot"))]
    assert drift.ok is False
    assert "owner is evolve, expected examplebot" in drift.detail
    assert "evolve read ACE missing" in drift.detail
    assert drift.apply is not None
    assert "refresh-sudoers" in drift.fix_description


def test_drift_check_passes_a_correct_pod(tmp_path, monkeypatch):
    """Idempotency: a pod that already matches produces zero drift, so the
    hourly monitor does not emit a Signal on every pass."""
    fake = FakePerms()
    set_perms(fake)
    monkeypatch.setattr("evolve_admin.bot_shared_subdirs._owner_of",
                        lambda p: "examplebot")
    for entry in BOT_SHARED_SUBDIRS:
        target = entry.path(tmp_path, "examplebot")
        target.mkdir(parents=True)
        if entry.evolve_read_acl:
            fake.seed_acl(target, "evolve", BOT_SHARED_SUBDIR_READ_ACL_PERMS)

    checks = check_bot_shared_subdirs(tmp_path, "examplebot", "examplebot")

    assert [c for c in checks if not c.ok] == []


def test_drift_check_skips_a_bot_that_was_never_deployed(tmp_path):
    """No directory is not drift — it is a bot whose first deploy has not run."""
    set_perms(FakePerms())
    checks = check_bot_shared_subdirs(tmp_path, "newbot", "newbot")
    assert all(c.ok for c in checks)
    assert all("not created yet" in c.detail for c in checks)


def test_drift_apply_repairs_the_dir(tmp_path, monkeypatch):
    """The apply leg actually converges — the reason this is a self-heal and
    not just a report."""
    fake = FakePerms()
    set_perms(fake)
    runs = _Runs()
    monkeypatch.setattr("evolve_admin.bot_shared_subdirs._run", runs)
    owner = {"v": "evolve"}
    monkeypatch.setattr("evolve_admin.bot_shared_subdirs._owner_of",
                        lambda p: owner["v"])
    ledger = next(e for e in BOT_SHARED_SUBDIRS if e.glob == "*/exec-failures")
    target = ledger.path(tmp_path, "examplebot")
    target.mkdir(parents=True)

    drift = next(c for c in check_bot_shared_subdirs(tmp_path, "examplebot", "examplebot")
                 if c.target == str(target))
    assert drift.apply() is True
    owner["v"] = "examplebot"  # the chown the apply issued

    after = next(c for c in check_bot_shared_subdirs(tmp_path, "examplebot", "examplebot")
                 if c.target == str(target))
    assert after.ok is True


# ── 4. the privileged steps refuse a redirected path ────────────────────────


def test_planted_symlink_gets_no_chown_and_no_ace(tmp_path, monkeypatch):
    """``{sharedDir}/{botId}/`` carries a bot-user ``add_file`` ACE and a
    symlink is a file, so the bot can plant one at a subdir name it knows the
    next deploy will chown. Nothing privileged may follow it."""
    set_perms(FakePerms())
    runs = _Runs()
    monkeypatch.setattr("evolve_admin.bot_shared_subdirs._run", runs)
    ledger = next(e for e in BOT_SHARED_SUBDIRS if e.glob == "*/exec-failures")
    victim = tmp_path / "victim"
    victim.mkdir()
    target = ledger.path(tmp_path, "examplebot")
    target.parent.mkdir(parents=True)
    target.symlink_to(victim)

    ok = ensure_bot_subdir(ledger, target, "examplebot", "wheel", "/usr/sbin/chown")

    assert ok is False
    assert runs.calls == [], f"a privileged step followed the link: {runs.calls}"


def test_drift_check_refuses_to_auto_repair_a_redirected_path(tmp_path, monkeypatch):
    """Reported as drift so an operator sees it, with no ``apply`` — the
    self-heal must never be the thing that lands a chown on a chosen path."""
    set_perms(FakePerms())
    monkeypatch.setattr("evolve_admin.bot_shared_subdirs._owner_of",
                        lambda p: "evolve")
    ledger = next(e for e in BOT_SHARED_SUBDIRS if e.glob == "*/exec-failures")
    victim = tmp_path / "victim"
    victim.mkdir()
    target = ledger.path(tmp_path, "examplebot")
    target.parent.mkdir(parents=True)
    target.symlink_to(victim)

    check = next(c for c in check_bot_shared_subdirs(tmp_path, "examplebot", "examplebot")
                 if c.target == str(target))

    assert check.ok is False
    assert check.apply is None
    assert "symlink" in check.detail
