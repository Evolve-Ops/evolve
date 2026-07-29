"""Tests for Morning Briefing gallery entry + gallery install build_spec fix.

Verifies:
  1. Morning Briefing gallery entry is valid JSON with required fields.
  2. Entry appears in the builtin gallery index with the correct pkg_id.
  3. gallery.list_gallery_packages() includes Morning Briefing.
  4. gallery_routes install handler populates context_snapshot["build_spec"]
     from the gallery package before dispatching the forge job — without this
     fix, forge_engine.assemble_context_package returns an empty build_spec
     for first-install jobs (no manifest yet) and the LLM gets no spec.
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

# Gallery root is 4 levels up from packages/admin/evolve_admin/applications/gallery.py
# but we compute it from the test file for clarity.
_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY_DIR = _REPO_ROOT / "gallery"
_MB_PKG_ID = "p-a9a74bf7"
_MB_PKG_PATH = _GALLERY_DIR / "morning-briefing" / f"{_MB_PKG_ID}.json"


# ── Part 1: Gallery entry format ──────────────────────────────────────────────

class TestMorningBriefingGalleryEntry:

    def test_entry_file_exists(self):
        assert _MB_PKG_PATH.exists(), (
            f"Morning Briefing gallery entry not found at {_MB_PKG_PATH}"
        )

    def test_entry_is_valid_json(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert isinstance(pkg, dict)

    def test_required_fields_present(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        for field in ("pkg_id", "name", "display_name", "build_spec",
                      "pkg_version", "gallery_version", "schema_version",
                      "manifest_type", "description", "id", "requirements"):
            assert field in pkg, f"Missing required field: {field!r}"

    def test_pkg_id_matches_filename(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert pkg["pkg_id"] == _MB_PKG_ID

    def test_build_spec_is_non_empty(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert pkg["build_spec"].strip(), "build_spec must not be empty"

    def test_build_spec_contains_launchdaemon_path(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert "/Library/LaunchDaemons/ai.evolve.{bot_id}.morning-briefing.plist" in pkg["build_spec"], (
            "build_spec must include the correct LaunchDaemon plist path"
        )

    def test_build_spec_contains_test_sequence(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert "Test sequence" in pkg["build_spec"] or "test sequence" in pkg["build_spec"].lower(), (
            "build_spec must include a test sequence"
        )

    def test_requirements_integrations_is_empty(self):
        """Path A (2026.06.05-2.0): Morning Briefing is a composer over Calendar Sync
        and Email Integration. It does NOT call Google APIs directly, so it declares
        no direct integrations. All external data is routed via app_dependencies."""
        pkg = json.loads(_MB_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        assert integrations == [], (
            "Morning Briefing must declare zero direct integrations — data flows via "
            "app_dependencies on Calendar Sync + Email Integration. Found: "
            f"{[i.get('id') for i in integrations if isinstance(i, dict)]}"
        )

    def test_app_dependencies_include_calendar_sync_and_email_integration(self):
        """Path A: Calendar Sync (required) and Email Integration (optional) are the
        data foundations Morning Briefing reads from."""
        pkg = json.loads(_MB_PKG_PATH.read_text())
        deps = pkg.get("app_dependencies", [])
        by_id = {d.get("pkg_id"): d for d in deps if isinstance(d, dict)}
        assert "p-fe9acef3" in by_id, (
            "Calendar Sync (p-fe9acef3) must be declared as an app_dependency"
        )
        assert by_id["p-fe9acef3"].get("required") is True, (
            "Calendar Sync must be required=True — Morning Briefing's schedule block "
            "depends on memory/calendar-today.json"
        )
        assert "p-341576fa" in by_id, (
            "Email Integration (p-341576fa) must be declared as an app_dependency"
        )
        # Email Integration is optional — the briefing degrades to schedule-only.
        assert by_id["p-341576fa"].get("required") is False, (
            "Email Integration must be required=False — Morning Briefing degrades "
            "to schedule-only when email-digest.json is absent"
        )

    def test_objective_field_present(self):
        """objective field is used by the gallery install modal UI."""
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert "objective" in pkg, "objective field required for gallery install UI"
        assert pkg["objective"].strip()

    def test_schema_version_is_5(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert pkg["schema_version"] == 5, "schema_version must match existing gallery entries"

    def test_manifest_type_is_evolve_application(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert pkg["manifest_type"] == "evolve_application"


# ── Part 2: Gallery index registration ───────────────────────────────────────

class TestGalleryIndexRegistration:

    def test_index_includes_morning_briefing(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        pkg_ids = [e.get("pkg_id") for e in index]
        assert _MB_PKG_ID in pkg_ids, (
            f"Morning Briefing pkg_id {_MB_PKG_ID!r} not found in gallery/index.json"
        )

    def test_index_entry_has_correct_path(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _MB_PKG_ID), None)
        assert entry is not None
        expected_path = f"morning-briefing/{_MB_PKG_ID}.json"
        assert entry.get("path") == expected_path, (
            f"index.json path must be {expected_path!r}, got {entry.get('path')!r}"
        )

    def test_index_entry_display_name(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _MB_PKG_ID), None)
        assert entry is not None
        assert entry.get("display_name") == "Morning Briefing"


# ── Part 3: gallery.list_gallery_packages includes Morning Briefing ───────────

class TestListGalleryPackages:

    def test_morning_briefing_in_list(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        pkg_ids = [p.get("pkg_id") for p in packages]
        assert _MB_PKG_ID in pkg_ids, (
            f"Morning Briefing ({_MB_PKG_ID}) not returned by list_gallery_packages"
        )

    def test_morning_briefing_has_source_builtin(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        mb = next((p for p in packages if p.get("pkg_id") == _MB_PKG_ID), None)
        assert mb is not None
        assert mb.get("source") == "builtin"

    def test_morning_briefing_installed_on_empty(self, tmp_path):
        """installed_on should be empty when no bots have it installed."""
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=["admin_bot"])
        mb = next((p for p in packages if p.get("pkg_id") == _MB_PKG_ID), None)
        assert mb is not None
        assert mb.get("installed_on") == []


# ── Part 4: install route populates context_snapshot["build_spec"] ───────────

class TestInstallRouteBuildSpecPopulation:
    """
    Regression test for the gallery install route bug:
    create_install_job does not populate context_snapshot["build_spec"],
    so forge_engine.assemble_context_package gets an empty build_spec for
    first-install jobs that have no existing manifest.

    The fix in gallery_routes.py sets job.context_snapshot["build_spec"]
    from the gallery package before dispatching.
    """

    @pytest.fixture
    def flask_env(self, tmp_path):
        """Set up a minimal Flask test client for gallery routes."""
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

    def test_install_sets_context_snapshot_build_spec(self, flask_env):
        """
        After POSTing /api/gallery/<pkg_id>/install, the created forge job
        must have context_snapshot["build_spec"] populated from the gallery
        package, so forge_engine can generate a valid implementation even
        when no manifest exists yet.

        _dispatch_forge_job is a closure inside register_gallery_routes, so
        we patch threading.Thread to prevent the forge job from actually
        running while still letting the route handler finish synchronously.
        """
        from evolve_admin.applications.forge_jobs import load_job

        client = flask_env["client"]
        shared_dir = flask_env["shared_dir"]

        # Patch threading.Thread so forge engine doesn't actually run
        mock_thread = MagicMock()
        mock_thread.return_value = mock_thread  # thread() returns itself

        with patch("threading.Thread", return_value=mock_thread):
            resp = client.post(
                f"/api/gallery/{_MB_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"], "force": True},
                content_type="application/json",
            )

        assert resp.status_code == 200, f"Install returned {resp.status_code}: {resp.data}"
        body = resp.get_json()
        jobs_returned = body.get("jobs", [])
        assert len(jobs_returned) == 1, f"Expected 1 job, got: {jobs_returned}"

        job_id = jobs_returned[0]["job_id"]
        job = load_job(job_id, shared_dir)
        assert job is not None, f"Job {job_id!r} not found on disk"

        # The critical assertion: build_spec must be set in context_snapshot
        build_spec = job.context_snapshot.get("build_spec", "")
        assert build_spec, (
            "context_snapshot['build_spec'] is empty — forge engine would receive no spec. "
            "The install handler must copy build_spec from the gallery package into "
            "job.context_snapshot before dispatching."
        )

        # Verify the build_spec is actually the one from the gallery entry
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert build_spec == pkg["build_spec"], (
            "context_snapshot['build_spec'] does not match the gallery package's build_spec"
        )

    def test_install_returns_job_id(self, flask_env):
        """Install response includes job_id and bot_id for the created job."""
        client = flask_env["client"]

        mock_thread = MagicMock()
        mock_thread.return_value = mock_thread

        with patch("threading.Thread", return_value=mock_thread):
            resp = client.post(
                f"/api/gallery/{_MB_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"], "force": True},
                content_type="application/json",
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert "jobs" in body
        assert len(body["jobs"]) == 1
        job = body["jobs"][0]
        assert "job_id" in job
        assert job["bot_id"] == "admin_bot"
        assert job["job_id"].startswith("j-")

    def test_install_unknown_pkg_returns_404(self, flask_env):
        """Installing an unknown pkg_id should return 404."""
        client = flask_env["client"]
        resp = client.post(
            "/api/gallery/p-00000000/install",
            json={"bot_ids": ["admin_bot"]},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_install_missing_bot_ids_returns_400(self, flask_env):
        """Installing without bot_ids should return 400."""
        client = flask_env["client"]
        resp = client.post(
            f"/api/gallery/{_MB_PKG_ID}/install",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400


# ── Part 5: load_gallery_package returns Morning Briefing ────────────────────

class TestLoadGalleryPackage:

    def test_load_returns_morning_briefing(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package(_MB_PKG_ID, tmp_path)
        assert pkg is not None, f"load_gallery_package({_MB_PKG_ID!r}) returned None"
        assert pkg["pkg_id"] == _MB_PKG_ID
        assert pkg["display_name"] == "Morning Briefing"

    def test_load_returns_none_for_unknown(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package("p-00000000", tmp_path)
        assert pkg is None
