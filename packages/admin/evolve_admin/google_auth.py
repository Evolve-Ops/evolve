"""Path-A (free Gmail + user OAuth) and Path-C (service-account + DwD) auth
for Google APIs.

Reads the calling bot's ``google_integration`` block from network.json and
dispatches on ``mode``:

* ``service_account_dwd`` (Path C) — loads the SA JSON from
  ``/Users/Shared/evolve/secrets/google_service_accounts/`` and impersonates
  ``subject`` via domain-wide delegation.
* ``free_gmail_oauth`` (Path A, PR Phase A.1) — loads the bot's refresh
  token from ``/Users/Shared/evolve/secrets/google_oauth_tokens/<bot>.json``,
  exchanges it for a fresh access token via Google's OAuth 2.0 token
  endpoint, and returns user-OAuth ``Credentials`` usable for read + write
  + send.
* ``workspace_user_oauth`` (Path B) — still raises ``NotImplementedError``.
  Same plumbing as Path A but is materially blocked on the umbrella
  spec's §10 migration command.

See [docs/spec-google-path-a-2026-06-01.md](../../docs/spec-google-path-a-2026-06-01.md)
for the Path-A decisions (storage layout, refresh-token failure mode,
scope-grant enforcement) and the umbrella
[docs/spec-google-integration-paths-2026-05-30.md](../../docs/spec-google-integration-paths-2026-05-30.md)
for the three-path framing.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config


_log = logging.getLogger(__name__)


# Where service-account JSON keys live on disk. One JSON per `secret_ref`,
# owned by ``evolve:wheel`` mode ``0600``. Only the MCP bridge (which runs
# as the evolve user) reads these — bots never see them, so per-bot ACLs
# are not required. The Path-C wizard (PR ε) manages these files; older
# pods install them manually per the runbook.
DEFAULT_SECRETS_DIR = Path("/Users/Shared/evolve/secrets/google_service_accounts")

# Path-A: per-bot OAuth refresh-token + last-known access-token records.
# One ``<bot_id>.json`` per Personal-Gmail bot, owned by ``evolve:wheel``
# mode ``0600``. The admin-server process (running as evolve) is the only
# reader/writer; the bot's own user account never sees these files. The
# legacy ``/Users/<bot>/.openclaw/auth-profiles.json`` continues to hold
# a copy for OC-plugin (read-only) use cases per the Phase-A spec §1.2.
DEFAULT_OAUTH_TOKENS_DIR = Path("/Users/Shared/evolve/secrets/google_oauth_tokens")

# Google's OAuth 2.0 token endpoint. Constant; documented for testability —
# tests inject a stub URL via the ``token_uri`` parameter on the helpers
# below.
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# When to pre-emptively refresh the access token. Google access tokens
# last ~3600s; refreshing 5min early eliminates the "expired-mid-call"
# race without thrashing on the token endpoint.
_REFRESH_LEEWAY = timedelta(minutes=5)


class GoogleIntegrationConfigError(ValueError):
    """The bot's ``google_integration`` block is missing or malformed."""


class RefreshTokenExpired(RuntimeError):
    """The Path-A refresh token was rejected by Google's token endpoint.

    Raised when Google returns ``400 invalid_grant`` (or equivalent) on
    refresh — the canonical signal that the operator must re-consent.
    The MCP bridge tools translate this into a structured operator-facing
    error AND emit a ``gmail_oauth_reauth_required`` Signal (Phase A.4).
    """

    def __init__(self, bot_id: str, *, status_code: int = 0, body: str = ""):
        self.bot_id = bot_id
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"refresh token for bot {bot_id!r} was rejected by Google "
            f"(HTTP {status_code}); operator must re-consent. "
            f"Response body (truncated): {body[:200]!r}"
        )


class InsufficientGrantedScopes(RuntimeError):
    """The bot consented to a narrower scope set than this call needs.

    Raised by ``load_credentials`` BEFORE calling Google so the operator
    gets a clean error instead of a Google 403. The remediation is
    re-consent with the missing scopes — Phase A.2 wizard surfaces this.
    """

    def __init__(self, bot_id: str, *, missing: list[str], granted: list[str]):
        self.bot_id = bot_id
        self.missing = list(missing)
        self.granted = list(granted)
        super().__init__(
            f"bot {bot_id!r} did not consent to scopes "
            f"{self.missing!r}; granted scopes are {self.granted!r}. "
            f"Re-consent via the Personal-Gmail wizard to grant the missing scopes."
        )


