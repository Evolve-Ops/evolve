"""Cross-OS path resolution for the audit_poller outbox helpers.

Regression guard for the Linux-VPS drain stall (2026-06-28): the poller's
outbox path helpers hardcoded ``/Users/{bot}`` (the macOS home root), so on a
Linux pod (home root ``/home``) every outbox path resolved to a directory that
doesn't exist. ``_list_outbox_files`` saw ``outbox.exists() == False``, returned
``[]``, and the hourly audit-scheduler tick drained ZERO records while the
bot-side runner kept writing them — darwin/evo outboxes piled up at ~195/192
root records, ``_ingested`` never created. The mini (macOS) drained fine because
``/Users/{bot}`` happens to be correct there.

The fix routes the home ROOT through ``platform_profile.get_profile()``. These
tests assert the helpers track the active profile — the existing
test_audit_poller.py suite monkeypatches ``_audit_outbox_dir`` wholesale, so it
could never have caught this.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin.applications import audit_poller  # noqa: E402


# The conftest autouse fixture pins MACOS before each test and restores MACOS
# after, so an in-test set_profile(LINUX) is self-cleaning. We assert MACOS
# explicitly too rather than leaning on the harness default.


def test_outbox_paths_use_macos_home_root_under_macos_profile() -> None:
    set_profile(MACOS)
    assert audit_poller._audit_outbox_dir("darwin") == Path(
        "/Users/darwin/.openclaw/workspace/evolve/audit_outbox"
    )
    assert audit_poller._audit_outbox_ingested("darwin") == Path(
        "/Users/darwin/.openclaw/workspace/evolve/audit_outbox/_ingested"
    )
    assert audit_poller._audits_dir_for_bot("darwin") == Path(
        "/Users/darwin/.openclaw/workspace/evolve/audits"
    )


def test_outbox_paths_use_linux_home_root_under_linux_profile() -> None:
    """The regression assertion: under the Linux profile the home root is
    ``/home``, NOT ``/Users``. A hardcoded literal makes this fail."""
    set_profile(LINUX)
    assert audit_poller._audit_outbox_dir("darwin") == Path(
        "/home/darwin/.openclaw/workspace/evolve/audit_outbox"
    )
    assert audit_poller._audit_outbox_ingested("evo") == Path(
        "/home/evo/.openclaw/workspace/evolve/audit_outbox/_ingested"
    )
    assert audit_poller._audits_dir_for_bot("evo") == Path(
        "/home/evo/.openclaw/workspace/evolve/audits"
    )


def test_outbox_root_never_hardcodes_users_literal() -> None:
    """Belt-and-suspenders: no Linux outbox path may contain ``/Users``."""
    set_profile(LINUX)
    for path in (
        audit_poller._audit_outbox_dir("darwin"),
        audit_poller._audit_outbox_ingested("darwin"),
        audit_poller._audits_dir_for_bot("darwin"),
    ):
        assert "/Users/" not in str(path), path
        assert str(path).startswith("/home/darwin/"), path
