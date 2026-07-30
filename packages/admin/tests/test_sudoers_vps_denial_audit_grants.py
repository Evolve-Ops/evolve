"""Sudoers grants from the 2026-07-29 VPS auth-journal denial audit.

Two denial families on the evolve-vps pod used CORRECT /home paths and were
still denied — both were missing-grant drift between callers and
``_render_evolve_sudoers`` (the same class the writer-alignment and
content-scan-doc tests pin for their own callers):

  1. ``set_evolve_read_acl``'s Linux argv shapes for the workspace-root write
     grant, the per-file workspace doc backfill, workspace/evolve-backup,
     ~/.claude/projects and ~/.zshrc — every one fired on each deploy/heal
     (~80×/48h) and died "command not allowed", silently (check=False).
  2. Producer ``sudo /bin/cat`` fallbacks for evolve-tiers.json,
     cron/jobs.json and logs/openclaw.log — no grant existed on EITHER
     platform; macOS never noticed because the ACL direct read always works
     there, while on Linux the OC gateway mints these files 0600 (create-mode
     group bits become the POSIX-ACL mask, capping evolve's inherited ACE).

This module pins each grant against the rendered content per profile so a
future caller/renderer drift fails at PR time, not as 3am journal noise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import setup_wizard  # noqa: E402

MAC_OC_PATH = "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw"
LINUX_OC_PATH = "/usr/lib/node_modules/openclaw/bin/openclaw"


@pytest.fixture(autouse=True)
def _restore_profile():
    yield
    set_profile(None)


def _render(monkeypatch: pytest.MonkeyPatch, profile, oc_path: str) -> str:
    set_profile(profile)
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path", lambda: oc_path)
    content = setup_wizard._render_evolve_sudoers()
    assert content is not None
    return content


# Family 1 — the Linux perms-seam argv shapes set_evolve_read_acl emits
# (observed verbatim in the VPS auth journal). Grant style follows the
# Stage-6 conventions: bare `-m *` spec for trailing-anchored paths, a
# literally-pinned entry spec when the path pattern carries a wildcard tail.
LINUX_SETFACL_GRANTS = [
    # workspace/ root write grant + its -d default-ACL pair (grant() with
    # file_inherit on a dir emits both)
    "/usr/bin/setfacl -m * /home/*/.openclaw/workspace\n",
    "/usr/bin/setfacl -d -m * /home/*/.openclaw/workspace\n",
    # per-file backfill for files directly in workspace/ (spec-pinned:
    # the path wildcard spans '/')
    "/usr/bin/setfacl -m u\\:evolve\\:rwX /home/*/.openclaw/workspace/*\n",
    # workspace/evolve-backup grant_write_recursive pair
    "/usr/bin/setfacl -R -m * /home/*/.openclaw/workspace/evolve-backup\n",
    "/usr/bin/setfacl -R -d -m * /home/*/.openclaw/workspace/evolve-backup\n",
    # Auto-Memory inventory read pair
    "/usr/bin/setfacl -R -m * /home/*/.claude/projects\n",
    "/usr/bin/setfacl -R -d -m * /home/*/.claude/projects\n",
    # .zshrc tamper-detection read grant
    "/usr/bin/setfacl -m * /home/*/.zshrc\n",
]


def test_linux_render_has_set_evolve_read_acl_grants(monkeypatch):
    content = _render(monkeypatch, LINUX, LINUX_OC_PATH)
    for grant in LINUX_SETFACL_GRANTS:
        assert f"evolve ALL=(root) NOPASSWD: {grant}" in content, grant


# Family 2 — producer sudo-cat fallback paths, granted on BOTH profiles with
# that profile's own cat binary + home root (drift-proof, like §3h).
CAT_FALLBACK_RELPATHS = [
    ".openclaw/evolve-tiers.json",     # audit_tier_drift
    ".openclaw/cron/jobs.json",        # audit_cron_health
    ".openclaw/logs/openclaw.log",     # cost-watchdog / embedding-monitor log tail
]


@pytest.mark.parametrize("profile,oc_path", [(MACOS, MAC_OC_PATH), (LINUX, LINUX_OC_PATH)],
                         ids=["macos", "linux"])
def test_producer_cat_fallback_grants_on_both_profiles(monkeypatch, profile, oc_path):
    content = _render(monkeypatch, profile, oc_path)
    cat = profile.cat
    home = profile.user_home_root
    for rel in CAT_FALLBACK_RELPATHS:
        line = f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/{rel}\n"
        assert line in content, line
