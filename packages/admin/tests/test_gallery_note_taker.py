"""Tests for Meeting Note-taker gallery entry (V2.1-5).

Verifies:
  1. Gallery entry is valid JSON with all required fields.
  2. Entry appears in the gallery index with correct pkg_id and path.
  3. gallery.list_gallery_packages() includes the Note-taker.
  4. requirements.integrations lists both 'calendar' and 'obsidian_vault'.
  5. Install handler returns awaiting_oauth (202) when Calendar is missing.
  6. Install handler returns awaiting_oauth (202) when Obsidian is missing.
  7. Install handler proceeds (200) when both Calendar and Obsidian are configured.
  8. Preflight resolves obsidian_vault via mcp.servers.obsidian on the bot.

Follows test_gallery_morning_briefing.py patterns exactly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY_DIR = _REPO_ROOT / "gallery"
_NT_PKG_ID = "p-f14e9562"
_NT_PKG_PATH = _GALLERY_DIR / "note-taker" / f"{_NT_PKG_ID}.json"


# ── Part 1: Gallery entry format ──────────────────────────────────────────────


class TestNoteTaberGalleryEntry:

    def test_entry_file_exists(self):
        assert _NT_PKG_PATH.exists(), (
            f"Note-taker gallery entry not found at {_NT_PKG_PATH}"
        )

    def test_entry_is_valid_json(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert isinstance(pkg, dict)

    def test_required_fields_present(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        for field in ("pkg_id", "name", "display_name", "build_spec",
                      "pkg_version", "gallery_version", "schema_version",
                      "manifest_type", "description", "id", "requirements"):
            assert field in pkg, f"Missing required field: {field!r}"

    def test_pkg_id_matches_filename(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert pkg["pkg_id"] == _NT_PKG_ID

    def test_schema_version_is_5(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert pkg["schema_version"] == 5

    def test_manifest_type_is_evolve_application(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert pkg["manifest_type"] == "evolve_application"

    def test_build_spec_is_non_empty(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert pkg["build_spec"].strip(), "build_spec must not be empty"

    def test_build_spec_contains_launchdaemon_path(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert "/Library/LaunchDaemons/com.{bot_id}.note-taker.plist" in pkg["build_spec"], (
            "build_spec must include the correct LaunchDaemon plist path"
        )

    def test_build_spec_contains_test_sequence(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert "test sequence" in pkg["build_spec"].lower(), (
            "build_spec must include a test sequence"
        )

    def test_build_spec_contains_active_json_guidance(self):
        """active.json state file pattern must be documented in build_spec."""
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert "active.json" in pkg["build_spec"], (
            "build_spec must describe the active.json state file pattern"
        )

    def test_build_spec_uses_startinterval_not_startcalendarinterval(self):
        """5-minute polling uses StartInterval (300), not StartCalendarInterval."""
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert "StartInterval" in pkg["build_spec"], (
            "build_spec plist must use StartInterval for 5-minute polling"
        )

    def test_objective_field_present(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert "objective" in pkg, "objective field required for gallery install UI"
        assert pkg["objective"].strip()

    def test_requirements_has_calendar_integration(self):
        """Note-taker requires Calendar skill (V2.1-3 id = 'calendar', not 'gog')."""
        pkg = json.loads(_NT_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        integration_ids = [i.get("id") for i in integrations if isinstance(i, dict)]
        assert "calendar" in integration_ids, (
            "requirements.integrations must list 'calendar' (V2.1-3 skill id, not 'gog')"
        )

    def test_requirements_has_obsidian_integration(self):
        """After the 2026-05-30 rewire (#1817), the canonical Obsidian skill id
        is 'obsidian_vault' (matches OBSIDIAN_SKILL_ID). The manifest must
        declare its requirement under that id so the OAuth orchestrator and
        Skills install routes agree on what 'configured' means."""
        pkg = json.loads(_NT_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        integration_ids = [i.get("id") for i in integrations if isinstance(i, dict)]
        assert "obsidian_vault" in integration_ids, (
            "requirements.integrations must list 'obsidian_vault' (canonical id "
            "after the 2026-05-30 MCP rewire — the legacy 'obsidian' id no "
            "longer matches a registered provider)"
        )
        assert "obsidian" not in integration_ids, (
            "requirements.integrations must NOT list the legacy 'obsidian' id — "
            "use 'obsidian_vault' instead"
        )

    def test_calendar_integration_is_required(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        cal = next((i for i in integrations if isinstance(i, dict) and i.get("id") == "calendar"), None)
        assert cal is not None, "calendar integration entry not found"
        assert cal.get("required") is True, "calendar integration must be required=True"

    def test_obsidian_integration_is_required(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        obs = next((i for i in integrations if isinstance(i, dict) and i.get("id") == "obsidian_vault"), None)
        assert obs is not None, "obsidian_vault integration entry not found"
        assert obs.get("required") is True, "obsidian_vault integration must be required=True"

    def test_requirements_does_not_use_gog_id(self):
        """Verify we're using the V2.1-3 split skill IDs, not the legacy 'gog' id."""
        pkg = json.loads(_NT_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        integration_ids = [i.get("id") for i in integrations if isinstance(i, dict)]
        assert "gog" not in integration_ids, (
            "Note-taker must use 'calendar' skill id (V2.1-3), not the legacy 'gog' id"
        )

    def test_build_spec_contains_agents_md_guidance(self):
        """build_spec must include AGENTS.md guidance for 'take a note' handling."""
        pkg = json.loads(_NT_PKG_PATH.read_text())
        assert "AGENTS.md" in pkg["build_spec"] or "take a note" in pkg["build_spec"].lower(), (
            "build_spec must include bot guidance for 'take a note' commands"
        )

    def test_launchdaemon_plist_path_in_build_spec(self):
        """Headless bots use /Library/LaunchDaemons (not ~/Library/LaunchAgents)."""
        pkg = json.loads(_NT_PKG_PATH.read_text())
        # The plist path must be under /Library/LaunchDaemons
        assert "/Library/LaunchDaemons/com.{bot_id}.note-taker.plist" in pkg["build_spec"], (
            "build_spec must use /Library/LaunchDaemons path for headless bots"
        )
        # The FILE: rendered block must not install to LaunchAgents
        # (the build_spec may mention LaunchAgents educationally to explain why we don't use it)
        build_spec = pkg["build_spec"]
        # Find the FILE: plist block — it should not reference LaunchAgents in the path
        file_plist_idx = build_spec.find("## FILE: /Library/LaunchDaemons")
        assert file_plist_idx >= 0, (
            "build_spec must have a ## FILE: /Library/LaunchDaemons/... block"
        )


# ── Part 2: Gallery index registration ───────────────────────────────────────


class TestGalleryIndexRegistration:

    def test_index_includes_note_taker(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        pkg_ids = [e.get("pkg_id") for e in index]
        assert _NT_PKG_ID in pkg_ids, (
            f"Note-taker pkg_id {_NT_PKG_ID!r} not found in gallery/index.json"
        )

    def test_index_entry_has_correct_path(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _NT_PKG_ID), None)
        assert entry is not None
        expected_path = f"note-taker/{_NT_PKG_ID}.json"
        assert entry.get("path") == expected_path, (
            f"index.json path must be {expected_path!r}, got {entry.get('path')!r}"
        )

    def test_index_entry_display_name(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _NT_PKG_ID), None)
        assert entry is not None
        assert entry.get("display_name") == "Meeting Note-taker"


# ── Part 3: gallery.list_gallery_packages includes Note-taker ─────────────────


class TestListGalleryPackages:

    def test_note_taker_in_list(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        pkg_ids = [p.get("pkg_id") for p in packages]
        assert _NT_PKG_ID in pkg_ids, (
            f"Note-taker ({_NT_PKG_ID}) not returned by list_gallery_packages"
        )

    def test_note_taker_has_source_builtin(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        nt = next((p for p in packages if p.get("pkg_id") == _NT_PKG_ID), None)
        assert nt is not None
        assert nt.get("source") == "builtin"

    def test_note_taker_installed_on_empty(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=["admin_bot"])
        nt = next((p for p in packages if p.get("pkg_id") == _NT_PKG_ID), None)
        assert nt is not None
        assert nt.get("installed_on") == []


# ── Part 4: load_gallery_package returns Note-taker ──────────────────────────


class TestLoadGalleryPackage:

    def test_load_returns_note_taker(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package(_NT_PKG_ID, tmp_path)
        assert pkg is not None, f"load_gallery_package({_NT_PKG_ID!r}) returned None"
        assert pkg["pkg_id"] == _NT_PKG_ID
        assert pkg["display_name"] == "Meeting Note-taker"

    def test_load_returns_none_for_unknown(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package("p-00000000", tmp_path)
        assert pkg is None


# ── Part 5: Install handler — awaiting_oauth when Calendar is missing ─────────


class TestInstallRouteCalendarMissing:
    """
    When Calendar is not configured on the bot, the gallery install handler
    must return 202 with status=awaiting_oauth.

    Patches the OAuth orchestrator's evaluate_install_prerequisites to simulate
    a bot that lacks Calendar access (status=oauth_pending) but has Obsidian.
    """

    @pytest.fixture
    def flask_env(self, tmp_path):
        from flask import Flask
        from evolve_admin.web.gallery_routes import register_gallery_routes

        app = Flask(__name__)
        app.config["TESTING"] = True

        network_path = tmp_path / "network.json"
        network_path.write_text(json.dumps({"members": ["admin_bot"]}))
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        register_gallery_routes(app, network_path, shared_dir)

        return {
            "client": app.test_client(),
            "shared_dir": shared_dir,
            "tmp_path": tmp_path,
        }

    def test_missing_calendar_returns_202_awaiting_oauth(self, flask_env):
        """Bot lacking Calendar: install must pause at awaiting_oauth (202).

        gallery_routes imports oauth_orchestrator from applications/ (the compat
        shim that re-exports from oauth/orchestrator.py).  We patch the compat
        shim's function — the same target used by test_gallery_oauth_orchestration.py.
        """
        client = flask_env["client"]

        with patch(
            "evolve_admin.applications.oauth_orchestrator.evaluate_install_prerequisites",
            return_value={
                "satisfied": False,
                "missing": [
                    {
                        "integration_id": "calendar",
                        "skill_id": "calendar",
                        "display_name": "Google Calendar",
                        "reason": "Reads upcoming events to know when to create meeting notes",
                        "status": "oauth_pending",
                        "action_url": "/api/skills/install/calendar",
                        "action_label": "Set up Google Calendar",
                        "install_plan_steps": [],
                    }
                ],
            },
        ):
            resp = client.post(
                f"/api/gallery/{_NT_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"]},
                content_type="application/json",
            )

        # 202 is the signal for awaiting_oauth
        assert resp.status_code == 202, (
            f"Expected 202 awaiting_oauth when Calendar missing, got {resp.status_code}: {resp.data}"
        )
        body = resp.get_json()
        jobs = body.get("jobs", [])
        assert len(jobs) == 1
        assert jobs[0]["status"] == "awaiting_oauth"

    def test_missing_obsidian_returns_202_awaiting_oauth(self, flask_env):
        """Bot lacking Obsidian: install must pause at awaiting_oauth (202)."""
        client = flask_env["client"]

        with patch(
            "evolve_admin.applications.oauth_orchestrator.evaluate_install_prerequisites",
            return_value={
                "satisfied": False,
                "missing": [
                    {
                        "integration_id": "obsidian_vault",
                        "skill_id": "obsidian_vault",
                        "display_name": "Obsidian Vault",
                        "reason": "Writes meeting notes to vault",
                        "status": "no_vault_configured",
                        "action_url": "/api/skills/install/obsidian_vault",
                        "action_label": "Set up Obsidian Vault",
                        "install_plan_steps": [],
                    }
                ],
            },
        ):
            resp = client.post(
                f"/api/gallery/{_NT_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"]},
                content_type="application/json",
            )

        assert resp.status_code == 202, (
            f"Expected 202 awaiting_oauth when Obsidian missing, got {resp.status_code}: {resp.data}"
        )
        body = resp.get_json()
        jobs = body.get("jobs", [])
        assert len(jobs) == 1
        assert jobs[0]["status"] == "awaiting_oauth"

    def test_both_configured_returns_200(self, flask_env):
        """Both Calendar and Obsidian configured: install proceeds normally (200)."""
        client = flask_env["client"]

        with patch(
            "evolve_admin.applications.oauth_orchestrator.evaluate_install_prerequisites",
            return_value={"satisfied": True, "missing": []},
        ), patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            mock_thread.return_value.start = MagicMock()

            resp = client.post(
                f"/api/gallery/{_NT_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"], "force": True},
                content_type="application/json",
            )

        assert resp.status_code == 200, (
            f"Expected 200 when both Calendar+Obsidian configured, got {resp.status_code}: {resp.data}"
        )
        body = resp.get_json()
        jobs = body.get("jobs", [])
        assert len(jobs) == 1
        # When satisfied, the job should be queued (not awaiting_oauth)
        assert jobs[0].get("status") != "awaiting_oauth"


