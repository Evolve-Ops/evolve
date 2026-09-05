"""wizard_google_personal_routes.py — Path-A wizard backend (Phase A.2).

Spec: [internal/spec-google-path-a-2026-06-01.md](../../../../internal/spec-google-path-a-2026-06-01.md).

The Personal-Gmail wizard reuses the existing
``/api/admin/onboard/google/{configure,begin,callback,poll,revoke,status}``
flow (which already handles GCP-client setup, OAuth begin, callback +
token exchange, refresh, and revoke against the bot's
``auth-profiles.json``). What it adds is **just** the Path-A-specific
side of the integration:

* **Scope catalog filter** — ``GET /api/wizard/google/scopes?path=A``
  returns the SCOPE_CATALOG from the path-C wizard with each entry's
  ``paths`` field honoured. Entries restricted to path C (e.g.
  ``gmail.modify``, ``drive``) are filtered out because unverified
  Personal-Gmail OAuth can't request them.

* **Finalize** — ``POST /api/wizard/google/personal/finalize`` is
  called by the wizard after OAuth has succeeded. It:
  1. Reads the bot's ``auth-profiles.json`` Google profile (already
     populated by the OAuth callback).
  2. Mirrors the refresh/access tokens + granted scopes into the
     canonical Path-A token store at
     ``/Users/Shared/evolve/secrets/google_oauth_tokens/<bot>.json``
     (evolve-owned, 0600). This is the file
     ``google_auth.load_credentials`` reads for Path A.
  3. Writes the bot's ``google_integration`` block to network.json
     (``mode: "free_gmail_oauth"``, ``scopes: [...granted...]``,
     ``consent_screen_state``, ``reauth_contact``).
  4. Runs a light Path-A preflight (calls ``gmail.users.getProfile``
     to confirm the token works) so a misconfigured client surfaces
     immediately, not at first MCP tool call.

* **State** — ``GET /api/wizard/google/personal/state?bot=<id>``
  reports back the bot's current state: whether a canonical token
  exists, what scopes are granted, what the OAuth client's
  ``consent_screen_state`` is, and whether a refresh would succeed.
  Drives the wizard's open-screen decision (consent vs re-consent vs
  finalize-needed) and the chooser modal's per-bot chip.

* **Re-consent** — ``POST /api/wizard/google/personal/reconsent``
  builds an authorize URL that pre-fills the bot's existing granted
  scopes plus any newly-checked ones, and returns it to the wizard
  frontend. Wraps the existing ``/api/admin/onboard/google/begin``.

Notes:

- Tests inject ``read_oauth_profile`` / ``preflight_fn`` so the
  finalize endpoint is exercisable without standing up the real
  OAuth callback flow. Production wiring (set up in
  ``register_google_personal_wizard_routes``) reads from the OC bot's
  ``auth-profiles.json`` via the same helper the legacy flow uses.
- Per CLAUDE.md, the admin server runs as the ``evolve`` user, so
  writes to ``/Users/Shared/evolve/secrets/google_oauth_tokens/`` go
  directly; no sudo.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, jsonify, request

from .. import google_auth
from ..config import load_network, save_network
from .wizard_google_routes import SCOPE_CATALOG

_log = logging.getLogger(__name__)


# ── Scope-catalog ``paths`` defaults ────────────────────────────────────────
#
# The shared SCOPE_CATALOG in :mod:`wizard_google_routes` currently has
# no ``paths`` field; we apply the Path-A spec §1.5 defaults here. This
# keeps the canonical catalog single-source while the Path-A frontend
# gets a path-filtered view. When the catalog grows a ``paths`` field
# of its own (umbrella PR), this dict can be removed.
#
# Restricted scopes — ``gmail.modify`` and ``drive`` — are Path-C-only
# because Google's unverified consent flow will not grant them at all.
# Asking for them on Path-A produces a yellow "Google hasn't verified
# this app" screen AND a "this app is requesting sensitive scopes"
# error during the consent. Bot operators on Path-A can re-consent
# after Google verification completes; until then, omit.

_RESTRICTED_TO_PATH_C: frozenset[str] = frozenset({
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
})


def _annotate_paths(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of ``catalog`` with a ``paths`` field per entry."""
    out: list[dict[str, Any]] = []
    for entry in catalog:
        annotated = dict(entry)
        if "paths" not in annotated:
            if entry["id"] in _RESTRICTED_TO_PATH_C:
                annotated["paths"] = ["C"]
            else:
                annotated["paths"] = ["A", "C"]
        out.append(annotated)
    return out


