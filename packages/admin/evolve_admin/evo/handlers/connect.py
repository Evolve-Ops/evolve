"""``evo connect google`` — per-bot Google Workspace OAuth, surfaced in chat.

Once :func:`evo setup-google` has been run on this bot to wire its own
OAuth client (per-bot under PR #1123), this command issues consent
against that client to authorize Gmail / Calendar / Drive / Docs /
Sheets / Slides for this bot. Returns a one-tap Google OAuth URL; the
existing admin server callback writes tokens to that bot's
``auth-profiles.json`` and the install-job sweeper auto-resumes any
pending forge jobs that were awaiting OAuth.

State branches:

  * Bot not configured yet (no per-bot ``googleOAuthClient`` for this
    bot, no legacy pod-level fallback, or no ``adminBaseUrl``) → tell
    the operator to run ``evo setup-google`` from this bot first;
    surface no auth URL.
  * Wizard-managed auth already present on this bot → confirm + show
    scopes; offer to re-authorize (different URL) if the operator wants
    to expand scope or rotate.
  * Legacy ``oc gws`` auth detected (per the same probe the admin UI
    uses) → explain the migration, surface the auth URL.
  * Fresh setup → surface the auth URL with a brief "click + come back"
    instruction.

The OAuth URL itself is built locally with the same state-token
machinery the admin server's
``/api/admin/onboard/google/begin`` route uses (``_google_state_create``
is module-level on ``web.server``). Redirect URI is composed from
``adminBaseUrl`` so it matches what the operator whitelisted during
``evo setup-google`` — independent of whatever Host header any
incoming HTTP request might have carried.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import speak


# Default scope set requested when an operator types `evo connect google`
# with no arguments. Picked to match what admin_bot / team_bot_a / team_bot_c currently
# have through the legacy `oc gws` CLI — Gmail (send + read), Calendar
# (read + write), Drive (per-file), Docs, Sheets, Slides. Operators who
# want a narrower set can run e.g. `evo connect google gmail_readonly`
# to pick specific service ids from `_GOOGLE_SCOPE_REGISTRY`.
_DEFAULT_SERVICES: tuple[str, ...] = (
    "gmail", "calendar", "drive", "docs", "sheets", "slides",
)

# Mirror of `web.server._GOOGLE_SCOPE_REGISTRY` for the services this
# handler can request. The full upstream registry has more entries
# (restricted scopes, etc.); we keep this trim because chat operators
# shouldn't be granting Gmail.modify or Drive.all by typing into
# Telegram. Operator can use the admin UI for those.
_SCOPE_BY_SERVICE: dict[str, list[str]] = {
    "gmail_readonly": [
        "https://www.googleapis.com/auth/gmail.readonly",
    ],
    "calendar_readonly": [
        "https://www.googleapis.com/auth/calendar.readonly",
    ],
    "gmail": [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    ],
    "calendar": [
        "https://www.googleapis.com/auth/calendar",
    ],
    "drive": [
        "https://www.googleapis.com/auth/drive.file",
    ],
    "docs": [
        "https://www.googleapis.com/auth/documents",
    ],
    "sheets": [
        "https://www.googleapis.com/auth/spreadsheets",
    ],
    "slides": [
        "https://www.googleapis.com/auth/presentations",
    ],
}


_GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    """Entry: parse args, route to the right branch, return Team_bot_a-style body."""
    parts = (args or "").strip().split()
    if not parts:
        return speak("connect", _usage_message(), role)

    integration = parts[0].lower()
    if integration not in {"google", "gmail", "calendar", "workspace"}:
        return speak(
            "connect",
            f"I don't know how to connect `{integration}`. Try "
            "`evo connect google` (the only one wired in chat today).",
            role,
        )

    requested_services = _resolve_services(parts[1:])
    if requested_services is None:
        return speak("connect", _bad_services_message(parts[1:]), role)

    # ── 1. Per-bot OAuth client gate (with legacy pod-level fallback) ──
    client_id = _resolve_client_id_for_bot(bot_id, network)
    admin_base_url = (network.get("adminBaseUrl") or "").strip()
    if not client_id or not admin_base_url:
        return speak("connect", _setup_first_message(
            bot_id, client_id, admin_base_url,
        ), role)

    # ── 2. Detect existing state on this bot ───────────────────────────
    wizard_state = _detect_wizard_auth(bot_id)
    legacy_state = _detect_legacy_auth(bot_id)

    # ── 3. Build the OAuth URL ─────────────────────────────────────────
    try:
        auth_url = _build_authorize_url(
            bot_id=bot_id,
            client_id=client_id,
            admin_base_url=admin_base_url,
            services=requested_services,
        )
    except Exception as e:
        return speak("connect", (
            f"Couldn't build the Google OAuth URL: {e}. "
            "Check that the admin server's state module is reachable, "
            "or run `evo setup-google` again to refresh this bot's config."
        ), role)

    # ── 4. Render the right variant ────────────────────────────────────
    if wizard_state.get("ok"):
        body = _wizard_already_present_message(
            bot_id, wizard_state, requested_services, auth_url,
        )
    elif legacy_state.get("present"):
        body = _legacy_migrate_message(
            bot_id, legacy_state, requested_services, auth_url,
        )
    else:
        body = _fresh_setup_message(
            bot_id, requested_services, auth_url,
        )
    return speak("connect", body, role)


# ─────────────────────────────────────────────────────────────────────────────
# Arg parsing
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_services(arg_tokens: list[str]) -> list[str] | None:
    """Return the service list to request. ``None`` on validation failure.

    Empty input → default set. Single token ``readonly`` → readonly defaults.
    Otherwise tokens must each match an entry in ``_SCOPE_BY_SERVICE``.
    """
    if not arg_tokens:
        return list(_DEFAULT_SERVICES)
    if len(arg_tokens) == 1 and arg_tokens[0].lower() == "readonly":
        return ["gmail_readonly", "calendar_readonly"]
    cleaned: list[str] = []
    for tok in arg_tokens:
        t = tok.lower().strip().rstrip(",")
        if not t:
            continue
        if t not in _SCOPE_BY_SERVICE:
            return None
        if t not in cleaned:
            cleaned.append(t)
    return cleaned or None


# ─────────────────────────────────────────────────────────────────────────────
# State detection
# ─────────────────────────────────────────────────────────────────────────────


def _detect_wizard_auth(bot_id: str) -> dict:
    """Return ``{ok: bool, services: list, google_account: str}``.

    Reads ``auth-profiles.json`` directly (canonical path first, then a
    few historical fallbacks). Reading via the bot user's home dir is
    fine for the chat handler — the admin server's evolve user has the
    macOS ACL grants set up at deploy time.

    Uses ``bot_home(bot_id)`` so logical bot ids that differ from the
    macOS user (team_bot_b/personal_bot_user) resolve correctly.
    """
    profile_id = f"google_workspace:{bot_id}"
    # Resolve the bot's home directory through config.bot_home so the
    # logical bot_id → macOS user mapping is honored. Falls back to
    # ``/Users/{bot_id}`` if the resolver can't load the network (e.g.
    # called early in process startup before config is initialised).
    try:
        from ...config import bot_home
        home = bot_home(bot_id)
    except Exception:
        # See packages/admin/tests/test_no_bot_id_paths.py — this fallback
        # is allowlisted because the primary path goes through bot_home().
        home = Path(f"/Users/{bot_id}")
    candidates = [
        home / ".openclaw/agents/main/agent/auth-profiles.json",
        home / ".openclaw/agents/main/auth-profiles.json",
        home / ".openclaw/agent/auth-profiles.json",
        home / ".openclaw/auth-profiles.json",
    ]
    for path in candidates:
        text = _read_with_fallback(path)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        profiles = (data.get("profiles") or {}) if isinstance(data, dict) else {}
        prof = profiles.get(profile_id) or {}
        if not isinstance(prof, dict):
            continue
        token = prof.get("token") or {}
        if isinstance(token, dict) and token.get("access_token"):
            services = prof.get("services") or []
            return {
                "ok": True,
                "services": list(services) if isinstance(services, list) else [],
                "google_account": prof.get("google_account") or "",
                "path": str(path),
            }
    return {"ok": False}


def _detect_legacy_auth(bot_id: str) -> dict:
    """Wrap ``web.server._detect_legacy_gws`` defensively. Returns
    ``{present: bool, scopes: list, google_account: str, ...}`` or
    ``{present: False}`` on any failure."""
    try:
        from ...web.server import _detect_legacy_gws  # module-level helper
    except ImportError:
        return {"present": False}
    try:
        result = _detect_legacy_gws(bot_id)
    except Exception:
        return {"present": False}
    if not isinstance(result, dict):
        return {"present": False}
    return result


def _read_with_fallback(path: Path) -> str | None:
    """Direct read, then sudo /bin/cat fallback (per CLAUDE.md)."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except PermissionError:
        pass
    except Exception:
        return None
    import subprocess
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# URL build
# ─────────────────────────────────────────────────────────────────────────────


