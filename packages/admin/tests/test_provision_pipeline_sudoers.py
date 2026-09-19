"""tests/test_provision_pipeline_sudoers.py

Regression test: the rendered evolve sudoers file must include the narrow
NOPASSWD grants for the commands the bot-creation wizard's provision
pipeline runs.

Bug history: the wizard endpoint (POST /api/wizard/provision) executes
inside the admin-ui daemon as the `evolve` user. The CLI path
(`sudo evolve-admin provision-bot ...`) was historically invoked under
the operator's unrestricted sudo, so these commands worked silently
— but the wizard path hits the evolve user's sudoers, which lacked
grants for dscl / createhomedir / mkdir-on-.openclaw / chmod-on-.openclaw
/ rm-rf rollback. Without these, the wizard died at stage 2
(create_macos_user) with `sudo: a terminal is required to read the
password`. Surfaced 2026-05-31 mid-onboarding for the 9th bot on the
test pod.

These tests pin the grants so a future refactor of
`_render_evolve_sudoers` can't silently drop them and re-break the
wizard for new bot creation.
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


# ─── Stage 2: create_macos_user ──────────────────────────────────────────────


def test_dscl_create_grant_present(sudoers_content: str) -> None:
    """The provision pipeline runs six `sudo /usr/bin/dscl . -create
    /Users/<bot> ...` commands to populate the new macOS account record.
    All match against the single wildcard grant."""
    needle = "evolve ALL=(root) NOPASSWD: /usr/bin/dscl . -create /Users/*"
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for the wizard's "
        f"create_macos_user stage. Add this line in "
        f"setup_wizard._render_evolve_sudoers:\n  {needle}"
    )


def test_createhomedir_grant_present(sudoers_content: str) -> None:
    """createhomedir provisions the bot user's home directory after dscl."""
    needle = "evolve ALL=(root) NOPASSWD: /usr/sbin/createhomedir -c -u *"
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for createhomedir in the wizard's "
        f"create_macos_user stage. Add this line:\n  {needle}"
    )


def test_dscl_delete_grant_present(sudoers_content: str) -> None:
    """Rollback of stage 2 deletes the dscl record so the operator can
    re-run the wizard without manual cleanup."""
    needle = "evolve ALL=(root) NOPASSWD: /usr/bin/dscl . -delete /Users/*"
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for create_macos_user rollback. "
        f"Add this line:\n  {needle}"
    )


def test_home_dir_rm_grant_present(sudoers_content: str) -> None:
    """Rollback also removes the home directory; provisioning.py guards
    the path before invoking, so the broad grant is the policy floor."""
    needle = "evolve ALL=(root) NOPASSWD: /bin/rm -rf /Users/*\n"
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for home-directory rollback in "
        f"the wizard's create_macos_user stage. Add this line:\n  {needle}"
    )


# ─── Stage 3: create_openclaw_dir ────────────────────────────────────────────


def test_mkdir_openclaw_dir_grant_present(sudoers_content: str) -> None:
    """The pipeline creates the parent .openclaw/ dir as root before
    chown'ing it to the bot user."""
    needle = "evolve ALL=(root) NOPASSWD: /bin/mkdir -p /Users/*/.openclaw\n"
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for the wizard's "
        f"create_openclaw_dir stage. Add this line:\n  {needle}"
    )


def test_chmod_openclaw_dir_grant_present(sudoers_content: str) -> None:
    """chmod 700 locks the .openclaw/ dir to the bot user only."""
    needle = "evolve ALL=(root) NOPASSWD: /bin/chmod 700 /Users/*/.openclaw\n"
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for chmod 700 on the bot's "
        f".openclaw/ dir. Add this line:\n  {needle}"
    )


def test_rm_openclaw_dir_rollback_grant_present(sudoers_content: str) -> None:
    """Rollback removes the .openclaw/ tree."""
    needle = "evolve ALL=(root) NOPASSWD: /bin/rm -rf /Users/*/.openclaw\n"
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for create_openclaw_dir rollback. "
        f"Add this line:\n  {needle}"
    )


# ─── Grant block documented for future grep-ers ──────────────────────────────


def test_grant_section_documented(sudoers_content: str) -> None:
    """The provision-pipeline grants should be annotated so future operators
    grep'ing for the wizard's setup can find them."""
    assert "wizard /api/wizard/provision endpoint" in sudoers_content, (
        "The provision-pipeline grants block should reference the wizard "
        "endpoint in its comment so future operators understand why these "
        "grants exist and why they're shaped as they are."
    )


# ─── Stage 6: deploy ACL ritual (set_evolve_read_acl) ────────────────────────
#
# Added with add-bot M2: deploy.py's set_evolve_read_acl runs every one
# of these with check=False, so a missing grant fails SILENTLY and the
# freshly built bot is unreadable to the admin daemon (the M2 pod proof
# surfaced it as a smoke check reporting openclaw.json "not found" on a
# healthy bot). Exact-line pins — note these needles must not be
# prefixes of narrower grants or the pin passes spuriously.


def test_acl_read_grant_on_openclaw_root_present(sudoers_content: str) -> None:
    needle = "evolve ALL=(root) NOPASSWD: /bin/chmod +a * /Users/*/.openclaw\n"
    assert needle in sudoers_content, (
        "Missing the chmod +a grant on /Users/*/.openclaw itself (not a "
        "subdirectory) — set_evolve_read_acl's read-ACL line fails "
        "silently without it."
    )
    recursive = "evolve ALL=(root) NOPASSWD: /bin/chmod -R +a * /Users/*/.openclaw\n"
    assert recursive in sudoers_content


def test_acl_credentials_strip_grants_present(sudoers_content: str) -> None:
    assert (
        "evolve ALL=(root) NOPASSWD: /bin/chmod -N /Users/*/.openclaw/credentials\n"
        in sudoers_content
    )
    assert (
        "evolve ALL=(root) NOPASSWD: /bin/chmod 700 /Users/*/.openclaw/credentials\n"
        in sudoers_content
    )


def test_gateway_plist_install_grants_present(sudoers_content: str) -> None:
    """Stage 6's gateway install: log dir + the Scheduler seam's plist
    write ritual. The /tmp/*.plist source matches the seam's unprefixed
    tempfile staging."""
    for needle in (
        "evolve ALL=(root) NOPASSWD: /bin/mkdir -p /Users/*/.openclaw/logs\n",
        "evolve ALL=(root) NOPASSWD: /bin/cp /tmp/*.plist /Library/LaunchDaemons/ai.openclaw.*-gateway.plist\n",
        "evolve ALL=(root) NOPASSWD: /bin/chmod 644 /Library/LaunchDaemons/ai.openclaw.*-gateway.plist\n",
    ):
        assert needle in sudoers_content, f"missing grant: {needle.strip()}"