def filter_catalog_by_path(
    catalog: list[dict[str, Any]], path: str | None
) -> list[dict[str, Any]]:
    """Return entries that include ``path`` in their ``paths`` list.

    ``path=None`` or unrecognized → return the full annotated catalog
    (back-compat with the path-C wizard frontend that doesn't pass a
    path param).
    """
    annotated = _annotate_paths(catalog)
    if path not in ("A", "C"):
        return annotated
    return [e for e in annotated if path in e["paths"]]


# ── Default reader / writer factories (production wiring) ───────────────────


def _default_read_oc_profile(bot_id: str, *, network_path: Path) -> dict | None:
    """Read the bot's google_workspace profile from auth-profiles.json.

    This MUST agree with how the OAuth callback *writes* the profile
    (``server.py::api_admin_onboard_google_callback`` →
    ``_write_google_oauth_profile``) on two axes — both of which the
    previous bespoke reader got wrong (live incident 2026-06-21, bot
    ``atlas``: consent succeeded + credential on disk, but finalize
    reported "no OAuth profile found"):

    * **Location.** The callback writes to the bot's *agent dir*
      (``resolve_bot_paths()['auth_profiles']`` →
      ``~/.openclaw/agents/main/agent/auth-profiles.json``), NOT
      ``~/.openclaw/auth-profiles.json``. We delegate discovery to
      :func:`routes_admin_shared._read_auth_profiles`, the same helper
      the callback's write path and the refresh path use — it resolves
      the location from the bot's openclaw.json + a recursive search,
      with the sudo fallback baked in. Reusing it means the read and
      write can never drift apart again.

    * **Profile-id key.** The canonical key is
      ``server._google_oauth_profile_id(bot_id)`` →
      ``google_workspace_<bot_id>`` (underscore). The previous reader
      looked up ``google_workspace:<bot_id>`` (colon), which is the
      *OC-side* oauth-provider convention — a key the Evolve callback
      never writes. We use the canonical underscore key, with bare
      ``google_workspace`` as the legacy bot-level fallback (matching
      ``google_workspace_token_shim.py``).

    Returns the profile dict or None if absent / unreadable.
    """
    from . import routes_admin_shared as _ras
    from .server import _google_oauth_profile_id

    auth = _ras._read_auth_profiles(bot_id, network_path=network_path)
    profiles = (auth or {}).get("profiles") or {}
    return (
        profiles.get(_google_oauth_profile_id(bot_id))
        or profiles.get("google_workspace")
    )


def _default_preflight(bot_id: str, tokens_dir: Path) -> dict[str, Any]:
    """Run a Path-A preflight via ``google_preflight.run_preflight``.

    Returns the shared module's standard response shape, adapted to
    the wizard's existing ``{ok, error?, profile?, skipped?, skip_reason?}``
    contract so the frontend doesn't need a separate code path for
    Path-A vs Path-C preflight results.
    """
    from .. import google_preflight as _gp

    try:
        # The shared preflight currently dispatches on the bot's
        # google_integration block (which Phase A.2 finalize has just
        # written). It calls ``load_credentials`` under the hood, which
        # will pick up the Path-A free_gmail_oauth mode and use the
        # canonical token store at ``tokens_dir``.
        raw = _gp.run_preflight(
            bot_id,
            tokens_dir=tokens_dir,
        )
    except TypeError:
        # Back-compat: the shared module may not yet accept
        # ``tokens_dir`` (it predates Phase A.1). Call without and
        # rely on the default location.
        raw = _gp.run_preflight(bot_id)

    if raw.get("skipped"):
        return {
            "ok": True,
            "skipped": True,
            "skip_reason": raw.get("skip_reason") or "",
        }
    if raw.get("ok"):
        return {"ok": True, "profile": raw.get("profile") or {}}
    return {"ok": False, "error": raw.get("error") or "preflight failed"}


# ── Token-store mirror ──────────────────────────────────────────────────────


