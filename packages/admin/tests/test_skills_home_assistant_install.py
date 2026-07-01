"""Tests for evolve_admin.skills.home_assistant_install.

Covers the install module's pure logic (URL/token validation, state machine,
install-plan generation, verify_token's mapping of HTTP results to status)
plus the Flask routes (catalog, status, set-config, revoke).

The real HA HTTP API is never contacted — _ha_get_json is stubbed via
verify_token's injected check_token callable, and route tests pass through
a fake reader/writer so the filesystem stays untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Worktree import isolation.
_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.skills import home_assistant_install as ha


# ── URL / token validators ────────────────────────────────────────────────────

class TestValidators:
    @pytest.mark.parametrize("url,expected", [
        ("http://homeassistant.local:8123", True),
        ("https://my-ha.duckdns.org",       True),
        ("http://192.168.1.50:8123",        True),
        ("http://localhost:8123",           True),
        # Reject anything that's not plain http(s).
        ("file:///etc/passwd",              False),
        ("javascript:alert(1)",             False),
        ("",                                False),
        ("not a url",                       False),
        ("homeassistant.local:8123",        False),  # missing scheme
    ])
    def test_url_validation(self, url, expected):
        assert ha._url_looks_valid(url) is expected

    def test_token_validation_rejects_short(self):
        assert ha._token_looks_valid("short") is False
        assert ha._token_looks_valid("") is False

    def test_token_validation_accepts_long(self):
        assert ha._token_looks_valid("x" * 40) is True

    def test_normalize_strips_trailing_slashes(self):
        assert ha._normalize_base_url("http://x.y/") == "http://x.y"
        assert ha._normalize_base_url("http://x.y///") == "http://x.y"
        assert ha._normalize_base_url("http://x.y") == "http://x.y"


# ── verify_token state mapping ────────────────────────────────────────────────

class TestVerifyToken:
    """verify_token classifies (status, body, error) tuples from _ha_get_json
    into the install-flow's user-facing states. We patch the HTTP layer so the
    tests don't need a real HA."""

    def _patch(self, status, body, err):
        return patch.object(
            ha, "_ha_get_json", return_value=(status, body, err),
        )

    def test_valid_when_api_returns_200(self):
        good_token = "x" * 40
        # First call → /api/ → 200; second call → /api/config → 200 with version.
        with patch.object(
            ha, "_ha_get_json",
            side_effect=[(200, {"message": "API running."}, None),
                         (200, {"version": "2026.4.0"}, None)],
        ):
            r = ha.verify_token("http://h.local:8123", good_token)
        assert r["ok"] is True
        assert r["status"] == "valid"
        assert r["ha_version"] == "2026.4.0"

    def test_valid_even_if_version_fetch_fails(self):
        """version is a nice-to-have; /api/ accepting the token is the
        authoritative signal."""
        good_token = "x" * 40
        with patch.object(
            ha, "_ha_get_json",
            side_effect=[(200, {"message": "API running."}, None),
                         (500, None, None)],
        ):
            r = ha.verify_token("http://h.local:8123", good_token)
        assert r["ok"] is True
        assert r["status"] == "valid"
        assert r["ha_version"] is None

    def test_revoked_on_401(self):
        with self._patch(401, None, None):
            r = ha.verify_token("http://h.local:8123", "x" * 40)
        assert r["status"] == "revoked"
        assert r["ok"] is False

    def test_unreachable_on_connection_failed(self):
        with self._patch(0, None, "connection_failed"):
            r = ha.verify_token("http://h.local:8123", "x" * 40)
        assert r["status"] == "unreachable"

    def test_unreachable_on_unexpected_http(self):
        """A 502 / 500 from the URL isn't a valid HA response → unreachable
        (so the user gets directed at the URL rather than the token)."""
        with self._patch(502, None, None):
            r = ha.verify_token("http://h.local:8123", "x" * 40)
        assert r["status"] == "unreachable"

    def test_invalid_on_bad_url(self):
        r = ha.verify_token("not a url", "x" * 40)
        assert r["status"] == "invalid"
        assert r["error"] == "invalid_url_format"

    def test_invalid_on_short_token(self):
        r = ha.verify_token("http://h.local:8123", "short")
        assert r["status"] == "invalid"
        assert r["error"] == "invalid_token_format"


# ── resolve_status state machine ─────────────────────────────────────────────