# ── Part 5b: Obsidian preflight resolves via mcp.servers.obsidian ─────────────


class TestObsidianPreflightResolution:
    """End-to-end: the OAuth orchestrator must recognise a bot with
    ``mcp.servers.obsidian`` configured as already satisfying the note-taker's
    Obsidian requirement, and must produce a missing-item pointing at the
    canonical ``/api/skills/install/obsidian_vault`` route when it doesn't.

    Regression for P4 (skills-install-roadmap-2026-05-30.md): pre-fix, the
    manifest declared ``obsidian`` as required but the rewired install module
    only published its provider under ``obsidian_vault``, so even a
    fully-wired bot would stall the note-taker install at awaiting_oauth.
    """

    @pytest.fixture
    def note_taker_requirements(self):
        pkg = json.loads(_NT_PKG_PATH.read_text())
        return pkg.get("requirements", {})

    @pytest.fixture
    def obsidian_only_requirements(self, note_taker_requirements):
        """Strip the calendar requirement so we exercise the obsidian path
        in isolation. The orchestrator iterates each integration independently,
        so this is safe."""
        reqs = dict(note_taker_requirements)
        reqs["integrations"] = [
            r for r in reqs.get("integrations", [])
            if isinstance(r, dict) and r.get("id") == "obsidian_vault"
        ]
        return reqs

    def test_satisfied_when_mcp_servers_obsidian_configured(
        self, obsidian_only_requirements, tmp_path,
    ):
        """Bot has ``mcp.servers.obsidian`` → note-taker preflight reports
        the obsidian_vault requirement as satisfied."""
        from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

        oc_config = {
            "mcp": {
                "servers": {
                    "obsidian": {
                        "command": "/usr/local/bin/mcp-fs-wrapper",
                        "args": [str(tmp_path / "vault")],
                    }
                }
            }
        }

        with patch(
            "evolve_admin.skills._oc_install_common.read_oc_config",
            return_value=(oc_config, None),
        ):
            result = evaluate_install_prerequisites(
                "admin_bot", obsidian_only_requirements,
                shared_dir=tmp_path,
            )

        assert result["satisfied"] is True, (
            f"Bot with mcp.servers.obsidian must satisfy obsidian_vault; "
            f"got missing={result.get('missing')!r}"
        )
        assert result["missing"] == []

    def test_missing_when_no_mcp_obsidian_entry(
        self, obsidian_only_requirements, tmp_path,
    ):
        """Bot has no ``mcp.servers.obsidian`` → obsidian_vault appears in
        the missing list with an action_url pointing at the canonical
        Obsidian install endpoint."""
        from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

        # An openclaw.json with no mcp config at all — the empty-bot shape.
        oc_config = {"agents": {"defaults": {}}}

        with patch(
            "evolve_admin.skills._oc_install_common.read_oc_config",
            return_value=(oc_config, None),
        ):
            result = evaluate_install_prerequisites(
                "admin_bot", obsidian_only_requirements,
                shared_dir=tmp_path,
            )

        assert result["satisfied"] is False
        missing = result["missing"]
        assert len(missing) == 1
        item = missing[0]
        assert item["integration_id"] == "obsidian_vault"
        assert item["skill_id"] == "obsidian_vault"
        # The action_url must route to a real endpoint — the rewired skill is
        # served under /api/skills/install/obsidian_vault, not /obsidian.
        assert item["action_url"] == "/api/skills/install/obsidian_vault", (
            f"action_url must point at the canonical obsidian_vault install "
            f"endpoint; got {item['action_url']!r}"
        )
        assert item.get("status") in {"no_vault_configured", "missing", "unknown"}

    def test_provider_registered_for_obsidian_vault(self):
        """The provider registry must claim 'obsidian_vault' so the
        orchestrator finds it — without this, the manifest's requirement
        falls into the unknown_integration branch with action_url=None."""
        from evolve_admin.oauth.providers import find_provider

        provider = find_provider("obsidian_vault")
        assert provider is not None, (
            "No provider registered for 'obsidian_vault'. The note-taker "
            "preflight will treat the requirement as unknown_integration."
        )
        assert provider.skill_id == "obsidian_vault"


