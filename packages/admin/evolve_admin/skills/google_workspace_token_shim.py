"""evolve_admin.skills.google_workspace_token_shim — token-shape translator.

The Google Workspace skill suite (spec at
``internal/spec-google-workspace-suite-2026-06-04.md``) lands user OAuth tokens
in our standard ``auth-profiles.json`` shape, but the runtime consumer —
``taylorwilsdon/google_workspace_mcp`` (vetting doc at
``internal/vetting-workspace-mcp-2026-06-04.md``) — expects credentials on disk
in Google's own ``google.oauth2.credentials.Credentials.to_json()`` shape,
one file per Google user, at ``$WORKSPACE_MCP_CREDENTIALS_DIR/<email>.json``.

This module is the format translator. It does *not* refresh tokens (the MCP
server does that itself). It is called at two moments:

  1. **Install completion** — after ``/api/admin/onboard/google/callback``
     writes the OAuth profile, the install module calls
     :func:`write_credentials_for_bot` to stamp the credentials file the
     MCP server will read on first tool call.
  2. **Bot deploy / config refresh** — ``deploy.py`` calls the same function
     as part of the per-bot config-refresh sequence so that a bot whose
     install ran on a different version of Evolve still has a usable
     credentials file after redeploy.

The two callers share the same code path. The shim is idempotent: re-running
with the same inputs produces the same file (modulo the file's mtime).

Trust-chain decisions (recorded for the reviewer)
-------------------------------------------------

1. **The shim is the ONLY translation layer.** No other module reads from
   ``auth-profiles.json`` and writes the MCP credentials file. Two writers
   would drift and the MCP server would see whichever wrote last.

2. **No refresh logic here.** The vetting doc confirmed the MCP server
   calls ``credentials.refresh(Request())`` on expired tokens itself. If we
   pre-emptively refresh, we'd burn a quota unit per kickstart and race
   with the server's own refresh. Refer to vetting doc §3 — token-refresh
   handled by the server.

3. **Credentials file lives under ``.openclaw/``.** Specifically
   ``<bot_home>/.openclaw/google_workspace_mcp/credentials/<email>.json``,
   not the MCP server's default ``~/.google_workspace_mcp/credentials/``.
   Putting the file under ``.openclaw/`` gives us the same ACL / sudoers
   grant pattern as the other per-bot skill credentials (Notion, Dropbox,
   Obsidian) — one sudoers block covers reads and writes. The install
   module passes ``WORKSPACE_MCP_CREDENTIALS_DIR=<bot_home>/.openclaw/
   google_workspace_mcp/credentials`` to the MCP server's env binding so
   it looks in the right place.

4. **Filename is ``<google_email>.json``.** Matches the MCP server's
   single-user-or-multi-user filename convention (vetting doc §6).
   ``<email>`` is the value stored at ``profile.google_account`` —
   populated by the OAuth callback from Google's userinfo endpoint.
   Profile with empty ``google_account`` falls back to ``default.json``;
   the MCP server's single-user mode accepts any filename.

5. **Pure function plus thin IO wrapper.** :func:`build_credentials_json`
   takes the raw OAuth profile + client and returns the JSON payload —
   no disk. :func:`write_credentials_for_bot` wires the readers, calls
   the pure function, and writes via ``/tmp`` + ``sudo /bin/cp`` (per
   CLAUDE.md). Tests exercise the pure function with synthetic inputs;
   the IO wrapper has a thin integration test.

Sudoers grants required (added to ``setup_wizard.py`` §17 in a sibling PR)
-------------------------------------------------------------------------

::

    evolve ALL=(root) NOPASSWD: /bin/mkdir -p /Users/*/.openclaw/google_workspace_mcp/credentials
    evolve ALL=(root) NOPASSWD: /usr/sbin/chown * /Users/*/.openclaw/google_workspace_mcp
    evolve ALL=(root) NOPASSWD: /usr/sbin/chown -R * /Users/*/.openclaw/google_workspace_mcp
    evolve ALL=(root) NOPASSWD: /bin/cp /tmp/evolve-gws-*.json /Users/*/.openclaw/google_workspace_mcp/credentials/*.json
    evolve ALL=(root) NOPASSWD: /usr/sbin/chown * /Users/*/.openclaw/google_workspace_mcp/credentials/*.json
    evolve ALL=(root) NOPASSWD: /bin/chmod 600 /Users/*/.openclaw/google_workspace_mcp/credentials/*.json
    evolve ALL=(root) NOPASSWD: /bin/cat /Users/*/.openclaw/google_workspace_mcp/credentials/*.json
    evolve ALL=(root) NOPASSWD: /bin/rm /Users/*/.openclaw/google_workspace_mcp/credentials/*.json
    evolve ALL=(root) NOPASSWD: /bin/rm -rf /Users/*/.openclaw/google_workspace_mcp

The mkdir/chown for the parent dir is a one-time install-step concern; the
cp/chown/chmod for the per-email file is the steady-state shim write.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

#: Sub-directory under ``<bot_home>/.openclaw/`` where the MCP server reads
#: its per-user credentials files.
MCP_CREDENTIALS_SUBDIR = ".openclaw/google_workspace_mcp/credentials"

#: Default-on Google token URI; never varies for OAuth 2.0.
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

#: Filename when the OAuth profile has no ``google_account`` field. The MCP
#: server's single-user mode accepts any filename; ``default.json`` matches
#: the convention from server docs.
FALLBACK_CREDENTIALS_FILENAME = "default.json"

#: Regex for sanitising a Google email into a safe filename component.
#: Google emails can contain ``+`` and ``.``; both are filesystem-safe but
#: we strip anything outside ``[A-Za-z0-9._@+-]`` defensively.
_SAFE_FILENAME_CHAR = re.compile(r"[^A-Za-z0-9._@+\-]")


# ── Reader contracts (injectable for testability) ────────────────────────────


#: Reader callable: ``read_google_oauth_profile(bot_id) -> dict | None``.
#: The dict has the shape written by ``/api/admin/onboard/google/callback`` —
#: see ``server.py:17553``-ish. Specifically:
#: ``{provider, type, google_account, scopes, services, access_token,
#:    access_token_expires_at, refresh_token, issued_at, status, ...}``.
ProfileReader = Callable[[str], "dict[str, Any] | None"]


#: Reader callable:
#: ``read_google_oauth_client(bot_id) -> {client_id, client_secret, mode} | None``.
#: Resolved by the existing ``_read_google_oauth_client`` in ``server.py`` —
#: handles per-bot, legacy auth-profiles, and pod-wide fallbacks.
ClientReader = Callable[[str], "dict[str, Any] | None"]


# ── Pure: shape translation ──────────────────────────────────────────────────


def build_credentials_json(
    profile: dict[str, Any],
    oauth_client: dict[str, Any],
    *,
    quota_project_id: str | None = None,
) -> dict[str, Any]:
    """Translate an Evolve auth-profiles entry + OAuth client into the JSON
    shape ``taylorwilsdon/google_workspace_mcp`` reads.

    Pure: no disk, no network. Tests pass synthetic dicts.

    :param profile: the ``profiles[google_workspace_<bot_id>]`` entry from
        ``<bot_home>/.openclaw/auth-profiles.json``.
    :param oauth_client: ``{client_id, client_secret, mode}`` resolved by
        the OAuth client reader.
    :param quota_project_id: optional GCP project id to attribute quota to.
        Omit unless the operator explicitly wires it; the MCP server falls
        back to the OAuth client's project, which is fine for v1.

    :returns: dict ready to be ``json.dumps``ed into the credentials file.

    The output shape matches ``google.oauth2.credentials.Credentials.to_json()``
    (vetting doc §3):

    .. code-block:: json

       {
         "token": "ya29...",
         "refresh_token": "1//0g...",
         "client_id": "...apps.googleusercontent.com",
         "client_secret": "GOCSPX-...",
         "token_uri": "https://oauth2.googleapis.com/token",
         "scopes": ["https://www.googleapis.com/auth/gmail.send", ...],
         "expiry": "2026-06-04T19:30:00.000000Z",
         "id_token": null,
         "quota_project_id": null
       }

    Edge cases:
      * ``profile.refresh_token`` missing → we still write the access token.
        The MCP server will fail on first refresh, which surfaces via the
        health monitor as a re-auth Signal.
      * ``profile.access_token_expires_at`` missing → ``expiry`` is null;
        the MCP server treats that as "expires immediately" and refreshes
        on first use (which works as long as we have a refresh token).
      * ``profile.scopes`` empty → unusual but valid; the MCP server will
        fail at runtime with an "insufficient scope" error.
    """
    expiry_unix = profile.get("access_token_expires_at")
    expiry_iso: str | None = None
    if expiry_unix is not None:
        try:
            expiry_iso = (
                datetime.fromtimestamp(float(expiry_unix), tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            )
        except (TypeError, ValueError, OverflowError):
            expiry_iso = None

    return {
        "token": profile.get("access_token") or "",
        "refresh_token": profile.get("refresh_token") or "",
        "client_id": oauth_client.get("client_id") or "",
        "client_secret": oauth_client.get("client_secret") or "",
        "token_uri": GOOGLE_TOKEN_URI,
        "scopes": list(profile.get("scopes") or []),
        "expiry": expiry_iso,
        "id_token": None,
        "quota_project_id": quota_project_id,
    }


def credentials_filename_for(profile: dict[str, Any]) -> str:
    """Return the on-disk filename for ``profile``'s credentials file.

    The MCP server convention is ``<user_email>.json`` (vetting doc §1).
    For profiles missing ``google_account`` (rare, but possible on legacy
    paste-token installs) we fall back to ``default.json`` — the server's
    single-user mode accepts any filename and a missing email field at
    install time is itself a yellow flag the install module surfaces.
    """
    email = (profile.get("google_account") or "").strip()
    if not email:
        return FALLBACK_CREDENTIALS_FILENAME
    # Defensive: scrub anything outside the conservative filename charset.
    safe = _SAFE_FILENAME_CHAR.sub("_", email)
    return f"{safe}.json"


# ── Path resolution ──────────────────────────────────────────────────────────


def credentials_dir_for_bot(bot_id: str) -> Path:
    """Return the absolute path of the credentials directory for ``bot_id``.

    This is the value the install module passes to the MCP server's
    ``WORKSPACE_MCP_CREDENTIALS_DIR`` env var.
    """
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / MCP_CREDENTIALS_SUBDIR


def credentials_path_for_bot(bot_id: str, profile: dict[str, Any]) -> Path:
    """Return the absolute path of the credentials file for ``bot_id`` and
    ``profile``. Combines :func:`credentials_dir_for_bot` and
    :func:`credentials_filename_for`.
    """
    return credentials_dir_for_bot(bot_id) / credentials_filename_for(profile)


# ── IO: write via /tmp + sudo /bin/cp ────────────────────────────────────────


def _ensure_credentials_dir(bot_id: str) -> tuple[bool, str | None]:
    """Create the credentials dir if absent. Idempotent.

    Uses ``sudo /bin/mkdir -p`` + ``sudo /usr/sbin/chown -R <user>:staff``
    so the bot user can read its own credentials file. The
    ``/etc/sudoers.d/evolve`` grant for this path is added in
    ``setup_wizard.py`` (see module docstring).

    Returns ``(ok, error_message_or_None)``.
    """
    target = credentials_dir_for_bot(bot_id)

    # Idempotent: if dir exists with correct ownership, no-op.
    try:
        if target.is_dir():
            return True, None
    except (PermissionError, OSError):
        # We may not be able to stat as evolve; fall through to mkdir.
        pass

    try:
        from ..config import get_bot_user, load_network
        network = load_network()
        user = get_bot_user(bot_id, network)
    except Exception as exc:
        return False, f"network_load_failed: {exc.__class__.__name__}: {exc}"

    try:
        r = subprocess.run(
            ["sudo", "/bin/mkdir", "-p", str(target)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"mkdir failed: {r.stderr.strip() or 'unknown'}"
    except Exception as exc:
        return False, f"mkdir_error: {exc.__class__.__name__}: {exc}"

    # Make sure the bot user owns the dir tree (parent + credentials/).
    parent = target.parent  # .openclaw/google_workspace_mcp/
    try:
        subprocess.run(
            ["sudo", "/usr/sbin/chown", "-R", f"{user}:staff", str(parent)],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        # Non-fatal: dir exists, ownership may be wrong, the bot user's
        # process will hit EACCES on read and the install module's
        # post-write probe surfaces it. Logging only.
        log.warning("chown after mkdir failed for %s; continuing", target)

    return True, None


def write_credentials_for_bot(
    bot_id: str,
    *,
    read_oauth_profile: ProfileReader | None = None,
    read_oauth_client: ClientReader | None = None,
    quota_project_id: str | None = None,
) -> tuple[bool, str | None]:
    """Write (or refresh) the MCP credentials file for ``bot_id``.

    Reads the OAuth profile from ``auth-profiles.json`` and the OAuth
    client config from ``network.json`` / the secret store, translates
    via :func:`build_credentials_json`, and stages a ``/tmp`` write that
    ``sudo /bin/cp``s into the bot's ``.openclaw/google_workspace_mcp/
    credentials/<email>.json``.

    Idempotent. Safe to call from both the install module (after OAuth
    completes) and ``deploy.py`` (per-bot config refresh).

    :param bot_id: the logical bot id (network.json key).
    :param read_oauth_profile: injectable profile reader. Defaults to the
        same disk-+sudo-fallback shape used elsewhere.
    :param read_oauth_client: injectable client reader. Defaults to the
        cred-store-resolving reader.
    :param quota_project_id: passed through to
        :func:`build_credentials_json`. Almost always ``None`` in v1.

    :returns: ``(ok, error_message_or_None)``. On success returns
        ``(True, None)``. On ``profile_missing`` (skill not installed)
        returns ``(False, "profile_missing")`` — the caller treats this
        as "no work to do" rather than a hard error. On any other
        failure ``ok`` is False and the error string is a short reason.
    """
    profile_reader = read_oauth_profile or _default_profile_reader
    client_reader = read_oauth_client or _default_client_reader

    try:
        profile = profile_reader(bot_id)
    except Exception as exc:
        return False, f"profile_read_failed: {exc.__class__.__name__}: {exc}"

    if not profile:
        return False, "profile_missing"

    try:
        client = client_reader(bot_id)
    except Exception as exc:
        return False, f"client_read_failed: {exc.__class__.__name__}: {exc}"

    if not client or not client.get("client_id") or not client.get("client_secret"):
        return False, "client_not_configured"

    payload = build_credentials_json(profile, client, quota_project_id=quota_project_id)
    filename = credentials_filename_for(profile)
    dest = credentials_dir_for_bot(bot_id) / filename

    # Ensure the parent dir exists with bot ownership.
    ok, err = _ensure_credentials_dir(bot_id)
    if not ok:
        return False, err

    # Resolve target user for the final chown.
    try:
        from ..config import get_bot_user, load_network
        network = load_network()
        user = get_bot_user(bot_id, network)
    except Exception as exc:
        return False, f"network_load_failed: {exc.__class__.__name__}: {exc}"

    # /tmp staging — prefix matches the sudoers grant: "evolve-gws-*.json".
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-gws-{bot_id}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)

        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(dest)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"cp_failed: {r.stderr.strip() or 'unknown'}"

        r = subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", str(dest)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            # File is on disk but ownership wrong → bot can't read it.
            return False, f"chown_failed: {r.stderr.strip() or 'unknown'}"

        r = subprocess.run(
            ["sudo", "/bin/chmod", "600", str(dest)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"chmod_failed: {r.stderr.strip() or 'unknown'}"

        return True, None
    except Exception as exc:
        return False, f"write_failed: {exc.__class__.__name__}: {exc}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def remove_credentials_for_bot(bot_id: str) -> tuple[bool, str | None]:
    """Wipe the MCP credentials directory for ``bot_id``.

    Used by the revoke path. Best-effort — if the directory doesn't exist
    we return success. Uses ``sudo /bin/rm -rf`` because the files are
    bot-owned and evolve can't direct-remove them.

    Returns ``(ok, error_message_or_None)``.
    """
    target = credentials_dir_for_bot(bot_id).parent  # .openclaw/google_workspace_mcp/
    try:
        if not target.exists():
            return True, None
    except (PermissionError, OSError):
        # Can't stat; try the remove anyway.
        pass

    try:
        r = subprocess.run(
            ["sudo", "/bin/rm", "-rf", str(target)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"rm_failed: {r.stderr.strip() or 'unknown'}"
        return True, None
    except Exception as exc:
        return False, f"rm_error: {exc.__class__.__name__}: {exc}"


# ── Default readers (mirror gog_provider patterns) ───────────────────────────


def _default_profile_reader(bot_id: str) -> dict[str, Any] | None:
    """Default profile reader. Reads ``<bot_home>/.openclaw/auth-profiles.json``
    via direct read (ACL granted by ``set_evolve_read_acl``) with a
    ``sudo /bin/cat`` fallback for pre-ACL bot deploys.
    """
    from ..config import bot_home as _bot_home

    p = _bot_home(bot_id) / ".openclaw" / "auth-profiles.json"

    raw: str | None = None
    try:
        if p.exists():
            raw = p.read_text(encoding="utf-8")
    except PermissionError:
        raw = None
    except Exception:
        return None

    if raw is None:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(p)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return None
            raw = r.stdout
        except Exception:
            return None

    try:
        data = json.loads(raw) or {}
    except (json.JSONDecodeError, ValueError):
        return None

    profiles = (data.get("profiles") if isinstance(data, dict) else {}) or {}
    # Canonical key is ``google_workspace_<bot_id>`` (underscore form per
    # server.py::_google_oauth_profile_id). Legacy paste-token installs
    # used the bare ``google_workspace`` key; accept both.
    return profiles.get(f"google_workspace_{bot_id}") or profiles.get("google_workspace")


def _default_client_reader(bot_id: str) -> dict[str, Any] | None:
    """Default OAuth client reader.

    Resolution mirrors ``server.py::_read_google_oauth_client``:
      1. Per-bot ``network.bots[bot_id].googleOAuthClient`` + the Evolve
         credential store at ``{shared_dir}/credentials/<secret_bot>/
         google_oauth_client.json``.
      2. Legacy pod-level ``network.googleOAuthClient`` with a credential
         store fallback.

    Implemented inline (not by importing ``server.py``) so the shim
    stays callable from ``deploy.py`` without dragging in Flask.

    :returns: ``{client_id, client_secret, mode}`` or ``None``.
    """
    from ..config import load_network, DEFAULT_SHARED_DIR

    try:
        net = load_network()
    except Exception:
        return None

    shared_dir = Path(net.get("sharedDir") or DEFAULT_SHARED_DIR)

    def _read_secret_store(secret_bot: str) -> dict[str, Any] | None:
        path = shared_dir / "credentials" / secret_bot / "google_oauth_client.json"
        try:
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _resolve(cfg: dict[str, Any], default_secret_bot: str | None) -> dict[str, Any] | None:
        if not isinstance(cfg, dict):
            return None
        mode = cfg.get("mode") or "self_hosted"
        client_id = (cfg.get("client_id") or "").strip()
        if not client_id:
            return None
        secret_bot = (cfg.get("secret_bot") or default_secret_bot or "").strip() or None
        if secret_bot:
            store = _read_secret_store(secret_bot)
            if isinstance(store, dict) and store.get("client_secret"):
                return {
                    "mode": mode,
                    "client_id": store.get("client_id") or client_id,
                    "client_secret": store["client_secret"],
                }
        return None

    # 1) Per-bot
    bot_cfg = (net.get("bots") or {}).get(bot_id) or {}
    cfg = bot_cfg.get("googleOAuthClient") if isinstance(bot_cfg, dict) else None
    if isinstance(cfg, dict):
        resolved = _resolve(cfg, bot_id)
        if resolved is not None:
            return resolved

    # 2) Pod-level legacy
    legacy = net.get("googleOAuthClient")
    if isinstance(legacy, dict):
        resolved = _resolve(legacy, None)
        if resolved is not None:
            resolved.setdefault("mode", "legacy_pod_wide")
            return resolved

    return None
