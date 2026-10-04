"""
test_gallery_oauth_orchestration.py — Coverage for V2-4 in-line OAuth orchestration.

Three flow tests as specified:

1. Satisfied path: bot has GOG configured + install Morning Briefing
   → job goes queued and is dispatched immediately. No awaiting_oauth state.

2. Needed path: bot lacks GOG + install Morning Briefing
   → job goes awaiting_oauth
   → simulate GOG OAuth completion (mock the reader to return active)
   → sweeper transitions to queued
   → dispatch called

3. Abandoned path: bot lacks GOG + install Morning Briefing
   → job goes awaiting_oauth
   → 30+ minutes pass (mocked time)
   → sweeper transitions to failed with reason oauth_abandoned
   → state cleanup verified

Existing gallery install tests are preserved — this file adds new coverage
without touching the existing test_gallery_morning_briefing.py.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ── Path setup (mirrors conftest.py pattern) ──────────────────────────────────
_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY_DIR = _REPO_ROOT / "gallery"
_MB_PKG_ID = "p-a9a74bf7"


# ── Shared fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path):
    """Minimal shared_dir with a network.json (no real bots)."""
    sd = tmp_path / "shared"
    sd.mkdir()
    # Write a minimal network.json in the parent (shared_dir/../network.json)
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"members": ["admin_bot"]}))
    return sd


@pytest.fixture
def flask_env(shared_dir, tmp_path):
    """Flask test client with gallery routes registered."""
    from flask import Flask
    from evolve_admin.web.gallery_routes import register_gallery_routes

    app = Flask(__name__)
    app.config["TESTING"] = True

    network_path = tmp_path / "network.json"
    if not network_path.exists():
        network_path.write_text(json.dumps({"members": ["admin_bot"]}))

    # Patch start_sweeper so it doesn't spin up a real thread in tests
    with patch("evolve_admin.applications.oauth_orchestrator.start_sweeper"):
        register_gallery_routes(app, network_path, shared_dir)

    return {
        "client": app.test_client(),
        "shared_dir": shared_dir,
        "tmp_path": tmp_path,
        "app": app,
    }


def _active_reader(status: str):
    """Return a reader callable that reports GOG as 'active'."""
    from evolve_admin.skills.gog_install import InstallStatus, GOG_SKILL_ID

    def _read_plugin_enabled(bot_id: str) -> bool:
        return True

    def _read_oauth_profile(bot_id: str) -> dict:
        return {"status": "active", "google_account": "test@gmail.com", "services": ["gmail_readonly", "calendar_readonly"]}

    def _read_oauth_client() -> bool:
        return True

    return _read_plugin_enabled, _read_oauth_profile, _read_oauth_client


def _missing_reader():
    """Return reader callables that report GOG as oauth_pending (not configured)."""

    def _read_plugin_enabled(bot_id: str) -> bool:
        return True  # plugin is enabled but no OAuth profile yet

    def _read_oauth_profile(bot_id: str) -> None:
        return None  # no profile → oauth_pending

    def _read_oauth_client() -> bool:
        return True  # client IS configured (pod-wide), just no per-bot profile

    return _read_plugin_enabled, _read_oauth_profile, _read_oauth_client


# ── Test 1: Satisfied path ────────────────────────────────────────────────────


class TestSatisfiedPath:
    """Bot has GOG configured → install proceeds immediately, no awaiting_oauth."""

    def test_job_goes_to_queued_immediately(self, shared_dir):
        """evaluate_install_prerequisites returns satisfied → no awaiting_oauth."""
        from evolve_admin.applications.oauth_orchestrator import evaluate_install_prerequisites
        from evolve_admin.skills.gog_install import GOG_SKILL_ID

        plugin_fn, profile_fn, client_fn = _active_reader("active")

        requirements = {
            "integrations": [
                {"id": "gog", "display_name": "Google (GOG)", "required": True,
                 "reason": "Reads Gmail and Google Calendar for briefing content"}
            ]
        }
        result = evaluate_install_prerequisites(
            "admin_bot", requirements,
            shared_dir=shared_dir,
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )
        assert result["satisfied"] is True
        assert result["missing"] == []

    def test_install_route_dispatches_immediately(self, flask_env):
        """When prereqs satisfied, install route returns 200 with job dispatched.

        The forge_engine is not called in tests (no API key), so we just verify:
        - HTTP 200 response
        - job result shape (no awaiting_oauth)
        - job on disk is in queued state (not awaiting_oauth)

        Path A (2026.06.05-2.0) note: Morning Briefing's preflight now blocks on
        the Calendar Sync app_dependency. We pass force=True to bypass app-dep
        preflight — this test exercises the OAuth-satisfied dispatch path, not
        the app-dep preflight (covered separately in test_gallery_preflight*).
        """
        client = flask_env["client"]
        shared_dir = flask_env["shared_dir"]

        with patch(
            "evolve_admin.applications.oauth_orchestrator.evaluate_install_prerequisites",
            return_value={"satisfied": True, "missing": []},
        ), patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            mock_thread.return_value.start = MagicMock()

            resp = client.post(
                f"/api/gallery/{_MB_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"], "force": True},
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert "jobs" in data
        jobs = data["jobs"]
        # Should have exactly one job for admin_bot
        assert len(jobs) == 1
        assert jobs[0]["bot_id"] == "admin_bot"
        assert "job_id" in jobs[0]
        # No awaiting_oauth status in the result
        assert jobs[0].get("status") != "awaiting_oauth"

        # Job on disk should be queued (not awaiting_oauth)
        from evolve_admin.applications.forge_jobs import load_job
        job_id = jobs[0]["job_id"]
        job_on_disk = load_job(job_id, shared_dir)
        assert job_on_disk is not None
        assert job_on_disk.status == "queued"

    def test_no_awaiting_oauth_state_on_disk(self, shared_dir):
        """When prereqs satisfied, the forge job should be in queued state, not awaiting_oauth."""
        from evolve_admin.applications.forge_jobs import create_install_job, list_active_jobs

        # Manually test the evaluate path, not the route, for simplicity
        from evolve_admin.applications.oauth_orchestrator import evaluate_install_prerequisites

        plugin_fn, profile_fn, client_fn = _active_reader("active")
        reqs = {"integrations": [{"id": "gog", "required": True, "reason": "test"}]}
        result = evaluate_install_prerequisites(
            "admin_bot", reqs,
            shared_dir=shared_dir,
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )
        # Satisfied — no awaiting_oauth jobs should be created
        assert result["satisfied"] is True

        # If we created a job normally (satisfied path), it starts as "queued"
        job = create_install_job(
            pkg_id=_MB_PKG_ID,
            app_id="morning-briefing",
            bot_id="admin_bot",
            gallery_version="2026.05.12-1.0",
            shared_dir=shared_dir,
        )
        assert job.status == "queued"

        jobs = list_active_jobs(shared_dir)
        awaiting = [j for j in jobs if j.status == "awaiting_oauth"]
        assert awaiting == []


# ── Test 2: Needed path ───────────────────────────────────────────────────────


class TestNeededPath:
    """Bot lacks GOG → job goes awaiting_oauth → simulate completion → sweeper resumes."""

    def test_evaluate_returns_missing_when_no_gog(self, shared_dir):
        """evaluate_install_prerequisites returns not-satisfied when GOG missing."""
        from evolve_admin.applications.oauth_orchestrator import evaluate_install_prerequisites

        plugin_fn, profile_fn, client_fn = _missing_reader()

        reqs = {
            "integrations": [
                {"id": "gog", "display_name": "Google (GOG)", "required": True,
                 "reason": "Reads Gmail and Google Calendar for briefing content"}
            ]
        }
        result = evaluate_install_prerequisites(
            "admin_bot", reqs,
            shared_dir=shared_dir,
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )
        assert result["satisfied"] is False
        assert len(result["missing"]) == 1
        item = result["missing"][0]
        assert item["integration_id"] == "gog"
        assert item["action_url"] == "/api/skills/install/gog"
        assert item["action_label"] == "Set up Gmail & Calendar"
        assert "install_plan_steps" in item
        assert len(item["install_plan_steps"]) > 0

    def test_start_oauth_wait_sets_status(self, shared_dir):
        """start_oauth_wait transitions the job to awaiting_oauth."""
        from evolve_admin.applications.forge_jobs import create_install_job, load_job
        from evolve_admin.applications.oauth_orchestrator import start_oauth_wait

        job = create_install_job(
            pkg_id=_MB_PKG_ID,
            app_id="morning-briefing",
            bot_id="admin_bot",
            gallery_version="2026.05.12-1.0",
            shared_dir=shared_dir,
        )
        assert job.status == "queued"

        missing = [
            {
                "integration_id": "gog",
                "skill_id": "gog",
                "display_name": "Google (GOG)",
                "reason": "Reads Gmail and Google Calendar for briefing content",
                "status": "oauth_pending",
                "action_url": "/api/skills/install/gog",
                "action_label": "Set up Gmail & Calendar",
                "install_plan_steps": [],
            }
        ]
        start_oauth_wait(job.job_id, missing, shared_dir)

        reloaded = load_job(job.job_id, shared_dir)
        assert reloaded is not None
        assert reloaded.status == "awaiting_oauth"
        assert "oauth_missing" in reloaded.context_snapshot
        assert "oauth_wait_started" in reloaded.context_snapshot

    def test_get_status_returns_plex_test_shape(self, shared_dir):
        """get_status returns the Plex-test-friendly response shape."""
        from evolve_admin.applications.forge_jobs import create_install_job
        from evolve_admin.applications.oauth_orchestrator import start_oauth_wait, get_status

        job = create_install_job(
            pkg_id=_MB_PKG_ID,
            app_id="morning-briefing",
            bot_id="admin_bot",
            gallery_version="2026.05.12-1.0",
            shared_dir=shared_dir,
        )
        missing = [
            {
                "integration_id": "gog",
                "skill_id": "gog",
                "display_name": "Gmail & Calendar",
                "reason": "Morning Briefing needs to read your calendar and recent emails",
                "status": "oauth_pending",
                "action_url": "/api/skills/install/gog",
                "action_label": "Set up Gmail & Calendar",
                "install_plan_steps": [],
            }
        ]
        start_oauth_wait(job.job_id, missing, shared_dir)

        status = get_status(job.job_id, shared_dir)
        assert status is not None
        assert status["ok"] is True
        assert status["status"] == "awaiting_oauth"
        assert status["job_id"] == job.job_id
        assert len(status["missing"]) == 1
        m = status["missing"][0]
        assert m["integration"] == "Gmail & Calendar"
        assert m["action_url"] == "/api/skills/install/gog"
        assert m["action_label"] == "Set up Gmail & Calendar"
        assert "next" in status
        assert "30 seconds" in status["next"]

    def test_sweeper_resumes_job_when_oauth_satisfied(self, shared_dir):
        """After OAuth completes, sweeper transitions job to queued and calls dispatch."""
        from evolve_admin.applications.forge_jobs import create_install_job, load_job
        from evolve_admin.applications.oauth_orchestrator import (
            start_oauth_wait, sweep_awaiting_oauth_jobs,
        )

        job = create_install_job(
            pkg_id=_MB_PKG_ID,
            app_id="morning-briefing",
            bot_id="admin_bot",
            gallery_version="2026.05.12-1.0",
            shared_dir=shared_dir,
        )
        missing = [
            {
                "integration_id": "gog",
                "skill_id": "gog",
                "display_name": "Google (GOG)",
                "reason": "test",
                "status": "oauth_pending",
                "action_url": "/api/skills/install/gog",
                "action_label": "Set up Gmail & Calendar",
                "install_plan_steps": [],
            }
        ]
        start_oauth_wait(job.job_id, missing, shared_dir)

        # Verify job is awaiting_oauth
        reloaded = load_job(job.job_id, shared_dir)
        assert reloaded.status == "awaiting_oauth"

        # Simulate GOG OAuth completion: inject active readers
        plugin_fn, profile_fn, client_fn = _active_reader("active")

        dispatched: list[tuple] = []

        def _fake_dispatch(job_id: str, bot_id: str) -> None:
            dispatched.append((job_id, bot_id))

        # Run sweeper — should detect GOG is now active and resume
        now = datetime.now(timezone.utc)
        resumed = sweep_awaiting_oauth_jobs(
            shared_dir,
            dispatch_fn=_fake_dispatch,
            now=now,
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )

        assert job.job_id in resumed

        # Job should now be queued
        final = load_job(job.job_id, shared_dir)
        assert final.status == "queued"
        assert final.context_snapshot.get("oauth_resume_reason") == "oauth_satisfied"

        # Dispatch should have been called
        assert len(dispatched) == 1
        assert dispatched[0][0] == job.job_id
        assert dispatched[0][1] == "admin_bot"

    def test_install_route_returns_202_when_missing_oauth(self, flask_env):
        """Install route returns 202 with awaiting_oauth shape when GOG missing.

        S5a note: Morning Briefing's required Calendar Sync dep would now
        route this install onto the dependency-CHAIN path before the OAuth
        check is ever reached. We pin installed_state to "installed" so the
        test keeps exercising the single-app OAuth-needed path (chains are
        covered in test_install_chain.py).
        """
        client = flask_env["client"]

        plugin_fn, profile_fn, client_fn = _missing_reader()

        with patch(
            "evolve_admin.applications.gallery.installed_state",
            return_value="installed",
        ), patch(
            "evolve_admin.applications.oauth_orchestrator.evaluate_install_prerequisites",
        ) as mock_eval:
            mock_eval.return_value = {
                "satisfied": False,
                "missing": [
                    {
                        "integration_id": "gog",
                        "skill_id": "gog",
                        "display_name": "Gmail & Calendar",
                        "reason": "Morning Briefing needs to read your calendar and recent emails",
                        "status": "oauth_pending",
                        "action_url": "/api/skills/install/gog",
                        "action_label": "Set up Gmail & Calendar",
                        "install_plan_steps": [],
                    }
                ],
            }

            resp = client.post(
                f"/api/gallery/{_MB_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"]},
            )

        assert resp.status_code == 202
        data = resp.get_json()
        assert "jobs" in data
        jobs = data["jobs"]
        assert len(jobs) == 1
        job_result = jobs[0]
        assert job_result["status"] == "awaiting_oauth"
        assert job_result["ok"] is True
        assert "missing" in job_result
        assert len(job_result["missing"]) >= 1
        missing_item = job_result["missing"][0]
        assert "action_url" in missing_item
        assert "action_label" in missing_item
        assert "next" in job_result


# ── Test 3: Abandoned path ────────────────────────────────────────────────────


class TestAbandonedPath:
    """Bot lacks GOG → job goes awaiting_oauth → time passes → sweeper abandons it."""

    def test_sweeper_abandons_stale_job(self, shared_dir):
        """After OAUTH_ABANDON_MINUTES, sweeper transitions to failed with oauth_abandoned."""
        from evolve_admin.applications.forge_jobs import create_install_job, load_job
        from evolve_admin.applications.oauth_orchestrator import (
            OAUTH_ABANDON_MINUTES,
            start_oauth_wait,
            sweep_awaiting_oauth_jobs,
        )

        job = create_install_job(
            pkg_id=_MB_PKG_ID,
            app_id="morning-briefing",
            bot_id="admin_bot",
            gallery_version="2026.05.12-1.0",
            shared_dir=shared_dir,
        )
        missing = [
            {
                "integration_id": "gog",
                "skill_id": "gog",
                "display_name": "Google (GOG)",
                "reason": "test",
                "status": "oauth_pending",
                "action_url": "/api/skills/install/gog",
                "action_label": "Set up Gmail & Calendar",
                "install_plan_steps": [],
            }
        ]
        start_oauth_wait(job.job_id, missing, shared_dir)

        # Verify job is awaiting_oauth
        reloaded = load_job(job.job_id, shared_dir)
        assert reloaded.status == "awaiting_oauth"

        # Still missing (readers still return no GOG)
        plugin_fn, profile_fn, client_fn = _missing_reader()
        dispatched: list = []

        def _fake_dispatch(job_id: str, bot_id: str) -> None:
            dispatched.append(job_id)

        # Simulate time past the abandon threshold
        future = datetime.now(timezone.utc) + timedelta(minutes=OAUTH_ABANDON_MINUTES + 1)
        resumed = sweep_awaiting_oauth_jobs(
            shared_dir,
            dispatch_fn=_fake_dispatch,
            now=future,
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )

        # Should NOT be in resumed — it was abandoned
        assert job.job_id not in resumed
        assert dispatched == []

        # Job should be failed with oauth_abandoned reason
        final = load_job(job.job_id, shared_dir)
        assert final.status == "failed"
        assert final.context_snapshot.get("oauth_abandon_reason") == "oauth_abandoned"

    def test_sweeper_does_not_abandon_fresh_job(self, shared_dir):
        """A job that just entered awaiting_oauth is NOT abandoned on next sweep."""
        from evolve_admin.applications.forge_jobs import create_install_job, load_job
        from evolve_admin.applications.oauth_orchestrator import (
            OAUTH_ABANDON_MINUTES,
            start_oauth_wait,
            sweep_awaiting_oauth_jobs,
        )

        job = create_install_job(
            pkg_id=_MB_PKG_ID,
            app_id="morning-briefing",
            bot_id="admin_bot",
            gallery_version="2026.05.12-1.0",
            shared_dir=shared_dir,
        )
        missing = [
            {
                "integration_id": "gog",
                "skill_id": "gog",
                "display_name": "Google (GOG)",
                "reason": "test",
                "status": "oauth_pending",
                "action_url": "/api/skills/install/gog",
                "action_label": "Set up Gmail & Calendar",
                "install_plan_steps": [],
            }
        ]
        start_oauth_wait(job.job_id, missing, shared_dir)

        plugin_fn, profile_fn, client_fn = _missing_reader()

        # Sweep with "now" at exactly the threshold (not past it)
        just_below_abandon = datetime.now(timezone.utc) + timedelta(
            minutes=OAUTH_ABANDON_MINUTES - 1
        )
        resumed = sweep_awaiting_oauth_jobs(
            shared_dir,
            dispatch_fn=lambda jid, bid: None,
            now=just_below_abandon,
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )

        assert job.job_id not in resumed

        # Job should still be awaiting_oauth
        final = load_job(job.job_id, shared_dir)
        assert final.status == "awaiting_oauth"

    def test_cancel_job_transitions_to_failed(self, shared_dir):
        """cancel_job() transitions an awaiting_oauth job to failed."""
        from evolve_admin.applications.forge_jobs import create_install_job, load_job
        from evolve_admin.applications.oauth_orchestrator import start_oauth_wait, cancel_job

        job = create_install_job(
            pkg_id=_MB_PKG_ID,
            app_id="morning-briefing",
            bot_id="admin_bot",
            gallery_version="2026.05.12-1.0",
            shared_dir=shared_dir,
        )
        start_oauth_wait(job.job_id, [], shared_dir)

        cancel_job(job.job_id, shared_dir, reason="test_cancel")

        final = load_job(job.job_id, shared_dir)
        assert final.status == "failed"
        assert final.context_snapshot.get("oauth_cancel_reason") == "test_cancel"


# ── Test 4: install route on Morning Briefing manifest (integration) ──────────


class TestInstallRouteIntegration:
    """Path A (2026.06.05-2.0): Morning Briefing is a composer over Calendar Sync
    and Email Integration. It no longer declares gmail/calendar directly — those
    integrations live on the prerequisite data-foundation apps. These tests
    exercise the same OAuth orchestration logic against Email Integration's
    direct-integration shape (gmail) so the orchestrator coverage stays intact."""

    _EI_PKG_ID = "p-341576fa"  # Email Integration — has gmail in requirements.integrations

    def test_mb_manifest_has_no_direct_integrations(self):
        """Path A invariant: Morning Briefing routes gmail/calendar through
        app_dependencies on Calendar Sync + Email Integration, NOT through
        requirements.integrations. The 'gog' legacy id must also stay absent."""
        mb_path = _GALLERY_DIR / "morning-briefing" / f"{_MB_PKG_ID}.json"
        pkg = json.loads(mb_path.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        assert integrations == [], (
            "Morning Briefing must declare zero direct integrations under Path A — "
            "data flows from sibling apps via app_dependencies. "
            f"Found: {[i.get('id') for i in integrations if isinstance(i, dict)]}"
        )
        app_dep_ids = {
            d.get("pkg_id")
            for d in pkg.get("app_dependencies", [])
            if isinstance(d, dict)
        }
        assert "p-fe9acef3" in app_dep_ids, "Calendar Sync must be an app_dependency"
        assert "p-341576fa" in app_dep_ids, "Email Integration must be an app_dependency"

    def test_evaluate_with_email_integration_requirements(self, shared_dir):
        """evaluate_install_prerequisites correctly handles an app with a direct
        gmail integration. Email Integration is the canonical single-integration
        app under Path A — gmail must be satisfied for its install to proceed."""
        from evolve_admin.applications.oauth_orchestrator import evaluate_install_prerequisites

        ei_path = _GALLERY_DIR / "email-integration" / f"{self._EI_PKG_ID}.json"
        pkg = json.loads(ei_path.read_text())
        requirements = pkg.get("requirements", {})

        # Missing — no OAuth profile → gmail is unsatisfied
        plugin_fn, profile_fn, client_fn = _missing_reader()
        result = evaluate_install_prerequisites(
            "admin_bot", requirements,
            shared_dir=shared_dir,
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )
        assert result["satisfied"] is False
        missing_ids = {m["integration_id"] for m in result["missing"]}
        assert "gmail" in missing_ids, (
            "gmail must appear in missing when no OAuth profile exists"
        )

        # Active — profile with gmail scope satisfies the gmail provider
        plugin_fn, profile_fn, client_fn = _active_reader("active")
        result = evaluate_install_prerequisites(
            "admin_bot", requirements,
            shared_dir=shared_dir,
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )
        assert result["satisfied"] is True
        assert result["missing"] == []


# ── Sweeper re-check with full requirement entries (S1 follow-up) ─────────────
# The sweeper used to reconstruct requirements as bare {"id": ...} entries from
# the missing snapshot, dropping the manifest's check_path / alternatives[] /
# setup_doc. An operator who satisfied a requirement via a declared alternative
# (e.g. path-C service-account DwD) DURING the 30-minute awaiting_oauth window
# was never detected — the job abandoned anyway. start_oauth_wait now stores
# the full requirements dict and the sweeper re-evaluates with the full entries.


_CALENDAR_SYNC_PKG_PATH = _GALLERY_DIR / "calendar-sync" / "p-fe9acef3.json"


def _calendar_sync_requirements() -> dict:
    """The real Calendar Sync requirements block (google_calendar with a
    declared path-C alternative) — the production shape this fix exists for."""
    pkg = json.loads(_CALENDAR_SYNC_PKG_PATH.read_text())
    integ = pkg["requirements"]["integrations"][0]
    # Drift guards — mirror test_oauth_orchestrator.py's fixture assertions.
    assert integ["id"] == "google_calendar"
    assert integ.get("alternatives"), "calendar-sync no longer declares alternatives"
    return pkg["requirements"]


def _patch_integration_checker(monkeypatch, fn):
    """Replace the gallery preflight satisfaction check the orchestrator
    consults for declared alternatives. fn(bot_id, req) → (state, message)."""
    import evolve_admin.applications.gallery as gallery_mod
    monkeypatch.setattr(gallery_mod, "check_integration_requirement", fn)


class TestSweeperFullRequirementReEval:
    """Sweeper re-checks use the manifest's full requirement entries."""

    def _awaiting_job(self, shared_dir, *, requirements=None):
        """Create a job in awaiting_oauth with the calendar integration missing.

        The missing item mirrors what ``_calendar_build_missing_item`` really
        emits for a manifest ``google_calendar`` requirement: the item's
        ``integration_id`` is CALENDAR_SKILL_ID (``"calendar"``), NOT the
        manifest id. The sweeper must bridge that namespace gap when matching
        the item back to the stored full requirement entry.
        """
        from evolve_admin.applications.forge_jobs import create_install_job
        from evolve_admin.applications.oauth_orchestrator import start_oauth_wait

        job = create_install_job(
            pkg_id="p-fe9acef3",
            app_id="calendar-sync",
            bot_id="admin_bot",
            gallery_version="2026.05.12-1.0",
            shared_dir=shared_dir,
        )
        missing = [
            {
                "integration_id": "calendar",
                "skill_id": "calendar",
                "display_name": "Google Calendar",
                "reason": "Calendar access required",
                "status": "oauth_pending",
                "action_url": "/api/skills/install/calendar",
                "action_label": "Set up Google Calendar",
                "install_plan_steps": [],
            }
        ]
        start_oauth_wait(job.job_id, missing, shared_dir, requirements=requirements)
        return job

    def test_start_oauth_wait_stores_full_requirements(self, shared_dir):
        """start_oauth_wait persists the requirements dict on the snapshot;
        omitting it (legacy callers) leaves the snapshot without the key."""
        from evolve_admin.applications.forge_jobs import load_job

        reqs = _calendar_sync_requirements()
        job = self._awaiting_job(shared_dir, requirements=reqs)
        reloaded = load_job(job.job_id, shared_dir)
        assert reloaded.context_snapshot["oauth_requirements"] == reqs

        legacy = self._awaiting_job(shared_dir)
        reloaded = load_job(legacy.job_id, shared_dir)
        assert "oauth_requirements" not in reloaded.context_snapshot

    def test_sweeper_passes_full_entries_to_evaluate(self, shared_dir):
        """The sweeper's re-check receives the manifest entries with
        check_path / alternatives / setup_doc intact — not bare ids — even
        though the missing item carries the provider's skill id ("calendar")
        while the stored entry uses the manifest id ("google_calendar")."""
        from evolve_admin.applications.oauth_orchestrator import sweep_awaiting_oauth_jobs

        self._awaiting_job(shared_dir, requirements=_calendar_sync_requirements())

        captured: list[dict] = []

        def _capture_eval(bot_id, requirements, **kwargs):
            captured.append(requirements)
            return {"satisfied": False, "missing": [{"integration_id": "google_calendar"}]}

        with patch(
            "evolve_admin.oauth.orchestrator.evaluate_install_prerequisites",
            side_effect=_capture_eval,
        ):
            sweep_awaiting_oauth_jobs(
                shared_dir,
                dispatch_fn=lambda job_id, bot_id: None,
                now=datetime.now(timezone.utc),
            )

        # Other tests in this process can spawn dispatch daemon threads whose
        # own evaluate calls land inside this patch window — filter to the
        # sweep of OUR job (the one whose entry resolved to the manifest id).
        ours = [
            reqs for reqs in captured
            if any(
                e.get("id") == "google_calendar"
                for e in reqs.get("integrations", [])
            )
        ]
        assert len(ours) == 1, (
            f"sweeper re-check never received the full google_calendar entry "
            f"(captured: {captured})"
        )
        entries = ours[0]["integrations"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["id"] == "google_calendar"
        assert entry.get("alternatives"), "sweeper re-check dropped alternatives[]"
        assert "check_path" in entry, "sweeper re-check dropped check_path"

    def test_sweeper_resumes_when_alternative_satisfied_during_wait(self, shared_dir, monkeypatch):
        """The S1 follow-up scenario: the calendar provider still says missing
        (no OAuth profile), but the operator satisfied the declared path-C
        alternative during the wait window → sweeper resumes the job."""
        from evolve_admin.applications.forge_jobs import load_job
        from evolve_admin.applications.oauth_orchestrator import sweep_awaiting_oauth_jobs

        job = self._awaiting_job(shared_dir, requirements=_calendar_sync_requirements())

        def _checker(bot_id, req):
            # Faithful to the real check_integration_requirement contract: it
            # walks the primary requirement THEN each declared alternative,
            # first match wins (mirrors test_oauth_orchestrator.py's fixture).
            for entry in [req, *(req.get("alternatives") or [])]:
                if entry.get("id") == "google_path_c":
                    return "satisfied", "path-C service-account configured"
            return "missing", "not configured"
        _patch_integration_checker(monkeypatch, _checker)

        plugin_fn, profile_fn, client_fn = _missing_reader()
        dispatched: list[tuple] = []
        resumed = sweep_awaiting_oauth_jobs(
            shared_dir,
            dispatch_fn=lambda job_id, bot_id: dispatched.append((job_id, bot_id)),
            now=datetime.now(timezone.utc),
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )

        assert job.job_id in resumed
        final = load_job(job.job_id, shared_dir)
        assert final.status == "queued"
        assert final.context_snapshot.get("oauth_resume_reason") == "oauth_satisfied"
        assert dispatched == [(job.job_id, "admin_bot")]

    def test_legacy_job_without_requirements_stays_waiting(self, shared_dir, monkeypatch):
        """A job whose snapshot predates oauth_requirements falls back to the
        bare-id re-check: the alternative satisfaction is invisible (the very
        bug this fix addresses for new jobs) and the job keeps waiting — the
        sweeper must not crash on the old snapshot shape."""
        from evolve_admin.applications.forge_jobs import load_job
        from evolve_admin.applications.oauth_orchestrator import sweep_awaiting_oauth_jobs

        job = self._awaiting_job(shared_dir)  # no requirements stored

        def _checker(bot_id, req):
            # Same faithful contract as above — the bare-id fallback req has
            # no alternatives, so path-C satisfaction stays invisible.
            for entry in [req, *(req.get("alternatives") or [])]:
                if entry.get("id") == "google_path_c":
                    return "satisfied", "path-C service-account configured"
            return "missing", "not configured"
        _patch_integration_checker(monkeypatch, _checker)

        plugin_fn, profile_fn, client_fn = _missing_reader()
        resumed = sweep_awaiting_oauth_jobs(
            shared_dir,
            dispatch_fn=lambda job_id, bot_id: None,
            now=datetime.now(timezone.utc),
            read_plugin_enabled=plugin_fn,
            read_oauth_profile=profile_fn,
            read_oauth_client_configured=client_fn,
        )

        assert resumed == []
        final = load_job(job.job_id, shared_dir)
        assert final.status == "awaiting_oauth"
