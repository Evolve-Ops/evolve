"""tests/test_sudoers_filesystem_skill_acl.py — sudoers grants for
filesystem-MCP skill ACL toggles.

obsidian_install.grant_vault_acl and dropbox_install.grant_dropbox_acl
both shell out to `sudo /bin/chmod +a <ace> <user-chosen path>`. The
path is operator-supplied (e.g. /Users/cjalden/Documents/Obsidian), so
the sudoers grant needs a wildcard that matches arbitrary user-dir
paths.

Pre-2026-05-30 the only chmod-+a grants in /etc/sudoers.d/evolve were
for /Users/Shared/evolve/proposals and /signals — the install routes
returned 500 acl_grant_failed before ever creating the InstallMcpServer
proposal. See docs/skills-deep-audit-2026-05-30.md P0-2 for the
incident.

This test pins the new grants (P1b of the audit fix sprint):
  - `/bin/chmod +a * /Users/*/**` and -a / -R variants
  - `/Users/*/**` requires at least 3 path components (/Users/<user>/<x>)
    so the grant does NOT allow chmod against `/Users/<user>/` itself
  - `**` is sudo 1.9+ syntax for `*` that also matches `/`; validated
    via visudo on the mini's sudo 1.9.17
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


def test_evolve_sudoers_grants_chmod_plus_a_on_user_dirs():
    """+a grant must allow obsidian_install / dropbox_install to apply
    a read or read+write ACE to a user-chosen vault path."""
    from evolve_admin.setup_wizard import _render_evolve_sudoers

    content = _render_evolve_sudoers() or ""
    assert "evolve ALL=(root) NOPASSWD: /bin/chmod +a * /Users/*/**" in content, (
        "Missing `chmod +a` grant for /Users/*/** — obsidian / dropbox "
        "install routes will return 500 acl_grant_failed. See "
        "docs/skills-deep-audit-2026-05-30.md P0-2."
    )


def test_evolve_sudoers_grants_chmod_R_plus_a_on_user_dirs():
    """Recursive +a is needed for already-populated vaults — without
    it the ACE applies to the root dir but not pre-existing children."""
    from evolve_admin.setup_wizard import _render_evolve_sudoers

    content = _render_evolve_sudoers() or ""
    assert "evolve ALL=(root) NOPASSWD: /bin/chmod -R +a * /Users/*/**" in content


def test_evolve_sudoers_grants_chmod_minus_a_on_user_dirs():
    """-a grant must allow revoke_vault_acl / revoke_dropbox_acl to
    strip the ACE cleanly when the operator uninstalls the skill."""
    from evolve_admin.setup_wizard import _render_evolve_sudoers

    content = _render_evolve_sudoers() or ""
    assert "evolve ALL=(root) NOPASSWD: /bin/chmod -a * /Users/*/**" in content, (
        "Missing `chmod -a` grant for /Users/*/** — revoke route can't "
        "fully strip the ACE; uninstall leaves bot read access intact."
    )


def test_evolve_sudoers_grants_chmod_R_minus_a_on_user_dirs():
    """Recursive -a is the mirror of recursive +a for revoke."""
    from evolve_admin.setup_wizard import _render_evolve_sudoers

    content = _render_evolve_sudoers() or ""
    assert "evolve ALL=(root) NOPASSWD: /bin/chmod -R -a * /Users/*/**" in content


def test_evolve_sudoers_does_not_allow_chmod_on_user_home_root():
    """Critical scope guard: the /Users/*/** pattern requires at least
    3 path components so the grant does NOT cover /Users/<user>/ alone.
    If anyone widens the pattern to /Users/** or /Users/*, a buggy
    caller could chmod against an entire user's home dir.

    The /Users/*/** pattern means: /Users/<one-token>/<at-least-one-more>.
    A future change to /Users/** or /Users/* would weaken this.
    """
    from evolve_admin.setup_wizard import _render_evolve_sudoers

    content = _render_evolve_sudoers() or ""
    # The 4 grants must use exactly /Users/*/** — not /Users/** or /Users/*
    for op in ("+a", "-R +a", "-a", "-R -a"):
        assert f"evolve ALL=(root) NOPASSWD: /bin/chmod {op} * /Users/*/**" in content, (
            f"chmod {op} grant must scope to /Users/*/** (3-segment minimum)"
        )
        # And these unsafe-wider forms must NOT appear
        assert f"evolve ALL=(root) NOPASSWD: /bin/chmod {op} * /Users/**\n" not in content, (
            f"chmod {op} must NOT use /Users/** alone — that allows scoping "
            f"the ACE to /Users/<user> itself (whole home dir)."
        )
        assert f"evolve ALL=(root) NOPASSWD: /bin/chmod {op} * /Users/*\n" not in content, (
            f"chmod {op} must NOT use /Users/* alone — too narrow (only "
            f"matches direct children of /Users)."
        )