class DwdScopeNotEnrolled(InsufficientGrantedScopes):
    """A ``service_account_dwd`` tool requested a scope outside the SA's
    domain-wide-delegation grant, and no broader enrolled scope subsumes it.

    DwD is enrolled per-scope in the Workspace Admin console and a token mint
    is all-or-nothing: a single un-enrolled scope makes Google reject the
    ENTIRE request with ``unauthorized_client`` — naming none of the
    offending scopes. ``_clamp_dwd_scopes`` raises this *before* the mint so
    the operator (and the bot-facing route) sees exactly which scope is
    missing instead of Google's opaque error.

    Subclasses :class:`InsufficientGrantedScopes` so existing handlers (the
    bot route + MCP bridge, which surface ``exc.missing`` as ``missing_scopes``)
    treat it identically. Only the remediation differs: a service account has
    no per-user re-consent, so the fix is to add the scope to the DwD grant
    AND to ``google_integration.scopes`` — hence the overridden message.
    """

    def __init__(self, bot_id: str, *, missing: list[str], granted: list[str]):
        self.bot_id = bot_id
        self.missing = list(missing)
        self.granted = list(granted)
        # Skip the Path-A-flavored parent message; go straight to RuntimeError
        # with DwD-specific remediation text.
        RuntimeError.__init__(
            self,
            f"bot {bot_id!r} (service_account_dwd): requested scope(s) "
            f"{self.missing!r} are not enrolled in the service account's "
            f"domain-wide delegation grant, and no broader enrolled scope "
            f"covers them. Enrolled scopes: {self.granted!r}. Grant the "
            f"missing scope to the service account in the Google Workspace "
            f"Admin console (Security → API controls → Domain-wide "
            f"delegation) and add it to network.json "
            f"google_integration.scopes for this bot."
        )


# ── Path-A: token-record helpers ─────────────────────────────────────────────


def _token_path(bot_id: str, tokens_dir: Path) -> Path:
    return tokens_dir / f"{bot_id}.json"


def load_token_record(bot_id: str, tokens_dir: Path = DEFAULT_OAUTH_TOKENS_DIR) -> dict[str, Any]:
    """Read ``<bot_id>.json`` from the token store.

    Raises ``FileNotFoundError`` if the file is absent — the caller
    (typically ``load_credentials``) translates this into a clean
    operator-facing error pointing at the wizard.
    """
    p = _token_path(bot_id, tokens_dir)
    if not p.exists():
        raise FileNotFoundError(
            f"no OAuth refresh-token record for bot {bot_id!r} at {p}. "
            f"Run the Personal-Gmail wizard to consent: open the Skills "
            f"page → Google → Personal @gmail.com → Sign in with Google."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def write_token_record(
    bot_id: str,
    record: dict[str, Any],
    tokens_dir: Path = DEFAULT_OAUTH_TOKENS_DIR,
) -> None:
    """Atomically write the token record to disk with ``0600`` perms.

    Same-directory temp + ``os.replace`` matches the SA wizard's
    ``_install_sa_file`` pattern: no half-written tokens, no widening
    permission window. The file mode is 0o600 from inception via
    ``os.open`` with the mode arg + O_NOFOLLOW.
    """
    tokens_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(tokens_dir, 0o700)
    except PermissionError:
        # Best-effort — if we can write the file we can usually chmod
        # the dir, but a pre-existing dir owned by another user (e.g. an
        # earlier sudo-based install) may legitimately reject this.
        # The file mode bits are what matter for confidentiality.
        pass

    dest = _token_path(bot_id, tokens_dir)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    fd = os.open(
        str(tmp),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=2)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, dest)
    # Defense in depth: re-assert mode in case umask altered it.
    os.chmod(dest, 0o600)