def _build_authorize_url(
    *,
    bot_id: str,
    client_id: str,
    admin_base_url: str,
    services: list[str],
) -> str:
    """Construct the Google OAuth URL with the same state-token machinery
    the admin server's ``/api/admin/onboard/google/begin`` route uses.

    Uses ``adminBaseUrl`` from network.json for the redirect URI rather
    than incoming request headers — chat handlers never have a Flask
    request context, and we want the URL to match what the operator
    whitelisted in Google Cloud Console regardless of how this code
    path is invoked.
    """
    # Compute scopes from services
    scopes: list[str] = []
    for svc in services:
        for scope in _SCOPE_BY_SERVICE.get(svc, []):
            if scope not in scopes:
                scopes.append(scope)
    if not scopes:
        raise ValueError("no scopes resolved from services")

    redirect_uri = admin_base_url.rstrip("/") + "/api/admin/onboard/google/callback"

    # State creation is module-level on web.server
    from ...web.server import _google_state_create
    state = _google_state_create(bot_id, services, scopes, redirect_uri)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return _GOOGLE_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


# ─────────────────────────────────────────────────────────────────────────────
# Message rendering
# ─────────────────────────────────────────────────────────────────────────────


def _service_summary(services: list[str]) -> str:
    """Human-readable list of services for the operator. Trims to a
    short list of common-name labels."""
    label = {
        "gmail": "Gmail (send + read)",
        "gmail_readonly": "Gmail (read-only)",
        "calendar": "Calendar (read + write)",
        "calendar_readonly": "Calendar (read-only)",
        "drive": "Drive (per-file)",
        "docs": "Docs",
        "sheets": "Sheets",
        "slides": "Slides",
    }
    return ", ".join(label.get(s, s) for s in services)


