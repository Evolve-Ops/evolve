"""Shared constants + helpers lifted out of ``register_admin_routes``.

This module is the first step (Increment 0) of the ``routes_admin.py``
decomposition (roadmap 4.1b — strategy memo
``docs/design-routes-admin-decomposition-2026-06-12.md``). It holds the
immutable constants/regex tables and the genuinely cross-region helpers
that the closure previously trapped, so they become importable and
unit-testable directly — no Flask test client required.

**No route handlers live here.** The handlers stay in
``routes_admin.py``; their closure-bound shims now delegate to the
module-level functions below.

§1.3 monkeypatch-at-call-time invariant (memo): helpers that tests patch
on ``server._NAME`` are NOT imported as module-level names here. The three
server names the Google helpers need (``_google_oauth_profile_id``,
``_google_token_refresh``) are not patched by any test (verified), but they
are still imported *lazily inside the functions* — NOT at module top —
because ``server.py`` imports ``routes_oc`` (which imports this module) at
module scope, before it has defined those names; a module-top
``from .server import ...`` here would deadlock that cold-import order with
a partially-initialized ``server`` module. Lazy import sidesteps the cycle
and keeps ``_github_api`` + the constants importable with zero server
dependency. Collaborators that are closure-local (``read_auth_profiles`` /
``write_auth_profiles`` / ``write_google_oauth_profile``) are threaded in
as parameters so the caller binds the late-bound versions.

Import-safety: at module top this imports only stdlib + ``..config``.
``_github_api`` and every constant pull in no ``server`` name, so
``routes_oc.py`` imports them at module top without any cycle.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from ..config import CANONICAL_SHARED_DIR, bot_home, chown_bin, load_network
from ..telemetry import get_logger

_log = get_logger("web.routes_admin_shared")

# ── Credential registry / provider metadata (immutable; lifted verbatim) ──────

# (provider, key_type, display_label, credential_class, category)
# credential_class: "api_key" | "token_pair" | "oauth" | "identifier"
# category: "llm" | "messaging" | "media" | "search" | "workspace" | "storage"
_KEY_REGISTRY: list[tuple[str, str, str, str, str]] = [
    # ── LLM Providers (always shown) ──────────────────────────────
    ("anthropic",         "token",      "MAX token",         "api_key",    "llm"),
    ("anthropic",         "api_key",    "API key",           "api_key",    "llm"),
    ("openai",            "api_key",    "API key",           "api_key",    "llm"),
    ("google",            "api_key",    "API key (Gemini)",  "api_key",    "llm"),
    ("xai",               "api_key",    "API key (Grok)",    "api_key",    "llm"),
    ("mistral",           "api_key",    "API key",           "api_key",    "llm"),
    ("groq",              "api_key",    "API key",           "api_key",    "llm"),
    ("perplexity",        "api_key",    "API key",           "api_key",    "llm"),
    ("together",          "api_key",    "API key",           "api_key",    "llm"),
    ("deepseek",          "api_key",    "API key",           "api_key",    "llm"),
    ("cohere",            "api_key",    "API key",           "api_key",    "llm"),
    # ── Messaging / Communication ──────────────────────────────────
    # Slack/Telegram store credentials in auth-profiles.json + mirror to
    # openclaw.json. Discord and WhatsApp are handled below as
    # integration_token rows because their credentials live entirely in
    # openclaw.json (channels.discord.token / channels.whatsapp.*).
    ("telegram",          "token_pair", "Bot token + Chat ID",   "token_pair", "messaging"),
    ("slack",             "token_pair", "Bot + App tokens",      "token_pair", "messaging"),
    # ── Media / Creative AI ────────────────────────────────────────
    ("runway",            "api_key",    "API key",           "api_key",    "media"),
    ("elevenlabs",        "api_key",    "API key",           "api_key",    "media"),
    ("suno",              "api_key",    "API key",           "api_key",    "media"),
    ("stability",         "api_key",    "API key",           "api_key",    "media"),
    # ── Search ────────────────────────────────────────────────────
    ("brave",             "api_key",    "API key",           "api_key",    "search"),
    # ── Storage / Files (OAuth) ────────────────────────────────────
    ("dropbox",           "oauth",      "OAuth",             "oauth",      "storage"),
    # ── Google Workspace (OAuth) ──────────────────────────────────
    ("google_workspace",  "oauth",      "OAuth",             "oauth",      "workspace"),
]

# Per-provider metadata for oauth and token_pair credential classes
_PROVIDER_META: dict[str, dict] = {
    # "google" is the Gemini LLM provider (api_key under google:default).
    # Distinct from "google_workspace" (OAuth into Gmail/Drive/Calendar)
    # below; the explicit "Google Gemini" display avoids the operator
    # ambiguity of seeing two "Google" rows on the dashboard.
    "google": {
        "display": "Google Gemini",
    },
    "google_workspace": {
        "display": "Google Workspace",
        "oauth_info": "Click Set Up to authorize Gmail, Calendar, Drive, Docs, Sheets, or Slides. Token refresh is automatic; reauthorize only when Google has revoked consent.",
    },
    "dropbox": {
        "display": "Dropbox",
        "oauth_info": "Dropbox uses OAuth. Access is granted by connecting your Dropbox account through the Dropbox app settings. Keys cannot be pasted directly.",
    },
    "telegram": {
        "display": "Telegram",
        "pair_info": "Create a bot at t.me/BotFather to get a token. Your chat ID appears in the bot's first message to you.",
        "fields": [
            {"key": "bot_token",  "label": "Bot token",  "secret": True},
            {"key": "chat_id",    "label": "Chat ID",    "secret": False},
        ],
    },
    "slack": {
        "display": "Slack",
        "pair_info": "Create a Slack app at api.slack.com/apps. Bot token from OAuth & Permissions, App token from Basic Information > App-Level Tokens.",
        "fields": [
            {"key": "bot_token",  "label": "Bot token (xoxb-...)",  "secret": True},
            {"key": "app_token",  "label": "App token (xapp-...)",  "secret": True},
            {"key": "user_token", "label": "User token (xoxp-..., optional)", "secret": True},
        ],
    },
}

# LLM providers OC's model layer looks up by ``<provider>:`` prefix.
# Profile keys for these MUST be colon-shaped; everything else
# (brave, runway, telegram, slack, ...) keeps the legacy
# underscore shape because their consumers read by name directly.
# Legacy underscore-shape profile keys that wizard-verify
# (`wizard_verify._LEGACY_KEY_RE`) flags as broken. Kept in sync with
# that regex — verify, rotate, and the batch normalizer all agree on
# which keys are wrong.
_LEGACY_PROFILE_KEY_RE = re.compile(
    r"^(anthropic|openai|brave)_(api_key|auth_token)$"
)

_PLACEHOLDER_RE = re.compile(
    r"x{5,}"
    r"|TEST\d{6,}"
    r"|\b(?:placeholder|example|dummy|replaceme|fake)\b"
    r"|your[-_]?key",
    re.IGNORECASE,
)

# ── View-config provider paths + secret-field mask list (immutable) ───────────

# Only providers whose VIEW_CONFIG affordance lands in the dashboard need
# an entry here; the endpoint returns 404 for anything else.
_VIEW_CONFIG_PATHS: dict[str, str] = {
    "telegram": "channels.telegram",
    "slack": "channels.slack",
    "discord": "channels.discord",
    "google_workspace": "plugins.entries.google",
}

# Field names that must be masked in the View-config response. Maps to
# the same secret intent as `secret: True` in token_pair fields_meta.
_VIEW_CONFIG_SECRET_FIELDS: tuple[str, ...] = (
    "botToken", "appToken", "userToken", "token",
    "client_secret", "private_key", "refresh_token", "access_token",
    "api_key", "apiKey",
)

# ── External API bases + shared HTTP timeout (immutable) ──────────────────────

GITHUB_API_BASE = "https://api.github.com"
BRAVE_API_BASE = "https://api.search.brave.com/res/v1/web/search"
HTTP_TIMEOUT_SECONDS = 8

# Canonical auth-profile id Evolve used (pre-store migration) to hold the
# Google OAuth client secret. Kept only for the backward-compat read path
# in ``_resolve_oauth_client_dict`` below.
GOOGLE_CLIENT_SECRET_PROFILE_ID = "_evolve_google_oauth_client"


# ── GitHub API caller (pure; no server-module dependency) ─────────────────────

def _github_api(
    method: str, path: str, token: str, body: dict | None = None,
) -> tuple[int, dict | list | None, dict]:
    """Make a github API call. Returns (status, parsed_json, headers).

    On network error or invalid JSON, returns (status_or_0, None, {}).
    Caller is responsible for interpreting per-endpoint semantics.

    Satisfies ``backup_keys.GithubApiFn`` so ``routes_oc.py`` can pass it
    to ``reconcile_pod`` instead of falling back to the module-private
    ``_default_github_api``.
    """
    import urllib.error
    import urllib.request
    url = f"{GITHUB_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            try:
                return resp.status, json.loads(raw) if raw else None, headers
            except json.JSONDecodeError:
                return resp.status, None, headers
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = None
        headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return e.code, parsed, headers
    except Exception:
        return 0, None, {}


# ── Google OAuth client/profile helpers (shared across onboard + skills) ──────
#
# These accept ``network_path`` (the one runtime dependency) and any
# closure-local collaborator (``read_auth_profiles`` / ``write_auth_profiles``
# / ``write_google_oauth_profile``) as explicit parameters, so the
# ``register_admin_routes`` shims bind this app's ``network_path`` and the
# late-bound auth-profile readers/writers. Logic is byte-identical to the
# closure versions they replace.

def _google_oauth_client_store_path(secret_bot: str, network_path: Path) -> Path:
    """Canonical Evolve-owned location for the bot's OAuth client secret.

    Lives under ``{shared_dir}/credentials/`` so the ``evolve`` user owns
    the full path and openclaw's auth-profiles management never sees it.

    Reads ``sharedDir`` from network.json each call rather than capturing
    at startup — supports tests that swap the network file mid-run, and
    keeps the resolver consistent with where ``load_network`` reads from
    elsewhere.
    """
    net = load_network(network_path)
    sd = Path(net.get("sharedDir") or CANONICAL_SHARED_DIR)
    return sd / "credentials" / secret_bot / "google_oauth_client.json"


def _read_google_oauth_client_secret_from_store(
    secret_bot: str, network_path: Path,
) -> dict | None:
    """Return ``{client_id, client_secret}`` from the Evolve credential
    store for ``secret_bot``, or ``None`` if the file is missing or
    malformed. Tolerant of partial / older shapes — only returns a
    dict when both fields are non-empty.
    """
    path = _google_oauth_client_store_path(secret_bot, network_path)
    try:
        text = path.read_text()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    cid = (obj.get("client_id") or "").strip()
    csec = (obj.get("client_secret") or "").strip()
    if not cid or not csec:
        return None
    return {"client_id": cid, "client_secret": csec}


def _resolve_oauth_client_dict(
    cfg: dict,
    *,
    default_secret_bot: str | None,
    network_path: Path,
    read_auth_profiles: Callable[[str], dict],
) -> dict | None:
    """Network-config dict → resolved ``{mode, client_id, client_secret}``.

    Tries (in order):
      a. Evolve credential store — ``cfg.secret_bot`` (default:
         ``default_secret_bot`` — the calling bot's id, or ``None``
         for legacy pod-level configs)
      b. Legacy ``client_secret_ref`` → bot's auth-profiles entry
         (the original shape; preserved for migration)

    Returns ``None`` when neither path resolves to a non-empty
    secret.
    """
    mode = cfg.get("mode") or "self_hosted"
    client_id = (cfg.get("client_id") or "").strip()
    if not client_id:
        return None

    # (a) Evolve credential store — the new canonical source.
    # ``secret_bot`` field is explicit-or-default. For per-bot
    # configs ``default_secret_bot`` is the calling bot's id; for
    # legacy pod-level configs it's None (no store path to try).
    secret_bot: str | None = (cfg.get("secret_bot") or default_secret_bot or "").strip() or None
    if secret_bot:
        store_hit = _read_google_oauth_client_secret_from_store(secret_bot, network_path)
        if store_hit is not None:
            # Store's client_id wins when present (avoids drift between
            # network.json and the credential file).
            return {
                "mode": mode,
                "client_id": store_hit.get("client_id") or client_id,
                "client_secret": store_hit["client_secret"],
            }

    # (b) Legacy auth-profiles shape — preserved for the migration
    # window. Once openclaw's auth-profiles rewrite clobbers the
    # entry (which it eventually does), this path returns None and
    # the operator needs to re-run ``evo setup-google`` to populate
    # the new store.
    secret_ref = cfg.get("client_secret_ref") or {}
    if isinstance(secret_ref, dict):
        ref_bot = secret_ref.get("bot") or secret_bot
        secret_profile = secret_ref.get("profile") or GOOGLE_CLIENT_SECRET_PROFILE_ID
        if ref_bot:
            auth = read_auth_profiles(ref_bot)
            prof = (auth.get("profiles") or {}).get(secret_profile) or {}
            client_secret = prof.get("client_secret") or prof.get("value") or ""
            if client_secret:
                return {
                    "mode": mode,
                    "client_id": client_id,
                    "client_secret": client_secret,
                }

    return None


def _read_google_oauth_client(
    bot_id: str | None,
    *,
    network_path: Path,
    read_auth_profiles: Callable[[str], dict],
) -> dict | None:
    """Resolve the configured Google OAuth client for ``bot_id``.

    Resolution order:

      1. **Per-bot via Evolve credential store (canonical):**
         reads ``network.bots[bot_id].googleOAuthClient`` for
         ``{mode, client_id, secret_bot}``, then loads
         ``client_secret`` from
         ``{shared_dir}/credentials/<secret_bot>/google_oauth_client.json``.
         ``secret_bot`` defaults to ``bot_id`` when omitted — two bots
         share a single OAuth app by setting ``secret_bot`` to a
         sibling's id.
      2. **Per-bot via legacy auth-profiles (migration fallback):**
         when the per-bot block has the older ``client_secret_ref``
         shape, reads from the referenced bot's auth-profiles entry.
         Surfaces a warn-class signal so operators can spot stale
         config; the actual credential still resolves so existing
         deploys keep working.
      3. **Legacy pod-level (deprecated):** ``network.googleOAuthClient``.
         Preserved for single-bot installs that haven't run
         ``evo setup-google`` per bot.

    Returns ``{client_id, client_secret, mode}`` or ``None``.

    Calling without ``bot_id`` reads only the pod-level legacy key —
    same shape as before, kept for callers that ask "is anything
    configured anywhere".
    """
    net = load_network(network_path)

    # 1+2) Per-bot path (canonical store first, legacy auth-profiles fallback)
    if bot_id:
        bots = net.get("bots") or {}
        bot_cfg = bots.get(bot_id) or {}
        cfg = bot_cfg.get("googleOAuthClient") if isinstance(bot_cfg, dict) else None
        if isinstance(cfg, dict):
            resolved = _resolve_oauth_client_dict(
                cfg, default_secret_bot=bot_id,
                network_path=network_path, read_auth_profiles=read_auth_profiles,
            )
            if resolved is not None:
                return resolved

    # 3) Legacy pod-level fallback (lives in network.googleOAuthClient)
    legacy = net.get("googleOAuthClient")
    if isinstance(legacy, dict):
        resolved = _resolve_oauth_client_dict(
            legacy, default_secret_bot=None,
            network_path=network_path, read_auth_profiles=read_auth_profiles,
        )
        if resolved is not None:
            # Mark the source so callers can log a deprecation if they want.
            resolved["mode"] = resolved.get("mode") or "legacy_pod_wide"
            return resolved

    return None


def _read_google_oauth_profile(
    bot_id: str,
    *,
    read_auth_profiles: Callable[[str], dict],
) -> dict | None:
    """Load the bot's Google OAuth profile from auth-profiles.json. Returns
    the raw profile dict or None if missing.
    """
    from .server import _google_oauth_profile_id  # lazy: see module docstring
    auth = read_auth_profiles(bot_id)
    profiles = auth.get("profiles") or {}
    return profiles.get(_google_oauth_profile_id(bot_id))


def _delete_google_oauth_profile(
    bot_id: str,
    *,
    read_auth_profiles: Callable[[str], dict],
    write_auth_profiles: Callable[[str, dict], bool],
) -> bool:
    """Remove the bot's Google OAuth profile from auth-profiles.json."""
    from .server import _google_oauth_profile_id  # lazy: see module docstring
    auth = read_auth_profiles(bot_id)
    if not auth:
        return True  # nothing to do
    profiles = auth.get("profiles") or {}
    pid = _google_oauth_profile_id(bot_id)
    if pid not in profiles:
        return True
    del profiles[pid]
    auth["profiles"] = profiles
    return write_auth_profiles(bot_id, auth)