def _read_oauth_client_secret_from_store(
    network: dict[str, Any], secret_bot: str
) -> tuple[str, str] | None:
    """Read ``(client_id, client_secret)`` from the Evolve credential store.

    The OAuth client SECRET is written to
    ``{shared_dir}/credentials/<secret_bot>/google_oauth_client.json`` by
    ``/api/admin/onboard/google/configure`` (the wizard's GCP-client step,
    via ``_save_google_oauth_client`` in ``web/routes_admin.py``). network.json
    holds only the non-secret ``client_id`` plus a ``secret_bot`` pointer — so a
    resolver that reads only inline network.json can never find the secret. That
    omission was the root cause of the live ``atlas`` silent-rot (2026-06-22): the
    consumer bot consented fine (``/begin`` resolves via the store-aware
    ``routes_admin_shared`` reader) but token *refresh* through this module
    raised ``GoogleIntegrationConfigError`` the moment the access token expired.

    This mirrors ``web/routes_admin_shared._read_google_oauth_client_secret_from_store``
    — it lives here in the core module (not the Flask web layer) because the
    MCP-bridge refresh path imports ``google_auth`` and must not pull in
    ``web/``. The store path + JSON shape are the cross-module contract; keep
    the two store readers in sync.

    Scope note: the canonical ``routes_admin_shared._resolve_oauth_client_dict``
    has a THIRD secret source after the store — the deprecated
    ``client_secret_ref`` → bot ``auth-profiles.json`` shape. As of the G7
    follow-up that fallback IS now mirrored here too (see
    ``_read_oauth_client_secret_from_auth_profiles`` and step 3 of
    ``_resolve_oauth_client``), so this resolver covers all three shapes the
    canonical onboarding reader does — consent and refresh can never resolve
    different credentials. That shape is migration-era only: no current write
    path emits it (the wizard writes ``{client_id, secret_bot}`` + the store),
    and openclaw's auth-profiles rewrite eventually clobbers it; it is resolved
    purely so a bot that *was* configured the old way keeps refreshing instead
    of silently rotting.

    Returns ``None`` (not an error) on any miss — missing file, unreadable
    (EACCES under a 0700 parent), malformed JSON, or either field blank — so
    the caller falls through to the next resolution candidate.
    """
    shared_dir = Path(network.get("sharedDir") or config.DEFAULT_SHARED_DIR)
    path = shared_dir / "credentials" / secret_bot / "google_oauth_client.json"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, OSError, ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    cid = (obj.get("client_id") or "").strip()
    csec = (obj.get("client_secret") or "").strip()
    if not cid or not csec:
        return None
    return cid, csec


# Default ``auth-profiles.json`` profile id holding the OAuth client secret for
# the deprecated ``client_secret_ref`` shape. Mirrors
# ``routes_admin_shared.GOOGLE_CLIENT_SECRET_PROFILE_ID`` — duplicated as a
# module-level constant rather than imported so this core module never pulls in
# the Flask ``web/`` layer (the MCP bridge imports ``google_auth``). Keep the
# two values in sync.
_GOOGLE_CLIENT_SECRET_PROFILE_ID = "_evolve_google_oauth_client"


def _read_oauth_client_secret_from_auth_profiles(
    secret_ref: Any, network: dict[str, Any], *, default_ref_bot: str | None
) -> str | None:
    """Read the OAuth client secret from a bot's ``auth-profiles.json``.

    Mirrors path (b) of ``routes_admin_shared._resolve_oauth_client_dict``:
    the deprecated ``client_secret_ref = {bot, profile}`` shape, where the
    secret lives under ``profiles[<profile>].client_secret`` (or ``.value``)
    of the referenced bot's auth-profiles file.

    ``client_secret_ref.bot`` names the *bot_id* whose auth-profiles hold the
    secret (default: ``default_ref_bot`` — the per-candidate ``secret_bot`` /
    calling bot). ``.profile`` defaults to ``_GOOGLE_CLIENT_SECRET_PROFILE_ID``.

    The auth-profiles reader takes a macOS *account name*, not a bot_id (the
    two can differ — a bot's id is not necessarily its macOS account name),
    so resolve bot_id → user via ``config.get_bot_user`` before reading.

    ``wizard_verify`` is imported LAZILY here: this fallback is reached only
    when a ``client_secret_ref`` is actually present, and no current write path
    emits one — so the MCP-bridge common path never pays the import.
    ``wizard_verify`` is stdlib + ``.config`` only (no Flask/web), so even the
    rare hit stays light.

    Returns ``None`` on any miss (no/blank ref, no bot, unreadable profiles,
    blank secret) so the caller falls through to the next candidate.
    """
    if not isinstance(secret_ref, dict):
        return None
    ref_bot = (secret_ref.get("bot") or default_ref_bot or "").strip()
    if not ref_bot:
        return None
    secret_profile = secret_ref.get("profile") or _GOOGLE_CLIENT_SECRET_PROFILE_ID

    # Lazy import — keeps the module-load path light for the MCP bridge, which
    # never carries a client_secret_ref. (wizard_verify imports only stdlib +
    # .config, so this stays web-free even on the rare hit.)
    from . import wizard_verify

    try:
        bot_user = config.get_bot_user(ref_bot, network)
    except Exception:
        # bot_id not in network / resolver failure — degrade to treating the id
        # as the account name (matches config.bot_user_for's degrade-don't-raise).
        bot_user = ref_bot

    auth = wizard_verify._read_auth_profiles(bot_user)
    if not isinstance(auth, dict):
        return None
    prof = (auth.get("profiles") or {}).get(secret_profile)
    if not isinstance(prof, dict):
        return None
    secret = prof.get("client_secret") or prof.get("value") or ""
    if not isinstance(secret, str):
        return None
    secret = secret.strip()
    return secret or None


