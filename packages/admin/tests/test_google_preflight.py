"""tests/test_google_preflight.py — shared preflight categorization + hints.

The wizard and the gmail_integration_health monitor both depend on this
module. Tests focus on the public surface:

  - :func:`categorize` maps observed error strings to the right category
  - :func:`hint_for` returns non-empty operator-facing copy for every
    failure category (so the monitor's Signal body always has a fix path)
  - :func:`run_preflight` short-circuits cleanly on config / library
    failures without forcing the test to mock out the Google SDK

The live API path (real :mod:`googleapiclient` call) is covered by the
wizard's integration tests against a mock service.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolve_admin import google_preflight


# ── categorize ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error_text,expected",
    [
        # DwD client_id not authorized — invalid_grant is the canonical signal.
        ("unauthorized_client: Client is not authorized to use this scope",
         "dwd_unauthorized"),
        ("HttpError 401: Not authorized", "dwd_unauthorized"),
        ("invalid_grant: account not authorized", "dwd_unauthorized"),
        # Scope insufficiency.
        ("Request had insufficient authentication scopes",
         "scope_unauthorized"),
        ("403 insufficient_scope", "scope_unauthorized"),
        # PR #1934: API not enabled buckets as scope_unauthorized — same
        # operator action shape (fix the GCP/Workspace side), the hint
        # matrix distinguishes which.
        ("Gmail API has not been used in project 12345 before",
         "scope_unauthorized"),
        ("SERVICE_DISABLED: please enable the API", "scope_unauthorized"),
        # 404 subject + the PR #1934 patterns (userNotFound, Group, alias).
        ("HttpError 404 when requesting: User Not Found", "subject_not_found"),
        ("not found in workspace", "subject_not_found"),
        ("userNotFound: User not found", "subject_not_found"),
        ("Domain policy exempts user from delegation", "subject_not_found"),
        ("Caller does not have access to user", "subject_not_found"),
        # SA key revoked / rotated.
        ("invalid_jwt: signature verification failed", "key_revoked"),
        ("private key is invalid", "key_revoked"),
        # Transient.
        ("503 Service Unavailable", "transient"),
        ("HttpError 502 Bad Gateway", "transient"),
        ("connection reset by peer", "transient"),
        ("Read timed out after 30s", "transient"),
        # Empty / unknown fall through.
        ("", "unknown"),
        ("some weird new google error", "unknown"),
    ],
)
def test_categorize(error_text, expected):
    assert google_preflight.categorize(error_text) == expected


def test_categorize_404_alone_routes_to_subject_not_found():
    """A bare HTTP 404 with no body text bucketing as subject_not_found
    matches the spec's interpretation: getProfile 404 means 'this user
    isn't in the Workspace.'"""
    assert google_preflight.categorize("HttpError 404") == "subject_not_found"


def test_categorize_more_specific_wins_over_generic():
    """key_revoked (invalid_jwt) takes precedence over dwd_unauthorized
    (which also matches invalid_grant). Important so an operator who just
    rotated the SA key gets the rotate-key hint, not the DwD-Admin hint."""
    msg = "invalid_jwt: signature verification failed (also invalid_grant)"
    assert google_preflight.categorize(msg) == "key_revoked"


# ── hint_for ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("category", [
    "dwd_unauthorized", "scope_unauthorized", "subject_not_found",
    "key_revoked", "transient", "library_missing", "config_missing",
    "unknown",
])
def test_every_category_has_an_operator_hint(category):
    """Every failure category must produce a non-empty remediation. A
    Signal body without a fix is dead weight for the operator."""
    hint = google_preflight.hint_for(category)
    assert isinstance(hint, str) and hint, f"empty hint for {category!r}"


def test_ok_has_empty_hint():
    """The 'ok' category never needs a hint."""
    assert google_preflight.hint_for("ok") == ""


def test_unknown_category_falls_back_to_unknown_hint():
    """A category string the catalog doesn't know still gets a hint."""
    fallback = google_preflight.hint_for("zzz-not-in-catalog")
    assert fallback == google_preflight.hint_for("unknown")


