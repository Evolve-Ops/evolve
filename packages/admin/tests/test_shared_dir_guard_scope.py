"""The shared-dir guard is active here, and its e2e exemption is narrow.

The guard in ``conftest.py`` blocks writes under either platform's canonical
shared dir. ``tests/e2e_linux/`` is exempt because that suite's whole purpose
is a live deploy to the real path on a throwaway runner (see the comment above
``_E2E_EXEMPT_DIR``). An exemption with no test is one that quietly widens, and
widening it would re-open the class the guard was added to close — so pin both
directions: still blocking here, exempt only for that directory.

The Linux root is used for the "still blocking" assertion on purpose: it does
not exist on a macOS dev box, so a regression that lets the write through
cannot land a file in the developer's real pod at /Users/Shared/evolve.
"""

from __future__ import annotations

import os
import sys

import pytest

# The guard lives in tests/conftest.py, which pytest registers as the plugin
# module ``tests.conftest``. Import it from sys.modules rather than by path:
# re-importing the file would execute it again and install a SECOND audit hook.
_conftest = sys.modules["tests.conftest"]
_E2E_EXEMPT_DIR = _conftest._E2E_EXEMPT_DIR
_TESTS_DIR = _conftest._TESTS_DIR
_current_test = _conftest._current_test
_current_test_is_exempt = _conftest._current_test_is_exempt
# The guard's own exception type. It is deliberately NOT `PermissionError`: that is
# the branch production writers catch to retry the write under sudo, which turned a
# blocked write into a root-owned one (PR #4229). Pin the specific type, not `OSError`
# — a test that accepts any OSError would pass against a guard that stopped guarding.
_SharedDirGuardError = _conftest._SharedDirGuardError


def test_guard_is_active_for_an_ordinary_test():
    """A write under a guarded root still raises from the audit hook."""
    assert not _current_test_is_exempt()
    with pytest.raises(_SharedDirGuardError, match=r"\[shared-dir-guard\]"):
        open("/var/lib/evolve/should-never-be-created.tmp", "w")

    # The hook RECORDS every blocked write, and the autouse teardown fixture
    # fails any test that has a record. This test provoked one on purpose to
    # prove the hook fires, so drop its own record — otherwise the guard
    # correctly fails the test that exists to show the guard works.
    _conftest._shared_dir_writes.pop(_current_test["nodeid"], None)


def test_this_file_is_not_exempt():
    assert _current_test["path"].endswith("test_shared_dir_guard_scope.py")
    assert not _current_test_is_exempt()


@pytest.mark.parametrize(
    "path, exempt",
    [
        (str(_TESTS_DIR / "e2e_linux" / "test_ubuntu_e2e.py"), True),
        (str(_TESTS_DIR / "e2e_linux" / "nested" / "test_x.py"), True),
        # Scoping: the exemption is a directory prefix, not a substring match.
        (str(_TESTS_DIR / "test_e2e_linux_helpers.py"), False),
        (str(_TESTS_DIR / "e2e_linux_extra" / "test_x.py"), False),
        (str(_TESTS_DIR / "test_deploy.py"), False),
        ("", False),
    ],
)
def test_exemption_is_scoped_to_the_e2e_directory(monkeypatch, path, exempt):
    monkeypatch.setitem(_current_test, "path", path)
    assert _current_test_is_exempt() is exempt


def test_exempt_dir_points_at_the_real_suite_and_ends_with_a_separator():
    """A prefix without the trailing separator would also match e2e_linux_extra."""
    assert _E2E_EXEMPT_DIR.endswith(os.sep)
    assert (_TESTS_DIR / "e2e_linux" / "test_ubuntu_e2e.py").is_file()


def test_exempt_context_lets_the_write_through_to_the_os(monkeypatch):
    """With the exemption active the hook steps aside — the OS answers instead.

    Discriminates the two outcomes without ever creating a file: the target's
    PARENT directory does not exist under either guarded root, so

      guard active  -> PermissionError raised by the audit hook, before the OS
      guard exempt  -> FileNotFoundError raised by the OS (no such directory)

    A regression that stopped honouring the exemption would surface here as a
    `_SharedDirGuardError`, and one that let the guard be bypassed generally
    would be caught by test_guard_is_active_for_an_ordinary_test above.
    """
    target = "/var/lib/evolve/no-such-dir-4f3a/should-never-be-created.tmp"

    monkeypatch.setitem(_current_test, "path", str(_TESTS_DIR / "e2e_linux" / "test_ubuntu_e2e.py"))
    assert _current_test_is_exempt()
    with pytest.raises(FileNotFoundError):
        open(target, "w")

    monkeypatch.setitem(_current_test, "path", str(_TESTS_DIR / "test_deploy.py"))
    assert not _current_test_is_exempt()
    with pytest.raises(_SharedDirGuardError, match=r"\[shared-dir-guard\]"):
        open(target, "w")
    _conftest._shared_dir_writes.pop(_current_test["nodeid"], None)