def _resolve_oauth_client(
    bot_id: str, network: dict[str, Any]
) -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` for the bot's OAuth app.

    Resolution order (per-bot candidate before pod-level; within each candidate:
    inline secret → credential store → deprecated ``client_secret_ref``):

      1. ``network.bots[<bot>].googleOAuthClient`` — inline ``client_id`` +
         ``client_secret`` (legacy / hand-edited self-hosted shape).
      2. ``network.bots[<bot>].googleOAuthClient`` — ``{client_id, secret_bot}``
         with the secret in the Evolve credential store (the canonical shape the
         wizard writes; ``secret_bot`` defaults to ``bot_id``).
      3. ``network.bots[<bot>].googleOAuthClient`` — ``{client_id,
         client_secret_ref: {bot, profile}}`` with the secret in that bot's
         ``auth-profiles.json`` (deprecated migration-era shape; no current
         write path emits it).
      4. legacy pod-level ``network.googleOAuthClient`` — inline, then store,
         then ``client_secret_ref``.

    The pod-level fallback IS the default per the Phase-A spec §1.1
    (per-pod OAuth client). Per-bot blocks remain supported for
    operators who want isolation. This resolution order now covers all three
    secret sources the store-aware onboarding reader
    ``routes_admin_shared._read_google_oauth_client`` does, so consent and
    refresh always resolve the *same* credential — closing the parity gap the
    G7 follow-up addressed (the ``client_secret_ref`` source the original G7
    resolver omitted; see the scope note in
    ``_read_oauth_client_secret_from_store``).

    Raises ``GoogleIntegrationConfigError`` if no block yields both a
    ``client_id`` and a ``client_secret`` (inline, store-backed, or
    ``client_secret_ref``).
    """
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    # (config dict, default secret_bot). Per-bot configs default secret_bot to
    # the calling bot; legacy pod-level configs have no implicit owner (None) —
    # they must name secret_bot explicitly, carry the inline secret, or name a
    # bot in their client_secret_ref.
    candidates: list[tuple[Any, str | None]] = [
        (bot_cfg.get("googleOAuthClient"), bot_id),
        (network.get("googleOAuthClient"), None),
    ]
    for cfg, default_secret_bot in candidates:
        if not isinstance(cfg, dict):
            continue
        cid = (cfg.get("client_id") or "").strip()
        csec = (cfg.get("client_secret") or "").strip()
        if cid and csec:
            return cid, csec
        # Inline secret absent. The remaining two sources (store, then the
        # deprecated client_secret_ref) carry the secret out-of-band but still
        # need a client_id from network.json — without one there is nothing to
        # resolve for this candidate.
        if not cid:
            continue
        secret_bot = (cfg.get("secret_bot") or default_secret_bot or "").strip() or None
        # (2) Evolve credential store — the canonical wizard shape.
        if secret_bot:
            hit = _read_oauth_client_secret_from_store(network, secret_bot)
            if hit is not None:
                store_cid, store_csec = hit
                # Store's client_id wins when present (avoids drift between
                # network.json and the credential file), matching
                # routes_admin_shared._resolve_oauth_client_dict.
                return (store_cid or cid), store_csec
        # (3) Deprecated client_secret_ref → bot auth-profiles. Reached only for
        # the migration-era shape (no current write path emits it); mirrors path
        # (b) of routes_admin_shared._resolve_oauth_client_dict so consent and
        # refresh can never resolve different credentials. ref_bot defaults to
        # secret_bot (which itself defaults to the calling bot). The network.json
        # client_id is authoritative here — the ref carries only the secret.
        ref_secret = _read_oauth_client_secret_from_auth_profiles(
            cfg.get("client_secret_ref"), network, default_ref_bot=secret_bot,
        )
        if ref_secret is not None:
            return cid, ref_secret
    raise GoogleIntegrationConfigError(
        f"no OAuth client_id/secret available for bot {bot_id!r}. "
        f"Configure the pod-wide OAuth app via the Google wizard "
        f"(or /api/admin/onboard/google/configure), which writes "
        f"network.bots[{bot_id!r}].googleOAuthClient (client_id + secret_bot) "
        f"plus the client secret to "
        f"{{shared_dir}}/credentials/<secret_bot>/google_oauth_client.json."
    )


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    token_uri: str = GOOGLE_TOKEN_URI,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Exchange a refresh token for a fresh access token.

    Returns a dict with at least ``access_token`` and ``expires_in``
    (seconds). The caller writes the new access token + ``expires_at``
    timestamp into the token record.

    Raises ``RefreshTokenExpired`` on the canonical ``invalid_grant``
    failure (Google's signal that the operator must re-consent). Raises
    a generic ``RuntimeError`` on transient transport failures so the
    caller can retry without triggering a Signal storm.
    """
    # Lazy import — requests is a transitive dep of
    # google-api-python-client; importing at module load forces it on
    # every test env even when only error-matrix tests run.
    import requests  # type: ignore[import-untyped]

    try:
        resp = requests.post(
            token_uri,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        # Transport-level failure. NOT a refresh-token expiry — caller
        # surfaces this as a transient health-monitor blip, not a
        # re-consent prompt.
        raise RuntimeError(f"refresh token endpoint unreachable: {exc}") from exc

    if resp.status_code == 200:
        return resp.json()

    # The canonical "operator must re-consent" signal is HTTP 400 with
    # body containing ``invalid_grant``. Other 4xx (e.g. 401 from a
    # mis-configured client) propagate as plain RuntimeError so the
    # monitor doesn't paper-storm the user with re-consent prompts for
    # an operator misconfiguration.
    body = resp.text or ""
    if resp.status_code == 400 and "invalid_grant" in body:
        # ``bot_id`` is not in scope here — caller wraps with the bot id.
        # We raise a partially-populated exception that the caller
        # re-raises with the bot id attached.
        raise RefreshTokenExpired(
            bot_id="<unknown>", status_code=resp.status_code, body=body
        )
    raise RuntimeError(
        f"refresh token endpoint returned HTTP {resp.status_code}: {body[:200]!r}"
    )


def _now_utc() -> datetime:
    """Wallclock now in UTC. Patchable in tests via monkeypatch."""
    return datetime.now(timezone.utc)


def _parse_expires_at(value: Any) -> datetime | None:
    """Parse an ISO-8601 ``expires_at`` field into an aware datetime.

    Returns ``None`` on missing/unparseable input so callers treat the
    cached token as expired and refresh proactively.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        # ``fromisoformat`` accepts "2026-06-01T18:42:00+00:00" but not
        # the trailing-Z form Google uses; normalize.
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _ensure_fresh_access_token(
    bot_id: str,
    record: dict[str, Any],
    client_id: str,
    client_secret: str,
    *,
    tokens_dir: Path,
    token_uri: str = GOOGLE_TOKEN_URI,
) -> dict[str, Any]:
    """Return a token record with an unexpired access_token.

    If the cached access_token is within ``_REFRESH_LEEWAY`` of expiry
    (or has no recorded expiry), refresh via Google and write the
    updated record back atomically. If the refresh fails with
    ``invalid_grant``, raise ``RefreshTokenExpired(bot_id=...)``.

    Returns the (possibly updated) record. The on-disk record is
    updated as a side effect; callers don't need to write it back.
    """
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        raise RefreshTokenExpired(
            bot_id=bot_id,
            status_code=0,
            body="token record has no refresh_token field",
        )

    expires_at = _parse_expires_at(record.get("access_token_expires_at"))
    needs_refresh = (
        not record.get("access_token")
        or expires_at is None
        or expires_at <= _now_utc() + _REFRESH_LEEWAY
    )

    if not needs_refresh:
        return record

    try:
        fresh = refresh_access_token(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            token_uri=token_uri,
        )
    except RefreshTokenExpired as exc:
        # The lower-level helper doesn't know the bot id; re-raise with
        # it attached so callers (the MCP bridge, the health monitor)
        # don't have to plumb it through manually.
        raise RefreshTokenExpired(
            bot_id=bot_id, status_code=exc.status_code, body=exc.body
        ) from exc

    new_access = fresh.get("access_token")
    if not new_access:
        raise RuntimeError(
            f"refresh response for bot {bot_id!r} had no access_token: "
            f"{fresh!r}"
        )
    expires_in = int(fresh.get("expires_in") or 3600)
    new_expires_at = _now_utc() + timedelta(seconds=expires_in)

    record = dict(record)  # don't mutate caller's dict in place
    record["access_token"] = new_access
    record["access_token_expires_at"] = new_expires_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    record["last_refreshed_at"] = _now_utc().isoformat(timespec="seconds").replace("+00:00", "Z")
    # Google sometimes rotates refresh tokens; persist the new one if
    # provided. Most refreshes return only access_token.
    if fresh.get("refresh_token"):
        record["refresh_token"] = fresh["refresh_token"]

    try:
        write_token_record(bot_id, record, tokens_dir)
    except Exception:
        _log.exception(
            "google_auth: failed to write refreshed token record for %s; "
            "next call will refresh again",
            bot_id,
        )
    return record


def _load_free_gmail_oauth_credentials(
    bot_id: str,
    gi: dict[str, Any],
    network: dict[str, Any],
    scopes: list[str],
    tokens_dir: Path,
    token_uri: str = GOOGLE_TOKEN_URI,
):
    """Build a google.oauth2.credentials.Credentials for Path A.

    1. Load the token record from disk.
    2. Verify requested ``scopes`` ⊆ ``scopes_granted`` on the record.
    3. Refresh the access token if it's near expiry.
    4. Return ``Credentials`` ready for googleapiclient.

    Lazy-imports ``google.oauth2.credentials`` so error-matrix unit
    tests don't require the Google SDK.
    """
    record = load_token_record(bot_id, tokens_dir)

    granted = list(record.get("scopes_granted") or [])
    missing = [s for s in scopes if s not in granted]
    if missing:
        raise InsufficientGrantedScopes(
            bot_id=bot_id, missing=missing, granted=granted
        )

    client_id, client_secret = _resolve_oauth_client(bot_id, network)

    record = _ensure_fresh_access_token(
        bot_id,
        record,
        client_id,
        client_secret,
        tokens_dir=tokens_dir,
        token_uri=token_uri,
    )

    from google.oauth2.credentials import Credentials  # type: ignore[import-untyped]

    return Credentials(
        token=record["access_token"],
        refresh_token=record["refresh_token"],
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(scopes),
    )


# ── Path-C: service-account credential loading ──────────────────────────────


# Google scope subsumption for the DwD clamp (see ``_clamp_dwd_scopes``).
# Maps a *narrower* scope to the ordered tuple of *broader* scopes whose
# access strictly includes it. Only safe ``narrow ⊆ broader`` substitutions
# belong here: ``calendar`` (read+write) covers ``calendar.readonly``,
# ``gmail.modify`` / the full-mailbox scope cover ``gmail.readonly``, and the
# full ``drive`` scope covers ``drive.readonly``.
#
# ``drive.file`` is the one caveat: it satisfies a ``drive.readonly`` *mint*
# but exposes ONLY app-created / explicitly-shared files (the same visibility
# as ``drive_list_files`` already has), so it is listed AFTER the genuinely
# broader ``drive`` and is used only when ``drive`` itself isn't enrolled.
_SCOPE_PREFIX = "https://www.googleapis.com/auth/"
_DWD_BROADER_SCOPES: dict[str, tuple[str, ...]] = {
    _SCOPE_PREFIX + "calendar.readonly": (_SCOPE_PREFIX + "calendar",),
    _SCOPE_PREFIX + "gmail.readonly": (
        _SCOPE_PREFIX + "gmail.modify",
        "https://mail.google.com/",
    ),
    _SCOPE_PREFIX + "drive.readonly": (
        _SCOPE_PREFIX + "drive",
        _SCOPE_PREFIX + "drive.file",
    ),
}


def _clamp_dwd_scopes(
    bot_id: str, requested: list[str], enrolled: list[str],
) -> list[str]:
    """Resolve ``requested`` scopes onto the SA's enrolled DwD grant.

    Domain-wide delegation is enrolled per-scope and its token mint is
    all-or-nothing: one requested scope outside the grant makes Google reject
    the WHOLE request with an opaque ``unauthorized_client``. So a bot
    enrolled for the broad ``calendar`` scope can't run a tool that hardcodes
    ``calendar.readonly`` even though read is a strict subset of what it may
    do. This maps each requested scope onto the grant:

      * already in ``enrolled``        → kept unchanged (least privilege — we
                                         never widen an already-granted scope)
      * subsumed by an enrolled scope  → that broader scope is substituted
                                         (per ``_DWD_BROADER_SCOPES``)
      * neither                        → collected and reported together via
                                         :class:`DwdScopeNotEnrolled`, which
                                         NAMES the missing scope(s)

    Order is preserved and the result de-duplicates (several requested scopes
    can collapse onto one enrolled scope). Returns ``requested`` unchanged
    when ``enrolled`` is empty — a bot with no configured grant is left to
    Google's own enforcement rather than hard-failing here (preserves the
    pre-clamp behavior for that degenerate config).

    Path A (``free_gmail_oauth``) never calls this: its grants are per-user
    OAuth consents, so requesting the narrow ``.readonly`` scope is exactly
    right there — clamping it to a broader scope would over-request consent.
    """
    if not enrolled:
        return requested
    enrolled_set = set(enrolled)
    resolved: list[str] = []
    missing: list[str] = []
    for scope in requested:
        if scope in enrolled_set:
            substitute: str | None = scope
        else:
            substitute = next(
                (b for b in _DWD_BROADER_SCOPES.get(scope, ()) if b in enrolled_set),
                None,
            )
        if substitute is None:
            missing.append(scope)
        elif substitute not in resolved:
            resolved.append(substitute)
    if missing:
        raise DwdScopeNotEnrolled(
            bot_id, missing=missing, granted=sorted(enrolled_set)
        )
    return resolved


def _load_service_account_dwd_credentials(
    bot_id: str,
    gi: dict[str, Any],
    scopes: list[str],
    secrets_dir: Path,
):
    """Build subject-impersonated credentials for Path C.

    Extracted from the historical inline body of ``load_credentials``
    in this PR so the dispatch table in ``load_credentials`` is flat and
    each path's preconditions are localized.

    Before minting, requested scopes are clamped onto the SA's enrolled DwD
    grant (``_clamp_dwd_scopes``) so a tool that hardcodes a narrow
    ``.readonly`` scope rides the broader enrolled scope the operator
    actually delegated, instead of failing the whole mint.
    """
    subject = gi.get("subject")
    if not subject:
        raise GoogleIntegrationConfigError(
            f"bot {bot_id!r}: google_integration.subject is required "
            f"for mode 'service_account_dwd' (the Workspace user the "
            f"service account impersonates)"
        )

    secret_ref = gi.get("service_account_secret_ref")
    if not secret_ref:
        raise GoogleIntegrationConfigError(
            f"bot {bot_id!r}: google_integration.service_account_secret_ref "
            f"is required for mode 'service_account_dwd'"
        )

    sa_path = secrets_dir / f"{secret_ref}.json"
    if not sa_path.exists():
        raise FileNotFoundError(
            f"service account JSON not found at {sa_path}. Install it via the "
            f"manual fallback documented in the operator onboarding doc "
            f"(stage 2.2): sudo cp + chown evolve:wheel + chmod 0600. "
            f"The secrets-store CLI will automate this in a follow-on PR."
        )

    # Clamp requested scopes onto the per-bot DwD grant. Raises
    # DwdScopeNotEnrolled (naming the scope) if a requested scope is neither
    # enrolled nor covered by a broader enrolled scope.
    scopes = _clamp_dwd_scopes(bot_id, list(scopes), list(gi.get("scopes") or []))

    # Lazy import: google.oauth2 isn't installed in every dev/test env.
    from google.oauth2 import service_account  # type: ignore[import-untyped]

    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=list(scopes)
    )
    return creds.with_subject(subject)


# ── Scope discovery (Phase A.3) ─────────────────────────────────────────────


def available_scopes(
    bot_id: str,
    network: dict[str, Any] | None = None,
    *,
    tokens_dir: Path = DEFAULT_OAUTH_TOKENS_DIR,
) -> set[str]:
    """Return the scope set the bot can request without re-consenting.

    Path C (``service_account_dwd``): the configured ``scopes`` set —
    DwD authorization is what gates which scopes the SA can request, and
    is authoritatively known only by Workspace Admin. We trust the
    operator's config as the source of truth here; the pre-flight call
    catches mismatches.

    Path A (``free_gmail_oauth``): the union of consented scopes on the
    canonical token record, intersected with the bot's configured
    ``scopes`` list. The token record's grants are authoritative — a
    bot can't use a scope it didn't consent to even if the config lists
    it — so we use the record's ``scopes_granted`` field as the
    upper bound.

    Returns the empty set if the bot has no google_integration config,
    no token record, or an unsupported mode. Callers should treat empty
    as "Google not configured for this bot" and surface that to the
    operator before attempting the tool call.
    """
    if network is None:
        try:
            network = config.load_network()
        except Exception:
            return set()

    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    gi = bot_cfg.get("google_integration") or {}
    mode = gi.get("mode")
    configured = set(gi.get("scopes") or [])

    if mode == "service_account_dwd":
        return configured

    if mode == "free_gmail_oauth":
        try:
            record = load_token_record(bot_id, tokens_dir)
        except FileNotFoundError:
            # No consent yet — nothing available.
            return set()
        granted = set(record.get("scopes_granted") or [])
        if configured:
            # Intersect with configured: an operator who narrowed
            # ``scopes`` post-consent gets the narrowed set, not the
            # wider granted set. Symmetry with Path C, where the
            # configured scopes are also the effective set.
            return configured & granted
        return granted

    return set()


def assert_scopes_available(
    bot_id: str,
    required_scopes: list[str],
    network: dict[str, Any] | None = None,
    *,
    tokens_dir: Path = DEFAULT_OAUTH_TOKENS_DIR,
) -> None:
    """Raise ``InsufficientGrantedScopes`` if any required scope is missing.

    Convenience wrapper for MCP tools that want to fail fast with a
    clean operator-facing error before calling Google. Path A's
    ``load_credentials`` does this same check internally but the
    wrapper exposes it without the side effect of refreshing tokens —
    useful from a help/preflight UI surface.
    """
    available = available_scopes(bot_id, network=network, tokens_dir=tokens_dir)
    missing = [s for s in required_scopes if s not in available]
    if missing:
        raise InsufficientGrantedScopes(
            bot_id=bot_id, missing=missing, granted=sorted(available)
        )


# ── Public entry point ──────────────────────────────────────────────────────


def load_credentials(
    bot_id: str,
    scopes: list[str] | None = None,
    network: dict[str, Any] | None = None,
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
    tokens_dir: Path = DEFAULT_OAUTH_TOKENS_DIR,
    token_uri: str = GOOGLE_TOKEN_URI,
):  # -> google.auth.credentials.Credentials (lazy import for test envs)
    """Return Google API credentials for ``bot_id``.

    Dispatches on ``google_integration.mode``:

    * ``service_account_dwd`` (Path C) — impersonates ``subject`` via DwD.
    * ``free_gmail_oauth`` (Path A) — loads + refreshes the user-OAuth
      refresh token from the token store.
    * ``workspace_user_oauth`` (Path B) — still raises NotImplementedError.

    Args:
        bot_id: key in ``network.json`` ``bots``.
        scopes: OAuth scope strings to request. If ``None``, uses
            the bot's configured ``google_integration.scopes``.
        network: pre-loaded network config, or ``None`` to load fresh.
        secrets_dir: override the SA-JSON directory (Path C; tests).
        tokens_dir: override the OAuth-token directory (Path A; tests).
        token_uri: override the Google token endpoint (Path A; tests).

    Raises:
        GoogleIntegrationConfigError: missing block, missing required
            field, unknown ``mode``.
        NotImplementedError: mode is ``workspace_user_oauth``.
        FileNotFoundError: configured SA JSON or token record absent.
        RefreshTokenExpired: Path A refresh-token rejected by Google.
        InsufficientGrantedScopes: requested scopes ⊄ granted scopes.
    """
    if network is None:
        network = config.load_network()

    bot_cfg = (network.get("bots") or {}).get(bot_id)
    if not bot_cfg:
        raise GoogleIntegrationConfigError(
            f"bot {bot_id!r} not found in network.json"
        )

    gi = bot_cfg.get("google_integration")
    if not gi:
        raise GoogleIntegrationConfigError(
            f"bot {bot_id!r} has no google_integration block; "
            f"see docs/spec-google-integration-paths-2026-05-30.md "
            f"for the schema"
        )

    mode = gi.get("mode")
    if mode == "workspace_user_oauth":
        raise NotImplementedError(
            f"google_integration mode {mode!r} is not implemented in v1; "
            f"only 'service_account_dwd' and 'free_gmail_oauth' are "
            f"supported. The remaining path is tracked as a follow-on PR "
            f"in docs/spec-google-integration-paths-2026-05-30.md."
        )
    if mode not in ("service_account_dwd", "free_gmail_oauth"):
        raise GoogleIntegrationConfigError(
            f"bot {bot_id!r} google_integration.mode is "
            f"{mode!r}; expected one of "
            f"'service_account_dwd', 'free_gmail_oauth'"
        )

    if scopes is None:
        scopes = gi.get("scopes")
    if not scopes:
        raise GoogleIntegrationConfigError(
            f"bot {bot_id!r}: scopes must be provided either via "
            f"google_integration.scopes or the load_credentials call"
        )

    if mode == "service_account_dwd":
        return _load_service_account_dwd_credentials(
            bot_id, gi, list(scopes), secrets_dir
        )

    # mode == "free_gmail_oauth"
    return _load_free_gmail_oauth_credentials(
        bot_id,
        gi,
        network,
        list(scopes),
        tokens_dir,
        token_uri=token_uri,
    )