def _usage_message() -> str:
    return (
        "**evo connect — what to wire up?**\n\n"
        "Today only Google Workspace is wired in chat. Try:\n"
        "  `evo connect google` — full scope set "
        "(Gmail / Calendar / Drive / Docs / Sheets / Slides)\n"
        "  `evo connect google readonly` — Gmail + Calendar read-only "
        "(safer for bots that only summarize)\n"
    )


def _bad_services_message(tokens: list[str]) -> str:
    valid = ", ".join(f"`{s}`" for s in _SCOPE_BY_SERVICE)
    return (
        f"I don't recognize one or more of those services. "
        f"Got: `{' '.join(tokens)}`.\n\n"
        f"Valid: {valid}\n\n"
        "Or `evo connect google` for the default set, or "
        "`evo connect google readonly` for read-only."
    )


def _resolve_client_id_for_bot(
    bot_id: str, network: dict[str, Any],
) -> str:
    """Per-bot OAuth client_id with legacy pod-level fallback.

    Mirrors the resolution order in
    :func:`evolve_admin.web.server._read_google_oauth_client`:

      1. ``network.bots[bot_id].googleOAuthClient.client_id``
      2. legacy ``network.googleOAuthClient.client_id``
    """
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    if isinstance(bot_cfg, dict):
        per_bot = bot_cfg.get("googleOAuthClient") or {}
        if isinstance(per_bot, dict):
            cid = (per_bot.get("client_id") or "").strip()
            if cid:
                return cid
    legacy = network.get("googleOAuthClient") or {}
    if isinstance(legacy, dict):
        cid = (legacy.get("client_id") or "").strip()
        if cid:
            return cid
    return ""


