"""tests/test_sudoers_retire_archive.py

Regression test: the rendered evolve sudoers file must include the
grants retire-bot's archive sudo-cp fallback needs.

Bug history: the §8c grants shipped 2026-06-01 after a wizard-Delete
failed in the API path (admin-ui daemon running as `evolve`) while the
same retire-bot CLI invocation succeeded — plugin runtimes create
subdirs under .openclaw/ with restrictive modes that don't inherit the
evolve read ACL, so the archive step's shutil.copytree hits EACCES and
the fallback re-copies as root. PR #1956 (commit 34ffba154, a forge
fix) accidentally reverted the block via a stale-branch hunk, silently
re-breaking API-path retire on exactly those bots.

The needles are full grant lines including the trailing newline —
a needle that is a prefix of a longer sibling grant passes spuriously
when the pinned grant itself is dropped (three older pins had exactly
that bug).

Consumer: evolve_admin.retire._sudo_cp_tree_fallback — argv shapes
documented in its docstring, which mirrors these three grants 1:1.
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


def test_rm_partial_archive_grant_present(sudoers_content: str) -> None:
    """The fallback's first step removes the partial-copy state the
    failing shutil.copytree left behind."""
    needle = (
        "evolve ALL=(root) NOPASSWD: "
        "/bin/rm -rf /Users/Shared/evolve/archived-bots/*\n"
    )
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for retire._sudo_cp_tree_fallback's "
        f"partial-copy cleanup. Add this line in "
        f"setup_wizard._render_evolve_sudoers:\n  {needle}"
    )


def test_cp_tree_grant_present(sudoers_content: str) -> None:
    """The root-powered re-copy of the bot's .openclaw tree into the
    archive dir."""
    needle = (
        "evolve ALL=(root) NOPASSWD: "
        "/bin/cp -R /Users/*/.openclaw /Users/Shared/evolve/archived-bots/*/openclaw\n"
    )
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for retire._sudo_cp_tree_fallback's "
        f"sudo cp -R re-copy. Add this line:\n  {needle}"
    )


def test_chown_archive_grant_present(sudoers_content: str) -> None:
    """Post-copy chown to evolve so the archive audit can walk the tree
    without a second sudo round-trip."""
    needle = (
        "evolve ALL=(root) NOPASSWD: "
        "/usr/sbin/chown -R * /Users/Shared/evolve/archived-bots/*\n"
    )
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for retire._sudo_cp_tree_fallback's "
        f"post-copy chown. Add this line:\n  {needle}"
    )


def test_grant_section_documented(sudoers_content: str) -> None:
    """The archive-fallback grants should be annotated so future operators
    grep'ing for retire behaviour can find them."""
    assert "retire-bot archive sudo-cp fallback" in sudoers_content, (
        "The retire-archive grant block should reference the sudo-cp "
        "fallback in its comment so future operators understand why "
        "it's there."
    )
