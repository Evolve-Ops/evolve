"""Regression test: the wizard-verify ownership-repair sudoers grant exists.

The verify gauntlet's Check 1 fix path runs
  sudo /usr/sbin/chown -R <bot>:staff <path>
against any root-owned path under /Users/<bot>/.openclaw/. Without the
matching sudoers grant the fix button silently fails with the obscure
"sudo: a password is required" error. This test asserts the grant is
present so a future refactor of _render_evolve_sudoers can't drop it
without the test loudly failing.

Spec: internal/spec-wizard-verification-gauntlet-2026-05-30.md §5.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.setup_wizard import _render_evolve_sudoers  # noqa: E402


@pytest.fixture(scope="module")
def sudoers_content() -> str:
    content = _render_evolve_sudoers()
    assert content is not None, "openclaw not discoverable at test time"
    return content


def test_ownership_repair_recursive_grant_present(sudoers_content: str) -> None:
    needle = "/usr/sbin/chown -R * /Users/*/.openclaw"
    assert any(line.rstrip().endswith(needle) for line in sudoers_content.splitlines()), (
        "Expected sudoers grant 'evolve ALL=(root) NOPASSWD: /usr/sbin/chown -R * "
        "/Users/*/.openclaw' is missing. The wizard-verify gauntlet's Check-1 fix "
        "button will fail without it. See internal/spec-wizard-verification-gauntlet-2026-05-30.md §5."
    )


def test_ownership_repair_single_path_grant_present(sudoers_content: str) -> None:
    """The non-recursive form is needed for fixing a single file (e.g. pairing-allowFrom)."""
    needle = "/usr/sbin/chown * /Users/*/.openclaw"
    matched = [
        line for line in sudoers_content.splitlines()
        if line.rstrip().endswith(needle)
    ]
    assert matched, (
        "Expected sudoers grant 'evolve ALL=(root) NOPASSWD: /usr/sbin/chown * "
        "/Users/*/.openclaw' is missing — needed for single-file ownership fixes. "
        "See internal/spec-wizard-verification-gauntlet-2026-05-30.md §5."
    )


def test_grant_section_documented(sudoers_content: str) -> None:
    """The verify-gauntlet sudoers section is annotated for future grep-ers."""
    assert "wizard verify gauntlet" in sudoers_content.lower(), (
        "The ownership-repair grant block should reference the verify gauntlet "
        "in its comment so future operators understand why it's there."
    )