def _ensure_fresh_google_access_token(
    bot_id: str,
    *,
    network_path: Path,
    read_auth_profiles: Callable[[str], dict],
    write_google_oauth_profile: Callable[[str, dict], bool],
) -> tuple[str | None, str | None]:
    """Return a usable access_token for `bot_id`, refreshing if needed.

    Returns (access_token, error). On success `error` is None. On failure
    (no profile, no client config, refresh failed) `access_token` is None
    and `error` is a short reason. On `invalid_grant` (refresh token
    revoked by Google) sets the profile's `status` to `reauth_required`
    and returns (None, "reauth_required").
    """
    import time
    from .server import _google_token_refresh  # lazy: see module docstring
    prof = _read_google_oauth_profile(bot_id, read_auth_profiles=read_auth_profiles)
    if not prof:
        return None, "missing"
    client = _read_google_oauth_client(
        bot_id, network_path=network_path, read_auth_profiles=read_auth_profiles,
    )
    if not client:
        return None, "client_not_configured"
    access = prof.get("access_token") or ""
    exp = float(prof.get("access_token_expires_at") or 0)
    # Refresh 60s before actual expiry to avoid a race.
    if access and exp > time.time() + 60:
        return access, None
    refresh = prof.get("refresh_token") or ""
    if not refresh:
        return None, "no_refresh_token"
    status, body = _google_token_refresh(refresh, client["client_id"], client["client_secret"])
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        err_code = (body or {}).get("error") if isinstance(body, dict) else None
        if err_code == "invalid_grant":
            prof = dict(prof)
            prof["status"] = "reauth_required"
            prof["access_token"] = ""
            prof["access_token_expires_at"] = 0
            write_google_oauth_profile(bot_id, prof)
            return None, "reauth_required"
        return None, f"refresh_http_{status}_{err_code or 'unknown'}"
    new_access = body["access_token"]
    new_exp = time.time() + int(body.get("expires_in") or 3500)
    # Google may issue a new refresh token; preserve the old one if not.
    prof = dict(prof)
    prof["_evolve_prev_access_token"] = prof.get("access_token") or ""
    prof["access_token"] = new_access
    prof["access_token_expires_at"] = new_exp
    if body.get("refresh_token"):
        prof["_evolve_prev_refresh_token"] = prof.get("refresh_token") or ""
        prof["refresh_token"] = body["refresh_token"]
    prof["status"] = "active"
    write_google_oauth_profile(bot_id, prof)
    return new_access, None