def mirror_oc_profile_to_canonical_store(
    bot_id: str,
    profile: dict[str, Any],
    tokens_dir: Path,
) -> dict[str, Any]:
    """Translate an OC ``auth-profiles.json`` profile into the canonical record.

    The OC profile shape comes from ``server.py::_write_google_oauth_profile``:

      {provider, type, google_account, scopes, services,
       access_token, access_token_expires_at (epoch seconds),
       refresh_token, issued_at, status}

    The canonical Path-A record shape (per spec §1.2) uses:

      {refresh_token, access_token, access_token_expires_at (ISO-8601 UTC),
       scopes_granted, google_account, obtained_at}

    Returns the canonical record. Writing it to disk is the caller's
    responsibility (we keep that side effect testable via mocking).
    """
    epoch = profile.get("access_token_expires_at") or 0
    try:
        expires_at = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        # No / unparseable expiry — record a 1-minute-future expiry so the
        # first load triggers a refresh immediately. This is the safe
        # default for an unknown token state.
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    issued_at = profile.get("issued_at") or 0
    try:
        obtained_at_dt = datetime.fromtimestamp(float(issued_at), tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        obtained_at_dt = datetime.now(timezone.utc)
    return {
        "refresh_token": profile.get("refresh_token") or "",
        "access_token": profile.get("access_token") or "",
        "access_token_expires_at": expires_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "scopes_granted": list(profile.get("scopes") or []),
        "google_account": profile.get("google_account") or "",
        "obtained_at": obtained_at_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


# ── google_integration block writer ─────────────────────────────────────────


_VALID_CONSENT_STATES: frozenset[str] = frozenset(
    {"testing", "internal", "published", "unknown"}
)


def _validate_finalize_payload(
    body: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """Pull + validate the finalize body. Returns (errors, cleaned)."""
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    bot_id = (body.get("bot_id") or "").strip()
    if not bot_id:
        errors.append("bot_id is required")
    cleaned["bot_id"] = bot_id

    consent_state = (body.get("consent_screen_state") or "testing").strip().lower()
    if consent_state not in _VALID_CONSENT_STATES:
        errors.append(
            f"consent_screen_state must be one of {sorted(_VALID_CONSENT_STATES)} "
            f"(got {consent_state!r})"
        )
    cleaned["consent_screen_state"] = consent_state

    reauth_contact = body.get("reauth_contact") or {}
    if not isinstance(reauth_contact, dict):
        errors.append("reauth_contact must be an object")
        reauth_contact = {}
    cleaned_contact: dict[str, Any] = {}
    channel = (reauth_contact.get("channel") or "").strip()
    user_ext_id = (reauth_contact.get("user_external_id") or "").strip()
    if channel:
        cleaned_contact["channel"] = channel
    if user_ext_id:
        cleaned_contact["user_external_id"] = user_ext_id
    cleaned["reauth_contact"] = cleaned_contact

    return errors, cleaned


def _apply_path_a_config(
    network: dict[str, Any],
    bot_id: str,
    granted_scopes: list[str],
    consent_screen_state: str,
    reauth_contact: dict[str, Any],
) -> None:
    """Mutate ``network[bots][<id>][google_integration]`` for Path-A."""
    bots = network.setdefault("bots", {})
    bot_entry = bots.setdefault(bot_id, {})
    gi = bot_entry.setdefault("google_integration", {})
    gi["mode"] = "free_gmail_oauth"
    gi["scopes"] = list(granted_scopes)
    gi["consent_screen_state"] = consent_screen_state
    # Defaults per spec §1.1: oauth_client_secret_ref points at the
    # pod-wide entry; per-bot override is supported by hand-editing
    # network.json but the wizard never sets it.
    gi.setdefault("oauth_client_secret_ref", "google-oauth-client-pod")
    gi.setdefault("oauth_token_secret_ref", bot_id)
    if reauth_contact:
        gi["reauth_contact"] = reauth_contact


# ── State endpoint shape ────────────────────────────────────────────────────


def _bot_path_a_state(
    network: dict[str, Any],
    bot_id: str,
    tokens_dir: Path,
) -> dict[str, Any]:
    """Build the per-bot state response.

    Returns the bot's google_integration block (or ``configured: false``
    if absent), whether the canonical token file exists, and the
    last-known refresh status. Does NOT run a live preflight — that's
    the explicit ``preflight`` call. Cheap; safe to poll.
    """
    bot_entry = (network.get("bots") or {}).get(bot_id) or {}
    gi = bot_entry.get("google_integration") or {}
    token_path = tokens_dir / f"{bot_id}.json"
    token_record: dict[str, Any] = {}
    if token_path.exists():
        try:
            token_record = json.loads(token_path.read_text(encoding="utf-8"))
        except Exception:
            token_record = {}
    return {
        "bot_id": bot_id,
        "configured": gi.get("mode") == "free_gmail_oauth",
        "mode": gi.get("mode"),
        "scopes": list(gi.get("scopes") or []),
        "consent_screen_state": gi.get("consent_screen_state"),
        "reauth_contact": gi.get("reauth_contact") or {},
        "canonical_token_present": bool(token_record),
        "google_account": token_record.get("google_account", ""),
        "last_refreshed_at": token_record.get("last_refreshed_at"),
        "scopes_granted": list(token_record.get("scopes_granted") or []),
    }


# ── Flask registration ──────────────────────────────────────────────────────


def register_google_personal_wizard_routes(
    app: Flask,
    network_path: Path,
    *,
    tokens_dir: Path = google_auth.DEFAULT_OAUTH_TOKENS_DIR,
    read_oc_profile: Callable[[str], dict | None] | None = None,
    preflight_fn: Callable[[str, Path], dict[str, Any]] | None = None,
) -> None:
    """Register ``/api/wizard/google/personal/*`` + scope-filter endpoints."""

    _read_profile = read_oc_profile or (
        lambda bot_id: _default_read_oc_profile(bot_id, network_path=network_path)
    )
    _preflight = preflight_fn or _default_preflight

    @app.get("/api/wizard/google/scopes-by-path")
    def api_google_scopes_by_path() -> Response:
        """Filtered scope catalog: ?path=A|C.

        Kept distinct from the path-C wizard's existing
        ``/api/wizard/google/scopes`` so we don't break that contract.
        Phase A.5 may unify these once both wizards consume the same
        endpoint.
        """
        path = (request.args.get("path") or "").strip().upper() or None
        filtered = filter_catalog_by_path(SCOPE_CATALOG, path)
        return jsonify({
            "scopes": filtered,
            "default_scopes": [s["id"] for s in filtered if s.get("default_set")],
            "path": path,
        })

    @app.get("/api/wizard/google/personal/state")
    def api_google_personal_state() -> Response:
        bot_id = (request.args.get("bot") or request.args.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"error": "bot query param required"}), 400
        network = load_network(network_path)
        if bot_id not in (network.get("bots") or {}):
            return jsonify({"error": f"bot {bot_id!r} not in network.json"}), 404
        return jsonify(_bot_path_a_state(network, bot_id, tokens_dir))

    @app.post("/api/wizard/google/personal/finalize")
    def api_google_personal_finalize() -> Response:
        """Mirror the OAuth profile + write google_integration + preflight.

        Body:
          {
            "bot_id": "ada",
            "consent_screen_state": "testing",       # required; spec §1.4
            "reauth_contact": {                       # optional but recommended
              "channel": "telegram",
              "user_external_id": "<id>"
            }
          }

        Returns:
          {
            "ok": bool,
            "errors": [...],
            "token_mirrored": bool,
            "preflight": {ok, error?, profile?},
            "bot_state": <_bot_path_a_state output>
          }
        """
        body = request.get_json(silent=True) or {}
        errors, cleaned = _validate_finalize_payload(body)
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400

        bot_id = cleaned["bot_id"]
        network = load_network(network_path)
        if bot_id not in (network.get("bots") or {}):
            return jsonify({
                "ok": False,
                "errors": [
                    f"bot {bot_id!r} not in network.json; provision the bot "
                    "first via the add-bot wizard"
                ],
            }), 400

        oc_profile = _read_profile(bot_id)
        if not oc_profile:
            return jsonify({
                "ok": False,
                "errors": [
                    f"no OAuth profile found for bot {bot_id!r} in "
                    f".openclaw/auth-profiles.json — the OAuth consent step "
                    "must run first via /api/admin/onboard/google/begin"
                ],
            }), 400

        if oc_profile.get("status") == "reauth_required":
            return jsonify({
                "ok": False,
                "errors": [
                    f"bot {bot_id!r} OAuth profile is in reauth_required "
                    "state; complete re-consent before finalize"
                ],
            }), 400

        if not oc_profile.get("refresh_token"):
            return jsonify({
                "ok": False,
                "errors": [
                    f"bot {bot_id!r} OAuth profile has no refresh_token; "
                    "re-run consent with prompt=consent to obtain one"
                ],
            }), 400

        # 1. Mirror to canonical token store.
        canonical_record = mirror_oc_profile_to_canonical_store(
            bot_id, oc_profile, tokens_dir
        )
        try:
            google_auth.write_token_record(bot_id, canonical_record, tokens_dir)
        except Exception as exc:
            _log.exception("wizard/google/personal: token-mirror write failed")
            return jsonify({
                "ok": False,
                "errors": [
                    f"writing canonical token store failed: {exc}. "
                    f"Check ownership of /Users/Shared/evolve/secrets/."
                ],
            }), 500

        # 2. Write the google_integration block to network.json.
        granted_scopes = list(oc_profile.get("scopes") or [])
        _apply_path_a_config(
            network,
            bot_id,
            granted_scopes,
            cleaned["consent_screen_state"],
            cleaned["reauth_contact"],
        )
        try:
            save_network(network, network_path)
        except Exception as exc:
            _log.exception("wizard/google/personal: save_network failed")
            return jsonify({
                "ok": False,
                "errors": [f"writing network.json failed: {exc}"],
            }), 500

        # 3. Preflight.
        preflight = _preflight(bot_id, tokens_dir)

        return jsonify({
            "ok": True,
            "errors": [],
            "token_mirrored": True,
            "preflight": preflight,
            "bot_state": _bot_path_a_state(network, bot_id, tokens_dir),
        })

    @app.post("/api/wizard/google/personal/reconsent")
    def api_google_personal_reconsent() -> Response:
        """Build a re-consent authorize URL preserving granted scopes.

        Body: ``{bot_id, additional_services?: [str]}``. Returns the
        same shape as ``/api/admin/onboard/google/begin``
        (``{authorize_url, state, scopes}``) but the wizard frontend
        gets a pre-filled scope set instead of having to recompute it.

        Implementation: thin orchestrator over the existing
        ``/api/admin/onboard/google/begin`` endpoint — we delegate via
        the test client to keep the redirect-uri building + state
        token generation in one place. In production wiring this is
        ``app.test_client().post(...)``.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"error": "bot_id required"}), 400

        network = load_network(network_path)
        bot_entry = (network.get("bots") or {}).get(bot_id) or {}
        gi = bot_entry.get("google_integration") or {}
        existing_scopes = list(gi.get("scopes") or [])
        additional = body.get("additional_services") or []

        # The legacy /begin endpoint takes service ids (e.g. "gmail_send"),
        # not raw scopes. For re-consent we already have the bot's full
        # scope set on disk, so build the authorize URL inline using the
        # same shape /begin does. This keeps re-consent independent of
        # the registry-id mapping (which is path-C-flavored).
        client_id, _client_secret = _resolve_oauth_client_for_reconsent(
            bot_id, network
        )
        if not client_id:
            return jsonify({
                "error": "google_oauth_client_not_configured",
                "hint": (
                    "Configure the pod-wide OAuth app first via "
                    "/api/admin/onboard/google/configure (or the wizard's "
                    "Screen 1)."
                ),
            }), 412

        # Re-consent must include every previously-granted scope plus
        # any newly-checked ones; missing previous scopes would shrink
        # the bot's access. Use a set to dedup.
        new_scopes = sorted(set(existing_scopes) | set(additional))
        if not new_scopes:
            return jsonify({
                "error": "no_scopes_to_reconsent",
                "hint": (
                    "Re-consent requires at least one scope. Configure the "
                    "bot's google_integration.scopes or pass "
                    "additional_services in the request body."
                ),
            }), 400

        host = request.headers.get("Host") or "localhost:5050"
        scheme = request.headers.get("X-Forwarded-Proto") or (
            "https" if request.is_secure else "http"
        )
        redirect_uri = f"{scheme}://{host}/api/admin/onboard/google/callback"

        import urllib.parse as _up
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(new_scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            # The state token would normally be created by the legacy
            # /begin's _google_state_create. Here we mark the URL as
            # re-consent-flavored; the wizard frontend uses its own
            # popup poll. Wired by Phase A.2 frontend (index.html).
            "state": f"reconsent:{bot_id}",
        }
        authorize_url = (
            f"https://accounts.google.com/o/oauth2/v2/auth?{_up.urlencode(params)}"
        )

        return jsonify({
            "ok": True,
            "authorize_url": authorize_url,
            "scopes": new_scopes,
            "redirect_uri": redirect_uri,
        })


def _resolve_oauth_client_for_reconsent(
    bot_id: str, network: dict[str, Any]
) -> tuple[str, str]:
    """Resolve (client_id, client_secret) for reconsent. Empty on miss.

    Delegates to the store-aware ``google_auth._resolve_oauth_client`` so a
    store-backed client — the canonical wizard shape, where network.json holds
    only ``{client_id, secret_bot}`` and the secret lives in the Evolve
    credential store — resolves here too. Before the G7 fix this read only
    inline secrets, so re-consent wrongly 412'd "client not configured" for a
    bot that WAS configured (e.g. live atlas). Returns empty strings instead of
    raising so the route handler can return a clean 412 with a hint.
    """
    try:
        return google_auth._resolve_oauth_client(bot_id, network)
    except google_auth.GoogleIntegrationConfigError:
        return "", ""