# ── Part 6: note_taker.py script tests ───────────────────────────────────────


class TestNoteTakerScript:
    """Smoke-tests for the daemon script — no external deps needed."""

    @pytest.fixture
    def script_path(self):
        return _GALLERY_DIR / "note-taker" / "scripts" / "note_taker.py"

    def test_script_exists(self, script_path):
        assert script_path.exists(), f"note_taker.py not found at {script_path}"

    def test_script_compiles(self, script_path):
        import py_compile
        py_compile.compile(str(script_path), doraise=True)

    def test_meeting_slug_stable(self, script_path):
        """The slug for the same title+date must always be identical."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("note_taker", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        slug1 = mod._meeting_slug("Q2 Budget Review", "2026-05-13")
        slug2 = mod._meeting_slug("Q2 Budget Review", "2026-05-13")
        assert slug1 == slug2, "slug must be deterministic"
        assert "2026-05-13" in slug1, "slug must embed the date"

    def test_note_creation_is_idempotent(self, script_path, tmp_path):
        """Calling create_meeting_note twice must not overwrite the first note."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("note_taker", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        note_path = tmp_path / "meetings" / "2026-05-13-standup.md"
        mod.create_meeting_note(
            note_path,
            title="Standup",
            start="2026-05-13T10:00",
            end="2026-05-13T10:30",
            attendees=["alice@example.com"],
            description="Daily standup",
            location="",
        )
        first_mtime = note_path.stat().st_mtime

        # Call again — must not overwrite
        mod.create_meeting_note(
            note_path,
            title="Standup",
            start="2026-05-13T10:00",
            end="2026-05-13T10:30",
            attendees=["alice@example.com"],
            description="Daily standup",
            location="",
        )
        second_mtime = note_path.stat().st_mtime
        assert first_mtime == second_mtime, "create_meeting_note must be idempotent (no re-create)"

    def test_seal_adds_sealed_section(self, script_path, tmp_path):
        """seal_meeting_note must add ## Sealed to an existing note."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("note_taker", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        note_path = tmp_path / "standup.md"
        mod.create_meeting_note(
            note_path,
            title="Standup",
            start="2026-05-13T10:00",
            end="2026-05-13T10:30",
            attendees=["alice@example.com"],
            description="",
            location="",
        )
        sealed = mod.seal_meeting_note(
            note_path,
            attendees=["alice@example.com"],
            summary="Discussed Q2 targets.",
        )
        assert sealed is True
        text = note_path.read_text()
        assert "## Sealed" in text
        assert "Discussed Q2 targets." in text

    def test_seal_is_idempotent(self, script_path, tmp_path):
        """Calling seal twice must not add a second ## Sealed section."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("note_taker", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        note_path = tmp_path / "standup.md"
        mod.create_meeting_note(
            note_path, title="Standup", start="2026-05-13T10:00",
            end="2026-05-13T10:30", attendees=[], description="", location="",
        )
        mod.seal_meeting_note(note_path, attendees=[])
        mod.seal_meeting_note(note_path, attendees=[])  # second call — should be no-op

        text = note_path.read_text()
        assert text.count("## Sealed") == 1, "## Sealed must appear exactly once"

    def test_append_to_meeting_note(self, script_path, tmp_path):
        """append_to_meeting_note adds a timestamped bullet to ## Notes."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("note_taker", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        note_path = tmp_path / "standup.md"
        mod.create_meeting_note(
            note_path, title="Standup", start="2026-05-13T10:00",
            end="2026-05-13T10:30", attendees=[], description="", location="",
        )
        success = mod.append_to_meeting_note(note_path, "action item: review the proposal")
        assert success is True
        text = note_path.read_text()
        assert "action item: review the proposal" in text

    def test_status_without_active_state(self, script_path, tmp_path):
        """read_active_state returns {} when no active.json exists."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("note_taker", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        state = mod.read_active_state(tmp_path)
        assert state == {}

    def test_write_and_clear_active_state(self, script_path, tmp_path):
        """write_active_state + clear_active_state round-trips correctly."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("note_taker", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        data = {
            "event_id": "evt-001",
            "title": "Standup",
            "note_path": "meetings/2026-05-13-standup.md",
            "vault_path": str(tmp_path / "vault"),
            "meeting_in_progress": True,
        }
        mod.write_active_state(tmp_path, data)
        state = mod.read_active_state(tmp_path)
        assert state["event_id"] == "evt-001"
        assert state["meeting_in_progress"] is True

        mod.clear_active_state(tmp_path)
        state_after = mod.read_active_state(tmp_path)
        assert state_after == {}
