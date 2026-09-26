"""tests/test_real_shared_dir_guard_no_sudo_escalation.py

The real-shared-dir guard (end of conftest.py) blocks Python writes under
the pod's canonical shared dir. Two properties make that block actually
protect the host rather than redirect the damage, and neither is visible
from the guard's own failure message -- so they are pinned here.

1. The exception is NOT a ``PermissionError``. A pile of production
   writers catch exactly that and retry the write under ``sudo``
   (``config.save_network``, ``evolve_config._patch_network_json``,
   ``deploy.write_install_json``, ``deploy._create_dir_with_mode``, ...).
   On a CI runner sudo is passwordless, so a PermissionError from the
   guard used to hand the write to root and succeed -- a shard run ended
   with a root-owned /Users/Shared/evolve/network.json on the runner,
   which then made unrelated tests trip the guard. It stays an OSError so
   the ``except OSError`` paths the 52 baselined files rely on keep
   swallowing it.

2. A child process that would MUTATE a guarded root is blocked before it
   execs. ``sys.addaudithook`` is per-interpreter: once a child starts,
   nothing here can see it. Reads through a child stay allowed -- the
   guard has never guarded reads.

These reach the LIVE conftest out of ``sys.modules``. Importing the file
would install a SECOND audit hook, which cannot be removed for the life
of the process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _live_conftest():
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f and Path(f).resolve() == _CONFTEST and hasattr(mod, "_ROOTS"):
            return mod
    pytest.skip("admin tests conftest not importable from sys.modules")


@pytest.fixture
def guard():
    """The live guard module, with this test's recorded blocks discarded.

    The hook records every block against the running nodeid, and the
    autouse fixture fails any non-baselined test that recorded one. This
    file trips the guard ON PURPOSE, so it clears its own record -- the
    alternative would be adding this file to the baseline, which is
    exactly the move the baseline forbids.
    """
    mod = _live_conftest()
    if mod._GUARD_OFF:
        pytest.skip("guard disabled via EVOLVE_TESTS_ALLOW_REAL_SHARED_DIR")
    yield mod
    mod._shared_dir_writes.pop(mod._current_test["nodeid"], None)


def test_a_blocked_write_is_not_a_permission_error(guard):
    """The crux: PermissionError is the "escalate to sudo" signal."""
    target = os.path.join(guard._ROOTS[0], "__guard_probe__")
    with pytest.raises(OSError) as exc:
        open(target, "w")
    assert not isinstance(exc.value, PermissionError), (
        "the guard raised PermissionError -- production fallbacks read that "
        "as 'retry under sudo' and will write the host as root"
    )
    assert isinstance(exc.value, guard._SharedDirGuardError)
    assert not Path(target).exists()


def test_the_block_is_still_recorded(guard):
    """Type change only. The fixture's evidence trail must survive it."""
    target = os.path.join(guard._ROOTS[0], "__guard_probe__")
    with pytest.raises(OSError):
        open(target, "w")
    recorded = guard._shared_dir_writes.get(guard._current_test["nodeid"], set())
    assert os.path.realpath(target) in recorded


def test_a_mutating_child_process_is_blocked_before_it_runs(guard):
    """sudo mkdir of the guarded root -- how the root really got created."""
    target = os.path.join(guard._ROOTS[0], "__guard_probe_dir__")
    with pytest.raises(OSError) as exc:
        subprocess.run(["sudo", "/bin/mkdir", "-p", target], capture_output=True)
    assert isinstance(exc.value, guard._SharedDirGuardError)
    assert not isinstance(exc.value, PermissionError)
    assert not Path(target).exists(), "the child ran anyway"


def test_a_child_that_mutates_nothing_guarded_is_untouched(guard):
    """A mutating command aimed elsewhere must not be blocked."""
    subprocess.run(["/bin/mkdir", "-p", "/dev/null/nope"], capture_output=True)