# ── Auth-profiles read/write substrate (4.1b prep lift) ───────────────────────
# Lifted out of ``register_admin_routes``'s closure so the skills-install and
# Google-onboard regions can read/write auth-profiles.json without the closure.
# These touch SECRETS (API keys, OAuth tokens) and run privileged sudo writes —
# held to the doctrine's auditor-grade bar. Bodies are byte-identical to the
# former closures; the only mechanical substitutions are: ``_subproc`` →
# ``subprocess``; ``_resolve_user(bot_id)`` / ``_read_oc_json(bot_id)`` →
# late-bound ``_module._resolve_bot_user`` / ``_module._read_oc_json`` (so test
# monkeypatches on ``server._NAME`` are still honored at call time, memo §1.3);
# ``_bot_home`` → ``bot_home``; ``_json`` → ``json``. ``network_path`` is a
# keyword-only parameter so the closure shims bind this app's path.


def _sudo_read(bot_id: str, path: str) -> str | None:
    """Read a file, trying direct read then sudo /bin/cat as root. Returns text or None."""
    # 1. Direct read (ACL gives evolve access after set_evolve_read_acl during deploy)
    try:
        text = Path(path).read_text()
        if text.strip():
            return text
    except (PermissionError, OSError, FileNotFoundError):
        pass

    # 2. sudo /bin/cat as root (sudoers grant covers .openclaw/ paths)
    try:
        proc = subprocess.run(
            ["sudo", "/bin/cat", path],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
    except Exception:
        pass

    return None


def _normalize_auth_profiles(raw) -> dict:
    """Coerce any known auth-profiles format into canonical {profiles: {id: {...}}}.

    Handles:
      1. Canonical:  {"profiles": {"id": {provider, type, api_key/token/value/key}}}
      2. Alt root keys: {"credentials"|"keys"|"api_keys"|"apiKeys": {...}}
      3. Flat provider map: {"anthropic": {"api_key": "..."}, ...}
      4. Ultra-flat:  {"anthropic": "sk-ant-...", "openai": "sk-..."}
      5. Array:  [{provider, api_key, ...}, ...]
    """
    if isinstance(raw, list):
        profiles: dict = {}
        for item in raw:
            if isinstance(item, dict):
                prov = item.get("provider") or item.get("name") or "unknown"
                ptype = item.get("type", "api_key")
                profiles.setdefault(f"{prov}:{ptype}", {**item, "provider": prov, "type": ptype})
        return {"profiles": profiles} if profiles else {}

    if not isinstance(raw, dict):
        return {}

    # Format 1: already canonical
    if isinstance(raw.get("profiles"), dict):
        return raw

    # Format 2: alternate root keys that wrap the profiles dict
    for alt in ("credentials", "keys", "api_keys", "apiKeys"):
        if isinstance(raw.get(alt), dict):
            return {"profiles": raw[alt]}

    # Formats 3/4: entire dict IS the profiles map (values are dicts or raw strings)
    if raw and all(isinstance(v, (dict, str)) for v in raw.values()):
        profiles = {}
        for k, v in raw.items():
            if isinstance(v, str):
                # ultra-flat: key name == provider, value == the key string
                profiles[f"{k}:default"] = {"provider": k, "type": "api_key", "api_key": v}
            else:
                prov = v.get("provider", k)
                ptype = v.get("type", "api_key")
                profiles[k] = {**v, "provider": prov, "type": ptype}
        return {"profiles": profiles}

    return {}


def _find_auth_profile_paths(bot_id: str, *, network_path: Path) -> list:
    """Discover candidate paths; resolve_bot_paths result comes first, then find results."""
    from .server import resolve_bot_paths  # lazy: see module docstring
    _module = sys.modules["evolve_admin.web.server"]
    user = _module._resolve_bot_user(bot_id, network_path)
    paths = resolve_bot_paths(bot_id, user=user)
    bot_home_dir = str(Path(paths["oc_config"]).parent.parent)  # ~/.openclaw → home
    # Primary: resolved from bot's openclaw.json config (most accurate)
    resolved = paths["auth_profiles"]
    found: list = [resolved]

    search_root = f"{bot_home_dir}/.openclaw"
    # Direct glob (ACL gives evolve access after deploy), fallback to sudo find as root
    try:
        for p in Path(search_root).rglob("auth-profiles.json"):
            s = str(p)
            if s not in found:
                found.append(s)
    except PermissionError:
        try:
            proc = subprocess.run(
                ["sudo", "find", search_root, "-name", "auth-profiles.json", "-maxdepth", "7"],
                capture_output=True, text=True, timeout=8,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                for p in proc.stdout.splitlines():
                    p = p.strip()
                    if p and p not in found:
                        found.append(p)
        except Exception:
            pass
    except Exception:
        pass

    for fallback in [
        f"{bot_home_dir}/.openclaw/agents/main/agent/auth-profiles.json",
        f"{bot_home_dir}/.openclaw/agents/main/auth-profiles.json",
        f"{bot_home_dir}/.openclaw/auth-profiles.json",
    ]:
        if fallback not in found:
            found.append(fallback)
    return found


def _read_auth_profiles(bot_id: str, *, network_path: Path) -> dict:
    """Find + read auth-profiles.json from the first usable location.

    Returns canonical {"profiles": {...}, "_source_path": path} or {}.
    Tries every discovered location and normalizes different data shapes.
    """
    _module = sys.modules["evolve_admin.web.server"]
    for path in _find_auth_profile_paths(bot_id, network_path=network_path):
        text = _sudo_read(bot_id, path)
        if text is None:
            continue
        try:
            normalized = _normalize_auth_profiles(json.loads(text))
            if normalized.get("profiles"):
                normalized["_source_path"] = path
                return normalized
        except Exception:
            continue

    # Last resort: check openclaw.json auth section for embedded credentials
    try:
        oc_cfg = _module._read_oc_json(bot_id, network_path)
        auth_sec = oc_cfg.get("auth", {})
        for sub in ("credentials", "profiles", "keys", "apiKeys"):
            cand = auth_sec.get(sub)
            if isinstance(cand, dict):
                normalized = _normalize_auth_profiles(cand)
                if normalized.get("profiles"):
                    normalized["_source_path"] = str(bot_home(bot_id) / ".openclaw" / "openclaw.json")
                    return normalized
    except Exception:
        pass

    return {}


def _write_auth_profiles(bot_id: str, data: dict, *, network_path: Path) -> bool:
    """Write auth-profiles.json via /tmp staging + sudo /bin/cp as root.

    Five sudo steps; the chown of the parent is load-bearing:
      1. sudo /bin/mkdir -p <parent>            (may leave parent root-owned)
      2. sudo /usr/sbin/chown <bot>:staff <parent>
                                                (so OC can atomically write
                                                tmp files in the dir during
                                                token-refresh cycles)
      3. sudo /bin/cp /tmp/... <dest>           (writes root-owned)
      4. sudo /usr/sbin/chown <bot>:staff <dest> (file readable+writable by bot)
      5. sudo /bin/chmod 600 <dest>             (API keys → not 644)

    Why steps 2 and 4 are both required:
      - Without step 2, OC's auth-profile refresh fails atomic write:
        it creates <dest>.<uuid>.tmp in the parent dir, which requires
        write access to the parent. Root-owned 755 parent → EACCES.
        Symptom: "Embedded agent failed before reply: EACCES ...
        auth-profiles.json.<uuid>.tmp" on every Telegram message.
      - Without step 4, the file is root-owned mode 600 → bot user
        can't read its own credentials. Same shape as the pairing-store
        bug fixed in #1815.

    Strips internal _source_path key before writing.
    """
    import tempfile as _tmpmod
    import os as _os
    import time as _time
    _module = sys.modules["evolve_admin.web.server"]
    from .server import resolve_bot_paths  # lazy: see module docstring
    user = _module._resolve_bot_user(bot_id, network_path)
    path = data.pop("_source_path", None)
    if path and not path.endswith("openclaw.json"):
        write_path = path
    else:
        write_path = resolve_bot_paths(bot_id, user=user)["auth_profiles"]

    content = json.dumps({k: v for k, v in data.items() if not k.startswith("_")}, indent=2)

    def _sudo(*cmd):
        return subprocess.run(list(cmd), capture_output=True, text=True, timeout=10)

    ts = int(_time.time())
    fd, tmp = _tmpmod.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-auth-{ts}-", suffix=".json")
    try:
        with _os.fdopen(fd, "w") as f:
            f.write(content)

        # 1. Ensure parent directory exists. Idempotent — may have been
        # created on an earlier write or by the wizard's initial setup.
        parent = str(Path(write_path).parent)
        _sudo("sudo", "/bin/mkdir", "-p", parent)

        # 2. Hand the parent dir to the bot user. Load-bearing: OC's
        # token-refresh path needs to atomic-write tmp files here.
        r_chown_parent = _sudo(
            "sudo", chown_bin(), f"{user}:staff", parent,
        )
        if r_chown_parent.returncode != 0:
            # Don't bail — log a warning and continue. Older bots may
            # have working parent dirs already; the chown is mostly
            # a no-op there.
            _log.warning(
                "chown parent dir failed for %s (%s): %s",
                bot_id, parent,
                (r_chown_parent.stderr or "").strip() or "unknown",
            )

        # 3. Copy to final path as root.
        r = _sudo("sudo", "/bin/cp", tmp, write_path)
        if r.returncode != 0:
            return False

        # 4. Hand the file to the bot user — bot must own it to
        # rename over it during OC's atomic-write cycle.
        r_chown_file = _sudo(
            "sudo", chown_bin(), f"{user}:staff", write_path,
        )
        if r_chown_file.returncode != 0:
            _log.warning(
                "chown file failed for %s (%s): %s",
                bot_id, write_path,
                (r_chown_file.stderr or "").strip() or "unknown",
            )

        # 5. Lock the mode. 600 (not 644) because auth-profiles.json
        # carries API keys; the file is bot-owned after step 4 so
        # the bot still has read access.
        _sudo("sudo", "/bin/chmod", "600", write_path)

        return True
    except Exception:
        return False
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
