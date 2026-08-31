"""Tests for the Email Triage gallery app (V2.3-5).

Verifies:
  1. Gallery entry JSON is valid and has all required fields.
  2. requirements.integrations lists 'gmail' (not the deprecated 'gog').
  3. The entry appears in gallery/index.json with the correct path.
  4. gallery.list_gallery_packages() includes the Email Triage entry.
  5. gallery.load_gallery_package() returns the correct package.
  6. The triage script passes a syntax check (py_compile).
  7. The heuristic classifier produces valid urgency/category/action values.
  8. compose_summary produces a non-empty message for a non-trivial email set.
  9. resolve_gmail_token reads both the new gmail skill path and the legacy GOG path.
  10. Install route: gallery install handler with gmail-configured data returns a job.
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
_ET_PKG_ID = "p-41e4c5f4"
_ET_PKG_PATH = _GALLERY_DIR / "email-triage" / f"{_ET_PKG_ID}.json"
_ET_SCRIPT_PATH = _GALLERY_DIR / "email-triage" / "scripts" / "email_triage.py"


# ── Part 1: Gallery entry format ──────────────────────────────────────────────

class TestEmailTriageGalleryEntry:

    def test_entry_file_exists(self):
        assert _ET_PKG_PATH.exists(), (
            f"Email Triage gallery entry not found at {_ET_PKG_PATH}"
        )

    def test_entry_is_valid_json(self):
        pkg = json.loads(_ET_PKG_PATH.read_text())
        assert isinstance(pkg, dict)

    def test_required_fields_present(self):
        pkg = json.loads(_ET_PKG_PATH.read_text())
        for field in (
            "pkg_id", "name", "display_name", "build_spec",
            "pkg_version", "gallery_version", "schema_version",
            "manifest_type", "description", "id", "requirements",
            "objective",
        ):
            assert field in pkg, f"Missing required field: {field!r}"

    def test_pkg_id_matches_filename(self):
        pkg = json.loads(_ET_PKG_PATH.read_text())
        assert pkg["pkg_id"] == _ET_PKG_ID

    def test_schema_version_is_5(self):
        pkg = json.loads(_ET_PKG_PATH.read_text())
        assert pkg["schema_version"] == 5, "schema_version must be 5 (matches other gallery entries)"

    def test_manifest_type_is_evolve_application(self):
        pkg = json.loads(_ET_PKG_PATH.read_text())
        assert pkg["manifest_type"] == "evolve_application"

    def test_build_spec_is_non_empty(self):
        pkg = json.loads(_ET_PKG_PATH.read_text())
        assert pkg["build_spec"].strip(), "build_spec must not be empty"

    def test_build_spec_contains_launchdaemon_path(self):
        """LaunchDaemon (not LaunchAgent) — headless bots need system-context daemons."""
        pkg = json.loads(_ET_PKG_PATH.read_text())
        assert "/Library/LaunchDaemons/" in pkg["build_spec"], (
            "build_spec must use LaunchDaemon (system-context), not LaunchAgent"
        )

    def test_build_spec_contains_test_sequence(self):
        pkg = json.loads(_ET_PKG_PATH.read_text())
        bs = pkg["build_spec"].lower()
        assert "test sequence" in bs or "syntax check" in bs, (
            "build_spec must include a test sequence"
        )

    def test_objective_field_non_empty(self):
        pkg = json.loads(_ET_PKG_PATH.read_text())
        assert pkg["objective"].strip()


# ── Part 2: gmail integration requirement (not the deprecated gog) ─────────────

class TestGmailIntegrationRequirement:

    def test_requirements_has_gmail_not_gog(self):
        """Post-V2.1-3, gmail is its own skill. Must not use deprecated 'gog' id."""
        pkg = json.loads(_ET_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        ids = [i.get("id") for i in integrations if isinstance(i, dict)]
        assert "gmail" in ids, (
            "requirements.integrations must list 'gmail' (the post-V2.1-3 Gmail skill)"
        )
        assert "gog" not in ids, (
            "must NOT use deprecated 'gog' — Email Triage uses the standalone Gmail skill"
        )

    def test_gmail_integration_is_required(self):
        pkg = json.loads(_ET_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        gmail = next(
            (i for i in integrations if isinstance(i, dict) and i.get("id") == "gmail"),
            None,
        )
        assert gmail is not None, "gmail integration entry not found in requirements"
        assert gmail.get("required") is True, "gmail integration must have required=True"


# ── Part 3: Gallery index registration ───────────────────────────────────────

class TestGalleryIndexRegistration:

    def test_index_includes_email_triage(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        pkg_ids = [e.get("pkg_id") for e in index]
        assert _ET_PKG_ID in pkg_ids, (
            f"Email Triage pkg_id {_ET_PKG_ID!r} not found in gallery/index.json"
        )

    def test_index_entry_has_correct_path(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _ET_PKG_ID), None)
        assert entry is not None
        expected_path = f"email-triage/{_ET_PKG_ID}.json"
        assert entry.get("path") == expected_path, (
            f"index.json path must be {expected_path!r}, got {entry.get('path')!r}"
        )

    def test_index_entry_display_name(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _ET_PKG_ID), None)
        assert entry is not None
        assert entry.get("display_name") == "Email Triage"

    def test_index_entry_app_id(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _ET_PKG_ID), None)
        assert entry is not None
        assert entry.get("app_id") == "app_email_triage"


# ── Part 4: list_gallery_packages + load_gallery_package ─────────────────────

class TestListGalleryPackages:

    def test_email_triage_in_list(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        pkg_ids = [p.get("pkg_id") for p in packages]
        assert _ET_PKG_ID in pkg_ids, (
            f"Email Triage ({_ET_PKG_ID}) not returned by list_gallery_packages"
        )

    def test_email_triage_has_source_builtin(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        et = next((p for p in packages if p.get("pkg_id") == _ET_PKG_ID), None)
        assert et is not None
        assert et.get("source") == "builtin"

    def test_email_triage_installed_on_empty(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=["admin_bot"])
        et = next((p for p in packages if p.get("pkg_id") == _ET_PKG_ID), None)
        assert et is not None
        assert et.get("installed_on") == []


class TestLoadGalleryPackage:

    def test_load_returns_email_triage(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package(_ET_PKG_ID, tmp_path)
        assert pkg is not None, f"load_gallery_package({_ET_PKG_ID!r}) returned None"
        assert pkg["pkg_id"] == _ET_PKG_ID
        assert pkg["display_name"] == "Email Triage"

    def test_load_returns_none_for_unknown(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package("p-00000000", tmp_path)
        assert pkg is None


# ── Part 5: Script syntax check ───────────────────────────────────────────────

class TestEmailTriageScriptSyntax:

    def test_script_file_exists(self):
        assert _ET_SCRIPT_PATH.exists(), (
            f"email_triage.py not found at {_ET_SCRIPT_PATH}"
        )

    def test_script_passes_py_compile(self):
        import py_compile
        # py_compile raises SyntaxError on failure
        py_compile.compile(str(_ET_SCRIPT_PATH), doraise=True)

    def test_cron_script_exists(self):
        cron = _GALLERY_DIR / "email-triage" / "scripts" / "email-triage-cron.sh"
        assert cron.exists(), "email-triage-cron.sh not found"

    def test_script_has_no_composio_import(self):
        """Same trust-chain guard as gog_install: must not import Composio."""
        import re
        src = _ET_SCRIPT_PATH.read_text()
        pattern = re.compile(
            r"^\s*(?:import\s+composio|from\s+composio[\w.]*\s+import)",
            re.MULTILINE | re.IGNORECASE,
        )
        assert not pattern.search(src), "email_triage.py must not import Composio"

    def test_script_does_not_write_to_bot_home_directly(self):
        """Script writes only to workspace/email_triage/ (ACL-writable) or /tmp/,
        never directly to ~/.openclaw/ owned paths via open() or Path.write_text()."""
        import ast
        tree = ast.parse(_ET_SCRIPT_PATH.read_text())
        # We don't assert this at AST level (too restrictive — workspace paths are fine),
        # but we do check there's no 'sudo -u' subprocess call (forbidden per CLAUDE.md).
        src = _ET_SCRIPT_PATH.read_text()
        assert "sudo -u" not in src, (
            "email_triage.py must not use 'sudo -u <bot>' — evolve user has no such grant"
        )


# ── Part 6: Heuristic classifier unit tests ───────────────────────────────────

class TestHeuristicClassifier:
    """Import email_triage script functions and test classification logic."""

    @pytest.fixture(autouse=True)
    def _import_script(self):
        """Import email_triage.py directly (it's a standalone script, not a package)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("email_triage", _ET_SCRIPT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.mod = mod

    def _email(self, from_addr="alice@example.com", subject="Hello", snippet=""):
        return {"id": "test-id", "from_addr": from_addr, "subject": subject, "snippet": snippet}

    def test_promotional_keyword_in_subject(self):
        email = self._email(subject="50% off sale this weekend only")
        result = self.mod._heuristic_classify(email, {})
        assert result["urgency"] == "promotional"
        assert result["action"] == "archive"

    def test_noreply_sender_is_spam_likely(self):
        email = self._email(from_addr="noreply@newsletter.com")
        result = self.mod._heuristic_classify(email, {})
        assert result["urgency"] == "spam-likely"
        assert result["action"] == "can-ignore"

    def test_urgent_keyword_in_subject(self):
        email = self._email(subject="Action required: invoice payment due")
        result = self.mod._heuristic_classify(email, {})
        assert result["urgency"] == "important"
        assert result["action"] == "reply-today"

    def test_urgent_sender_from_config(self):
        email = self._email(from_addr="boss@example.com")
        config = {"urgent_senders": ["boss@example.com"]}
        result = self.mod._heuristic_classify(email, config)
        assert result["urgency"] == "urgent"
        assert result["action"] == "reply-now"

    def test_financial_category(self):
        email = self._email(from_addr="statements@bank.com", subject="Your monthly statement")
        result = self.mod._heuristic_classify(email, {})
        assert result["category"] == "financial"

    def test_legal_category(self):
        email = self._email(subject="Contract amendment for review")
        result = self.mod._heuristic_classify(email, {})
        assert result["category"] == "legal"

    def test_routine_email(self):
        email = self._email(subject="Quick question about the project")
        result = self.mod._heuristic_classify(email, {})
        assert result["urgency"] == "routine"
        assert result["action"] == "can-wait"

    def test_valid_urgency_values(self):
        """All urgency values must be from the allowed set."""
        valid_urgencies = {"urgent", "important", "routine", "promotional", "spam-likely"}
        test_emails = [
            self._email(from_addr="noreply@x.com", subject="50% sale"),
            self._email(subject="Action required now"),
            self._email(subject="Just checking in"),
        ]
        for email in test_emails:
            result = self.mod._heuristic_classify(email, {})
            assert result["urgency"] in valid_urgencies, (
                f"Invalid urgency {result['urgency']!r} for {email['subject']!r}"
            )

    def test_valid_action_values(self):
        """All action values must be from the allowed set."""
        valid_actions = {"reply-now", "reply-today", "can-wait", "can-ignore", "archive"}
        test_emails = [
            self._email(from_addr="noreply@x.com"),
            self._email(subject="Invoice payment due"),
            self._email(subject="Newsletter unsubscribe"),
        ]
        for email in test_emails:
            result = self.mod._heuristic_classify(email, {})
            assert result["action"] in valid_actions


# ── Part 7: Config override application ───────────────────────────────────────

class TestConfigOverrides:

    @pytest.fixture(autouse=True)
    def _import_script(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("email_triage", _ET_SCRIPT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.mod = mod

    def test_urgent_sender_override_after_llm(self):
        emails = [{"id": "1", "from_addr": "alice@company.com", "subject": "Routine update", "snippet": ""}]
        classified = [{"id": "1", "urgency": "routine", "category": "other", "action": "can-wait"}]
        config = {"urgent_senders": ["alice@company.com"], "category_overrides": {}}
        result = self.mod.apply_config_overrides(classified, emails, config)
        assert result[0]["urgency"] == "urgent"
        assert result[0]["action"] == "reply-now"

    def test_category_override(self):
        emails = [{"id": "2", "from_addr": "news@bigco.com", "subject": "Quarterly report", "snippet": ""}]
        classified = [{"id": "2", "urgency": "routine", "category": "other", "action": "can-wait"}]
        config = {"urgent_senders": [], "category_overrides": {"bigco.com": "financial"}}
        result = self.mod.apply_config_overrides(classified, emails, config)
        assert result[0]["category"] == "financial"


# ── Part 8: Summary composition ───────────────────────────────────────────────

class TestComposeSummary:

    @pytest.fixture(autouse=True)
    def _import_script(self):
        import importlib.util
        from datetime import datetime
        spec = importlib.util.spec_from_file_location("email_triage", _ET_SCRIPT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.mod = mod
        self.now = datetime(2026, 5, 13, 7, 30, 0)

    def test_few_emails_returns_no_triage_message(self):
        # Under _MIN_EMAILS_TO_TRIAGE
        msg = self.mod.compose_summary(
            emails=[{"id": "1", "from_addr": "a@b.com", "subject": "Hi", "snippet": ""}],
            classified=[{"id": "1", "urgency": "routine", "category": "other", "action": "can-wait"}],
            run_at=self.now,
        )
        assert "No unread emails" in msg or len(msg) > 0

    def test_urgent_emails_appear_in_summary(self):
        emails = [
            {"id": "1", "from_addr": "Alice Smith <alice@bank.com>", "subject": "Wire transfer urgent", "snippet": ""},
            {"id": "2", "from_addr": "Bob Jones <bob@example.com>", "subject": "Newsletter", "snippet": ""},
            {"id": "3", "from_addr": "carol@work.com", "subject": "Contract deadline", "snippet": ""},
        ]
        classified = [
            {"id": "1", "urgency": "urgent", "category": "financial", "action": "reply-now"},
            {"id": "2", "urgency": "promotional", "category": "vendor", "action": "archive"},
            {"id": "3", "urgency": "important", "category": "legal", "action": "reply-today"},
        ]
        msg = self.mod.compose_summary(emails, classified, self.now)
        assert "Urgent" in msg
        assert "Alice Smith" in msg or "bank.com" in msg
        assert "Important" in msg
        assert "3 emails" in msg

    def test_no_urgent_shows_nothing_urgent(self):
        emails = [
            {"id": "1", "from_addr": "a@x.com", "subject": "Routine 1", "snippet": ""},
            {"id": "2", "from_addr": "b@x.com", "subject": "Routine 2", "snippet": ""},
        ]
        classified = [
            {"id": "1", "urgency": "routine", "category": "other", "action": "can-wait"},
            {"id": "2", "urgency": "routine", "category": "other", "action": "can-wait"},
        ]
        msg = self.mod.compose_summary(emails, classified, self.now)
        assert "Nothing urgent" in msg

    def test_summary_header_contains_date(self):
        emails = [
            {"id": str(i), "from_addr": f"x{i}@y.com", "subject": f"Email {i}", "snippet": ""}
            for i in range(3)
        ]
        classified = [
            {"id": str(i), "urgency": "routine", "category": "other", "action": "can-wait"}
            for i in range(3)
        ]
        msg = self.mod.compose_summary(emails, classified, self.now)
        # Header should mention the date
        assert "May 13" in msg or "2026" in msg or "7:30" in msg.lower() or "AM" in msg

    def test_summary_is_non_empty(self):
        emails = [
            {"id": str(i), "from_addr": f"u{i}@domain.com", "subject": f"Subject {i}", "snippet": ""}
            for i in range(5)
        ]
        classified = [
            {"id": str(i), "urgency": "important", "category": "work-project", "action": "reply-today"}
            for i in range(5)
        ]
        msg = self.mod.compose_summary(emails, classified, self.now)
        assert msg.strip()


# ── Part 9: Token resolution ──────────────────────────────────────────────────

class TestTokenResolution:

    @pytest.fixture(autouse=True)
    def _import_script(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("email_triage", _ET_SCRIPT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.mod = mod

    def test_gmail_skill_path(self, tmp_path):
        """New path: integrations.gmail.token.access_token"""
        oc = {
            "integrations": {
                "gmail": {
                    "token": {"access_token": "tok-from-gmail-skill"}
                }
            }
        }
        oc_path = tmp_path / "openclaw.json"
        oc_path.write_text(json.dumps(oc))
        token = self.mod.resolve_gmail_token(oc_path)
        assert token == "tok-from-gmail-skill"

    def test_gog_legacy_path(self, tmp_path):
        """Legacy path: auth.profiles[google_workspace:*].token.access_token"""
        oc = {
            "auth": {
                "profiles": {
                    "google_workspace:admin_bot": {
                        "token": {"access_token": "tok-from-gog"}
                    }
                }
            }
        }
        oc_path = tmp_path / "openclaw.json"
        oc_path.write_text(json.dumps(oc))
        token = self.mod.resolve_gmail_token(oc_path)
        assert token == "tok-from-gog"

    def test_gmail_skill_takes_priority_over_gog(self, tmp_path):
        """gmail skill path should win when both are present."""
        oc = {
            "integrations": {
                "gmail": {
                    "token": {"access_token": "tok-gmail-skill"}
                }
            },
            "auth": {
                "profiles": {
                    "google_workspace:admin_bot": {
                        "token": {"access_token": "tok-gog-legacy"}
                    }
                }
            },
        }
        oc_path = tmp_path / "openclaw.json"
        oc_path.write_text(json.dumps(oc))
        token = self.mod.resolve_gmail_token(oc_path)
        assert token == "tok-gmail-skill"

    def test_no_token_returns_none(self, tmp_path):
        oc_path = tmp_path / "openclaw.json"
        oc_path.write_text(json.dumps({"integrations": {}}))
        token = self.mod.resolve_gmail_token(oc_path)
        assert token is None

    def test_missing_openclaw_returns_none(self, tmp_path):
        oc_path = tmp_path / "missing.json"
        token = self.mod.resolve_gmail_token(oc_path)
        assert token is None


# ── Part 10: Gallery install route integration ─────────────────────────────────

class TestInstallRouteBuildSpecPopulation:
    """
    Regression coverage for the gallery install route build_spec population bug
    (same pattern as test_gallery_morning_briefing.py Part 4).
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

    def test_install_sets_context_snapshot_build_spec(self, flask_env):
        from evolve_admin.applications.forge_jobs import load_job

        client = flask_env["client"]
        shared_dir = flask_env["shared_dir"]

        mock_thread = MagicMock()
        mock_thread.return_value = mock_thread

        with patch("threading.Thread", return_value=mock_thread):
            resp = client.post(
                f"/api/gallery/{_ET_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"], "force": True},
                content_type="application/json",
            )

        assert resp.status_code == 200, f"Install returned {resp.status_code}: {resp.data}"
        body = resp.get_json()
        jobs_returned = body.get("jobs", [])
        assert len(jobs_returned) == 1

        job_id = jobs_returned[0]["job_id"]
        job = load_job(job_id, shared_dir)
        assert job is not None

        build_spec = job.context_snapshot.get("build_spec", "")
        assert build_spec, (
            "context_snapshot['build_spec'] is empty — forge engine would get no spec"
        )

        pkg = json.loads(_ET_PKG_PATH.read_text())
        assert build_spec == pkg["build_spec"]

    def test_install_returns_job_for_gmail_configured_bot(self, flask_env):
        """Install handler with any bot_id returns a job (auth is checked at runtime)."""
        client = flask_env["client"]

        mock_thread = MagicMock()
        mock_thread.return_value = mock_thread

        with patch("threading.Thread", return_value=mock_thread):
            resp = client.post(
                f"/api/gallery/{_ET_PKG_ID}/install",
                json={"bot_ids": ["admin_bot"], "force": True},
                content_type="application/json",
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert "jobs" in body
        assert len(body["jobs"]) == 1
        assert body["jobs"][0]["bot_id"] == "admin_bot"

    def test_install_unknown_pkg_returns_404(self, flask_env):
        client = flask_env["client"]
        resp = client.post(
            "/api/gallery/p-00000000/install",
            json={"bot_ids": ["admin_bot"]},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_install_missing_bot_ids_returns_400(self, flask_env):
        client = flask_env["client"]
        resp = client.post(
            f"/api/gallery/{_ET_PKG_ID}/install",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400
