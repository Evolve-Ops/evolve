"""Unit tests for google_auth.load_credentials (path-C only).

Covers the error matrix (missing block, missing fields, unknown mode,
NotImplementedError for paths A/B, missing SA file on disk) plus the
happy path with a mocked service_account.Credentials loader.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from evolve_admin import google_auth

# The happy-path tests need google.oauth2.service_account installed to patch it.
# Error-matrix tests don't (google_auth's import is lazy). Skip happy-path
# tests in environments without the optional Google SDK.
try:
    import google.oauth2.service_account  # type: ignore[import-untyped]  # noqa: F401
    _GOOGLE_SDK_AVAILABLE = True
except ImportError:
    _GOOGLE_SDK_AVAILABLE = False

requires_google_sdk = pytest.mark.skipif(
    not _GOOGLE_SDK_AVAILABLE,
    reason="google-auth SDK not installed (optional dep; install via pip install google-auth)",
)


def _network(bot_id: str = "lex", **bot_overrides) -> dict:
    base = {
        "bots": {
            bot_id: {
                "role": "member",
                "google_integration": {
                    "mode": "service_account_dwd",
                    "workspace_domain": "example-corp.com",
                    "subject": f"{bot_id}@example-corp.com",
                    "service_account_secret_ref": "google-sa-example-corp",
                    "scopes": ["https://www.googleapis.com/auth/gmail.send"],
                },
            }
        }
    }
    base["bots"][bot_id].update(bot_overrides)
    return base


def test_missing_bot_raises_config_error():
    network = _network("lex")
    with pytest.raises(google_auth.GoogleIntegrationConfigError, match="not found"):
        google_auth.load_credentials("nonexistent", network=network)


def test_missing_google_integration_block_raises():
    network = _network("lex", google_integration=None)
    # Remove the key entirely
    del network["bots"]["lex"]["google_integration"]
    with pytest.raises(google_auth.GoogleIntegrationConfigError, match="no google_integration block"):
        google_auth.load_credentials("lex", network=network)


def test_path_a_missing_token_record_raises_file_not_found(tmp_path: Path):
    """Path A is implemented (Phase A.1) — without a token record on
    disk, ``load_credentials`` raises FileNotFoundError pointing at the
    Personal-Gmail wizard. ``NotImplementedError`` belongs to Path B only.
    """
    network = _network("lex")
    network["bots"]["lex"]["google_integration"]["mode"] = "free_gmail_oauth"
    network["googleOAuthClient"] = {"client_id": "x", "client_secret": "y"}
    with pytest.raises(FileNotFoundError, match="OAuth refresh-token record"):
        google_auth.load_credentials("lex", network=network, tokens_dir=tmp_path)


def test_path_b_raises_not_implemented():
    network = _network("lex")
    network["bots"]["lex"]["google_integration"]["mode"] = "workspace_user_oauth"
    with pytest.raises(NotImplementedError, match="workspace_user_oauth"):
        google_auth.load_credentials("lex", network=network)


def test_unknown_mode_raises_config_error():
    network = _network("lex")
    network["bots"]["lex"]["google_integration"]["mode"] = "magic"
    with pytest.raises(google_auth.GoogleIntegrationConfigError, match="'magic'"):
        google_auth.load_credentials("lex", network=network)


def test_missing_subject_raises():
    network = _network("lex")
    del network["bots"]["lex"]["google_integration"]["subject"]
    with pytest.raises(google_auth.GoogleIntegrationConfigError, match="subject is required"):
        google_auth.load_credentials("lex", network=network)


def test_missing_secret_ref_raises():
    network = _network("lex")
    del network["bots"]["lex"]["google_integration"]["service_account_secret_ref"]
    with pytest.raises(google_auth.GoogleIntegrationConfigError, match="service_account_secret_ref"):
        google_auth.load_credentials("lex", network=network)


def test_missing_sa_file_raises_file_not_found(tmp_path: Path):
    network = _network("lex")
    with pytest.raises(FileNotFoundError, match="service account JSON not found"):
        google_auth.load_credentials("lex", network=network, secrets_dir=tmp_path)


def test_missing_scopes_raises(tmp_path: Path):
    network = _network("lex")
    del network["bots"]["lex"]["google_integration"]["scopes"]
    (tmp_path / "google-sa-example-corp.json").write_text("{}")
    with pytest.raises(google_auth.GoogleIntegrationConfigError, match="scopes must be provided"):
        google_auth.load_credentials("lex", network=network, secrets_dir=tmp_path)


@requires_google_sdk
def test_happy_path_returns_subject_impersonated_creds(tmp_path: Path):
    network = _network("lex")
    sa_path = tmp_path / "google-sa-example-corp.json"
    sa_path.write_text("{}")  # contents don't matter — we mock the loader

    fake_base_creds = MagicMock()
    fake_subject_creds = MagicMock()
    fake_base_creds.with_subject.return_value = fake_subject_creds

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_base_creds,
    ) as loader:
        result = google_auth.load_credentials(
            "lex", network=network, secrets_dir=tmp_path
        )

    loader.assert_called_once_with(
        str(sa_path), scopes=["https://www.googleapis.com/auth/gmail.send"]
    )
    fake_base_creds.with_subject.assert_called_once_with("lex@example-corp.com")
    assert result is fake_subject_creds


@requires_google_sdk
def test_scopes_param_overrides_config(tmp_path: Path):
    # Enroll both scopes so the override is *within* the DwD grant (the clamp
    # rejects a requested scope the grant doesn't enroll — see the clamp tests
    # below). The override still narrows the request from the configured two
    # scopes down to one, which is what this test pins.
    network = _network("lex")
    network["bots"]["lex"]["google_integration"]["scopes"] = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/drive.file",
    ]
    sa_path = tmp_path / "google-sa-example-corp.json"
    sa_path.write_text("{}")

    fake_base_creds = MagicMock()
    fake_base_creds.with_subject.return_value = MagicMock()

    custom_scopes = ["https://www.googleapis.com/auth/drive.file"]
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_base_creds,
    ) as loader:
        google_auth.load_credentials(
            "lex", scopes=custom_scopes, network=network, secrets_dir=tmp_path
        )

    loader.assert_called_once_with(str(sa_path), scopes=custom_scopes)


# ── DwD scope clamp (calendar.readonly ⊂ calendar, etc.) ─────────────────────
#
# Regression coverage for a live field bug: a curated read tool hardcodes a
# narrow ``*.readonly`` scope, but the SA's domain-wide-delegation grant
# enrolls only the broad scope (e.g. ``calendar``). DwD mints all-or-nothing,
# so the narrow request rejected the ENTIRE token with ``unauthorized_client``.
# The clamp substitutes the broader enrolled scope before the mint.

_DWD_GRANT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file",
]


def test_clamp_substitutes_broad_calendar_for_readonly():
    # calendar.readonly is NOT enrolled; calendar IS → substitute calendar.
    out = google_auth._clamp_dwd_scopes(
        "lex",
        ["https://www.googleapis.com/auth/calendar.readonly"],
        _DWD_GRANT_SCOPES,
    )
    assert out == ["https://www.googleapis.com/auth/calendar"]


def test_clamp_substitutes_drive_file_for_readonly():
    # drive.readonly not enrolled; only drive.file is → it satisfies the mint.
    out = google_auth._clamp_dwd_scopes(
        "lex",
        ["https://www.googleapis.com/auth/drive.readonly"],
        _DWD_GRANT_SCOPES,
    )
    assert out == ["https://www.googleapis.com/auth/drive.file"]


def test_clamp_prefers_full_drive_over_drive_file():
    enrolled = _DWD_GRANT_SCOPES + ["https://www.googleapis.com/auth/drive"]
    out = google_auth._clamp_dwd_scopes(
        "lex",
        ["https://www.googleapis.com/auth/drive.readonly"],
        enrolled,
    )
    assert out == ["https://www.googleapis.com/auth/drive"]


def test_clamp_keeps_enrolled_scope_unchanged():
    # An exactly-enrolled scope is kept (least privilege — never widened).
    out = google_auth._clamp_dwd_scopes(
        "lex",
        ["https://www.googleapis.com/auth/gmail.readonly"],
        _DWD_GRANT_SCOPES,
    )
    assert out == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_clamp_dedupes_collapsed_scopes():
    # gmail.readonly is enrolled (kept); a second request for the same broad
    # scope collapses — no duplicate in the minted set.
    out = google_auth._clamp_dwd_scopes(
        "lex",
        [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
        _DWD_GRANT_SCOPES,
    )
    assert out == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar",
    ]


def test_clamp_raises_naming_missing_scope():
    with pytest.raises(google_auth.DwdScopeNotEnrolled) as excinfo:
        google_auth._clamp_dwd_scopes(
            "lex",
            ["https://www.googleapis.com/auth/spreadsheets"],
            _DWD_GRANT_SCOPES,
        )
    exc = excinfo.value
    # NAMES the missing scope (not Google's opaque unauthorized_client).
    assert "spreadsheets" in str(exc)
    assert exc.missing == ["https://www.googleapis.com/auth/spreadsheets"]
    # Subclasses InsufficientGrantedScopes so existing route/bridge handlers
    # catch it and surface ``missing_scopes`` unchanged.
    assert isinstance(exc, google_auth.InsufficientGrantedScopes)


def test_clamp_no_op_when_grant_empty():
    # Degenerate config (no enrolled scopes) → pass through, let Google decide.
    requested = ["https://www.googleapis.com/auth/calendar.readonly"]
    assert google_auth._clamp_dwd_scopes("lex", requested, []) == requested


@requires_google_sdk
def test_load_credentials_clamps_calendar_readonly_end_to_end(tmp_path: Path):
    """The live repro: calendar_list_events asks for calendar.readonly on a
    bot enrolled only for the broad ``calendar`` scope. load_credentials must
    mint with ``calendar``, not the narrow scope that DwD would reject."""
    network = _network("lex")
    network["bots"]["lex"]["google_integration"]["scopes"] = _DWD_GRANT_SCOPES
    sa_path = tmp_path / "google-sa-example-corp.json"
    sa_path.write_text("{}")

    fake_base_creds = MagicMock()
    fake_base_creds.with_subject.return_value = MagicMock()

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_base_creds,
    ) as loader:
        google_auth.load_credentials(
            "lex",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            network=network,
            secrets_dir=tmp_path,
        )

    loader.assert_called_once_with(
        str(sa_path), scopes=["https://www.googleapis.com/auth/calendar"]
    )


def test_load_credentials_raises_dwd_scope_not_enrolled(tmp_path: Path):
    """A requested scope with no enrolled cover fails loud BEFORE the mint —
    pure-Python, so it needs no Google SDK and no SA file read."""
    network = _network("lex")
    network["bots"]["lex"]["google_integration"]["scopes"] = _DWD_GRANT_SCOPES
    sa_path = tmp_path / "google-sa-example-corp.json"
    sa_path.write_text("{}")

    with pytest.raises(google_auth.DwdScopeNotEnrolled, match="spreadsheets"):
        google_auth.load_credentials(
            "lex",
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
            network=network,
            secrets_dir=tmp_path,
        )


def test_path_a_does_not_clamp_readonly(tmp_path: Path):
    """Regression guard: the clamp is Path-C only. A free_gmail_oauth bot that
    consented to calendar.readonly must keep requesting calendar.readonly — the
    clamp must not rewrite it to the broad ``calendar`` scope (which would
    over-request and fail the granted-scope subset check)."""
    network = _network("ada")
    gi = network["bots"]["ada"]["google_integration"]
    gi["mode"] = "free_gmail_oauth"
    gi["scopes"] = ["https://www.googleapis.com/auth/calendar.readonly"]
    network["googleOAuthClient"] = {"client_id": "x", "client_secret": "y"}

    # Token record granting exactly calendar.readonly. The Path-A loader checks
    # requested ⊆ granted; if the clamp had rewritten the request to the broad
    # ``calendar`` scope, this would raise InsufficientGrantedScopes instead of
    # the expected refresh attempt.
    record = {
        "refresh_token": "rt",
        "scopes_granted": ["https://www.googleapis.com/auth/calendar.readonly"],
    }
    (tmp_path / "ada.json").write_text(json.dumps(record))

    with patch(
        "evolve_admin.google_auth._ensure_fresh_access_token",
        side_effect=AssertionError("reached refresh — scope check passed (clamp did not fire)"),
    ):
        with pytest.raises(AssertionError, match="reached refresh"):
            google_auth.load_credentials(
                "ada",
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                network=network,
                tokens_dir=tmp_path,
            )
