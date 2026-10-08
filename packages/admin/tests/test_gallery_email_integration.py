"""Tests for the Email Integration gallery app (V2.4-1 fix).

Verifies:
  1. Gallery entry JSON is valid and has all required fields.
  2. requirements.integrations lists 'gmail' (not the deprecated 'gog').
  3. build_spec references LaunchDaemon, not LaunchAgent (V1.1-2 lesson:
     headless bots run without a user session so LaunchAgents never load).
  4. build_spec plist path uses /Library/LaunchDaemons/ (system context).
  5. build_spec plist includes UserName key so the daemon runs as the bot user.
  6. The entry appears in gallery/index.json with the correct path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY_DIR = _REPO_ROOT / "gallery"
_EI_PKG_ID = "p-341576fa"
_EI_PKG_PATH = _GALLERY_DIR / "email-integration" / f"{_EI_PKG_ID}.json"


# ── Part 1: Gallery entry format ──────────────────────────────────────────────

class TestEmailIntegrationGalleryEntry:

    def test_entry_file_exists(self):
        assert _EI_PKG_PATH.exists(), (
            f"Email Integration gallery entry not found at {_EI_PKG_PATH}"
        )

    def test_entry_is_valid_json(self):
        pkg = json.loads(_EI_PKG_PATH.read_text())
        assert isinstance(pkg, dict)

    def test_required_fields_present(self):
        pkg = json.loads(_EI_PKG_PATH.read_text())
        for field in ("pkg_id", "name", "display_name", "build_spec",
                      "pkg_version", "gallery_version", "schema_version",
                      "manifest_type", "description", "id", "requirements"):
            assert field in pkg, f"Missing required field: {field!r}"

    def test_pkg_id_matches_filename(self):
        pkg = json.loads(_EI_PKG_PATH.read_text())
        assert pkg["pkg_id"] == _EI_PKG_ID

    def test_schema_version_is_5(self):
        pkg = json.loads(_EI_PKG_PATH.read_text())
        assert pkg["schema_version"] == 5

    def test_manifest_type_is_evolve_application(self):
        pkg = json.loads(_EI_PKG_PATH.read_text())
        assert pkg["manifest_type"] == "evolve_application"


# ── Part 2: Integration requirement ──────────────────────────────────────────

class TestEmailIntegrationRequirements:

    def test_requirements_lists_gmail_not_gog(self):
        """Email Integration uses the V2.1-3 gmail skill, not the legacy gog."""
        pkg = json.loads(_EI_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        integration_ids = {i.get("id") for i in integrations if isinstance(i, dict)}
        assert "gmail" in integration_ids, (
            "requirements.integrations must list 'gmail' (V2.1-3+ skill id)"
        )
        assert "gog" not in integration_ids, (
            "requirements.integrations must NOT reference deprecated 'gog' — use 'gmail'"
        )

    def test_gmail_integration_is_required(self):
        pkg = json.loads(_EI_PKG_PATH.read_text())
        integrations = pkg.get("requirements", {}).get("integrations", [])
        gmail_entry = next(
            (i for i in integrations if isinstance(i, dict) and i.get("id") == "gmail"),
            None,
        )
        assert gmail_entry is not None, "gmail integration entry not found"
        assert gmail_entry.get("required") is True, "gmail integration must be required=True"


# ── Part 3: LaunchDaemon (V1.1-2 fix) ────────────────────────────────────────

class TestEmailIntegrationLaunchDaemon:
    """Headless bots run without a user GUI session. LaunchAgents require a
    logged-in user session and therefore never fire on the mini. All bot crons
    must be LaunchDaemons (V1.1-2 lesson)."""

    def test_build_spec_uses_launchdaemon_not_launchagent(self):
        """build_spec plist section must be LaunchDaemon, not LaunchAgent."""
        pkg = json.loads(_EI_PKG_PATH.read_text())
        bs = pkg.get("build_spec", "")
        assert "## LaunchDaemon" in bs, (
            "build_spec must have a '## LaunchDaemon' section (V1.1-2: headless bots "
            "need LaunchDaemons, not LaunchAgents)"
        )
        assert "## LaunchAgent Plist" not in bs, (
            "build_spec must NOT have a '## LaunchAgent Plist' section — "
            "headless bots require LaunchDaemons (V1.1-2 lesson)"
        )

    def test_build_spec_plist_path_uses_launchdaemons_dir(self):
        """Plist must be written to /Library/LaunchDaemons/ (system context)."""
        pkg = json.loads(_EI_PKG_PATH.read_text())
        bs = pkg.get("build_spec", "")
        assert "/Library/LaunchDaemons/" in bs, (
            "build_spec plist path must be /Library/LaunchDaemons/com.{bot_id}.email-sync.plist "
            "(system LaunchDaemon, not user LaunchAgent)"
        )
        assert "/Library/LaunchAgents/" not in bs, (
            "build_spec must NOT reference /Library/LaunchAgents/ — "
            "use /Library/LaunchDaemons/ for headless-bot crons"
        )

    def test_build_spec_plist_has_username_key(self):
        """LaunchDaemon plist must include UserName so daemon runs as the bot user."""
        pkg = json.loads(_EI_PKG_PATH.read_text())
        bs = pkg.get("build_spec", "")
        assert "<key>UserName</key>" in bs, (
            "LaunchDaemon plist must include <key>UserName</key> so the daemon "
            "runs as the bot user (not root)"
        )


# ── Part 4: Gallery index registration ───────────────────────────────────────

class TestEmailIntegrationIndexRegistration:

    def test_index_includes_email_integration(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        pkg_ids = [e.get("pkg_id") for e in index]
        assert _EI_PKG_ID in pkg_ids, (
            f"Email Integration pkg_id {_EI_PKG_ID!r} not found in gallery/index.json"
        )

    def test_index_entry_has_correct_path(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _EI_PKG_ID), None)
        assert entry is not None
        expected_path = f"email-integration/{_EI_PKG_ID}.json"
        assert entry.get("path") == expected_path, (
            f"index.json path must be {expected_path!r}, got {entry.get('path')!r}"
        )

    def test_index_entry_display_name(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next((e for e in index if e.get("pkg_id") == _EI_PKG_ID), None)
        assert entry is not None
        assert entry.get("display_name") == "Email Integration"
