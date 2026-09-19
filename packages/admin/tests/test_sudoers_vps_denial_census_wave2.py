"""Sudoers grants from the 2026-07-29 VPS auth-journal denial census, wave 2.

Wave 1 (the sibling denial-audit PR) covered the bot-home setfacl shapes and
the tiers/cron/OC-log producer cat fallbacks. This wave pins the REMAINING
denial families from the 24h census on evolve-vps-pod — every one fired daily
with no grant on either platform and failed silently (check=False callers or
checks degrading to "skipped" findings):

  1. `crontab -l` via `sudo -u <bot>` — application scanner cron collection
     (188/day). Runas-ALL grant, command pinned to the read-only listing.
  2. `sshd -T` + `lsof -iTCP -sTCP:LISTEN -n -P` — audit.py machine-hygiene
     checks (91/day each). The callers now take both binaries from the
     platform-profile table (bare `sshd` could NEVER resolve: /usr/sbin is
     not on the sudoers secure_path).
  3. `git -C <workspace> rev-parse HEAD` / `show HEAD:<f>` — audit_identity's
     baseline (92/day/bot). `-c safe.directory=*` is part of the pinned argv:
     git 2.35.2+ refuses bot-owned repos even as root (verified live on the
     VPS).
  4. cat fallbacks for workspace/evolve channels outside the content-scan doc
     set: pod_config.json + audit_outbox/* (69/day).
  5. Shared-store heal shapes: proposal lifecycle subdir chown,
     _create_bot_subdir's mkdir/chmod/chown trio + per-bot read-ACE grants,
     _ensure_evo_write_acl on keystore/config_intents, and the deploy-checkout
     packages/ read ACL.

Each grant is pinned against the rendered content per profile so caller ↔
renderer drift fails at PR time, not as 3am journal noise.
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

PROFILES = pytest.mark.parametrize(
    "profile,oc_path", [(MACOS, MAC_OC_PATH), (LINUX, LINUX_OC_PATH)],
    ids=["macos", "linux"],
)


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


# ── Family 1-3: §24 machine + workspace audit probes ─────────────────────────


@PROFILES
def test_section24_probe_grants_on_both_profiles(monkeypatch, profile, oc_path):
    content = _render(monkeypatch, profile, oc_path)
    home = profile.user_home_root
    expected = [
        # crontab is runas-ALL (bots are dynamic; the command is the pinned
        # read-only listing) — every other probe runs as root.
        f"evolve ALL=(ALL) NOPASSWD: {profile.crontab} -l\n",
        f"evolve ALL=(root) NOPASSWD: {profile.sshd} -T\n",
        f"evolve ALL=(root) NOPASSWD: {profile.lsof} -iTCP -sTCP\\:LISTEN -n -P\n",
        f"evolve ALL=(root) NOPASSWD: {profile.git} -c safe.directory=* "
        f"-C {home}/*/.openclaw/workspace rev-parse HEAD\n",
        f"evolve ALL=(root) NOPASSWD: {profile.git} -c safe.directory=* "
        f"-C {home}/*/.openclaw/workspace show HEAD\\:*\n",
    ]
    for line in expected:
        assert line in content, line


@PROFILES
def test_probe_binaries_resolve_from_profile_table(monkeypatch, profile, oc_path):
    """The audit.py callers build their argv from the SAME profile fields the
    renderer consumes (one writer, one table) — pin the fields the grants
    depend on so a table edit that would orphan a grant fails loudly."""
    assert profile.commands["sshd"] == profile.sshd == "/usr/sbin/sshd"
    assert profile.commands["git"] == profile.git == "/usr/bin/git"
    assert profile.commands["crontab"] == profile.crontab == "/usr/bin/crontab"
    # lsof is the one divergent binary in this set.
    assert profile.lsof == (
        "/usr/sbin/lsof" if profile.name == "macos" else "/usr/bin/lsof"
    )


# ── Family 4: workspace/evolve cat fallbacks ─────────────────────────────────


@PROFILES
def test_workspace_evolve_cat_fallback_grants(monkeypatch, profile, oc_path):
    content = _render(monkeypatch, profile, oc_path)
    cat = profile.cat
    home = profile.user_home_root
    for rel in (
        ".openclaw/workspace/evolve/pod_config.json",   # audit_pod_config compare-read
        ".openclaw/workspace/evolve/audit_outbox/*",    # audit_poller outbox drain
    ):
        line = f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/{rel}\n"
        assert line in content, line


# ── Family 5: shared-store heal shapes ───────────────────────────────────────


@PROFILES
def test_shared_store_bootstrap_grants(monkeypatch, profile, oc_path):
    """§9a: deploy_shared_dir's lifecycle-subdir chown + _create_bot_subdir's
    mkdir/chmod/chown trio (the sudo leg fires when the bot's gateway won the
    dir-creation race and the dir landed bot-owned)."""
    content = _render(monkeypatch, profile, oc_path)
    shared = profile.shared_dir_default
    chown, mkdir, chmod = profile.chown, profile.mkdir, profile.chmod
    expected = [
        # (a) proposal lifecycle subdirs — non-recursive chown; the -R grants
        # in §9b don't match this argv.
        *(f"{chown} * {shared}/proposals/{sub}\n"
          for sub in ("pending", "snoozed", "applied", "archived")),
        # (b) per-bot shared-store subdirs.
        *(f"{mkdir} -p {shared}/{pat}\n"
          for pat in ("metrics/*", "annotations/*", "*/turns", "*/spans",
                      "*/cascade", "*/recommendations")),
        f"{chmod} 1777 {shared}/metrics/*\n",
        f"{chmod} 1777 {shared}/*/recommendations\n",
        *(f"{chmod} 755 {shared}/{pat}\n"
          for pat in ("annotations/*", "*/spans", "*/cascade")),
        *(f"{chown} * {shared}/{pat}\n"
          for pat in ("metrics/*", "annotations/*", "*/turns", "*/spans",
                      "*/cascade", "*/recommendations")),
    ]
    for grant in expected:
        assert f"evolve ALL=(root) NOPASSWD: {grant}" in content, grant


def test_linux_evo_write_acl_covers_all_evo_write_subdirs(monkeypatch):
    """§9b Linux: keystore/ and config_intents/ joined EVO_WRITE_SHARED_SUBDIRS
    after the proposals/signals grants were written; _ensure_evo_write_acl
    fired for them on every pass and died silently (16 denials/day/shape)."""
    content = _render(monkeypatch, LINUX, LINUX_OC_PATH)
    from evolve_admin.deploy import EVO_WRITE_SHARED_SUBDIRS
    shared = LINUX.shared_dir_default
    for sub in EVO_WRITE_SHARED_SUBDIRS:
        for grant in (
            f"{LINUX.setfacl} -R -m * {shared}/{sub}\n",
            f"{LINUX.setfacl} -R -d -m * {shared}/{sub}\n",
            f"{LINUX.chown} -R * {shared}/{sub}\n",
        ):
            assert f"evolve ALL=(root) NOPASSWD: {grant}" in content, grant


def test_macos_evo_write_acl_covers_all_evo_write_subdirs(monkeypatch):
    content = _render(monkeypatch, MACOS, MAC_OC_PATH)
    from evolve_admin.deploy import EVO_WRITE_SHARED_SUBDIRS
    shared = MACOS.shared_dir_default
    for sub in EVO_WRITE_SHARED_SUBDIRS:
        for grant in (
            f"{MACOS.chmod} +a * {shared}/{sub}\n",
            f"{MACOS.chmod} -R +a * {shared}/{sub}\n",
            f"{MACOS.chown} -R * {shared}/{sub}\n",
        ):
            assert f"evolve ALL=(root) NOPASSWD: {grant}" in content, grant


def test_linux_per_bot_subdir_read_ace_grants_are_spec_pinned(monkeypatch):
    """The per-bot ACE grants carry a wildcard path tail, so the entry spec is
    pinned to u:evolve:rX (LinuxPerms' read verb bits) — evolve can grant only
    ITSELF read through these lines."""
    content = _render(monkeypatch, LINUX, LINUX_OC_PATH)
    shared = LINUX.shared_dir_default
    for pat in ("metrics/*", "annotations/*", "*/turns", "*/spans", "*/cascade"):
        for flags in ("-m", "-d -m"):
            grant = f"{LINUX.setfacl} {flags} u\\:evolve\\:rX {shared}/{pat}\n"
            assert f"evolve ALL=(root) NOPASSWD: {grant}" in content, grant
    # Deploy-checkout packages/ read ACL (fixed path → bare `-m *` spec).
    for grant in (
        f"{LINUX.setfacl} -R -m * {LINUX.deploy_checkout_default}/packages\n",
        f"{LINUX.setfacl} -R -d -m * {LINUX.deploy_checkout_default}/packages\n",
    ):
        assert f"evolve ALL=(root) NOPASSWD: {grant}" in content, grant