class TestResolveStatus:
    def test_missing_when_no_config(self):
        st = ha.resolve_status("admin_bot",
                                read_cfg=lambda b: None,
                                check_token=lambda u, t: {"ok": False})
        assert st.status == "missing"

    def test_missing_when_config_lacks_token(self):
        st = ha.resolve_status("admin_bot",
                                read_cfg=lambda b: {"base_url": "http://x:8123"},
                                check_token=lambda u, t: {"ok": False})
        assert st.status == "missing"

    def test_invalid_when_stored_url_is_garbage(self):
        st = ha.resolve_status("admin_bot",
                                read_cfg=lambda b: {
                                    "base_url": "not a url",
                                    "access_token": "x" * 40,
                                },
                                check_token=lambda u, t: {"ok": True, "status": "valid"})
        assert st.status == "invalid"

    def test_valid_when_check_passes(self):
        st = ha.resolve_status("admin_bot",
                                read_cfg=lambda b: {
                                    "base_url": "http://h:8123",
                                    "access_token": "x" * 40,
                                },
                                check_token=lambda u, t: {
                                    "ok": True, "status": "valid",
                                    "ha_version": "2026.5.0",
                                })
        assert st.status == "valid"
        assert st.ha_version == "2026.5.0"

    def test_revoked_propagates_from_check(self):
        st = ha.resolve_status("admin_bot",
                                read_cfg=lambda b: {
                                    "base_url": "http://h:8123",
                                    "access_token": "x" * 40,
                                },
                                check_token=lambda u, t: {
                                    "ok": False, "status": "revoked",
                                    "error": "unauthorized",
                                })
        assert st.status == "revoked"

    def test_unreachable_propagates_from_check(self):
        st = ha.resolve_status("admin_bot",
                                read_cfg=lambda b: {
                                    "base_url": "http://h:8123",
                                    "access_token": "x" * 40,
                                },
                                check_token=lambda u, t: {
                                    "ok": False, "status": "unreachable",
                                    "error": "connection_failed",
                                })
        assert st.status == "unreachable"

    def test_unknown_when_read_raises(self):
        def boom(b): raise PermissionError("nope")
        st = ha.resolve_status("admin_bot",
                                read_cfg=boom,
                                check_token=lambda u, t: {"ok": True})
        assert st.status == "unknown"
        assert "PermissionError" in (st.error or "")

    def test_unknown_when_check_raises(self):
        def boom(u, t): raise TimeoutError("slow")
        st = ha.resolve_status("admin_bot",
                                read_cfg=lambda b: {
                                    "base_url": "http://h:8123",
                                    "access_token": "x" * 40,
                                },
                                check_token=boom)
        assert st.status == "unknown"


# ── build_install_plan ───────────────────────────────────────────────────────

class TestInstallPlan:
    def test_valid_status_yields_no_steps(self):
        st = ha.InstallStatus(bot_id="admin_bot", status="valid")
        assert ha.build_install_plan(st) == []

    def test_unknown_status_yields_no_steps(self):
        st = ha.InstallStatus(bot_id="admin_bot", status="unknown", error="x")
        assert ha.build_install_plan(st) == []

    def test_missing_yields_set_config_then_confirm(self):
        st = ha.InstallStatus(bot_id="admin_bot", status="missing")
        steps = ha.build_install_plan(st)
        assert [s.id for s in steps] == ["set_config", "confirm"]
        # set_config carries the form field definitions the modal renders.
        fields = steps[0].fields
        names = [f["name"] for f in fields]
        assert "base_url" in names
        assert "access_token" in names
        # access_token must be a password-type field so the modal masks it.
        token_field = next(f for f in fields if f["name"] == "access_token")
        assert token_field["type"] == "password"

    def test_revoked_yields_same_plan_as_missing(self):
        """If HA rejected the stored token, the recovery flow is to paste a
        fresh one — same as a first-time install."""
        st = ha.InstallStatus(bot_id="admin_bot", status="revoked")
        steps = ha.build_install_plan(st)
        assert [s.id for s in steps] == ["set_config", "confirm"]


# ── Flask route smoke tests ───────────────────────────────────────────────────


# Route tests removed 2026-05-30. This skill was withdrawn from the
# Skills catalog because no code consumed the credential file at runtime;
# the /api/skills/install/<id>/{status, set-token, revoke} routes now
# 404. Regression guards live in
# tests/test_skills_install_orchestrator_parity.py::TestWithdrawnSkills.
# See docs/design/paste-token-skills-future-2026-05-30.md.
#
# Unit tests above (token format, verify_token, resolve_status, build_install_plan)
# stay because the install module is kept for verify_token reuse when
# this skill returns as an MCP server install.