@pytest.mark.parametrize("argv", [
    ["sudo", "/bin/cat", "/Users/Shared/evolve/network.json"],
    ["git", "-C", "/Users/Shared/evolve-repo", "remote", "get-url", "origin"],
    ["/usr/bin/stat", "/Users/Shared/evolve"],
])
def test_reads_through_a_child_stay_allowed(guard, argv):
    """Scope: ~6k reads of the real shared dir exist and are not this
    guard's business. Classified without running anything."""
    assert guard._mutating_argv(argv) is None


@pytest.mark.parametrize("argv", [
    ["sudo", "/bin/cp", "/tmp/x", "/Users/Shared/evolve/network.json"],
    ["sudo", "/bin/chmod", "755", "/Users/Shared/evolve/logs"],
    ["sudo", "/usr/sbin/chown", "-R", "evolve", "/Users/Shared/evolve"],
])
def test_the_mutating_commands_that_actually_polluted_ci_are_classified(guard, argv):
    """The three shapes a shard run really executed on a CI runner."""
    assert guard._mutating_argv(argv) == argv


def test_a_shell_one_liner_is_tokenized(guard):
    """``shell=True`` hands the hook ["/bin/sh", "-c", "mkdir -p X"] -- the
    verb and the path are inside ONE element, so the argv has to be split or
    the whole shell surface is a hole."""
    argv = ["/bin/sh", "-c", "mkdir -p /Users/Shared/evolve/x"]
    assert guard._mutating_argv(argv) == [
        "/bin/sh", "-c", "mkdir", "-p", "/Users/Shared/evolve/x",
    ]


def test_a_shell_one_liner_is_blocked_end_to_end(guard):
    target = os.path.join(guard._ROOTS[0], "__guard_probe_shell__")
    with pytest.raises(OSError) as exc:
        subprocess.run(f"mkdir -p {target}", shell=True, capture_output=True)
    assert isinstance(exc.value, guard._SharedDirGuardError)
    assert not Path(target).exists()


# ── the e2e exemption applies to BOTH branches of the hook ───────────────────
#
# tests/e2e_linux/ is exempt because it deploys a real pod on a throwaway CI
# VM, and it does that almost entirely through sudo children -- the very first
# pod-state step is `sudo /bin/mkdir -p <shared>/logs`. The subprocess branch
# used to return before the exemption check, so every one of those children was
# blocked and the Linux e2e job died on its first write. These two call the
# hook DIRECTLY: an exempt call that is genuinely not blocked would otherwise
# execute the child, and nothing here may mutate a developer's real pod.


def _popen_event(argv: list[str]) -> tuple:
    """The audit payload CPython passes for subprocess.Popen."""
    return (argv[0].encode(), argv, None, None)


def test_the_e2e_exemption_covers_a_mutating_child(guard, monkeypatch):
    target = os.path.join(guard._ROOTS[0], "logs")
    argv = ["sudo", "/bin/mkdir", "-p", target]
    monkeypatch.setitem(
        guard._current_test, "path", guard._E2E_EXEMPT_DIR + "test_ubuntu_e2e.py"
    )
    # No raise: the exemption is the whole reason tests/e2e_linux/ can deploy.
    guard._shared_dir_audit_hook("subprocess.Popen", _popen_event(argv))
    assert target not in guard._shared_dir_writes.get(
        guard._current_test["nodeid"], set()
    ), "an exempt child must not be recorded as an offence either"


def test_a_mutating_child_outside_the_exempt_dir_is_still_blocked(guard, monkeypatch):
    """The other half: the exemption is scoped by PATH, so a test anywhere
    else that tries the same child is blocked exactly as before."""
    target = os.path.join(guard._ROOTS[0], "logs")
    argv = ["sudo", "/bin/mkdir", "-p", target]
    monkeypatch.setitem(guard._current_test, "path", str(Path(__file__).resolve()))
    with pytest.raises(OSError) as exc:
        guard._shared_dir_audit_hook("subprocess.Popen", _popen_event(argv))
    assert isinstance(exc.value, guard._SharedDirGuardError)
    assert not isinstance(exc.value, PermissionError)