def _setup_first_message(
    bot_id: str, client_id: str, admin_base_url: str,
) -> str:
    """Per-bot OAuth client missing — point the operator at `evo setup-google`."""
    missing: list[str] = []
    if not client_id:
        missing.append(
            f"`network.bots[{bot_id}].googleOAuthClient.client_id` "
            "(no per-bot OAuth app, no legacy fallback)"
        )
    if not admin_base_url:
        missing.append("`adminBaseUrl`")
    missing_str = "\n  - ".join(missing) or "Google OAuth client config"
    return (
        f"**Google isn't set up for `{bot_id}` yet.**\n\n"
        f"Missing:\n  - {missing_str}\n\n"
        f"Run `evo setup-google` from this bot — it's a per-bot wizard "
        "that wires this bot's own OAuth app (each bot typically has its "
        "own Google account / Cloud project). After that, `evo connect "
        "google` works."
    )


def _fresh_setup_message(
    bot_id: str, services: list[str], auth_url: str,
) -> str:
    services_label = _service_summary(services)
    return (
        f"**Connect Google for `{bot_id}`**\n\n"
        f"Click to authorize {services_label}:\n"
        f"{auth_url}\n\n"
        "You'll see a Google consent screen — pick the account this bot "
        "should use, then approve. Tokens land in this bot's "
        "`auth-profiles.json` and gallery apps like Morning Briefing pick "
        "them up automatically.\n\n"
        "Check `evo integrations` afterward to confirm. Or come back here "
        "after the consent screen — the callback prints a 'you can close "
        "this tab' page."
    )


def _legacy_migrate_message(
    bot_id: str, legacy: dict, services: list[str], auth_url: str,
) -> str:
    legacy_scopes = legacy.get("scopes") or []
    scope_count = len(legacy_scopes) if isinstance(legacy_scopes, list) else 0
    google_account = legacy.get("google_account") or "(unknown account)"
    services_label = _service_summary(services)
    return (
        f"**Migrate `{bot_id}` from legacy `oc gws` auth**\n\n"
        f"You have legacy credentials for {google_account} "
        f"({scope_count} scopes) at `~/.config/gws/`. Those still work "
        "for the existing tool definitions on this bot, but new gallery "
        "apps (Morning Briefing, etc.) read from the wizard-managed "
        "path. Migrating gives you a single source of truth.\n\n"
        f"Click to re-authorize ({services_label}):\n"
        f"{auth_url}\n\n"
        "It's a full Google consent flow — the legacy tokens stay where "
        "they are (no auto-delete) but the new tokens take over for "
        "wizard-aware code. Existing functionality is preserved.\n\n"
        "After consent, `evo integrations` will show this bot as "
        "wizard-authorized."
    )


def _wizard_already_present_message(
    bot_id: str, wizard: dict, services: list[str], auth_url: str,
) -> str:
    current_services = wizard.get("services") or []
    google_account = wizard.get("google_account") or "(unknown account)"
    requested = set(services)
    have = set(current_services if isinstance(current_services, list) else [])
    new = requested - have
    if not new:
        return (
            f"**`{bot_id}` is already connected to Google** ✓\n\n"
            f"Account: {google_account}\n"
            f"Services: {_service_summary(current_services)}\n\n"
            "Nothing to do. If you want to expand or rotate, run "
            f"`evo connect google <service1> <service2>` with services "
            "not in the current set, or use the admin UI for "
            "fine-grained scope changes."
        )
    return (
        f"**`{bot_id}` is already connected to Google.**\n\n"
        f"Currently: {_service_summary(current_services)} "
        f"({google_account})\n"
        f"You're asking to add: "
        f"{_service_summary(sorted(new))}\n\n"
        "Click to re-authorize with the expanded scope set "
        f"({_service_summary(services)}):\n"
        f"{auth_url}\n\n"
        "Google will replace the existing token. Existing refresh "
        "tokens get rotated atomically."
    )
