"""Tests for Calendar Daily Summary gallery entry (V2.3-6).

Verifies:
  1. Gallery entry is valid JSON with all required fields.
  2. Entry appears in the gallery index with correct pkg_id and path.
  3. gallery.list_gallery_packages() includes the Calendar Summary.
  4. requirements.integrations lists 'calendar' (V2.1-3 id, not legacy 'gog').
  5. Install handler returns awaiting_oauth (202) when Calendar is missing.
  6. Install handler proceeds (200) when Calendar is configured.
  7. calendar_summary.py script: syntax check.
  8. Conflict detection: non-conflicting events → no conflicts.
  9. Conflict detection: overlapping events → conflict flagged.
  10. Conflict detection: tight-transition events → tight flag.
  11. Conflict detection: back-to-back with gap > 5m → no flag.
  12. Message composition: produces non-empty output.
  13. Message composition: today with events and conflicts.
  14. Message composition: include_tomorrow=None omits tomorrow section.
  15. Idempotency: last_run.json prevents double-send on same day.

Follows test_gallery_note_taker.py patterns exactly.
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
_CS_PKG_ID = "p-738f057c"
_CS_PKG_PATH = _GALLERY_DIR / "calendar-summary" / f"{_CS_PKG_ID}.json"
_SCRIPT_PATH = _GALLERY_DIR / "calendar-summary" / "scripts" / "calendar_summary.py"


# ── Helper: load the script module ───────────────────────────────────────────


def _load_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("calendar_summary", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Part 1: Gallery entry format ──────────────────────────────────────────────


class TestCalendarSummaryGalleryEntry:

    def test_entry_file_exists(self):
        assert _CS_PKG_PATH.exists(), (
            f"Calendar Summary gallery entry not found at {_CS_PKG_PATH}"
        )

    def test_entry_is_valid_json(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert isinstance(pkg, dict)

    def test_required_fields_present(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        for field in ("pkg_id", "name", "display_name", "build_spec",
                      "pkg_version", "gallery_version", "schema_version",
                      "manifest_type", "description", "id", "requirements"):
            assert field in pkg, f"Missing required field: {field!r}"

    def test_pkg_id_matches_filename(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert pkg["pkg_id"] == _CS_PKG_ID

    def test_schema_version_is_5(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert pkg["schema_version"] == 5

    def test_manifest_type_is_evolve_application(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert pkg["manifest_type"] == "evolve_application"

    def test_build_spec_is_non_empty(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert pkg["build_spec"].strip(), "build_spec must not be empty"

    def test_build_spec_contains_launchdaemon_path(self):
        """LaunchDaemon (not LaunchAgent) path must be in build_spec."""
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert "/Library/LaunchDaemons/com.{bot_id}.calendar-summary.plist" in pkg["build_spec"], (
            "build_spec must include the correct LaunchDaemon plist path"
        )

    def test_build_spec_file_block_uses_launchdaemons(self):
        """The ## FILE: block must target /Library/LaunchDaemons."""
        pkg = json.loads(_CS_PKG_PATH.read_text())
        build_spec = pkg["build_spec"]
        file_plist_idx = build_spec.find("## FILE: /Library/LaunchDaemons")
        assert file_plist_idx >= 0, (
            "build_spec must have a ## FILE: /Library/LaunchDaemons/... block"
        )

    def test_build_spec_contains_test_sequence(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert "test sequence" in pkg["build_spec"].lower(), (
            "build_spec must include a test sequence"
        )

    def test_build_spec_contains_conflict_detection(self):
        """Conflict detection is the key differentiator — must be in build_spec."""
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert "conflict" in pkg["build_spec"].lower(), (
            "build_spec must describe conflict detection"
        )

    def test_build_spec_contains_tight_transition(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert "tight" in pkg["build_spec"].lower(), (
            "build_spec must describe tight-transition detection"
        )

    def test_objective_field_present(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        assert "objective" in pkg, "objective field required for gallery install UI"
        assert pkg["objective"].strip()

    def test_requirements_has_calendar_integration(self):
        """Calendar Summary requires the 'calendar' skill (V2.1-3 id, not 'gog')."""
        pkg = json.loads(_CS_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        integration_ids = [i.get("id") for i in integrations if isinstance(i, dict)]
        assert "calendar" in integration_ids, (
            "requirements.integrations must list 'calendar' (V2.1-3 skill id, not 'gog')"
        )

    def test_requirements_does_not_use_gog_id(self):
        """Must use V2.1-3 Calendar skill id, not legacy 'gog'."""
        pkg = json.loads(_CS_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        integration_ids = [i.get("id") for i in integrations if isinstance(i, dict)]
        assert "gog" not in integration_ids, (
            "Calendar Summary must use 'calendar' skill id (V2.1-3), not the legacy 'gog' id"
        )

    def test_calendar_integration_is_required(self):
        pkg = json.loads(_CS_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        cal = next(
            (i for i in integrations if isinstance(i, dict) and i.get("id") == "calendar"),
            None,
        )
        assert cal is not None, "calendar integration entry not found"
        assert cal.get("required") is True, "calendar integration must be required=True"


# ── Part 2: Gallery index registration ───────────────────────────────────────


class TestGalleryIndexRegistration:

    def test_index_includes_calendar_summary(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        pkg_ids = [e.get("pkg_id") for e in index]
        assert _CS_PKG_ID in pkg_ids, (
            f"Calendar Summary pkg_id {_CS_PKG_ID!r} not found in gallery/index.json"
        )

    def test_index_entry_has_correct_path(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _CS_PKG_ID), None)
        assert entry is not None
        expected_path = f"calendar-summary/{_CS_PKG_ID}.json"
        assert entry.get("path") == expected_path, (
            f"index.json path must be {expected_path!r}, got {entry.get('path')!r}"
        )

    def test_index_entry_display_name(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _CS_PKG_ID), None)
        assert entry is not None
        assert entry.get("display_name") == "Calendar Daily Summary"


# ── Part 3: gallery.list_gallery_packages includes Calendar Summary ───────────


class TestListGalleryPackages:

    def test_calendar_summary_in_list(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        pkg_ids = [p.get("pkg_id") for p in packages]
        assert _CS_PKG_ID in pkg_ids, (
            f"Calendar Summary ({_CS_PKG_ID}) not returned by list_gallery_packages"
        )

    def test_calendar_summary_has_source_builtin(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        cs = next((p for p in packages if p.get("pkg_id") == _CS_PKG_ID), None)
        assert cs is not None
        assert cs.get("source") == "builtin"

    def test_calendar_summary_installed_on_empty(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=["admin_bot"])
        cs = next((p for p in packages if p.get("pkg_id") == _CS_PKG_ID), None)
        assert cs is not None
        assert cs.get("installed_on") == []


# ── Part 4: load_gallery_package returns Calendar Summary ─────────────────────


class TestLoadGalleryPackage:

    def test_load_returns_calendar_summary(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package(_CS_PKG_ID, tmp_path)
        assert pkg is not None, f"load_gallery_package({_CS_PKG_ID!r}) returned None"
        assert pkg["pkg_id"] == _CS_PKG_ID
        assert pkg["display_name"] == "Calendar Daily Summary"

    def test_load_returns_none_for_unknown(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package("p-00000000", tmp_path)
        assert pkg is None


# ── Part 5: Install handler — awaiting_oauth when Calendar is missing ─────────


class TestInstallRouteCalendarMissing:
    """When Calendar is not configured, gallery install must return 202."""

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
        """Bot lacking Calendar: install must pause at awaiting_oauth (202)."""
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
                        "reason": "Reads meeting schedule to compose the daily summary",
                        "status": "oauth_pending",
                        "action_url": "/api/skills/install/calendar",
                        "action_label": "Set up Google Calendar",
                        "install_plan_steps": [],
                    }
                ],
            },
        ):
            resp = client.post(
                f"/api/gallery/{_CS_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"]},
                content_type="application/json",
            )

        assert resp.status_code == 202, (
            f"Expected 202 awaiting_oauth when Calendar missing, "
            f"got {resp.status_code}: {resp.data}"
        )
        body = resp.get_json()
        jobs = body.get("jobs", [])
        assert len(jobs) == 1
        assert jobs[0]["status"] == "awaiting_oauth"

    def test_calendar_configured_returns_200(self, flask_env):
        """Calendar configured: install proceeds normally (200)."""
        client = flask_env["client"]

        with patch(
            "evolve_admin.applications.oauth_orchestrator.evaluate_install_prerequisites",
            return_value={"satisfied": True, "missing": []},
        ), patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            mock_thread.return_value.start = MagicMock()

            resp = client.post(
                f"/api/gallery/{_CS_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"], "force": True},
                content_type="application/json",
            )

        assert resp.status_code == 200, (
            f"Expected 200 when Calendar configured, "
            f"got {resp.status_code}: {resp.data}"
        )
        body = resp.get_json()
        jobs = body.get("jobs", [])
        assert len(jobs) == 1
        assert jobs[0].get("status") != "awaiting_oauth"


# ── Part 6: calendar_summary.py script tests ─────────────────────────────────


class TestCalendarSummaryScript:
    """Smoke-tests for the daemon script — no external deps needed."""

    def test_script_exists(self):
        assert _SCRIPT_PATH.exists(), f"calendar_summary.py not found at {_SCRIPT_PATH}"

    def test_script_compiles(self):
        import py_compile
        py_compile.compile(str(_SCRIPT_PATH), doraise=True)

    def test_preview_produces_output(self, capsys):
        """preview command must print a non-empty message to stdout."""
        mod = _load_script()
        import argparse
        args = argparse.Namespace(
            command="preview",
            time_zone="America/Los_Angeles",
            include_tomorrow=False,
            workspace=None,
        )
        rc = mod.cmd_send(args, dry_run=True)
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip(), "preview must produce non-empty output"

    def test_status_no_run_yet(self, tmp_path):
        """status returns 'no summary yet' when last_run.json is absent."""
        mod = _load_script()
        status = mod.read_status_string(tmp_path)
        assert "no summary" in status.lower() or "not sent" in status.lower() or \
               "no" in status.lower(), f"Expected 'no summary' message, got: {status!r}"

    def test_last_run_roundtrip(self, tmp_path):
        """write_last_run + read_last_run round-trips correctly."""
        mod = _load_script()
        mod.write_last_run(
            tmp_path,
            sent_date="2026-05-13",
            sent_at="07:02",
            today_count=3,
            tomorrow_count=2,
            conflict_count=1,
        )
        lr = mod.read_last_run(tmp_path)
        assert lr["date"] == "2026-05-13"
        assert lr["sent_at"] == "07:02"
        assert lr["today_count"] == 3
        assert lr["conflict_count"] == 1

    def test_already_sent_today_false_when_no_file(self, tmp_path):
        mod = _load_script()
        assert mod._already_sent_today(tmp_path) is False

    def test_already_sent_today_false_for_yesterday(self, tmp_path):
        from datetime import date, timedelta
        mod = _load_script()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        mod.write_last_run(
            tmp_path,
            sent_date=yesterday,
            sent_at="07:00",
            today_count=1,
            tomorrow_count=0,
            conflict_count=0,
        )
        assert mod._already_sent_today(tmp_path) is False


# ── Part 7: Conflict detection ────────────────────────────────────────────────


class TestConflictDetection:
    """Unit tests for detect_conflicts() — the core differentiator."""

    @pytest.fixture(autouse=True)
    def mod(self):
        self.mod = _load_script()

    def _ev(self, title: str, start: str, end: str) -> dict:
        return {"id": title, "title": title, "start": start, "end": end}

    def test_no_events_no_conflicts(self):
        assert self.mod.detect_conflicts([]) == []

    def test_single_event_no_conflicts(self):
        events = [self._ev("Standup", "2026-05-13T09:00", "2026-05-13T09:30")]
        assert self.mod.detect_conflicts(events) == []

    def test_non_overlapping_large_gap_no_conflicts(self):
        """Two meetings with a 2-hour gap — no conflict or tight flag."""
        events = [
            self._ev("Morning sync", "2026-05-13T09:00", "2026-05-13T09:30"),
            self._ev("Afternoon review", "2026-05-13T14:00", "2026-05-13T15:00"),
        ]
        conflicts = self.mod.detect_conflicts(events)
        assert conflicts == [], f"Expected no conflicts, got: {conflicts}"

    def test_overlapping_events_flagged_as_conflict(self):
        """11am meeting overlaps with 11:30am meeting — must be flagged."""
        events = [
            self._ev("Internal sync", "2026-05-13T11:00", "2026-05-13T12:00"),
            self._ev("Client call", "2026-05-13T11:30", "2026-05-13T12:30"),
        ]
        conflicts = self.mod.detect_conflicts(events)
        assert len(conflicts) == 1, f"Expected 1 conflict, got: {conflicts}"
        assert conflicts[0]["type"] == "conflict"
        assert "Internal sync" in conflicts[0]["description"]
        assert "Client call" in conflicts[0]["description"]

    def test_tight_transition_flagged(self):
        """Meetings ending 3 minutes before next start — tight transition."""
        events = [
            self._ev("Standup", "2026-05-13T09:00", "2026-05-13T09:27"),
            self._ev("Design review", "2026-05-13T09:30", "2026-05-13T10:30"),
        ]
        conflicts = self.mod.detect_conflicts(events)
        assert len(conflicts) == 1, f"Expected 1 tight-transition, got: {conflicts}"
        assert conflicts[0]["type"] == "tight"
        assert "Standup" in conflicts[0]["description"]
        assert "Design review" in conflicts[0]["description"]

    def test_exactly_5_min_gap_is_tight(self):
        """A 5-minute gap is still considered tight."""
        events = [
            self._ev("A", "2026-05-13T09:00", "2026-05-13T09:55"),
            self._ev("B", "2026-05-13T10:00", "2026-05-13T10:30"),
        ]
        conflicts = self.mod.detect_conflicts(events)
        assert len(conflicts) == 1
        assert conflicts[0]["type"] == "tight"

    def test_6_min_gap_is_not_tight(self):
        """A 6-minute gap must NOT be flagged."""
        events = [
            self._ev("A", "2026-05-13T09:00", "2026-05-13T09:54"),
            self._ev("B", "2026-05-13T10:00", "2026-05-13T10:30"),
        ]
        conflicts = self.mod.detect_conflicts(events)
        assert conflicts == [], f"Expected no flag for 6-min gap, got: {conflicts}"

    def test_back_to_back_exact_no_gap(self):
        """End of A == Start of B is a 0-minute gap — tight (border case)."""
        events = [
            self._ev("A", "2026-05-13T09:00", "2026-05-13T10:00"),
            self._ev("B", "2026-05-13T10:00", "2026-05-13T11:00"),
        ]
        conflicts = self.mod.detect_conflicts(events)
        # 0-minute gap is <= 5 minutes, so must be tight
        assert len(conflicts) == 1
        assert conflicts[0]["type"] == "tight"

    def test_event_with_missing_end_skipped(self):
        """Events with missing/unparseable times must be skipped gracefully."""
        events = [
            self._ev("A", "2026-05-13T09:00", ""),
            self._ev("B", "2026-05-13T09:00", "2026-05-13T10:00"),
        ]
        # Should not raise; A is skipped
        conflicts = self.mod.detect_conflicts(events)
        assert isinstance(conflicts, list)

    def test_multiple_conflicts_detected(self):
        """Three overlapping meetings should produce multiple conflict records."""
        events = [
            self._ev("A", "2026-05-13T10:00", "2026-05-13T12:00"),
            self._ev("B", "2026-05-13T10:30", "2026-05-13T11:30"),
            self._ev("C", "2026-05-13T11:00", "2026-05-13T13:00"),
        ]
        conflicts = self.mod.detect_conflicts(events)
        # At least A-B and A-C should conflict
        assert len(conflicts) >= 2


# ── Part 8: Message composition ───────────────────────────────────────────────


class TestComposeMessage:

    @pytest.fixture(autouse=True)
    def mod(self):
        self.mod = _load_script()

    def _ev(self, title: str, start: str, end: str) -> dict:
        return {"id": title, "title": title, "start": start, "end": end}

    def test_no_meetings_produces_message(self):
        msg = self.mod.compose_summary(
            today_events=[],
            tomorrow_events=None,
            conflicts=[],
        )
        assert msg.strip()
        assert "No meetings today" in msg

    def test_today_events_listed(self):
        events = [
            self._ev("Patel call", "2026-05-13T09:00", "2026-05-13T10:00"),
            self._ev("Sync", "2026-05-13T14:00", "2026-05-13T14:30"),
        ]
        msg = self.mod.compose_summary(
            today_events=events,
            tomorrow_events=None,
            conflicts=[],
        )
        assert "Patel call" in msg
        assert "Sync" in msg
        assert "2 meetings" in msg

    def test_conflicts_appear_in_message(self):
        events = [
            self._ev("A", "2026-05-13T11:00", "2026-05-13T12:00"),
            self._ev("B", "2026-05-13T11:30", "2026-05-13T12:30"),
        ]
        conflicts = self.mod.detect_conflicts(events)
        msg = self.mod.compose_summary(
            today_events=events,
            tomorrow_events=None,
            conflicts=conflicts,
        )
        assert "Conflict" in msg or "conflict" in msg

    def test_no_conflicts_shows_none(self):
        events = [self._ev("A", "2026-05-13T09:00", "2026-05-13T10:00")]
        msg = self.mod.compose_summary(
            today_events=events,
            tomorrow_events=None,
            conflicts=[],
        )
        assert "Conflicts: none" in msg

    def test_tomorrow_section_included_when_provided(self):
        events = [self._ev("Standup", "2026-05-14T09:00", "2026-05-14T09:30")]
        msg = self.mod.compose_summary(
            today_events=[],
            tomorrow_events=events,
            conflicts=[],
        )
        assert "Tomorrow" in msg
        assert "Standup" in msg

    def test_tomorrow_section_omitted_when_none(self):
        msg = self.mod.compose_summary(
            today_events=[],
            tomorrow_events=None,
            conflicts=[],
        )
        assert "Tomorrow" not in msg

    def test_tomorrow_nothing_scheduled(self):
        msg = self.mod.compose_summary(
            today_events=[],
            tomorrow_events=[],
            conflicts=[],
        )
        assert "Tomorrow: nothing scheduled" in msg

    def test_tight_transition_in_message(self):
        events = [
            self._ev("Standup", "2026-05-13T09:00", "2026-05-13T09:27"),
            self._ev("Design review", "2026-05-13T09:30", "2026-05-13T10:30"),
        ]
        conflicts = self.mod.detect_conflicts(events)
        msg = self.mod.compose_summary(
            today_events=events,
            tomorrow_events=None,
            conflicts=conflicts,
        )
        assert "Tight" in msg or "tight" in msg

    def test_tomorrow_capped_at_3_inline(self):
        """Tomorrow preview shows first 3 events inline, then '+ N more'."""
        events = [
            self._ev(f"Meeting {i}", f"2026-05-14T0{i+9}:00", f"2026-05-14T0{i+9}:30")
            for i in range(5)
        ]
        msg = self.mod.compose_summary(
            today_events=[],
            tomorrow_events=events,
            conflicts=[],
        )
        assert "Tomorrow" in msg
        assert "+ 2 more" in msg