# ── preflight_error_hint (PR #1934 patterns now in the shared module) ───────


def test_preflight_error_hint_personal_gmail():
    """@gmail.com subject + unauthorized_client → personal-account hint,
    NOT the generic DwD client_id hint."""
    hint = google_preflight.preflight_error_hint(
        "unauthorized_client: Client is unauthorized",
        subject="someone@gmail.com",
        workspace_domain="gmail.com",
    )
    assert "personal" in hint.lower() or "@gmail.com" in hint


def test_preflight_error_hint_api_not_enabled():
    msg = (
        "Gmail API has not been used in project 12345 before or it is "
        "disabled."
    )
    hint = google_preflight.preflight_error_hint(
        msg, subject="lex@corp.com", workspace_domain="corp.com",
    )
    assert "API" in hint and "enable" in hint.lower()


def test_preflight_error_hint_group_subject():
    hint = google_preflight.preflight_error_hint(
        "Domain policy exempts user from delegation",
        subject="team@corp.com",
        workspace_domain="corp.com",
    )
    assert "Group" in hint or "individual User" in hint


def test_preflight_error_hint_alias_subject():
    hint = google_preflight.preflight_error_hint(
        "userNotFound: User not found",
        subject="alias@corp.com",
        workspace_domain="corp.com",
    )
    assert "alias" in hint.lower() or "primary" in hint.lower()


def test_preflight_error_hint_empty_when_no_match():
    """A novel error text returns the empty string so the wizard knows to
    show the raw error alone."""
    hint = google_preflight.preflight_error_hint(
        "totally novel error nobody has seen", subject="", workspace_domain="",
    )
    assert hint == ""


# ── hint_for enrichment: combines generic + matrix-specific ─────────────────


def test_hint_for_with_subject_context_includes_specific_remediation():
    """hint_for(error_text=..., subject=..., workspace_domain=...) prepends
    the PR #1934 subject-aware hint to the generic category hint. The
    monitor uses this so the Signal body carries both the specific fix
    (e.g. "you typed an alias") AND the generic remediation."""
    body = google_preflight.hint_for(
        "subject_not_found",
        error_text="userNotFound: User not found",
        subject="alias@corp.com",
        workspace_domain="corp.com",
    )
    assert "alias" in body.lower() or "primary" in body.lower()
    # Generic remediation still appended.
    assert "google_integration.subject" in body or "primary address" in body.lower()


def test_hint_for_without_context_returns_generic():
    """No error_text → just the generic category hint, same as the v1 path."""
    generic = google_preflight.hint_for("dwd_unauthorized")
    assert "Workspace Admin" in generic
    # Same when called without context kwargs.
    assert generic == google_preflight.hint_for(
        "dwd_unauthorized", error_text="", subject="", workspace_domain="",
    )


# ── run_preflight: config-missing path (no Google SDK needed) ───────────────


def test_run_preflight_missing_bot_returns_config_missing(tmp_path):
    """Bot not in network.json → categorized as config_missing rather
    than crashing. The monitor relies on this so a stale --bot flag
    produces a structured result, not a traceback."""
    network = {"bots": {}}
    result = google_preflight.run_preflight(
        "ghost",
        secrets_dir=tmp_path,
        network=network,
    )
    assert result["ok"] is False
    assert result["category"] == "config_missing"
    assert result["hint"]
    assert "ghost" in (result["error"] or "")


def test_run_preflight_missing_secret_file_returns_config_missing(tmp_path):
    """SA file isn't on disk → config_missing (FileNotFoundError path)."""
    network = {"bots": {"lex": {"google_integration": {
        "mode": "service_account_dwd",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "service_account_secret_ref": "google-sa-example-corp",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }}}}
    result = google_preflight.run_preflight(
        "lex",
        secrets_dir=tmp_path,
        network=network,
    )
    assert result["ok"] is False
    assert result["category"] == "config_missing"
    assert "google-sa-example-corp.json" in (result["error"] or "")
