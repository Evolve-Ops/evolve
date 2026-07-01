"""Tests for evolve_admin.skills.apple_local_install.

The skill expanded from "Contacts + Calendar" to all four core Apple
local-apps (Contacts, Calendar, Reminders, Notes) under a single TCC
grant flow. The state machine collapsed from combinatorial
``needs_both_tcc`` / ``needs_contacts_tcc`` / ``needs_calendar_tcc``
strings into a single ``needs_tcc`` status carrying a ``missing[]`` list
of un-granted apps. These tests cover that new shape, the dynamic
instruction generator, and the probe layer (subprocess.run stubbed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.skills import apple_local_install as al


def _probes(**grants):
    """Build a set of probe callables that return True for keys listed in *grants*."""
    return {
        f"check_{k}": (lambda v=v: v) for k, v in grants.items()
    }


# ── resolve_status state machine ─────────────────────────────────────────────

class TestResolveStatus:
    def test_active_when_all_four_granted(self):
        st = al.resolve_status(
            "admin_bot",
            check_contacts=lambda: True, check_calendar=lambda: True,
            check_reminders=lambda: True, check_notes=lambda: True,
        )
        assert st.status == "active"
        assert st.missing == []
        # Back-compat alias fields still present for any callers reading the
        # v1 shape — to_dict surfaces both granted dict and the legacy keys.
        d = st.to_dict()
        assert d["contacts_granted"] is True
        assert d["calendar_granted"] is True

    def test_needs_tcc_when_one_missing(self):
        st = al.resolve_status(
            "admin_bot",
            check_contacts=lambda: True, check_calendar=lambda: True,
            check_reminders=lambda: False, check_notes=lambda: True,
        )
        assert st.status == "needs_tcc"
        assert st.missing == ["reminders"]
        assert st.granted == {
            "contacts": True, "calendar": True,
            "reminders": False, "notes": True,
        }

    def test_needs_tcc_when_multiple_missing(self):
        st = al.resolve_status(
            "admin_bot",
            check_contacts=lambda: False, check_calendar=lambda: True,
            check_reminders=lambda: False, check_notes=lambda: False,
        )
        assert st.status == "needs_tcc"
        # Order matches _APP_KEYS declaration (contacts, calendar, reminders, notes).
        assert st.missing == ["contacts", "reminders", "notes"]

    def test_needs_tcc_when_none_granted(self):
        st = al.resolve_status(
            "admin_bot",
            check_contacts=lambda: False, check_calendar=lambda: False,
            check_reminders=lambda: False, check_notes=lambda: False,
        )
        assert st.status == "needs_tcc"
        assert st.missing == ["contacts", "calendar", "reminders", "notes"]

    def test_unknown_when_any_probe_raises(self):
        def boom(): raise TimeoutError("hang")
        st = al.resolve_status(
            "admin_bot",
            check_contacts=lambda: True, check_calendar=lambda: True,
            check_reminders=boom, check_notes=lambda: True,
        )
        assert st.status == "unknown"
        assert "reminders_probe_failed" in (st.error or "")


# ── _instruction_for ─────────────────────────────────────────────────────────

class TestInstructionFor:
    """The label text rendered to the user must name only the missing toggles.
    Wrong toggle names lead to wrong clicks → silent failure → frustration."""

    def test_single_missing_uses_singular_phrasing(self):
        text = al._instruction_for(["reminders"])
        assert "the toggle for Reminders" in text
        # Other apps must not be named — the user shouldn't be told to flip
        # something they already flipped.
        assert "Contacts" not in text
        assert "Calendar" not in text
        assert "Notes" not in text

    def test_two_missing_joins_with_and(self):
        text = al._instruction_for(["contacts", "notes"])
        assert "Contacts and Notes" in text

    def test_three_missing_uses_oxford_comma(self):
        text = al._instruction_for(["calendar", "reminders", "notes"])
        # Plex test: serial comma reads naturally to a quick-scan user.
        assert "Calendar, Reminders, and Notes" in text

    def test_all_four_lists_all_toggles(self):
        text = al._instruction_for(["contacts", "calendar", "reminders", "notes"])
        for label in ("Contacts", "Calendar", "Reminders", "Notes"):
            assert label in text

    def test_links_to_correct_settings_pane(self):
        """The instruction must point at the exact pane the user opens —
        wrong pane wastes a click and dents Plex-test trust."""
        text = al._instruction_for(["contacts"])
        assert "Privacy & Security" in text
        assert "Automation" in text


# ── build_install_plan ───────────────────────────────────────────────────────

class TestInstallPlan:
    def test_active_yields_no_steps(self):
        st = al.InstallStatus(bot_id="admin_bot", status="active",
                               granted={k: True for k in al._APP_KEYS},
                               missing=[])
        assert al.build_install_plan(st) == []

    def test_unknown_yields_no_steps(self):
        st = al.InstallStatus(bot_id="admin_bot", status="unknown",
                               error="probe_failed")
        assert al.build_install_plan(st) == []

    def test_needs_tcc_yields_manual_setup_plus_confirm(self):
        st = al.InstallStatus(bot_id="admin_bot", status="needs_tcc",
                               granted={"contacts": True, "calendar": True,
                                        "reminders": False, "notes": False},
                               missing=["reminders", "notes"])
        steps = al.build_install_plan(st)
        assert [s.id for s in steps] == ["manual_setup", "confirm"]
        # Label names only the missing toggles, not granted ones.
        assert "Reminders and Notes" in steps[0].label
        assert "toggle for Contacts" not in steps[0].label

    def test_empty_missing_list_falls_back_to_all_four(self):
        """Defensive: if status='needs_tcc' arrives with an empty missing
        list (shouldn't happen, but the state field is the contract), we
        must still produce a workable instruction rather than blank text."""
        st = al.InstallStatus(bot_id="admin_bot", status="needs_tcc",
                               missing=[])
        steps = al.build_install_plan(st)
        assert steps and "Contacts" in steps[0].label
        assert "Notes" in steps[0].label

    def test_confirm_step_points_at_status_endpoint(self):
        st = al.InstallStatus(bot_id="admin_bot", status="needs_tcc",
                               missing=["contacts"])
        steps = al.build_install_plan(st)
        assert steps[1].endpoint and "/apple_local/status" in steps[1].endpoint


# ── AppleScript probe (subprocess stubbing) ──────────────────────────────────

class TestProbe:
    """Each of the four probes goes through the same osascript path. We test
    return-code mapping with subprocess.run stubbed so the tests don't depend
    on the host having Contacts / Calendar / Reminders / Notes set up."""

    def test_all_four_probes_return_true_on_rc_0(self):
        class _R:
            returncode = 0
        with patch("subprocess.run", return_value=_R()):
            assert al.probe_contacts_tcc() is True
            assert al.probe_calendar_tcc() is True
            assert al.probe_reminders_tcc() is True
            assert al.probe_notes_tcc() is True

    def test_all_four_probes_return_false_on_failure(self):
        class _R:
            returncode = 1
        with patch("subprocess.run", return_value=_R()):
            assert al.probe_contacts_tcc() is False
            assert al.probe_calendar_tcc() is False
            assert al.probe_reminders_tcc() is False
            assert al.probe_notes_tcc() is False

    def test_probes_target_the_right_app_and_collection(self):
        """Verify each probe issues the AppleScript expected for its app.
        A typo (e.g. probing Calendar for "every list") would silently break
        TCC detection — the script would error against any TCC state."""
        captured: list[str] = []

        class _R:
            returncode = 0

        def fake_run(cmd, **kw):
            # cmd is ['osascript', '-e', '<script>']
            captured.append(cmd[2])
            return _R()

        with patch("subprocess.run", side_effect=fake_run):
            al.probe_contacts_tcc()
            al.probe_calendar_tcc()
            al.probe_reminders_tcc()
            al.probe_notes_tcc()

        assert any('"Contacts"' in s and "every person" in s for s in captured)
        assert any('"Calendar"' in s and "every calendar" in s for s in captured)
        assert any('"Reminders"' in s and "every list" in s for s in captured)
        assert any('"Notes"' in s and "every note" in s for s in captured)

    def test_returns_false_when_osascript_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("osascript")):
            assert al.probe_contacts_tcc() is False

    def test_returns_false_on_timeout(self):
        import subprocess as sp
        with patch("subprocess.run",
                   side_effect=sp.TimeoutExpired(cmd="osascript", timeout=5)):
            assert al.probe_contacts_tcc() is False


# ── Flask route smoke tests ───────────────────────────────────────────────────

@pytest.fixture
def app_client(tmp_path):
    from flask import Flask
    from evolve_admin.web import server as _srv

    network = tmp_path / "network.json"
    network.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"admin_bot": {"user": "admin_bot"}},
    }))
    app = Flask(__name__)
    _srv._register_admin_routes(app, network)
    return app.test_client()


class TestRoutesWithdrawn:
    """apple_local was WITHDRAWN from the catalog 2026-05-30 (Phase 1c of
    the deep skills audit — see docs/skills-deep-audit-2026-05-30.md).
    The catalog/status/install routes return 404. The withdrawal-regression
    guard lives in
    test_skills_install_orchestrator_parity.py::TestWithdrawnSkills.

    The TestResolveStatus / TestInstructionFor / TestInstallPlan / TestProbe
    classes above still exercise the install module's internals — those
    stay live because the module is kept on disk for the eventual rewire
    (via apple-mcp-server or osascript tool surface)."""

    def test_apple_local_no_longer_appears_in_catalog(self, app_client):
        r = app_client.get("/api/skills/catalog")
        ids = {s["id"] for s in r.get_json()["skills"]}
        assert "apple_local" not in ids, (
            "apple_local must stay out of the catalog until a real "
            "runtime consumer ships (apple-mcp-server or osascript tool "
            "surface). See docs/skills-deep-audit-2026-05-30.md."
        )

    def test_apple_local_catalog_detail_returns_404(self, app_client):
        r = app_client.get("/api/skills/catalog/apple_local")
        assert r.status_code == 404, (
            "catalog detail must 404 so the install modal can't render "
            "for a withdrawn skill"
        )

    def test_apple_local_install_post_returns_404(self, app_client):
        r = app_client.post("/api/skills/install/apple_local",
                            json={"bot_id": "admin_bot"})
        assert r.status_code == 404

    def test_apple_local_status_get_returns_404(self, app_client):
        r = app_client.get("/api/skills/install/apple_local/status?bot_id=admin_bot")
        assert r.status_code == 404
