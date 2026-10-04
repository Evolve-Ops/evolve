"""tests/test_evo_connect.py — `evo connect google` per-bot OAuth handler.

Covers the four state branches:

  * **No pod setup** — googleOAuthClient + adminBaseUrl missing → pointer
    to `evo setup-google` rather than a broken URL
  * **Fresh setup** — bot has no auth yet → OAuth URL with default
    scopes, scope summary in body
  * **Legacy detected** — bot has `oc gws` tokens at `.config/gws/` →
    migration messaging
  * **Wizard already present** — bot has `google_workspace:<bot_id>` in
    auth-profiles.json → "you're already connected"

Plus arg parsing (default / readonly / explicit service list / bogus
input), and that the constructed OAuth URL uses ``adminBaseUrl`` from
network rather than a request header (the chat handler has no Flask
request context).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# Fully-configured network for tests that exercise the URL-building path.
_FAKE_CLIENT_ID = (
    "1234567890-abcdef.apps.googleusercontent.com"
)
_FAKE_ADMIN_URL = "https://evolve.example.com"


@pytest.fixture
def configured_network(tmp_path):
    return {
        "sharedDir": str(tmp_path),
        "members": ["admin_bot", "team_bot_a", "evolve"],
        "primary": "evolve",
        "googleOAuthClient": {
            "client_id": _FAKE_CLIENT_ID,
            "client_secret_ref": {
                "bot": "evolve",
                "profile": "_evolve_google_oauth_client",
            },
        },
        "adminBaseUrl": _FAKE_ADMIN_URL,
        "bots": {},
    }


def _call(network, bot_id, args):
    from evolve_admin.evo.handlers.connect import render
    return render(role="primary", bot_id=bot_id, args=args, network=network)


# ─────────────────────────────────────────────────────────────────────────────
# Arg parsing
# ─────────────────────────────────────────────────────────────────────────────


def test_no_args_returns_usage(configured_network):
    r = _call(configured_network, "admin_bot", "")
    body = r.direct_send_message or ""
    assert "evo connect google" in body
    assert "readonly" in body


def test_unknown_integration(configured_network):
    r = _call(configured_network, "admin_bot", "bitcoin")
    body = r.direct_send_message or ""
    assert "don't know how to connect" in body.lower() or "i don't know" in body.lower()


def test_default_services_request_full_workspace_scope(configured_network, monkeypatch):
    # Stub state creator since it requires writable state dir at the
    # canonical path; we just care the URL has the expected shape.
    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "fake-state-token")
    r = _call(configured_network, "admin_bot", "google")
    body = r.direct_send_message or ""
    assert "https://accounts.google.com" in body
    # Default set covers the full workspace
    assert "Gmail (send + read)" in body
    assert "Calendar (read + write)" in body
    assert "Drive (per-file)" in body
    assert "Docs" in body and "Sheets" in body and "Slides" in body


def test_readonly_keyword_narrows_scope(configured_network, monkeypatch):
    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "fake-state-token")
    r = _call(configured_network, "admin_bot", "google readonly")
    body = r.direct_send_message or ""
    assert "Gmail (read-only)" in body
    assert "Calendar (read-only)" in body
    assert "Drive (per-file)" not in body  # not in readonly defaults


def test_bad_service_name_rejected(configured_network):
    r = _call(configured_network, "admin_bot", "google not-a-service")
    body = r.direct_send_message or ""
    assert "don't recognize" in body.lower()
    assert "Valid:" in body


def test_explicit_service_list(configured_network, monkeypatch):
    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "fake-state-token")
    r = _call(configured_network, "admin_bot", "google gmail_readonly calendar_readonly drive")
    body = r.direct_send_message or ""
    assert "Gmail (read-only)" in body
    assert "Calendar (read-only)" in body
    assert "Drive (per-file)" in body
    # Should NOT include services we didn't request
    assert "Docs" not in body and "Slides" not in body


# ─────────────────────────────────────────────────────────────────────────────
# Pod setup gate
# ─────────────────────────────────────────────────────────────────────────────


def test_no_google_oauth_client_routes_to_setup(tmp_path):
    """Per-bot OAuth client missing AND no legacy fallback → setup pointer."""
    network = {
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        # Neither per-bot nor legacy pod-level config
    }
    r = _call(network, "admin_bot", "google")
    body = r.direct_send_message or ""
    assert "isn't set up for `admin_bot`" in body
    assert "evo setup-google" in body
    assert "admin_bot" in body  # message names the calling bot


def test_legacy_pod_level_client_id_satisfies_gate(tmp_path, monkeypatch):
    """Backward-compat: a pod with legacy ``network.googleOAuthClient``
    (no per-bot blocks yet) should still let `evo connect google` go
    through. Migration is opt-in, not forced."""
    network = {
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "googleOAuthClient": {"client_id": _FAKE_CLIENT_ID},
        "adminBaseUrl": _FAKE_ADMIN_URL,
    }
    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "fake-state-token")
    r = _call(network, "admin_bot", "google")
    body = r.direct_send_message or ""
    # Legacy fallback successful → fresh-setup message with auth URL
    assert "https://accounts.google.com" in body
    assert "isn't set up" not in body


def test_per_bot_client_id_takes_precedence_over_legacy(tmp_path, monkeypatch):
    """When a bot has its own block AND legacy pod-level exists, the
    per-bot block wins. Operators in transition can have one bot
    migrated while others fall back to legacy."""
    network = {
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "googleOAuthClient": {
            "client_id": "old-pod-wide.apps.googleusercontent.com",
        },
        "adminBaseUrl": _FAKE_ADMIN_URL,
        "bots": {
            "admin_bot": {
                "googleOAuthClient": {"client_id": _FAKE_CLIENT_ID},
            },
        },
    }
    import evolve_admin.web.server as server
    captured = {}

    def fake_state(bot_id, services, scopes, redirect_uri):
        captured["bot_id"] = bot_id
        return "tok"

    monkeypatch.setattr(server, "_google_state_create", fake_state)
    r = _call(network, "admin_bot", "google")
    body = r.direct_send_message or ""
    # The URL should embed admin_bot's per-bot client_id, NOT the pod-wide one
    assert _FAKE_CLIENT_ID.replace(".", "%2E") in body or _FAKE_CLIENT_ID in body
    assert "old-pod-wide" not in body


def test_missing_admin_base_url_routes_to_setup(tmp_path):
    network = {
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "googleOAuthClient": {"client_id": _FAKE_CLIENT_ID},
        # adminBaseUrl absent
    }
    r = _call(network, "admin_bot", "google")
    body = r.direct_send_message or ""
    assert "isn't set up" in body
    assert "adminBaseUrl" in body


def test_empty_client_id_treated_as_unconfigured(tmp_path):
    network = {
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "googleOAuthClient": {"client_id": ""},
        "adminBaseUrl": _FAKE_ADMIN_URL,
    }
    r = _call(network, "admin_bot", "google")
    body = r.direct_send_message or ""
    assert "isn't set up" in body


# ─────────────────────────────────────────────────────────────────────────────
# OAuth URL shape
# ─────────────────────────────────────────────────────────────────────────────


def test_oauth_url_uses_admin_base_url_for_redirect(configured_network, monkeypatch):
    import evolve_admin.web.server as server
    captured = {}

    def fake_state_create(bot_id, services, scopes, redirect_uri):
        captured["redirect_uri"] = redirect_uri
        captured["services"] = services
        return "fake-state-token"

    monkeypatch.setattr(server, "_google_state_create", fake_state_create)
    _call(configured_network, "admin_bot", "google")
    # Redirect URI uses the configured adminBaseUrl + canonical callback path
    assert captured["redirect_uri"] == (
        f"{_FAKE_ADMIN_URL}/api/admin/onboard/google/callback"
    )


def test_oauth_url_carries_state_and_required_params(configured_network, monkeypatch):
    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "test-state-xyz")
    r = _call(configured_network, "admin_bot", "google")
    body = r.direct_send_message or ""
    assert "state=test-state-xyz" in body
    assert "access_type=offline" in body
    assert "prompt=consent" in body
    assert "response_type=code" in body
    # Scopes URL-encoded
    assert "scope=" in body


def test_admin_base_url_trailing_slash_collapsed(tmp_path, monkeypatch):
    import evolve_admin.web.server as server
    captured = {}
    monkeypatch.setattr(server, "_google_state_create",
                        lambda b, s, sc, ru: (captured.update(redirect_uri=ru) or "tok"))
    network = {
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "googleOAuthClient": {"client_id": _FAKE_CLIENT_ID},
        # Operator typed a trailing slash — handler should strip it
        "adminBaseUrl": "https://evolve.example.com/",
    }
    _call(network, "admin_bot", "google")
    assert captured["redirect_uri"] == (
        "https://evolve.example.com/api/admin/onboard/google/callback"
    )
    assert "//api" not in captured["redirect_uri"]


# ─────────────────────────────────────────────────────────────────────────────
# Bot state detection — wizard already present
# ─────────────────────────────────────────────────────────────────────────────


def test_wizard_already_present_short_circuits_when_scope_matches(
    configured_network, monkeypatch,
):
    """When the requested scope set is a subset of what's already
    authorized, return the 'already connected' message — no auth URL."""
    # Patch the wizard-state detector directly
    from evolve_admin.evo.handlers import connect

    def fake_wizard(bot_id):
        return {
            "ok": True,
            "services": ["gmail", "calendar", "drive", "docs", "sheets", "slides"],
            "google_account": "admin_bot@example.com",
            "path": "/tmp/fake",
        }

    monkeypatch.setattr(connect, "_detect_wizard_auth", fake_wizard)
    monkeypatch.setattr(connect, "_detect_legacy_auth", lambda b: {"present": False})

    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "fake-state-token")

    r = _call(configured_network, "admin_bot", "google")
    body = r.direct_send_message or ""
    assert "already connected" in body.lower()
    assert "admin_bot@example.com" in body
    assert "Nothing to do" in body


def test_wizard_already_present_expanding_scopes_returns_reauth_url(
    configured_network, monkeypatch,
):
    """When the operator requests scopes beyond what's currently
    authorized, present the re-auth URL with explicit 'expand' framing."""
    from evolve_admin.evo.handlers import connect
    # Currently has read-only; asking for full set
    monkeypatch.setattr(connect, "_detect_wizard_auth", lambda b: {
        "ok": True,
        "services": ["gmail_readonly", "calendar_readonly"],
        "google_account": "admin_bot@example.com",
    })
    monkeypatch.setattr(connect, "_detect_legacy_auth", lambda b: {"present": False})
    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "fake-state-token")
    r = _call(configured_network, "admin_bot", "google")
    body = r.direct_send_message or ""
    assert "already connected" in body.lower()
    assert "expanded scope" in body.lower() or "asking to add" in body.lower()
    assert "https://accounts.google.com" in body


# ─────────────────────────────────────────────────────────────────────────────
# Bot state detection — legacy oc gws
# ─────────────────────────────────────────────────────────────────────────────


def test_legacy_detected_returns_migration_message(configured_network, monkeypatch):
    from evolve_admin.evo.handlers import connect
    monkeypatch.setattr(connect, "_detect_wizard_auth", lambda b: {"ok": False})
    monkeypatch.setattr(connect, "_detect_legacy_auth", lambda b: {
        "present": True,
        "scopes": [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/presentations",
        ],
        "google_account": "admin_bot@example.com",
        "token_age_days": 60,
    })
    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "fake-state-token")
    r = _call(configured_network, "admin_bot", "google")
    body = r.direct_send_message or ""
    assert "Migrate" in body
    assert "legacy" in body.lower()
    assert "admin_bot@example.com" in body
    assert "https://accounts.google.com" in body  # auth URL surfaced
    assert "(no auto-delete)" in body or "legacy tokens stay" in body  # safety messaging


# ─────────────────────────────────────────────────────────────────────────────
# Fresh setup (no legacy, no wizard)
# ─────────────────────────────────────────────────────────────────────────────


def test_fresh_setup_renders_clear_message(configured_network, monkeypatch):
    from evolve_admin.evo.handlers import connect
    monkeypatch.setattr(connect, "_detect_wizard_auth", lambda b: {"ok": False})
    monkeypatch.setattr(connect, "_detect_legacy_auth", lambda b: {"present": False})
    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "fake-state-token")
    r = _call(configured_network, "admin_bot", "google")
    body = r.direct_send_message or ""
    assert "**Connect Google for `admin_bot`**" in body
    assert "https://accounts.google.com" in body
    assert "consent screen" in body.lower()
    assert "evo integrations" in body


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch envelope
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_returns_correct_subcommand(configured_network, monkeypatch):
    import evolve_admin.web.server as server
    monkeypatch.setattr(server, "_google_state_create",
                        lambda *a, **kw: "fake-state-token")
    r = _call(configured_network, "admin_bot", "google")
    assert r.subcommand == "connect"
    assert r.mode == "speak"
    assert r.direct_send_message
