"""Tests for evolve_admin.skills.upstream_plugin_skills.

Coverage of the small state machine for Brave / GitHub / Drive / Dropbox —
skills that ride on upstream OpenClaw plugins. Each skill's resolve_status()
inspects openclaw.json under the bot's home directory; we patch the config
module's bot_home() so the tests can point at tmp_path trees.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# Worktree import isolation (see test_skills_inventory.py).
_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.skills import upstream_plugin_skills as ups


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_oc(tmp_path: Path, oc_data: dict, bot_user: str = "testbot") -> Path:
    home = tmp_path / bot_user
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps(oc_data))
    return home


def _resolve(tmp_path, skill, oc_data, bot_id="testbot"):
    home = _write_oc(tmp_path, oc_data, bot_id)
    with patch("evolve_admin.config.bot_home", return_value=home):
        return ups.resolve_status(skill, bot_id)


# ── Registry shape ───────────────────────────────────────────────────────────

class TestRegistry:
    def test_all_currently_listed_skills_present(self):
        # Dropbox migrated to MCP install 2026-05-30 (PR #1819) — kept here
        # as a regression-guard for the constants-only back-compat.
        # gdrive + unity WITHDRAWN 2026-05-30 (Phase 1c of deep skills
        # audit; see docs/skills-deep-audit-2026-05-30.md). gdrive piggybacked
        # on a broken GOG OAuth profile that no OC runtime consumed; unity's
        # install_hint pointed at a CLI command (`openclaw plugins install
        # unity`) the CLI rejects + no upstream plugin exists.
        for sid in ("brave", "github"):
            assert sid in ups.SKILLS, f"{sid} missing from SKILLS"
        for withdrawn in ("dropbox", "gdrive", "unity"):
            assert withdrawn not in ups.SKILLS, (
                f"{withdrawn} should no longer be in "
                f"upstream_plugin_skills.SKILLS. See "
                f"docs/skills-deep-audit-2026-05-30.md for context."
            )

    def test_get_skill_returns_none_for_unknown(self):
        assert ups.get_skill("nope") is None

    def test_get_skill_returns_entry_for_known(self):
        s = ups.get_skill("brave")
        assert s is not None
        assert s.id == "brave"
        assert s.display_name == "Brave Search"

    def test_gdrive_and_unity_withdrawn_from_registry(self):
        """Regression guard for the Phase 1c withdrawals — if either
        re-surfaces in SKILLS without a real runtime consumer landing
        first, the resurrection should fail loudly."""
        assert ups.get_skill("gdrive") is None
        assert ups.get_skill("unity") is None

    def test_brave_and_github_dont_require_auth_profile(self):
        """Brave needs no auth at all; GitHub uses env-var PAT, not auth.profiles."""
        assert ups.SKILLS["brave"].auth_profile_key is None
        assert ups.SKILLS["github"].auth_profile_key is None


# ── resolve_status state machine ─────────────────────────────────────────────

class TestResolveStatusBrave:
    """Brave has no auth profile requirement — just plugin enabled state."""

    def test_active_when_plugin_enabled(self, tmp_path):
        st = _resolve(tmp_path, ups.SKILLS["brave"],
                      {"plugins": {"entries": {"brave": {"enabled": True}}}})
        assert st.status == "active"

    def test_active_when_enabled_field_omitted(self, tmp_path):
        """openclaw treats missing 'enabled' as True — our resolver must match."""
        st = _resolve(tmp_path, ups.SKILLS["brave"],
                      {"plugins": {"entries": {"brave": {}}}})
        assert st.status == "active"

    def test_plugin_disabled_when_explicitly_off(self, tmp_path):
        st = _resolve(tmp_path, ups.SKILLS["brave"],
                      {"plugins": {"entries": {"brave": {"enabled": False}}}})
        assert st.status == "plugin_disabled"

    def test_missing_when_plugin_not_in_openclaw(self, tmp_path):
        st = _resolve(tmp_path, ups.SKILLS["brave"],
                      {"plugins": {"entries": {}}})
        assert st.status == "missing"


# TestResolveStatusDropbox removed 2026-05-30 — Dropbox is no longer an
# upstream_plugin_skills entry. The four tests here exercised the
# plugin+auth-profile state machine for an OAuth plugin that never
# actually existed (no @openclaw/dropbox-plugin on npm). Dropbox is now
# an MCP-backed install — see test_skills_dropbox_install.py for the
# new coverage and packages/admin/evolve_admin/skills/dropbox_install.py
# for the implementation.


# TestResolveStatusGdrive + TestResolveStatusUnity removed 2026-05-30 —
# both skills WITHDRAWN from the catalog (Phase 1c of the deep skills
# audit; see docs/skills-deep-audit-2026-05-30.md). They no longer have
# SKILLS entries to resolve against. The withdrawal-regression guard
# lives in test_skills_install_orchestrator_parity.py::TestWithdrawnSkills.


class TestResolveStatusGithub:
    """GitHub status is resolved from the workspace .git/config remote URL
    (the canonical store for purpose-1 backup), NOT from plugins.entries.github
    which no code path ever writes. There is no @openclaw/github-plugin on npm
    (verified 2026-05-30) so the old plugin-entry resolver always reported
    "missing" even on bots whose backup was wired correctly.

    Detection rule (matches skills.inventory's §5 supplemental detection +
    PR #1787 backup onboarding): active iff the workspace .git/config contains
    a github.com remote with an embedded token (https://<token>@github.com/...).
    """

    def _write_git_config(self, home: Path, contents: str) -> None:
        ws = home / ".openclaw" / "workspace" / ".git"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "config").write_text(contents)

    def test_active_when_workspace_git_config_has_token_bearing_github_remote(self, tmp_path):
        """Atlas's real shape after the Backup → Cloud wizard runs."""
        home = _write_oc(tmp_path, {})
        self._write_git_config(home, (
            '[remote "evolve-backup"]\n'
            '\turl = https://ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA@github.com/example/atlas-workspace.git\n'
            '\tfetch = +refs/heads/*:refs/remotes/evolve-backup/*\n'
        ))
        with patch("evolve_admin.config.bot_home", return_value=home):
            st = ups.resolve_status(ups.SKILLS["github"], "testbot")
        assert st.status == "active", st

    def test_missing_when_no_git_config(self, tmp_path):
        """No workspace/.git/config at all → missing."""
        home = _write_oc(tmp_path, {})  # openclaw.json only; no workspace
        with patch("evolve_admin.config.bot_home", return_value=home):
            st = ups.resolve_status(ups.SKILLS["github"], "testbot")
        assert st.status == "missing"

    def test_missing_when_git_config_has_no_github_remote(self, tmp_path):
        """git initialized but pointing at gitlab/bitbucket/local — not active."""
        home = _write_oc(tmp_path, {})
        self._write_git_config(home, (
            '[remote "origin"]\n'
            '\turl = https://gitlab.com/example/repo.git\n'
        ))
        with patch("evolve_admin.config.bot_home", return_value=home):
            st = ups.resolve_status(ups.SKILLS["github"], "testbot")
        assert st.status == "missing"

    def test_missing_when_github_remote_lacks_token(self, tmp_path):
        """A bare https://github.com/... URL (no embedded PAT) can't push
        backups, so we treat it as not-yet-wired rather than active."""
        home = _write_oc(tmp_path, {})
        self._write_git_config(home, (
            '[remote "origin"]\n'
            '\turl = https://github.com/example/repo.git\n'
        ))
        with patch("evolve_admin.config.bot_home", return_value=home):
            st = ups.resolve_status(ups.SKILLS["github"], "testbot")
        assert st.status == "missing"

    def test_ignores_plugins_entries_github(self, tmp_path):
        """Even if someone hand-writes plugins.entries.github = enabled (it
        does nothing — there's no @openclaw/github-plugin), status MUST still
        come from .git/config. This regression guard prevents the old false-
        positive resolution from creeping back in."""
        home = _write_oc(tmp_path, {
            "plugins": {"entries": {"github": {"enabled": True}}}
        })
        # No .git/config written → must report missing despite the plugin entry.
        with patch("evolve_admin.config.bot_home", return_value=home):
            st = ups.resolve_status(ups.SKILLS["github"], "testbot")
        assert st.status == "missing"

    def test_active_via_alternate_workspace_path(self, tmp_path):
        """If openclaw.json declares a non-default workspace path, the
        resolver must honour it (some bots relocate workspace/)."""
        home = _write_oc(tmp_path, {
            "agents": {"defaults": {"workspace": str(tmp_path / "alt-ws")}}
        })
        alt_git = tmp_path / "alt-ws" / ".git"
        alt_git.mkdir(parents=True)
        (alt_git / "config").write_text(
            '[remote "evolve-backup"]\n'
            '\turl = https://ghp_abc@github.com/example/repo.git\n'
        )
        with patch("evolve_admin.config.bot_home", return_value=home):
            st = ups.resolve_status(ups.SKILLS["github"], "testbot")
        assert st.status == "active"


class TestResolveStatusUnknown:
    """When openclaw.json can't be read, status falls back to 'unknown'."""

    def test_unknown_when_openclaw_missing(self, tmp_path):
        home = tmp_path / "testbot"
        home.mkdir()
        # No .openclaw/openclaw.json written.
        with patch("evolve_admin.config.bot_home", return_value=home):
            st = ups.resolve_status(ups.SKILLS["brave"], "testbot")
        assert st.status == "unknown"


# ── build_install_plan ───────────────────────────────────────────────────────

class TestInstallPlan:
    def test_active_status_yields_no_steps(self):
        st = ups.InstallStatus(bot_id="admin_bot", skill_id="brave", status="active")
        assert ups.build_install_plan(ups.SKILLS["brave"], st) == []

    def test_unknown_status_yields_no_steps(self):
        """Don't show install steps if we couldn't read the bot's config —
        the UI will surface the error from status.error instead."""
        st = ups.InstallStatus(bot_id="admin_bot", skill_id="brave", status="unknown",
                                error="permission_denied")
        assert ups.build_install_plan(ups.SKILLS["brave"], st) == []

    def test_missing_status_yields_manual_setup_plus_confirm_for_non_github(self):
        # After Phase 1c (2026-05-30) withdrew unity, dropbox, and gdrive,
        # `brave` is the only non-github SKILLS entry that still falls
        # through to the legacy manual_setup + confirm pair. (Brave's own
        # install_plan needs tightening too — flagged in audit P0-5/Phase 2
        # — but for this test we just pin the plan shape.)
        from evolve_admin.skills.upstream_plugin_skills import BRAVE_SKILL_ID
        st = ups.InstallStatus(bot_id="admin_bot", skill_id=BRAVE_SKILL_ID, status="missing")
        steps = ups.build_install_plan(ups.SKILLS[BRAVE_SKILL_ID], st)
        assert [s.id for s in steps] == ["manual_setup", "confirm"]
        # First-step label IS the install hint (the modal renders it inline).
        assert "brave" in steps[0].label.lower()
        # Regression guard from docs/audit-skills-install-flows-2026-05-30.md:
        # the previous "deploy step wires this" hint promised actions no
        # code performs.
        assert "deploy step wires" not in steps[0].label
        # Confirm step points at the status endpoint for polling.
        assert steps[1].endpoint and "/status" in steps[1].endpoint

    def test_github_missing_status_yields_backup_wizard_handoff(self):
        # GitHub's install hint used to ask the operator to set GITHUB_TOKEN
        # by hand and re-deploy. Now the Skills→GitHub install opens the
        # same Backup→Cloud wizard that handles PAT verify, repo
        # create/reuse, deploy key registration, and .git/config wiring —
        # so the plan is a single ``open_github_backup_wizard`` step
        # carrying the bot id in payload, no follow-up confirm step.
        st = ups.InstallStatus(bot_id="atlas", skill_id="github", status="missing")
        steps = ups.build_install_plan(ups.SKILLS["github"], st)
        assert [s.id for s in steps] == ["open_github_backup_wizard"]
        assert steps[0].payload == {"bot_id": "atlas"}
        assert "atlas" in steps[0].label
        assert "wizard" in steps[0].label.lower()
        # Access panel still threaded through so the modal's intro screen
        # can render the "Will / Won't" copy.
        assert steps[0].access_panel == dict(ups.SKILLS["github"].access_panel)

    def test_github_needs_auth_also_yields_backup_wizard_handoff(self):
        # If github reports needs_auth (e.g. plugin enabled but no PAT), the
        # same wizard handoff applies — wizard handles the whole credential
        # flow either way.
        st = ups.InstallStatus(bot_id="atlas", skill_id="github", status="needs_auth")
        steps = ups.build_install_plan(ups.SKILLS["github"], st)
        assert [s.id for s in steps] == ["open_github_backup_wizard"]

    # test_needs_auth_status_yields_manual_setup removed 2026-05-30 —
    # gdrive (its only test subject) was withdrawn in Phase 1c of the
    # deep skills audit. No auth-required upstream-plugin skill remains
    # in SKILLS after the withdrawal. If a future skill brings the
    # shape back (and it's a real, end-to-end-working skill), restore
    # a similar test parameterised on that skill_id.
