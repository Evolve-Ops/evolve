"""Unit tests for ``google_auth.load_credentials`` Path-A (free Gmail OAuth).

Covers token-record loading, scope-grant enforcement, access-token refresh
(happy path + expired-refresh + transient transport failure), atomic
on-disk writes, and the Credentials object returned to the caller.

The Google token endpoint is mocked at the ``requests.post`` boundary so
no network calls happen. The Google SDK is only required for the
happy-path test that asserts the returned object is an
``oauth2.credentials.Credentials`` — error-matrix tests skip the SDK
import entirely.

Spec: [internal/spec-google-path-a-2026-06-01.md](../../../internal/spec-google-path-a-2026-06-01.md).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evolve_admin import google_auth


# The Google SDK is required for the happy-path assertion only.
try:
    import google.oauth2.credentials  # type: ignore[import-untyped]  # noqa: F401
    _GOOGLE_SDK_AVAILABLE = True
except ImportError:
    _GOOGLE_SDK_AVAILABLE = False

requires_google_sdk = pytest.mark.skipif(
    not _GOOGLE_SDK_AVAILABLE,
    reason="google-auth SDK not installed (optional dep)",
)


# ── Fixtures + helpers ──────────────────────────────────────────────────────


def _network(
    bot_id: str = "ada",
    scopes: list[str] | None = None,
    client_id: str = "pod-client-id.apps.googleusercontent.com",
    client_secret: str = "pod-client-secret",
) -> dict:
    """A network.json with one Personal-Gmail bot + a pod-level OAuth client."""
    if scopes is None:
        scopes = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ]
    return {
        "googleOAuthClient": {
            "client_id": client_id,
            "client_secret": client_secret,
        },
        "bots": {
            bot_id: {
                "role": "member",
                "google_integration": {
                    "mode": "free_gmail_oauth",
                    "scopes": scopes,
                },
            }
        },
    }


def _token_record(
    *,
    refresh_token: str = "1//0gT_refresh_xyz",
    access_token: str = "ya29.cached_access",
    expires_at: datetime | None = None,
    scopes_granted: list[str] | None = None,
    google_account: str = "ada.smith@gmail.com",
) -> dict:
    """A minimal token record matching the Phase-A storage layout."""
    if expires_at is None:
        # Default: fresh enough that no refresh is triggered.
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    if scopes_granted is None:
        scopes_granted = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ]
    return {
        "refresh_token": refresh_token,
        "access_token": access_token,
        "access_token_expires_at": expires_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "scopes_granted": list(scopes_granted),
        "google_account": google_account,
        "obtained_at": "2026-05-31T09:14:00Z",
    }


def _write_record(tmp_path: Path, bot_id: str, record: dict) -> Path:
    p = tmp_path / f"{bot_id}.json"
    p.write_text(json.dumps(record))
    os.chmod(p, 0o600)
    return p


def _mock_post(status_code: int, body: dict | str):
    """Build a requests.post mock that returns one canned response."""
    fake_response = MagicMock()
    fake_response.status_code = status_code
    if isinstance(body, dict):
        fake_response.json.return_value = body
        fake_response.text = json.dumps(body)
    else:
        fake_response.text = body
        fake_response.json.side_effect = ValueError("not json")
    return fake_response


# ── Missing token record ─────────────────────────────────────────────────────


def test_missing_token_record_raises_file_not_found(tmp_path: Path):
    network = _network("ada")
    with pytest.raises(FileNotFoundError, match="OAuth refresh-token record"):
        google_auth.load_credentials("ada", network=network, tokens_dir=tmp_path)


def test_missing_oauth_client_raises_config_error(tmp_path: Path):
    network = _network("ada")
    del network["googleOAuthClient"]
    _write_record(tmp_path, "ada", _token_record(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    with pytest.raises(
        google_auth.GoogleIntegrationConfigError,
        match="no OAuth client_id/secret",
    ):
        google_auth.load_credentials("ada", network=network, tokens_dir=tmp_path)


# ── Scope-grant enforcement ─────────────────────────────────────────────────


def test_insufficient_granted_scopes_raises(tmp_path: Path):
    """Bot has gmail.readonly granted but caller asks for gmail.send."""
    network = _network("ada", scopes=[
        "https://www.googleapis.com/auth/gmail.send",
    ])
    _write_record(tmp_path, "ada", _token_record(
        scopes_granted=["https://www.googleapis.com/auth/gmail.readonly"],
    ))
    with pytest.raises(google_auth.InsufficientGrantedScopes) as excinfo:
        google_auth.load_credentials("ada", network=network, tokens_dir=tmp_path)
    assert excinfo.value.bot_id == "ada"
    assert excinfo.value.missing == [
        "https://www.googleapis.com/auth/gmail.send",
    ]
    assert "gmail.readonly" in excinfo.value.granted[0]


# ── Access-token freshness ──────────────────────────────────────────────────


@requires_google_sdk
def test_fresh_access_token_skips_refresh(tmp_path: Path):
    """A cached access token with >5min remaining is returned as-is."""
    network = _network("ada")
    _write_record(tmp_path, "ada", _token_record(
        access_token="ya29.still_good",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    ))
    with patch("requests.post") as post:
        creds = google_auth.load_credentials(
            "ada", network=network, tokens_dir=tmp_path,
        )
    post.assert_not_called()
    assert creds.token == "ya29.still_good"
    assert creds.refresh_token == "1//0gT_refresh_xyz"


@requires_google_sdk
def test_expiring_access_token_triggers_refresh(tmp_path: Path):
    """Access token expiring within the 5-min leeway is refreshed."""
    network = _network("ada")
    _write_record(tmp_path, "ada", _token_record(
        access_token="ya29.about_to_expire",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    ))
    with patch("requests.post", return_value=_mock_post(200, {
        "access_token": "ya29.fresh",
        "expires_in": 3600,
        "token_type": "Bearer",
    })) as post:
        creds = google_auth.load_credentials(
            "ada", network=network, tokens_dir=tmp_path,
        )
    post.assert_called_once()
    # Refresh payload check
    call_kwargs = post.call_args.kwargs
    data = call_kwargs.get("data") or post.call_args.args[1] if post.call_args.args[1:] else call_kwargs.get("data")
    assert data["grant_type"] == "refresh_token"
    assert data["refresh_token"] == "1//0gT_refresh_xyz"
    assert data["client_id"] == "pod-client-id.apps.googleusercontent.com"
    assert creds.token == "ya29.fresh"

    # New access token + expiry persisted to disk
    on_disk = json.loads((tmp_path / "ada.json").read_text())
    assert on_disk["access_token"] == "ya29.fresh"
    assert "last_refreshed_at" in on_disk


@requires_google_sdk
def test_missing_expires_at_triggers_refresh(tmp_path: Path):
    """No recorded expires_at → treat as expired, refresh."""
    network = _network("ada")
    record = _token_record()
    record.pop("access_token_expires_at")
    _write_record(tmp_path, "ada", record)
    with patch("requests.post", return_value=_mock_post(200, {
        "access_token": "ya29.recovered",
        "expires_in": 3600,
    })) as post:
        google_auth.load_credentials("ada", network=network, tokens_dir=tmp_path)
    post.assert_called_once()


# ── Refresh-token expiry ────────────────────────────────────────────────────


@requires_google_sdk
def test_invalid_grant_raises_refresh_token_expired(tmp_path: Path):
    """Google's invalid_grant on refresh → RefreshTokenExpired with bot_id."""
    network = _network("ada")
    _write_record(tmp_path, "ada", _token_record(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    body = '{"error":"invalid_grant","error_description":"Token has been expired or revoked."}'
    with patch("requests.post", return_value=_mock_post(400, body)):
        with pytest.raises(google_auth.RefreshTokenExpired) as excinfo:
            google_auth.load_credentials(
                "ada", network=network, tokens_dir=tmp_path,
            )
    assert excinfo.value.bot_id == "ada"
    assert excinfo.value.status_code == 400
    assert "invalid_grant" in excinfo.value.body


@requires_google_sdk
def test_transient_transport_failure_raises_generic_runtime_error(tmp_path: Path):
    """Network failures during refresh are NOT a re-consent signal."""
    network = _network("ada")
    _write_record(tmp_path, "ada", _token_record(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    import requests as _r
    with patch("requests.post", side_effect=_r.ConnectionError("network down")):
        with pytest.raises(RuntimeError, match="unreachable"):
            google_auth.load_credentials(
                "ada", network=network, tokens_dir=tmp_path,
            )


@requires_google_sdk
def test_non_400_failure_is_not_refresh_token_expired(tmp_path: Path):
    """500 from the token endpoint is a Google blip, not refresh expiry."""
    network = _network("ada")
    _write_record(tmp_path, "ada", _token_record(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    with patch("requests.post", return_value=_mock_post(500, "internal")):
        with pytest.raises(RuntimeError, match="HTTP 500"):
            google_auth.load_credentials(
                "ada", network=network, tokens_dir=tmp_path,
            )


# ── Atomic write ────────────────────────────────────────────────────────────


def test_write_token_record_atomic_and_0600(tmp_path: Path):
    record = _token_record(access_token="ya29.first")
    google_auth.write_token_record("ada", record, tmp_path)

    p = tmp_path / "ada.json"
    assert p.exists()
    assert json.loads(p.read_text())["access_token"] == "ya29.first"
    # Mode bits — owner-only read/write.
    assert (os.stat(p).st_mode & 0o777) == 0o600
    # No leftover temp.
    assert not (tmp_path / "ada.json.tmp").exists()

    # Overwrite with new content — same inode? No (replace), but content updates.
    record["access_token"] = "ya29.second"
    google_auth.write_token_record("ada", record, tmp_path)
    assert json.loads(p.read_text())["access_token"] == "ya29.second"
    assert (os.stat(p).st_mode & 0o777) == 0o600


# ── Refresh token rotation ──────────────────────────────────────────────────


@requires_google_sdk
def test_rotated_refresh_token_is_persisted(tmp_path: Path):
    """Google sometimes returns a new refresh_token on refresh; persist it."""
    network = _network("ada")
    _write_record(tmp_path, "ada", _token_record(
        refresh_token="1//OLD",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    with patch("requests.post", return_value=_mock_post(200, {
        "access_token": "ya29.fresh",
        "expires_in": 3600,
        "refresh_token": "1//ROTATED",
    })):
        google_auth.load_credentials("ada", network=network, tokens_dir=tmp_path)
    on_disk = json.loads((tmp_path / "ada.json").read_text())
    assert on_disk["refresh_token"] == "1//ROTATED"


# ── Per-bot OAuth client overrides pod-level ────────────────────────────────


@requires_google_sdk
def test_per_bot_oauth_client_overrides_pod_level(tmp_path: Path):
    network = _network("ada")
    network["bots"]["ada"]["googleOAuthClient"] = {
        "client_id": "per-bot.apps.googleusercontent.com",
        "client_secret": "per-bot-secret",
    }
    _write_record(tmp_path, "ada", _token_record(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    with patch("requests.post", return_value=_mock_post(200, {
        "access_token": "ya29.x", "expires_in": 3600,
    })) as post:
        google_auth.load_credentials("ada", network=network, tokens_dir=tmp_path)
    sent = post.call_args.kwargs["data"]
    assert sent["client_id"] == "per-bot.apps.googleusercontent.com"
    assert sent["client_secret"] == "per-bot-secret"


# ── G7: store-backed OAuth client (the canonical wizard shape) ──────────────
#
# The wizard's /api/admin/onboard/google/configure writes the SECRET to
# {shared_dir}/credentials/<secret_bot>/google_oauth_client.json and only the
# non-secret {client_id, secret_bot} into network.json. The live `atlas`
# incident (2026-06-22): consent succeeded (the onboarding reader is
# store-aware) but token refresh through google_auth raised
# GoogleIntegrationConfigError because the resolver read only inline secrets.


def _network_store_backed(
    bot_id: str = "atlas",
    *,
    client_id: str = "atlas-client.apps.googleusercontent.com",
    secret_bot: str | None = None,
    scopes: list[str] | None = None,
) -> dict:
    """network.json with a per-bot store-backed client (no inline secret)."""
    net = _network(bot_id, scopes=scopes)
    # No pod-level inline client — only the per-bot store pointer, exactly
    # as the wizard's configure step leaves it.
    net.pop("googleOAuthClient", None)
    client: dict = {"mode": "self_hosted", "client_id": client_id}
    if secret_bot is not None:
        client["secret_bot"] = secret_bot
    net["bots"][bot_id]["googleOAuthClient"] = client
    return net


def _write_client_store(
    shared_dir: Path, secret_bot: str, *, client_id: str, client_secret: str,
) -> Path:
    """Write the credential-store file the configure endpoint produces."""
    p = shared_dir / "credentials" / secret_bot / "google_oauth_client.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema_version": 1,
        "client_id": client_id,
        "client_secret": client_secret,
    }))
    os.chmod(p, 0o600)
    return p


def test_resolve_oauth_client_reads_store_backed_secret(tmp_path: Path):
    """Per-bot {client_id, secret_bot} resolves the secret from the store."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = _network_store_backed("atlas")  # secret_bot omitted → defaults to bot
    net["sharedDir"] = str(shared)
    _write_client_store(
        shared, "atlas",
        client_id="atlas-client.apps.googleusercontent.com",
        client_secret="store-held-secret",
    )
    cid, csec = google_auth._resolve_oauth_client("atlas", net)
    assert cid == "atlas-client.apps.googleusercontent.com"
    assert csec == "store-held-secret"


@requires_google_sdk
def test_refresh_succeeds_with_store_backed_client(tmp_path: Path):
    """End-to-end: an expired access token refreshes using the STORE secret.

    This is the regression guard for the atlas silent-rot — the exact path
    that raised GoogleIntegrationConfigError before G7.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    net = _network_store_backed("atlas", secret_bot="atlas")
    net["sharedDir"] = str(shared)
    _write_client_store(
        shared, "atlas",
        client_id="atlas-client.apps.googleusercontent.com",
        client_secret="store-held-secret",
    )
    _write_record(tokens, "atlas", _token_record(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    with patch("requests.post", return_value=_mock_post(200, {
        "access_token": "ya29.fresh_from_store", "expires_in": 3600,
    })) as post:
        creds = google_auth.load_credentials(
            "atlas", network=net, tokens_dir=tokens,
        )
    sent = post.call_args.kwargs["data"]
    assert sent["client_id"] == "atlas-client.apps.googleusercontent.com"
    assert sent["client_secret"] == "store-held-secret"
    assert sent["grant_type"] == "refresh_token"
    assert creds.token == "ya29.fresh_from_store"


def test_store_secret_pointed_at_sibling_secret_bot(tmp_path: Path):
    """An explicit secret_bot lets one bot share another's OAuth app."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = _network_store_backed("buddy", secret_bot="owner")
    net["sharedDir"] = str(shared)
    _write_client_store(
        shared, "owner",
        client_id="shared-client.apps.googleusercontent.com",
        client_secret="owners-secret",
    )
    cid, csec = google_auth._resolve_oauth_client("buddy", net)
    assert cid == "shared-client.apps.googleusercontent.com"
    assert csec == "owners-secret"


def test_inline_secret_still_wins_over_store(tmp_path: Path):
    """Back-compat: an inline client_secret resolves without touching the store."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = _network_store_backed("atlas", secret_bot="atlas")
    net["sharedDir"] = str(shared)
    net["bots"]["atlas"]["googleOAuthClient"]["client_secret"] = "inline-wins"
    # Store holds a DIFFERENT secret; inline must take precedence.
    _write_client_store(
        shared, "atlas",
        client_id="atlas-client.apps.googleusercontent.com",
        client_secret="store-secret-ignored",
    )
    cid, csec = google_auth._resolve_oauth_client("atlas", net)
    assert csec == "inline-wins"


def test_missing_store_file_raises_config_error(tmp_path: Path):
    """client_id present but no store file + no inline secret → config error."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = _network_store_backed("atlas", secret_bot="atlas")
    net["sharedDir"] = str(shared)
    # No store file written.
    with pytest.raises(google_auth.GoogleIntegrationConfigError):
        google_auth._resolve_oauth_client("atlas", net)


# ── G7 follow-up: deprecated client_secret_ref → auth-profiles ──────────────
#
# The migration-era shape: network.json holds
#   googleOAuthClient = {client_id, client_secret_ref: {bot, profile}}
# and the secret lives in that bot's auth-profiles.json under
#   profiles[<profile>].client_secret (or .value).
# The canonical onboarding reader (routes_admin_shared._resolve_oauth_client_dict
# path b) has always read this; the refresh resolver did not until this change.
# No current write path emits it (verified live 2026-06-21: zero bots use it) —
# these tests guard the parity, not a live config.

_REF_PROFILE = "_evolve_google_oauth_client"  # mirrors google_auth._GOOGLE_CLIENT_SECRET_PROFILE_ID


def _network_ref_backed(
    bot_id: str = "nova",
    *,
    client_id: str = "nova-client.apps.googleusercontent.com",
    ref_bot: str | None = None,
    profile: str | None = None,
    scopes: list[str] | None = None,
) -> dict:
    """network.json with a per-bot client_secret_ref client (no inline/store)."""
    net = _network(bot_id, scopes=scopes)
    net.pop("googleOAuthClient", None)  # no pod-level inline client
    ref: dict = {}
    if ref_bot is not None:
        ref["bot"] = ref_bot
    if profile is not None:
        ref["profile"] = profile
    net["bots"][bot_id]["googleOAuthClient"] = {
        "mode": "self_hosted",
        "client_id": client_id,
        "client_secret_ref": ref,
    }
    return net


def _auth_profiles_with_secret(
    secret: str, *, profile: str = _REF_PROFILE, key: str = "client_secret",
) -> dict:
    """The shape google_auth._read_oauth_client_secret_from_auth_profiles reads
    out of wizard_verify._read_auth_profiles (raw auth-profiles JSON)."""
    return {"profiles": {profile: {key: secret}}}


def test_resolve_oauth_client_reads_client_secret_ref_from_auth_profiles(tmp_path: Path):
    """Per-bot {client_id, client_secret_ref} resolves the secret from the bot's
    auth-profiles, with bot_id → account-name resolution on the way."""
    shared = tmp_path / "shared"
    shared.mkdir()
    # ref omits "bot" → defaults to the calling bot (nova); no store file present.
    net = _network_ref_backed("nova")
    net["sharedDir"] = str(shared)
    with (
        patch("evolve_admin.config.get_bot_user", return_value="nova-acct") as get_user,
        patch(
            "evolve_admin.wizard_verify._read_auth_profiles",
            return_value=_auth_profiles_with_secret("ref-held-secret"),
        ) as reader,
    ):
        cid, csec = google_auth._resolve_oauth_client("nova", net)
    assert cid == "nova-client.apps.googleusercontent.com"
    assert csec == "ref-held-secret"
    # bot_id "nova" resolved to account "nova-acct"; the ACCOUNT name (not the
    # bot_id) is what the auth-profiles reader receives.
    get_user.assert_called_once_with("nova", net)
    reader.assert_called_once_with("nova-acct")


@requires_google_sdk
def test_refresh_succeeds_with_client_secret_ref(tmp_path: Path):
    """End-to-end: an expired access token refreshes using the auth-profiles
    secret. The parity guard — this path would have raised
    GoogleIntegrationConfigError before this change."""
    shared = tmp_path / "shared"
    shared.mkdir()
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    net = _network_ref_backed("nova", ref_bot="nova")
    net["sharedDir"] = str(shared)
    _write_record(tokens, "nova", _token_record(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    with (
        patch("evolve_admin.config.get_bot_user", return_value="nova-acct"),
        patch(
            "evolve_admin.wizard_verify._read_auth_profiles",
            return_value=_auth_profiles_with_secret("ref-held-secret"),
        ),
        patch("requests.post", return_value=_mock_post(200, {
            "access_token": "ya29.example-access-token", "expires_in": 3600,
        })) as post,
    ):
        creds = google_auth.load_credentials("nova", network=net, tokens_dir=tokens)
    sent = post.call_args.kwargs["data"]
    assert sent["client_id"] == "nova-client.apps.googleusercontent.com"
    assert sent["client_secret"] == "ref-held-secret"
    assert sent["grant_type"] == "refresh_token"
    assert creds.token == "ya29.example-access-token"


def test_store_wins_over_client_secret_ref(tmp_path: Path):
    """Precedence within a candidate: the credential store beats the deprecated
    client_secret_ref, and the ref is never consulted when the store hits."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = _network_ref_backed("nova", ref_bot="nova")
    net["sharedDir"] = str(shared)
    # secret_bot defaults to the calling bot → store path is credentials/nova/.
    _write_client_store(
        shared, "nova",
        client_id="nova-client.apps.googleusercontent.com",
        client_secret="store-wins-secret",
    )
    with (
        patch("evolve_admin.config.get_bot_user", return_value="nova-acct"),
        patch(
            "evolve_admin.wizard_verify._read_auth_profiles",
            return_value=_auth_profiles_with_secret("ref-loses-secret"),
        ) as reader,
    ):
        cid, csec = google_auth._resolve_oauth_client("nova", net)
    assert csec == "store-wins-secret"
    reader.assert_not_called()


def test_inline_secret_wins_over_client_secret_ref(tmp_path: Path):
    """Precedence: an inline client_secret short-circuits before the ref (and
    auth-profiles is never read)."""
    net = _network_ref_backed("nova", ref_bot="nova")
    net["bots"]["nova"]["googleOAuthClient"]["client_secret"] = "inline-wins"
    with patch(
        "evolve_admin.wizard_verify._read_auth_profiles",
        return_value=_auth_profiles_with_secret("ref-ignored"),
    ) as reader:
        cid, csec = google_auth._resolve_oauth_client("nova", net)
    assert csec == "inline-wins"
    reader.assert_not_called()


def test_client_secret_ref_explicit_bot_and_profile_and_value_key(tmp_path: Path):
    """An explicit ref.bot (sibling) + ref.profile resolve against that bot's
    account, and the .value key is honored when .client_secret is absent."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = _network_ref_backed("nova", ref_bot="owner", profile="custom_profile")
    net["sharedDir"] = str(shared)
    profiles = _auth_profiles_with_secret(
        "siblings-secret", profile="custom_profile", key="value",
    )
    with (
        patch("evolve_admin.config.get_bot_user", return_value="owner-acct") as get_user,
        patch(
            "evolve_admin.wizard_verify._read_auth_profiles",
            return_value=profiles,
        ) as reader,
    ):
        cid, csec = google_auth._resolve_oauth_client("nova", net)
    assert cid == "nova-client.apps.googleusercontent.com"
    assert csec == "siblings-secret"
    # The ref names bot_id "owner"; its account "owner-acct" is read.
    get_user.assert_called_once_with("owner", net)
    reader.assert_called_once_with("owner-acct")


def _seed_sqlite_store_with_profiles(home: Path, profiles: dict) -> None:
    """Build a migrated openclaw-agent.sqlite under home's main agent dir."""
    import sqlite3
    agent = home.joinpath(".openclaw", "agents", "main", "agent")
    agent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(agent / "openclaw-agent.sqlite"))
    try:
        conn.execute(
            "CREATE TABLE auth_profile_store ("
            "store_key TEXT PRIMARY KEY, store_json TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO auth_profile_store VALUES ('primary', ?, 0)",
            (json.dumps({"version": 1, "profiles": profiles}),),
        )
        conn.commit()
    finally:
        conn.close()


def test_client_secret_ref_resolves_from_sqlite_store(tmp_path: Path, monkeypatch):
    """Transitive fix: the deprecated client_secret_ref path resolves the secret
    from a SQLITE-backed profiles blob through the REAL _read_auth_profiles (not
    a stub). OpenClaw 2026.6.9 migrated auth-profiles.json into the sqlite store;
    the OAuth client secret, if present, comes through its store_json blob."""
    import evolve_config
    shared = tmp_path / "shared"
    shared.mkdir()
    home = tmp_path / "nova-acct-home"
    _seed_sqlite_store_with_profiles(home, {
        _REF_PROFILE: {"provider": "google", "client_secret": "sqlite-held-secret"},
    })
    monkeypatch.setattr(evolve_config, "user_home", lambda user: home)
    net = _network_ref_backed("nova", ref_bot="nova")
    net["sharedDir"] = str(shared)
    with patch("evolve_admin.config.get_bot_user", return_value="nova-acct"):
        cid, csec = google_auth._resolve_oauth_client("nova", net)
    assert cid == "nova-client.apps.googleusercontent.com"
    assert csec == "sqlite-held-secret"


def test_client_secret_ref_sqlite_value_key_and_blank(tmp_path: Path, monkeypatch):
    """The `.value` key is honored when `.client_secret` is absent in the sqlite
    blob; a blank secret degrades to None (caller falls through)."""
    import evolve_config
    # .value carries the secret.
    home_v = tmp_path / "nova-v-home"
    _seed_sqlite_store_with_profiles(home_v, {
        _REF_PROFILE: {"provider": "google", "value": "via-value-key"},
    })
    monkeypatch.setattr(evolve_config, "user_home", lambda user: home_v)
    net = _network_ref_backed("nova", ref_bot="nova")
    net["sharedDir"] = str(tmp_path)
    with patch("evolve_admin.config.get_bot_user", return_value="nova-acct"):
        _, csec = google_auth._resolve_oauth_client("nova", net)
    assert csec == "via-value-key"

    # Blank secret in the store → degrade to None (no exception).
    home_blank = tmp_path / "nova-blank-home"
    _seed_sqlite_store_with_profiles(home_blank, {
        _REF_PROFILE: {"provider": "google", "client_secret": "   "},
    })
    monkeypatch.setattr(evolve_config, "user_home", lambda user: home_blank)
    with patch("evolve_admin.config.get_bot_user", return_value="nova-acct"):
        out = google_auth._read_oauth_client_secret_from_auth_profiles(
            {"bot": "nova", "profile": _REF_PROFILE}, net, default_ref_bot="nova",
        )
    assert out is None


def test_client_secret_ref_blank_secret_raises_config_error(tmp_path: Path):
    """A ref whose profile holds no secret resolves nothing → config error
    (not a silent empty credential)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = _network_ref_backed("nova", ref_bot="nova")
    net["sharedDir"] = str(shared)
    with (
        patch("evolve_admin.config.get_bot_user", return_value="nova-acct"),
        patch(
            "evolve_admin.wizard_verify._read_auth_profiles",
            return_value={"profiles": {_REF_PROFILE: {"client_secret": ""}}},
        ),
    ):
        with pytest.raises(google_auth.GoogleIntegrationConfigError):
            google_auth._resolve_oauth_client("nova", net)


def test_no_client_secret_ref_does_not_import_wizard_verify(tmp_path: Path):
    """The common path (inline/store config, no ref) never reads auth-profiles —
    the lazy reader is only touched when a client_secret_ref is present."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = _network_store_backed("atlas", secret_bot="atlas")
    net["sharedDir"] = str(shared)
    _write_client_store(
        shared, "atlas",
        client_id="atlas-client.apps.googleusercontent.com",
        client_secret="store-held-secret",
    )
    with patch("evolve_admin.wizard_verify._read_auth_profiles") as reader:
        cid, csec = google_auth._resolve_oauth_client("atlas", net)
    assert csec == "store-held-secret"
    reader.assert_not_called()


# ── Phase A.3: available_scopes / assert_scopes_available ───────────────────


def test_available_scopes_path_c_returns_configured(tmp_path: Path):
    """Path C: configured scopes are the source of truth."""
    network = {
        "bots": {
            "lex": {
                "google_integration": {
                    "mode": "service_account_dwd",
                    "subject": "lex@example.com",
                    "service_account_secret_ref": "x",
                    "scopes": [
                        "https://www.googleapis.com/auth/gmail.send",
                        "https://www.googleapis.com/auth/calendar",
                    ],
                }
            }
        }
    }
    avail = google_auth.available_scopes("lex", network=network)
    assert "https://www.googleapis.com/auth/gmail.send" in avail
    assert "https://www.googleapis.com/auth/calendar" in avail


def test_available_scopes_path_a_intersects_configured_and_granted(tmp_path: Path):
    """Path A: granted ∩ configured (operator-narrowed wins)."""
    network = _network("ada", scopes=[
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    ])
    _write_record(tmp_path, "ada", _token_record(scopes_granted=[
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive.file",  # granted but not configured
    ]))
    avail = google_auth.available_scopes("ada", network=network, tokens_dir=tmp_path)
    assert "https://www.googleapis.com/auth/gmail.send" in avail
    assert "https://www.googleapis.com/auth/gmail.readonly" in avail
    # drive.file is granted but not in google_integration.scopes → excluded
    assert "https://www.googleapis.com/auth/drive.file" not in avail


def test_available_scopes_path_a_no_token_record_returns_empty(tmp_path: Path):
    """Path A bot without consent yet has no available scopes."""
    network = _network("ada")
    avail = google_auth.available_scopes("ada", network=network, tokens_dir=tmp_path)
    assert avail == set()


def test_available_scopes_unconfigured_bot_returns_empty(tmp_path: Path):
    """No google_integration block → empty (caller surfaces 'not configured')."""
    network = {"bots": {"ada": {"role": "member"}}}
    avail = google_auth.available_scopes("ada", network=network, tokens_dir=tmp_path)
    assert avail == set()


def test_assert_scopes_available_raises_with_missing_list(tmp_path: Path):
    network = _network("ada", scopes=[
        "https://www.googleapis.com/auth/gmail.send",
    ])
    _write_record(tmp_path, "ada", _token_record(scopes_granted=[
        "https://www.googleapis.com/auth/gmail.send",
    ]))
    with pytest.raises(google_auth.InsufficientGrantedScopes) as excinfo:
        google_auth.assert_scopes_available(
            "ada",
            ["https://www.googleapis.com/auth/calendar"],
            network=network,
            tokens_dir=tmp_path,
        )
    assert excinfo.value.missing == ["https://www.googleapis.com/auth/calendar"]
    assert "https://www.googleapis.com/auth/gmail.send" in excinfo.value.granted


def test_assert_scopes_available_passes_when_subset(tmp_path: Path):
    network = _network("ada")
    _write_record(tmp_path, "ada", _token_record())
    # Should not raise
    google_auth.assert_scopes_available(
        "ada",
        ["https://www.googleapis.com/auth/gmail.send"],
        network=network,
        tokens_dir=tmp_path,
    )
