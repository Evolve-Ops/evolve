"""HTTP routes for the Admin (ocadmin / bot-config) surface.

/api/admin/*           per-bot config reads, writes, permission patches, kickstart
/api/bots              bot list, overview, tile data
/api/status/*          live bot status, heartbeat, transcript
/api/deploy/*          deploy, restart, model-set
/api/auth-profiles/*   auth profile CRUD

Extracted from server.py (Batch D).
"""

from __future__ import annotations

import json
import json as _json  # alias used by helpers copied from server.py
import os
import re
import sys
import tempfile
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, Response

from ..config import (
    load_network,
    save_network,
    DEFAULT_NETWORK_CONFIG,
    DEFAULT_POD_INVARIANT_INTEGRATIONS,
    get_bot_user,
)
from ..runtime import get_launchd_scheduler
from ..telemetry import get_logger
from .routes_shared import _redact_secrets
from .credentials_visibility import finalize_credential_rows
from .credentials_oc import brave_key_from_oc_config
# _audit_log_entry is accessed via _module._audit_log_entry so that
# monkeypatch on server._audit_log_entry in tests is respected at call time.
from .probes import (
    DOTENV_PROVIDER_KEYS,
    MANIFEST_CATALOG,
    OPENCLAW_CHANNELS_FIELDS,
    Affordance,
    AuthProfilesTokenPairProbe,
    DotenvProbe,
    GhCliProbe,
    IntegrationTokenProbe,
    OpenclawChannelsTokenProbe,
    ProbeContext,
    ProbeHelpers,
    ProbeOutcome,
    SshKeyProbe,
    WizardAuthProfilesProbe,
    build_probes,
    envvar_for_provider_field,
    manifests_matching_provider,
)

_log = get_logger("web.routes_admin")

# ── Names from server.py (cycle-safe: server.py imports us only inside the shim) ──
from .server import (  # noqa: E402  (import after logger setup is intentional)
    _now_iso,
    resolve_bot_paths,
    _apply_credential_to_oc_dict,
    _oc_keys_for_storage,
    _store_discovered_pat_nonce,
    _resolve_credential,
    _ensure_brave_wired_in_dict,
    _mask_key,
    _RUNTIME_MIRROR_PATH,
    GOOGLE_AUTHORIZE_URL,
    GOOGLE_OAUTH_BASE_SCOPES,
    _GOOGLE_SCOPE_REGISTRY,
    _google_state_create,
    _google_state_get,
    _google_state_set_result,
    _google_state_consume,
    # _google_token_refresh: lifted helper _ensure_fresh_google_access_token now
    # lazily imports it inside routes_admin_shared, so it is unused here.
    # GOOGLE_REVOKE_URL / _google_http_form_post / _operator_proposal_response:
    # moved to routes_skills_workspace.py with the skills-install handlers (4.1b
    # Inc 2d), so they are no longer referenced here.
    _google_token_revoke,
    _google_oauth_profile_id,
    _remediation_hint_for,
    _v2_probes_enabled,
)
# Patchable helpers: NOT imported as module-level names — accessed through
# _module (= sys.modules["evolve_admin.web.server"]) inside the function so
# monkeypatches on `server._NAME` in tests are respected at call time.
# (A `from .server import NAME` binding in this module would shadow patches.)

# ── Shared constants + cross-region helpers (4.1b Increment 0) ────────────────
# Lifted out of the register_admin_routes closure into routes_admin_shared.py so
# they are importable + unit-testable directly. The closure shims below delegate
# to these. See docs/design-routes-admin-decomposition-2026-06-12.md §3.
from .routes_admin_shared import (  # noqa: E402
    _KEY_REGISTRY,
    _PROVIDER_META,
    _VIEW_CONFIG_PATHS,
    _VIEW_CONFIG_SECRET_FIELDS,
    _LEGACY_PROFILE_KEY_RE,
    _PLACEHOLDER_RE,
    # GITHUB_API_BASE + GOOGLE_CLIENT_SECRET_PROFILE_ID also live in
    # routes_admin_shared but are no longer referenced here (their only users —
    # _github_api and _resolve_oauth_client_dict — were lifted too), so they are
    # not imported back into this module.
    BRAVE_API_BASE,
    HTTP_TIMEOUT_SECONDS,
    _github_api,
    _google_oauth_client_store_path as _shared_google_oauth_client_store_path,
    _read_google_oauth_client_secret_from_store as _shared_read_google_oauth_client_secret_from_store,
    _resolve_oauth_client_dict as _shared_resolve_oauth_client_dict,
    _read_google_oauth_client as _shared_read_google_oauth_client,
    _read_google_oauth_profile as _shared_read_google_oauth_profile,
    _delete_google_oauth_profile as _shared_delete_google_oauth_profile,
    _ensure_fresh_google_access_token as _shared_ensure_fresh_google_access_token,
    # Auth-profiles read/write substrate — lifted to routes_admin_shared.py
    # (4.1b prep) so the skills-install + Google-onboard regions can use it
    # without the closure. The shims below thread this app's network_path.
    _sudo_read as _shared_sudo_read,
    _normalize_auth_profiles as _shared_normalize_auth_profiles,
    _find_auth_profile_paths as _shared_find_auth_profile_paths,
    _read_auth_profiles as _shared_read_auth_profiles,
    _write_auth_profiles as _shared_write_auth_profiles,
)


def register_admin_routes(app: Flask, network_path: Path) -> None:
    import subprocess as _subproc  # noqa: needed for sudo reads within this scope

    # ── Keys helpers ────────────────────────────────────────────────────────

    # Closure-bound shims: delegate to the module-level helpers (defined just
    # above _register_admin_routes) with this app's `network_path` bound. The
    # module-level versions are what unit tests import directly. _mask_key has
    # no network_path dep and resolves naturally to the module-level name.
    import sys as _sys_for_shims
    # Functions like _bot_user/_read_oc_json/_write_oc_json are defined in
    # server.py (module-level). We moved this function body here, so __name__
    # is now 'evolve_admin.web.routes_admin'. Look up the server module
    # explicitly so the closure-bound shims below resolve correctly.
    _module = _sys_for_shims.modules["evolve_admin.web.server"]

    def _resolve_user(bot_id: str) -> str:
        # Late-bound lookup on the server module so test monkeypatches of
        # server._resolve_bot_user are respected at call time; threads this
        # app's network_path (which may differ from the default in tests).
        return _module._resolve_bot_user(bot_id, network_path)

    def _prime_auth_store(bot_id: str) -> None:
        # OC-SQLITE-AUTH-WRITE: import the just-written auth-profiles.json into the bot's per-agent
        # OpenClaw sqlite store so a (re)start reads the on-disk key (best-effort; key never on argv/stdin — see oc_auth_provision).
        from ..oc_auth_provision import ensure_agent_auth_store_imported
        ensure_agent_auth_store_imported(bot_id, _resolve_user(bot_id))

    # ── Auth-profiles read/write substrate → lifted to routes_admin_shared.py ──
    # Thin shims binding this app's network_path to the module-level helpers;
    # tests swap the _read_auth_profiles cell the Google-OAuth shims capture
    # (memo §1.3), so keep these closure cells even though the bodies moved.

    def _sudo_read(bot_id: str, path: str) -> str | None:
        return _shared_sudo_read(bot_id, path)

    def _normalize_auth_profiles(raw) -> dict:
        return _shared_normalize_auth_profiles(raw)

    def _find_auth_profile_paths(bot_id: str) -> list:
        return _shared_find_auth_profile_paths(bot_id, network_path=network_path)

    def _read_auth_profiles(bot_id: str) -> dict:
        return _shared_read_auth_profiles(bot_id, network_path=network_path)

    def _write_auth_profiles(bot_id: str, data: dict) -> bool:
        return _shared_write_auth_profiles(bot_id, data, network_path=network_path)

    def _read_oc_json(bot_id: str) -> dict:
        return _module._read_oc_json(bot_id, network_path)

    def _write_oc_json(bot_id: str, data: dict) -> bool:
        return _module._write_oc_json(bot_id, data, network_path)

    def _mirror_to_openclaw(
        bot_id: str, provider: str, field_key: str, value: str,
    ) -> tuple[bool, str | None]:
        """Mirror a rotated credential into the bot's openclaw.json.

        Reads `openclaw.json`, deep-sets the value at the registry path for
        (provider, field_key), then writes it back via the same /tmp+sudo cp
        pattern used elsewhere. Returns (ok, error_or_None).

        Idempotent and a no-op when the registry has no entry for the
        (provider, field_key) pair (e.g. non-secret fields like Telegram
        chat_id, or providers we never mirror).
        """
        if provider not in _RUNTIME_MIRROR_PATH:
            return True, None  # provider not mirrored — not an error
        oc_cfg = _read_oc_json(bot_id)
        if not oc_cfg:
            return False, "Could not read openclaw.json"
        applied = _apply_credential_to_oc_dict(oc_cfg, provider, field_key, value)
        if not applied:
            return True, None  # field_key not in registry for provider — no-op
        if not _write_oc_json(bot_id, oc_cfg):
            return False, "Failed to write openclaw.json"
        return True, None

    def _rotate_openclaw_channels(
        bot_id: str, provider: str, field_key: str, key_value: str,
    ) -> Response:
        """Rotate a token_pair secret stored in `openclaw.json#channels.<provider>`.

        Used when the bot was configured outside the wizard and has no
        auth-profiles entry to update — the OpenclawChannelsTokenProbe
        flags the row with `storage="openclaw_channels"` and the modal
        echoes that back so we hit this branch.

        Per decision B (no affordance may break a working integration):
        the runtime reads from this exact path (`_RUNTIME_MIRROR_PATH`),
        so writing here keeps the bot online. Auth-profiles is intentionally
        NOT touched — adding a stale auth-profiles entry would make a
        future routine-rotate (which mirrors auth-profiles → openclaw)
        clobber the value the operator just set.
        """
        spec = OPENCLAW_CHANNELS_FIELDS.get(provider)
        if not spec:
            return jsonify({
                "error": f"openclaw_channels storage not supported for {provider}",
                "valid_providers": sorted(OPENCLAW_CHANNELS_FIELDS.keys()),
            }), 400
        valid_secret_fields = {fk: oc for fk, oc, secret in spec if secret}
        if not field_key:
            return jsonify({
                "error": "field_key required for openclaw_channels rotation",
                "valid_fields": sorted(valid_secret_fields.keys()),
            }), 400
        if field_key not in valid_secret_fields:
            return jsonify({
                "error": (
                    f"field_key '{field_key}' is not a rotatable secret for "
                    f"{provider} under openclaw_channels storage"
                ),
                "valid_fields": sorted(valid_secret_fields.keys()),
            }), 400

        oc_cfg = _read_oc_json(bot_id)
        if not oc_cfg:
            return jsonify({"error": "Could not read openclaw.json"}), 500

        oc_field = valid_secret_fields[field_key]
        channels = oc_cfg.setdefault("channels", {})
        block = channels.setdefault(provider, {})
        if not isinstance(block, dict):
            return jsonify({
                "error": f"channels.{provider} is not an object",
            }), 500
        block[oc_field] = key_value

        if not _write_oc_json(bot_id, oc_cfg):
            return jsonify({"error": "Failed to write openclaw.json"}), 500

        # Verify the write landed where we expect — never claim success
        # without a side effect we can read back. Mirrors the discord
        # rotate route's post-write check.
        oc_after = _read_oc_json(bot_id)
        landed = (
            (oc_after.get("channels") or {})
            .get(provider, {})
            .get(oc_field)
        )
        if landed != key_value:
            return jsonify({
                "error": (
                    f"post-write verification failed: channels.{provider}."
                    f"{oc_field} did not match the value we just wrote"
                ),
            }), 500

        _module._audit_log_entry("keys.rotate", bot_id, {
            "provider": provider,
            "storage": "openclaw_channels",
            "field_key": field_key,
            "oc_field": oc_field,
        }, oc_keys=_oc_keys_for_storage("openclaw_channels", provider))
        return jsonify({
            "ok": True,
            "storage": "openclaw_channels",
            "provider": provider,
            "field_key": field_key,
            "oc_field": oc_field,
            "requires_restart": True,
            "restart_endpoint": f"/api/admin/gateway/{bot_id}/restart",
        })

    def _rotate_dotenv(
        bot_id: str, provider: str, field_key: str, key_value: str,
    ) -> Response:
        """Rotate a token_pair secret stored in `~/.openclaw/workspace/.env`.

        Used when the bot has no auth-profiles entry and no
        `openclaw.json#channels.<provider>` token — the team_bot_a-style pattern
        where the runtime picks tokens up from a workspace .env file. The
        DotenvProbe flags the row with `storage="dotenv"` and the modal
        echoes that back so we hit this branch.

        Per decision B (no affordance may break a working integration):
        the runtime reads from the .env directly, so writing here keeps
        the bot online. Other lines in the file are preserved verbatim
        (the file routinely holds unrelated secrets like database
        passwords) — see `_rewrite_workspace_dotenv_value` for the
        invariants. Auth-profiles is intentionally NOT touched.

        Audit log captures only the env-var name and the storage tag —
        never the rotated value.
        """
        if provider not in DOTENV_PROVIDER_KEYS:
            return jsonify({
                "error": f"dotenv storage not supported for {provider}",
                "valid_providers": sorted(DOTENV_PROVIDER_KEYS.keys()),
            }), 400
        if not field_key:
            return jsonify({
                "error": "field_key required for dotenv rotation",
                "valid_fields": sorted({"bot_token", "app_token", "user_token"}),
            }), 400
        env_var_name = envvar_for_provider_field(provider, field_key)
        if env_var_name is None:
            return jsonify({
                "error": (
                    f"field_key '{field_key}' is not a rotatable dotenv slot "
                    f"for {provider}"
                ),
                "valid_env_vars": list(DOTENV_PROVIDER_KEYS.get(provider) or ()),
            }), 400

        ok, err = _module._write_workspace_dotenv_value(
            bot_id, env_var_name, key_value, network_path=network_path,
        )
        if not ok:
            # Don't leak the value in the error path.
            return jsonify({"error": err or "failed to rewrite .env"}), 500

        # Verify the write landed by reading the updated key list back.
        # Presence-check only — we never read the rotated value back.
        present = _module._detect_workspace_dotenv_keys(
            bot_id, (env_var_name,), network_path=network_path,
        )
        if env_var_name not in present:
            return jsonify({
                "error": (
                    f"post-write verification failed: {env_var_name} not "
                    f"detected in .env after rewrite"
                ),
            }), 500

        _module._audit_log_entry("keys.rotate", bot_id, {
            "provider": provider,
            "storage": "dotenv",
            "field_key": field_key,
            "env_var": env_var_name,
        })
        return jsonify({
            "ok": True,
            "storage": "dotenv",
            "provider": provider,
            "field_key": field_key,
            "env_var": env_var_name,
            "requires_restart": True,
            "restart_endpoint": f"/api/admin/gateway/{bot_id}/restart",
        })

    def _discover_github_remote(bot_id: str) -> dict | None:
        return _module._discover_github_remote(bot_id, network_path)

    def _discover_github_token(bot_id: str) -> tuple[str, str] | None:
        """PAT-only view of _discover_github_remote, kept for callers that
        specifically need an HTTPS PAT (rotate route, pod-default cascade).
        Returns (token, repo_slug) for HTTPS-PAT bots; None otherwise.
        """
        info = _discover_github_remote(bot_id)
        if info and info.get("auth_type") == "https_pat":
            return (info["token"], info["repo_slug"])
        return None

    # ── Discord channel discovery ───────────────────────────────────────────
    # Openclaw stores the Discord credential at channels.discord.token (NOT
    # botToken — that key is rejected by the strict schema). Multi-account
    # configs additionally populate channels.discord.accounts.<id>.token; the
    # top-level token is itself the "default" account.
    def _discover_discord_account(bot_id: str) -> dict | None:
        """Return the Discord channel state for a bot, or None when missing.

        Shape:
          {"shape": "single",       "token": <str>, "enabled": <bool>, "guild_count": <int>}
          {"shape": "multi_account","accounts": [<id>...], "default_token": <str|None>,
           "enabled": <bool>}
          None when channels.discord is absent.

        Multi-account is surfaced as a separate shape so the rotate route can
        return a clear 400 (we don't want to silently rewrite only the default
        account's token when the operator may have meant a sub-account).
        """
        oc_cfg = _read_oc_json(bot_id)
        if not oc_cfg:
            return None
        discord = (oc_cfg.get("channels") or {}).get("discord")
        if not isinstance(discord, dict):
            return None
        accounts = discord.get("accounts") if isinstance(discord.get("accounts"), dict) else None
        token = discord.get("token") or None
        enabled = bool(discord.get("enabled", False))
        if accounts:
            return {
                "shape": "multi_account",
                "accounts": sorted(accounts.keys()),
                "default_token": token,
                "enabled": enabled,
            }
        if not token:
            # channels.discord exists but is just a placeholder — treat as missing
            # so the operator gets the Set up button, not an Active row.
            return None
        guilds = discord.get("guilds") if isinstance(discord.get("guilds"), dict) else {}
        return {
            "shape": "single",
            "token": token,
            "enabled": enabled,
            "guild_count": len(guilds),
        }

    def _validate_discord_token(token: str, timeout: float = 8.0) -> tuple[bool, str | None, dict | None]:
        """Probe Discord's /users/@me endpoint with the given bot token.

        Returns (ok, error, identity). identity (when ok) carries
        {"id": ..., "username": ..., "discriminator": ...} so the operator
        can confirm they pasted the right bot's token.
        """
        import urllib.request as _ureq, urllib.error as _uerr
        if not token or not token.strip():
            return False, "token is empty", None
        req = _ureq.Request(
            "https://discord.com/api/v10/users/@me",
            headers={
                "Authorization": f"Bot {token.strip()}",
                "User-Agent": "evolve-admin-ui (+https://evolve.local)",
            },
        )
        try:
            with _ureq.urlopen(req, timeout=timeout) as resp:
                body = _json.loads(resp.read().decode("utf-8"))
                return True, None, {
                    "id": body.get("id"),
                    "username": body.get("username"),
                    "discriminator": body.get("discriminator"),
                }
        except _uerr.HTTPError as e:
            if e.code == 401:
                return False, "Discord rejected the token (401 Unauthorized) — check that you pasted the bot token, not the application client secret", None
            return False, f"Discord API error {e.code}: {e.reason}", None
        except Exception as exc:
            return False, f"Discord API request failed: {exc}", None

    def _set_discord_token(bot_id: str, token: str) -> tuple[bool, str | None]:
        """Write `token` into openclaw.json at channels.discord.token.

        - Creates channels.discord if absent (with enabled=true).
        - Preserves all sibling keys (groupPolicy, guilds, etc).
        - Atomic via /tmp + sudo /bin/cp (the same pattern _write_oc_json uses).
        """
        oc_cfg = _read_oc_json(bot_id)
        if not isinstance(oc_cfg, dict):
            return False, "could not read openclaw.json"
        channels = oc_cfg.setdefault("channels", {})
        discord = channels.setdefault("discord", {})
        if not isinstance(discord, dict):
            return False, "channels.discord is not an object"
        # Refuse to clobber a multi-account config from this single-token route.
        if isinstance(discord.get("accounts"), dict) and discord["accounts"]:
            return False, (
                "channels.discord.accounts is populated — multi-account configs "
                "must be edited in openclaw.json directly to avoid accidentally "
                "rewriting only the default account's token"
            )
        discord["token"] = token
        # First-time setup: enable the channel so the gateway picks it up.
        discord.setdefault("enabled", True)
        if not _write_oc_json(bot_id, oc_cfg):
            return False, "failed to write openclaw.json"
        return True, None

    # ── WhatsApp channel discovery ──────────────────────────────────────────
    # WhatsApp via openclaw uses Baileys multi-file auth state (a directory of
    # session files written during a QR-pairing run on the bot host). There is
    # NO rotatable token — the credential is the paired session itself.
    # Detection just reports whether channels.whatsapp is present and enabled,
    # plus whether a Baileys auth directory looks populated.
    def _discover_whatsapp_account(bot_id: str) -> dict | None:
        """Return the WhatsApp channel state for a bot, or None when absent.

        Shape:
          {"present": True, "enabled": <bool>, "auth_dir": <str|None>,
           "auth_dir_populated": <bool>, "shape": "single"|"multi_account"}
          None when channels.whatsapp is absent.
        """
        oc_cfg = _read_oc_json(bot_id)
        if not oc_cfg:
            return None
        whatsapp = (oc_cfg.get("channels") or {}).get("whatsapp")
        if not isinstance(whatsapp, dict):
            return None
        accounts = whatsapp.get("accounts") if isinstance(whatsapp.get("accounts"), dict) else None
        auth_dir = whatsapp.get("authDir")
        # Default Baileys auth dir per openclaw is in the bot's .openclaw tree;
        # we don't probe its contents (bot-owned, not sudoers-readable for
        # arbitrary sub-paths) — but we report whatever the config points at.
        return {
            "present": True,
            "enabled": bool(whatsapp.get("enabled", False)),
            "auth_dir": auth_dir if isinstance(auth_dir, str) else None,
            "auth_dir_populated": False,  # opaque to admin UI; bot host owns the session
            "shape": "multi_account" if accounts else "single",
        }

    def _set_whatsapp_enabled(bot_id: str, enabled: bool) -> tuple[bool, str | None]:
        """Set channels.whatsapp.enabled in openclaw.json.

        Creates a minimal channels.whatsapp scaffold if absent so the operator
        gets a configurable starting point — matches the placeholder shape
        already present on most pod bots (dmPolicy/groupPolicy).

        Idempotency is the route's responsibility (it short-circuits before
        calling this); this helper always writes when called.
        """
        oc_cfg = _read_oc_json(bot_id)
        if not isinstance(oc_cfg, dict):
            return False, "could not read openclaw.json"
        channels = oc_cfg.setdefault("channels", {})
        whatsapp = channels.setdefault("whatsapp", {})
        if not isinstance(whatsapp, dict):
            return False, "channels.whatsapp is not an object"
        whatsapp["enabled"] = bool(enabled)
        whatsapp.setdefault("dmPolicy", "pairing")
        whatsapp.setdefault("groupPolicy", "allowlist")
        if not _write_oc_json(bot_id, oc_cfg):
            return False, "failed to write openclaw.json"
        return True, None

    def _ensure_github_remote(
        bot_id: str, login: str, repo: str, token: str,
        *, remote_name: str = "evolve-backup",
    ) -> tuple[bool, str | None]:
        """Idempotently ensure the bot's `.git/config` has a remote pointing
        at https://<token>@github.com/{login}/{repo}.git.

        - If `[remote "<remote_name>"]` is absent, append a fresh block.
        - If present with a different URL, replace just the url= line.
        - If present with the same URL, no-op.

        Used by the onboarding flow to initialize the backup remote on a
        fresh bot. The github rotate route (api_admin_rotate_github_integration_token)
        keeps its existing regex-based behavior — it rewrites ANY github URL,
        not just `evolve-backup`, so that bots whose backup remote happens to
        be named `origin` (legacy setups) still get rotated correctly.

        Returns (ok, error_or_None).
        """
        import re as _re3, tempfile as _tmpmod, os as _os
        user = _resolve_user(bot_id)
        paths = resolve_bot_paths(bot_id, user=user)
        git_config_path = str(Path(paths["workspace"]) / ".git" / "config")

        cfg_text: str | None = None
        try:
            cfg_text = Path(git_config_path).read_text()
        except PermissionError:
            # ``-n`` makes sudo fail immediately with a clear stderr if no
            # NOPASSWD rule matches, instead of trying to prompt for a password
            # on the launchd-spawned admin server (which has no TTY and would
            # otherwise hang until the 5-second timeout).
            r = _subproc.run(
                ["sudo", "-n", "/bin/cat", git_config_path],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                cfg_text = r.stdout
            else:
                return False, f"sudo /bin/cat failed: {(r.stderr or '').strip()}"
        except FileNotFoundError:
            return False, f".git/config not found at {git_config_path}"
        except Exception as _exc:
            return False, f"read error: {_exc}"
        if cfg_text is None:
            return False, "could not read .git/config"

        new_url = f"https://{token}@github.com/{login}/{repo}.git"
        new_block = (
            f'[remote "{remote_name}"]\n'
            f'\turl = {new_url}\n'
            f'\tfetch = +refs/heads/*:refs/remotes/{remote_name}/*\n'
        )

        # Look for an existing [remote "<remote_name>"] block.
        section_re = _re3.compile(
            rf'(\[remote "{_re3.escape(remote_name)}"\][^\[]*)',
            _re3.MULTILINE | _re3.DOTALL,
        )
        match = section_re.search(cfg_text)
        if match:
            section = match.group(1)
            url_re = _re3.compile(r'^\s*url\s*=\s*\S+\s*$', _re3.MULTILINE)
            if url_re.search(section):
                if new_url in section:
                    return True, None  # already correct, no-op
                new_section = url_re.sub(f'\turl = {new_url}', section, count=1)
            else:
                # Section exists but no url line — append one
                new_section = section.rstrip() + f"\n\turl = {new_url}\n"
            updated = cfg_text[:match.start()] + new_section + cfg_text[match.end():]
        else:
            # No section — append the whole block, preserving trailing newline
            sep = "" if cfg_text.endswith("\n") else "\n"
            updated = cfg_text + sep + new_block

        if updated == cfg_text:
            return True, None

        fd, tmp = _tmpmod.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".gitconfig")
        try:
            with _os.fdopen(fd, "w") as f:
                f.write(updated)
            # ``-n`` so a missing NOPASSWD rule fails immediately with a
            # readable stderr instead of trying to prompt for a password on
            # the TTY-less admin server (which would otherwise produce the
            # confusing "sudo: a terminal is required" message).
            r = _subproc.run(
                ["sudo", "-n", "/bin/cp", tmp, git_config_path],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return False, f"sudo /bin/cp failed: {(r.stderr or '').strip()}"
            # Restore bot ownership so a later rotate (or any git operation
            # that touches .git/config) doesn't trip over root-owned config.
            # 644 is git's expected mode. Best-effort — if chown/chmod fail
            # we still succeeded the write itself.
            _subproc.run(
                ["sudo", "-n", "/usr/sbin/chown", f"{user}:staff", git_config_path],
                capture_output=True, text=True, timeout=5,
            )
            _subproc.run(
                ["sudo", "-n", "/bin/chmod", "644", git_config_path],
                capture_output=True, text=True, timeout=5,
            )
            return True, None
        except Exception as _exc:
            return False, f"write error: {_exc}"
        finally:
            try:
                _os.unlink(tmp)
            except OSError:
                pass

    # _KEY_REGISTRY, _PROVIDER_META, and _LEGACY_PROFILE_KEY_RE were lifted to
    # routes_admin_shared.py (4.1b Increment 0) — they are immutable and now
    # imported at module scope. References below resolve to those.

    def _canonical_profile_id(provider: str, key_type: str) -> str:
        """Return the canonical auth-profile id: ``<provider>:<type>``.

        Single shape for every provider. This matches the wizard-verify
        gauntlet's ``_LEGACY_KEY_RE`` (which flags any underscore-shaped
        anthropic/openai/brave key as legacy) and the rotate endpoint's
        rename-on-write behaviour. Verify, rotate, onboard, add-key, and
        the batch normalizer all agree on one shape.

        Background: OC's model layer looks up LLM credentials by the
        ``<provider>:`` prefix at runtime — underscore-named LLM
        profiles are INVISIBLE there (atlas regression 2026-05-28,
        PR #1752). For non-LLM providers (brave, runway, etc.) the
        underscore name had been preserved because skill code "reads
        the key by name directly". Audit on 2026-06-01 found no
        production code that actually does this — ``community_intel``
        reads from ``config.json`` and atlas reads from its own
        config layer — so the carve-out was a phantom. Verify was
        flagging perfectly working brave keys as legacy with no
        operator-actionable fix; rotating the value re-wrote it under
        the same offending name and the error persisted. Single
        canonical shape closes the loop.
        """
        return f"{provider}:{key_type}"

    # ── Phase 3: affordance → action object mapping ─────────────────────────
    # Probes declare which affordances are *safe* for the storage shape they
    # discovered (decision B); this map turns those affordances into the
    # action-button records the frontend renders. Adding a new affordance
    # type is a one-place change here — the frontend's _renderActions reads
    # whatever objects appear in row.actions[] without provider-specific
    # branches.
    def _action_from_affordance(
        affordance: str, *, bot_id: str, provider: str,
    ) -> dict | None:
        if affordance == Affordance.ROTATE.value:
            return {
                "id": "rotate",
                "label": "Rotate",
                "style": "ghost",
                "modal": "rotate_modal",
                "endpoint": f"/api/admin/keys/{bot_id}/{provider}/rotate",
            }
        if affordance == Affordance.REAUTHORIZE_VIA_WIZARD.value:
            return {
                "id": "reauthorize",
                "label": "Reauthorize",
                "style": "ghost",
                "modal": "google_workspace_wizard",
            }
        if affordance == Affordance.DISCONNECT.value:
            # google_workspace runs through the OAuth wizard's revoke
            # endpoint (calls Google's revoke API + clears local profile).
            # Every other provider clears its auth-profile entry locally —
            # remote revocation is either unavailable (telegram) or out of
            # the dashboard's blast radius (slack/discord admin tools, API
            # key dashboards). The frontend POSTs `{bot_id}` to whichever
            # endpoint we emit here; both shapes return {ok, revoked}.
            if provider == "google_workspace":
                endpoint = "/api/admin/onboard/google/revoke"
            else:
                endpoint = (
                    f"/api/admin/keys/{bot_id}/{provider}/disconnect"
                )
            return {
                "id": "disconnect",
                "label": "Disconnect",
                "style": "danger",
                "endpoint": endpoint,
            }
        if affordance == Affordance.VIEW_CONFIG.value:
            return {
                "id": "view_config",
                "label": "View config",
                "style": "ghost",
                "modal": "config_view",
            }
        if affordance == Affordance.MIGRATE.value:
            return {
                "id": "reauthorize",
                "label": "Reauthorize via wizard",
                "style": "ghost",
                "modal": "google_workspace_wizard",
            }
        # Affordance.NONE / Affordance.EDIT_PLUGIN: no button.
        # EDIT_PLUGIN is reserved for the Phase 4 plugin-config editor
        # (out of scope for Phase 3 — see design doc migration plan).
        return None

    def _actions_for_winner(
        winner, *, bot_id: str, provider: str,
    ) -> list[dict]:
        """Map a winning probe's affordances to action-button records.
        Dedupes affordance ids so e.g. wizard+legacy both declaring
        REAUTHORIZE doesn't render two Reauthorize buttons.
        """
        if winner is None:
            return []
        seen: set[str] = set()
        out: list[dict] = []
        for aff in winner.affordances:
            if aff in seen:
                continue
            seen.add(aff)
            action = _action_from_affordance(
                aff, bot_id=bot_id, provider=provider,
            )
            if action is not None:
                out.append(action)
        return out

    # Per-provider probe registry is built per-request inside the keys
    # API handler so that the v2 feature flag (read from network.json) can
    # be flipped without restarting the admin server. See
    # api_admin_get_keys for the build_probes() call.

    def _key_field(key_type: str) -> str:
        """Return the JSON field name oc_keys.py uses for a given key type.

        oc_keys.py stores token keys in "token" and api_key keys in "key"
        (NOT "api_key") — see find_profile() in packages/analyzer/oc_keys.py.
        """
        return "token" if key_type == "token" else "key"

    def _key_val(pdata: dict) -> str:
        """Extract key value from a profile dict regardless of field name."""
        return (pdata.get("key") or pdata.get("token") or
                pdata.get("api_key") or pdata.get("value") or "")

    # _PLACEHOLDER_RE lifted to routes_admin_shared.py (4.1b Increment 0).

    def _placeholder_reason(value: str) -> str | None:
        # KR4 (2026-05-04): a verification-catalog probe POSTed
        # `BSAxxxKR4TEST<epoch>xxxx…` to /rotate; the rollback half died
        # under `set -o pipefail` and 4 bots ran on the placeholder until
        # 2026-05-18. Refuse obvious fixtures at the boundary.
        m = _PLACEHOLDER_RE.search(value)
        return m.group(0) if m else None

    @app.get("/api/admin/debug/paths/<bot_id>")
    def api_admin_debug_paths(bot_id: str) -> Response:
        """Debug: show resolved paths and file access status for a bot."""
        user = _resolve_user(bot_id)
        paths = resolve_bot_paths(bot_id, user=user)
        result = {"bot_id": bot_id, "system_user": user, "paths": {}}
        for k, v in paths.items():
            if k == "user":
                continue
            if isinstance(v, list):
                # turns_dir_candidates — show each entry
                result["paths"][k] = [
                    {"path": p, "exists": Path(p).exists(), "readable": _sudo_read(bot_id, p) is not None}
                    for p in v
                ]
            else:
                can_read = _sudo_read(bot_id, v) is not None
                result["paths"][k] = {"path": v, "readable": can_read}
        # Also check candidate paths
        candidates = _find_auth_profile_paths(bot_id)
        result["auth_candidates"] = []
        for p in candidates:
            text = _sudo_read(bot_id, p)
            result["auth_candidates"].append({"path": p, "readable": text is not None, "len": len(text) if text else 0})
        return jsonify(result)

    @app.get("/api/admin/keys/<bot_id>")
    def api_admin_get_keys(bot_id: str) -> Response:
        """Return per-provider key rows with credential type, category, and discovery."""
        auth_data = _read_auth_profiles(bot_id)
        source_path = auth_data.get("_source_path", "")
        profiles: dict = auth_data.get("profiles", {})
        oc_cfg = _read_oc_json(bot_id)
        auth_order: dict = oc_cfg.get("auth", {}).get("order", {})

        # Index profiles by provider for quick lookup (all entries for a provider)
        by_provider: dict[str, list[dict]] = {}
        for pname, pdata in profiles.items():
            prov = pdata.get("provider", pname).lower()
            by_provider.setdefault(prov, []).append({
                "profile_id": pname,
                "key_type": pdata.get("type", "api_key"),
                "value": _key_val(pdata),
                "raw": pdata,
            })

        # Also index by (provider, key_type) for api_key style lookups
        by_key: dict[tuple, dict] = {}
        for pname, pdata in profiles.items():
            prov = pdata.get("provider", pname).lower()
            ptype = pdata.get("type", "api_key")
            val = _key_val(pdata)
            if (prov, ptype) not in by_key or val:
                by_key[(prov, ptype)] = {
                    "profile_id": pname,
                    "value": val,
                    "has_prev": bool(pdata.get("_evolve_prev_key")),
                }

        # Build the per-request ProbeContext (closure-scoped helpers wrapped
        # in a ProbeHelpers callback bag so probes/__init__.py stays
        # decoupled from server.py internals).
        net_for_request = load_network(network_path)
        v2_on = _v2_probes_enabled(net_for_request)
        probe_helpers = ProbeHelpers(
            discover_github_remote=_discover_github_remote,
            detect_legacy_gws=_module._detect_legacy_gws,
            detect_dropbox_desktop=_module._detect_dropbox_desktop,
            read_google_oauth_client=_read_google_oauth_client,
            ensure_fresh_google_access_token=_ensure_fresh_google_access_token,
            scopes_to_services=_scopes_to_services,
            google_oauth_profile_id=_google_oauth_profile_id,
            mask_key=_mask_key,
            list_workspace_credentials=_module._list_workspace_credentials,
            detect_workspace_dotenv_keys=_module._detect_workspace_dotenv_keys,
            list_workspace_manifest_files=_module._list_workspace_manifest_files,
            list_user_ssh_private_keys=_module._list_user_ssh_private_keys,
            read_gh_cli_hosts=_module._read_gh_cli_hosts,
        )
        _PROBES = build_probes(_PROVIDER_META, v2_enabled=v2_on)
        probe_ctx = ProbeContext(
            bot_id=bot_id,
            profiles=profiles,
            oc_cfg=oc_cfg,
            network=net_for_request,
            by_provider=by_provider,
            by_key=by_key,
            auth_order=auth_order,
            helpers=probe_helpers,
        )

        # Q5: per-provider warning accumulator. Probes that ERROR (storage
        # location appeared to exist but couldn't be read) attach to the
        # row's `warnings` list — distinct from NO_EVIDENCE silence and
        # from MATCH success. Keyed by provider so the row renderer can
        # look them up without re-running probes.
        _provider_warnings: dict[str, list[dict]] = {}

        def _record_warning(provider: str, probe_name: str, reason: str) -> None:
            entry = {
                "probe_name": probe_name,
                "reason": reason,
            }
            hint = _remediation_hint_for(reason)
            if hint is not None:
                entry["remediation_hint"] = hint
            _provider_warnings.setdefault(provider, []).append(entry)

        def _run_matching(probes: list, predicate=None, provider: str | None = None) -> list:
            """Run each probe, collect MATCH results (filtered by `predicate`).

            ERROR outcomes accumulate into `_provider_warnings[provider]`
            (when `provider` is given) so the row renderer can attach the
            `warnings` field. NO_EVIDENCE stays silent.
            """
            matches = []
            for probe in probes:
                if predicate is not None and not predicate(probe):
                    continue
                outcome, result = probe.probe(probe_ctx)
                if outcome == ProbeOutcome.MATCH and result is not None:
                    matches.append(result)
                elif outcome == ProbeOutcome.ERROR and provider is not None:
                    reason = result if isinstance(result, str) and result else "probe error (no reason given)"
                    _record_warning(provider, getattr(probe, "name", probe.__class__.__name__), reason)
            return matches

        seen_providers: set = set()
        result_keys: list = []

        # ── Seeded registry: all known providers in category order ────────
        for prov, ptype, type_label, cred_class, category in _KEY_REGISTRY:
            seen_providers.add(prov)
            meta = _PROVIDER_META.get(prov, {})
            base = {
                "provider": prov,
                "display": meta.get("display", prov.replace("_", " ").title()),
                "type": ptype,
                "type_label": type_label,
                "credential_class": cred_class,
                "category": category,
                "order": None,
                "order_total": None,
            }
            probes_for_prov = _PROBES.get(prov, [])

            if cred_class == "oauth":
                # Google Workspace gets the rich OAuth treatment via the
                # WizardOAuthProbe + LegacyOcGwsCliProbe pair; wizard wins
                # when both match. Other OAuth providers (e.g. Dropbox)
                # have no probes registered yet and stay on the legacy
                # has-token-or-not path until Phase 2 introduces their
                # proper probes (see web/probes/__init__.py for the rationale).
                if prov == "google_workspace":
                    matches = _run_matching(probes_for_prov, provider=prov)
                    wizard_match = next(
                        (m for m in matches if m.flavor == "wizard"), None
                    )
                    legacy_match = next(
                        (m for m in matches if m.flavor == "legacy CLI"), None
                    )
                    plugin_match = next(
                        (m for m in matches
                         if m.flavor == "plugin-managed (workspace credentials)"),
                        None,
                    )
                    # Priority: wizard > legacy CLI > plugin-managed. The
                    # plugin-managed flavor only appears when v2 is on (the
                    # Phase 2 probe is gated behind the feature flag).
                    winner = wizard_match or legacy_match or plugin_match
                    # Per-bot OAuth client; reader falls back to legacy
                    # pod-level when this bot has no per-bot block yet.
                    client_cfg = _read_google_oauth_client(bot_id)
                    if winner is not None and winner is plugin_match:
                        # Plugin-managed (e.g. Team_bot_c's ranch plugin). The
                        # affordance-routing layer (Phase 3) drives the
                        # action buttons from `winner.affordances`; for
                        # the plugin-managed flavor that's VIEW_CONFIG
                        # only — Reauthorize/Disconnect would write
                        # tokens the running plugin doesn't read
                        # (decision B).
                        ext = plugin_match.extras
                        row = {
                            **base,
                            "profile_id": _canonical_profile_id(prov, ptype),
                            "masked": None,
                            "status": "active",
                            "oauth_info": meta.get("oauth_info", ""),
                            "google_account": plugin_match.account or "",
                            "granted_services": [],
                            "scopes": [],
                            "access_token_expires_at": None,
                            "client_configured": bool(client_cfg),
                            "flavor": "plugin-managed",
                            "auth_model": plugin_match.auth_model,
                            "oc_only": True,
                            "storage_locations": list(plugin_match.storage_locations),
                            "manifest_present": ext.get("manifest_present", False),
                            "manifest_files": list(ext.get("manifest_files") or []),
                            "plugin_credential_summary": {
                                "token_caches": ext.get("token_cache_count", 0),
                                "service_accounts": ext.get("service_account_count", 0),
                                "client_secrets": ext.get("client_secret_count", 0),
                            },
                            "actions": _actions_for_winner(
                                plugin_match, bot_id=bot_id, provider=prov,
                            ),
                        }
                    elif winner is not None:
                        ext = winner.extras
                        row = {
                            **base,
                            "profile_id": _canonical_profile_id(prov, ptype),
                            "masked": None,
                            "status": ext["row_status"],
                            "oauth_info": meta.get("oauth_info", ""),
                            "google_account": ext["google_account"],
                            "granted_services": ext["granted_services"],
                            "scopes": ext["scopes"],
                            "access_token_expires_at": ext["access_token_expires_at"],
                            "client_configured": bool(client_cfg),
                            "actions": _actions_for_winner(
                                winner, bot_id=bot_id, provider=prov,
                            ),
                        }
                        if ext.get("oc_only"):
                            row["oc_only"] = True
                            row["legacy_token_age_days"] = ext.get("legacy_token_age_days")
                        # Manifest evidence chip (Phase 2): when a wizard or
                        # legacy match wins but the bot also has manifest
                        # files, surface them so the operator can see the
                        # bot's runtime expects this integration. Cheap
                        # extra signal; informational only.
                        if v2_on and plugin_match is None:
                            try:
                                manifests = _module._list_workspace_manifest_files(bot_id) or []
                            except Exception:
                                manifests = []
                            google_manifests = manifests_matching_provider(
                                "google_workspace", manifests,
                            )
                            if google_manifests:
                                row["manifest_present"] = True
                                row["manifest_files"] = google_manifests
                    else:
                        # Missing Google Workspace: synthesize a Set up
                        # action so the action array remains the single
                        # source of truth for buttons. Without a probe
                        # match there are no affordances to route from,
                        # but the wizard is the one path operators have.
                        row = {
                            **base,
                            "profile_id": _canonical_profile_id(prov, ptype),
                            "masked": None,
                            "status": "missing",
                            "oauth_info": meta.get("oauth_info", ""),
                            "google_account": "",
                            "granted_services": [],
                            "scopes": [],
                            "access_token_expires_at": None,
                            "client_configured": bool(client_cfg),
                            "actions": [{
                                "id": "setup",
                                "label": "Set up",
                                "style": "warn",
                                "modal": "google_workspace_wizard",
                            }],
                        }
                    result_keys.append(row)
                else:
                    prov_entries = by_provider.get(prov, [])
                    has_token = any(e["value"] for e in prov_entries)
                    row = {
                        **base,
                        "profile_id": _canonical_profile_id(prov, ptype),
                        "masked": None,
                        "status": "active" if has_token else "missing",
                        "oauth_info": meta.get("oauth_info", ""),
                    }
                    # Dropbox in this pod is the macOS desktop sync app, not
                    # an OAuth/API integration — the Dropbox app on the bot's
                    # user account writes ~/.dropbox/info.json. Phase 1 keeps
                    # this inline; Phase 2 splits it into AuthProfilesProbe +
                    # LegacyDropboxDesktopProbe (per design doc, "macOS Dropbox
                    # app presence is NOT a probe" — but its info.json IS the
                    # active-row signal we're keeping).
                    if prov == "dropbox" and not has_token:
                        ddx = _module._detect_dropbox_desktop(bot_id)
                        if ddx.get("present"):
                            row["status"] = "active"
                            row["oc_only"] = True
                            row["dropbox_sync_path"] = ddx.get("sync_path")
                            row["dropbox_subscription_type"] = ddx.get("subscription_type")
                            row["dropbox_is_team"] = ddx.get("is_team")
                            row["dropbox_account_kind"] = ddx.get("account_kind")
                    result_keys.append(row)

            elif cred_class == "token_pair":
                pair_info = meta.get("pair_info", "")
                matches = _run_matching(
                    probes_for_prov,
                    predicate=lambda p: isinstance(p, AuthProfilesTokenPairProbe),
                    provider=prov,
                )
                # Phase 2 (v2 on): also collect DotenvProbe match for this
                # provider. auth-profiles wins when both exist; dotenv is
                # used as evidence (chip) plus as the primary status driver
                # when nothing else matches (team_bot_a-style: token only in .env).
                dotenv_match = None
                # Phase 2.5 (v2 on): collect OpenclawChannelsTokenProbe match.
                # Sits between auth-profiles (canonical) and dotenv in the
                # winner cascade — covers the live-pod case where 4 of 5
                # telegram-using bots store the bot_token only in
                # openclaw.json. Without this slot the Rotate button never
                # appears for those bots.
                oc_channels_match = None
                if v2_on:
                    dotenv_matches = _run_matching(
                        probes_for_prov,
                        predicate=lambda p: isinstance(p, DotenvProbe),
                        provider=prov,
                    )
                    dotenv_match = dotenv_matches[0] if dotenv_matches else None
                    oc_channels_matches = _run_matching(
                        probes_for_prov,
                        predicate=lambda p: isinstance(p, OpenclawChannelsTokenProbe),
                        provider=prov,
                    )
                    oc_channels_match = (
                        oc_channels_matches[0] if oc_channels_matches else None
                    )

                row_extras: dict = {}
                if matches:
                    winner = matches[0]
                    resolved_fields = winner.extras["fields"]
                    status = "active"
                    row_extras["storage"] = "auth_profiles"
                    row_extras["actions"] = _actions_for_winner(
                        winner, bot_id=bot_id, provider=prov,
                    )
                    # Attach openclaw_channels + dotenv as evidence chips on
                    # the wizard-driven row — operator sees every source in
                    # one place. Rotation always targets the auth-profiles
                    # winner, never the chips.
                    if oc_channels_match is not None:
                        row_extras["openclaw_channels_present"] = True
                    if dotenv_match is not None:
                        row_extras["dotenv_present"] = True
                        row_extras["dotenv_env_vars"] = list(
                            dotenv_match.extras.get("matched_env_vars") or []
                        )
                elif oc_channels_match is not None:
                    # Phase 2.5: openclaw.json carries the only copy of the
                    # token. Render row as active with the openclaw_channels
                    # storage hint so the rotate modal posts back the right
                    # storage parameter — writing to auth-profiles instead
                    # would silently leave the runtime reading the stale
                    # openclaw.json value (decision B violation).
                    resolved_fields = oc_channels_match.extras["fields"]
                    status = "active"
                    row_extras.update({
                        "flavor": "openclaw_channels",
                        "auth_model": "token_pair",
                        "oc_only": True,
                        "storage": "openclaw_channels",
                        "storage_locations": list(
                            oc_channels_match.storage_locations
                        ),
                        "actions": _actions_for_winner(
                            oc_channels_match, bot_id=bot_id, provider=prov,
                        ),
                    })
                    if dotenv_match is not None:
                        row_extras["dotenv_present"] = True
                        row_extras["dotenv_env_vars"] = list(
                            dotenv_match.extras.get("matched_env_vars") or []
                        )
                elif dotenv_match is not None:
                    # Team_bot_a case: no auth-profiles, no openclaw.json#channels,
                    # tokens live in `~/.openclaw/workspace/.env`. The
                    # DotenvProbe declares ROTATE + VIEW_CONFIG (Phase 2.5
                    # follow-up — extends rotation to this storage shape).
                    # Per-field `rotatable` is True only for fields whose
                    # canonical env var was actually matched; the rotate
                    # endpoint refuses to invent new lines (it only
                    # rewrites existing assignments).
                    fields_meta = meta.get("fields", [])
                    matched_vars = list(
                        dotenv_match.extras.get("matched_env_vars") or []
                    )
                    field_to_env = {
                        fm["key"]: envvar_for_provider_field(prov, fm["key"])
                        for fm in fields_meta
                    }
                    matched_field_keys = {
                        fk for fk, var in field_to_env.items()
                        if var and var in matched_vars
                    }
                    resolved_fields = []
                    for fm in fields_meta:
                        env_var = field_to_env.get(fm["key"])
                        is_active = fm["key"] in matched_field_keys
                        entry: dict = {
                            "key": fm["key"],
                            "label": fm["label"],
                            "secret": fm.get("secret", True),
                            "rotatable": is_active,
                            "masked": None,
                            "value": None,
                            "status": "active" if is_active else "missing",
                            "has_prev": False,
                        }
                        if env_var:
                            # Names the .env line the rotate endpoint will
                            # rewrite. Carried per-field so the modal can
                            # show the operator exactly which line on disk
                            # is being touched (decision B transparency).
                            entry["dotenv_var"] = env_var
                        resolved_fields.append(entry)
                    status = "active"
                    row_extras.update({
                        "flavor": "dotenv",
                        "auth_model": "env_var",
                        "oc_only": True,
                        "dotenv_present": True,
                        "dotenv_env_vars": matched_vars,
                        "storage": "dotenv",
                        "storage_locations": list(dotenv_match.storage_locations),
                        "actions": _actions_for_winner(
                            dotenv_match, bot_id=bot_id, provider=prov,
                        ),
                    })
                else:
                    # No match — render the same "all fields missing" shape
                    # the legacy code produced. The fields_meta list still
                    # determines the rendered fields; values are blank.
                    fields_meta = meta.get("fields", [])
                    resolved_fields = [
                        {
                            "key": fm["key"],
                            "label": fm["label"],
                            "secret": fm.get("secret", True),
                            "rotatable": False,
                            "masked": None,
                            "value": None,
                            "status": "missing",
                            "has_prev": False,
                        }
                        for fm in fields_meta
                    ]
                    status = "missing"
                result_keys.append({
                    **base,
                    "profile_id": _canonical_profile_id(prov, ptype),
                    "masked": None,
                    "status": status,
                    "pair_info": pair_info,
                    "fields": resolved_fields,
                    **row_extras,
                })

            else:
                # api_key / identifier: single value. Run only the probe
                # whose key_type matches this registry entry — anthropic
                # has TWO probes (token + api_key); we want the one that
                # corresponds to the row we're rendering.
                matches = _run_matching(
                    probes_for_prov,
                    predicate=lambda p: (
                        isinstance(p, WizardAuthProfilesProbe)
                        and p.key_type == ptype
                    ),
                    provider=prov,
                )
                winner = matches[0] if matches else None

                # Brave special-case: detect intentional opt-out via
                # tools.web.search.provider != null/"brave" (per v3 design),
                # AND surface keys that live in openclaw.json but not in
                # auth-profiles (drift from CLI / hand-edits). Kept inline
                # because both signals come from openclaw.json — Phase 2
                # may move this into a BraveOcConfigProbe.
                opted_out_reason = None
                oc_brave_key = None
                if prov == "brave":
                    # Honour canonical + legacy openclaw.json key locations
                    # (see brave_key_from_oc_config) so a key set by either
                    # path reads ACTIVE, not a mismatched "Setup required".
                    oc_brave_key = brave_key_from_oc_config(oc_cfg)
                    current_provider = (
                        oc_cfg.get("tools", {}).get("web", {}).get("search", {}) or {}
                    ).get("provider")
                    if current_provider not in (None, "", "brave"):
                        opted_out_reason = f"tools.web.search.provider = '{current_provider}'"

                if winner is not None:
                    ext = winner.extras
                    row = {
                        **base,
                        "profile_id": ext["profile_id"],
                        "masked": ext["masked"],
                        "status": "active",
                        "order": ext["order"],
                        "order_total": ext["order_total"],
                        "has_prev": ext["has_prev"],
                    }
                    if prov == "brave" and oc_brave_key and oc_brave_key != ext["value"]:
                        row["oc_drift"] = True
                    if opted_out_reason:
                        row["status"] = "opted_out"
                        row["opted_out_reason"] = opted_out_reason
                    result_keys.append(row)
                elif prov == "brave" and oc_brave_key:
                    # auth-profiles missing but openclaw.json carries the key —
                    # bot was configured outside the wizard. Treat as active so
                    # the onboarding banner / per-bot Set-up button skip it.
                    row = {
                        **base,
                        "profile_id": _canonical_profile_id(prov, ptype),
                        "masked": _mask_key(oc_brave_key),
                        "status": "active",
                        "oc_only": True,
                        "has_prev": False,
                    }
                    if opted_out_reason:
                        row["status"] = "opted_out"
                        row["opted_out_reason"] = opted_out_reason
                    result_keys.append(row)
                else:
                    row = {
                        **base,
                        "profile_id": _canonical_profile_id(prov, ptype),
                        "masked": None,
                        "status": "missing",
                        "has_prev": False,
                    }
                    if opted_out_reason:
                        row["status"] = "opted_out"
                        row["opted_out_reason"] = opted_out_reason
                    result_keys.append(row)

        # ── Discovery: extra profiles not in seeded list ──────────────────
        # Only surface entries that look like actual API keys — skip plugin/internal types.
        _SKIP_TYPES = {"plugin", "internal", "webhook", "discovered"}
        for pname, pdata in profiles.items():
            prov = pdata.get("provider", pname).lower()
            if prov in seen_providers:
                continue
            ptype = pdata.get("type", "api_key")
            if ptype in _SKIP_TYPES:
                seen_providers.add(prov)
                continue
            val = _key_val(pdata)
            if not val:
                # Don't surface unknown providers with no value — noise only
                seen_providers.add(prov)
                continue
            seen_providers.add(prov)
            result_keys.append({
                "provider": prov,
                "display": prov.replace("_", " ").title(),
                "type": ptype,
                "type_label": "API key",
                "credential_class": "api_key",
                "category": "custom",
                "profile_id": pname,
                "masked": _mask_key(val),
                "status": "active",
                "order": None,
                "order_total": None,
                "has_prev": bool(profiles.get(pname, {}).get("_evolve_prev_key")),
            })
        # Note: openclaw.json plugins are channels, not API keys — not surfaced here.

        # ── Integration tokens (GitHub from .git/config) ────────────────────
        # openclaw rejects "integrations" as an unknown top-level key, so we
        # read the GitHub remote directly from the workspace .git/config.
        # Three auth paths are all treated as "Active":
        #   - https_pat:        HTTPS URL with embedded PAT (legacy / wizard-onboarded)
        #   - https_credhelper: plain HTTPS, credentials in osxkeychain / helper
        #   - ssh:              git@github.com:owner/repo + deploy key at
        #                       /Users/evolve/.ssh/evolve-backup-<bot> (analyzer/backup.py)
        _gh_probe = IntegrationTokenProbe()
        _gh_outcome, _gh_result = _gh_probe.probe(probe_ctx)
        if _gh_outcome == ProbeOutcome.ERROR:
            _record_warning(
                "github", _gh_probe.name,
                _gh_result if isinstance(_gh_result, str) and _gh_result else "github probe error",
            )

        # Phase 2: collect ssh-key / gh-CLI evidence (v2 only). These attach
        # as evidence chips on the github row; they never override the
        # integration_token row's primary status. SSH and gh CLI are
        # managed out of band, so their affordance is NONE — the chips
        # are informational signals to the operator that other auth paths
        # exist on this bot beyond .git/config.
        gh_evidence_chips: list[dict] = []
        if v2_on:
            _ssh_probe = SshKeyProbe()
            ssh_outcome, ssh_result = _ssh_probe.probe(probe_ctx)
            if ssh_outcome == ProbeOutcome.MATCH and ssh_result is not None and not isinstance(ssh_result, str):
                gh_evidence_chips.append({
                    "kind": "ssh_key",
                    "label": "SSH keys",
                    "auth_model": ssh_result.auth_model,
                    "key_names": list(ssh_result.extras.get("key_names") or []),
                })
            elif ssh_outcome == ProbeOutcome.ERROR:
                _record_warning(
                    "github", _ssh_probe.name,
                    ssh_result if isinstance(ssh_result, str) and ssh_result else "ssh_key probe error",
                )
            _ghcli_probe = GhCliProbe()
            ghcli_outcome, ghcli_result = _ghcli_probe.probe(probe_ctx)
            if ghcli_outcome == ProbeOutcome.MATCH and ghcli_result is not None and not isinstance(ghcli_result, str):
                gh_evidence_chips.append({
                    "kind": "gh_cli",
                    "label": "gh CLI",
                    "auth_model": ghcli_result.auth_model,
                    "hosts": list(ghcli_result.extras.get("hosts") or []),
                })
            elif ghcli_outcome == ProbeOutcome.ERROR:
                _record_warning(
                    "github", _ghcli_probe.name,
                    ghcli_result if isinstance(ghcli_result, str) and ghcli_result else "gh_cli probe error",
                )

        if _gh_outcome == ProbeOutcome.MATCH and "github" not in seen_providers:
            seen_providers.add("github")
            ext = _gh_result.extras
            row = {
                "provider": "github",
                "display": "GitHub",
                "type": "api_key",
                "type_label": ext["type_label"],
                "credential_class": "integration_token",
                "category": "custom",
                "profile_id": "github_token",
                "masked": ext["masked"],
                "status": "active",
                "order": None,
                "order_total": None,
                "has_prev": False,
                # repo_slug exposes "owner/repo" so the frontend can compute
                # isOverride by comparing owner against pod_default_github_account.
                "repo_slug": ext["repo_slug"],
                "auth_type": ext["auth_type"],
            }
            if gh_evidence_chips:
                row["evidence_chips"] = gh_evidence_chips
            result_keys.append(row)
        elif "github" not in seen_providers:
            # Surface a missing github row for guided onboarding (per v3 design —
            # pod-invariant providers are always shown, not just hidden behind +Add).
            seen_providers.add("github")
            row = {
                "provider": "github",
                "display": "GitHub",
                "type": "api_key",
                "type_label": "Personal access token",
                "credential_class": "integration_token",
                "category": "custom",
                "profile_id": "github_token",
                "masked": None,
                "status": "missing",
                "order": None,
                "order_total": None,
                "has_prev": False,
                "repo_slug": None,
            }
            if gh_evidence_chips:
                row["evidence_chips"] = gh_evidence_chips
            result_keys.append(row)

        # ── Discord: detect from openclaw.json channels.discord ────────────
        # Like github, the credential lives outside auth-profiles.json, so we
        # discover it directly from the runtime config. Three states:
        #   - active single-account:  channels.discord.token is set
        #   - active multi-account:   channels.discord.accounts.<id>.token set
        #                             (rotate disabled — needs manual edit)
        #   - missing:                channels.discord absent or token-less
        seen_providers.add("discord")
        _discord_state = _discover_discord_account(bot_id)
        if _discord_state is None:
            result_keys.append({
                "provider": "discord",
                "display": "Discord",
                "type": "api_key",
                "type_label": "Bot token",
                "credential_class": "integration_token",
                "category": "messaging",
                "profile_id": "discord_token",
                "masked": None,
                "status": "missing",
                "order": None,
                "order_total": None,
                "has_prev": False,
                "discord_shape": None,
                "setup_endpoint": f"/api/admin/integration-token/{bot_id}/discord/rotate",
                "setup_help": (
                    "Create a Discord application at "
                    "discord.com/developers/applications, then copy the bot "
                    "token from the Bot section."
                ),
            })
        elif _discord_state["shape"] == "multi_account":
            # Show the row but disable Rotate — we won't silently rewrite only
            # the default account when the operator may have meant a sub-account.
            default_token = _discord_state.get("default_token")
            masked = _mask_key(default_token) if default_token else "(no default account token)"
            result_keys.append({
                "provider": "discord",
                "display": "Discord",
                "type": "api_key",
                "type_label": f"Bot token ({len(_discord_state['accounts'])} accounts)",
                "credential_class": "integration_token",
                "category": "messaging",
                "profile_id": "discord_token",
                "masked": masked,
                "status": "active",
                "order": None,
                "order_total": None,
                "has_prev": False,
                "discord_shape": "multi_account",
                "discord_accounts": _discord_state["accounts"],
                "discord_enabled": _discord_state["enabled"],
            })
        else:
            result_keys.append({
                "provider": "discord",
                "display": "Discord",
                "type": "api_key",
                "type_label": "Bot token",
                "credential_class": "integration_token",
                "category": "messaging",
                "profile_id": "discord_token",
                "masked": _mask_key(_discord_state["token"]),
                "status": "active",
                "order": None,
                "order_total": None,
                "has_prev": False,
                "discord_shape": "single",
                "discord_enabled": _discord_state["enabled"],
                "discord_guild_count": _discord_state["guild_count"],
            })

        # ── WhatsApp: detect from openclaw.json channels.whatsapp ──────────
        # WhatsApp uses Baileys session-based auth (a directory of paired
        # session files written on the bot host during a CLI QR-pairing run).
        # There is NO rotatable token — so this row never offers Rotate. The
        # actions are: enable/disable the channel flag, plus an info button
        # describing the CLI pairing flow.
        seen_providers.add("whatsapp")
        _whatsapp_state = _discover_whatsapp_account(bot_id)
        if _whatsapp_state is None:
            result_keys.append({
                "provider": "whatsapp",
                "display": "WhatsApp",
                "type": "api_key",
                "type_label": "Baileys QR pairing",
                "credential_class": "integration_token",
                "category": "messaging",
                "profile_id": "whatsapp",
                "masked": None,
                "status": "missing",
                "order": None,
                "order_total": None,
                "has_prev": False,
                "whatsapp_shape": None,
                "setup_endpoint": f"/api/admin/integration-token/{bot_id}/whatsapp/setup",
                "setup_help": (
                    "WhatsApp uses Baileys session-based auth. Setup writes "
                    "the channel scaffold to openclaw.json; pairing itself "
                    "happens on the bot host via the openclaw CLI."
                ),
            })
        else:
            status = "active" if _whatsapp_state["enabled"] else "configured_disabled"
            result_keys.append({
                "provider": "whatsapp",
                "display": "WhatsApp",
                "type": "api_key",
                "type_label": "Baileys QR pairing",
                "credential_class": "integration_token",
                "category": "messaging",
                "profile_id": "whatsapp",
                "masked": "(paired via CLI)" if _whatsapp_state["enabled"] else "(disabled)",
                "status": status,
                "order": None,
                "order_total": None,
                "has_prev": False,
                "whatsapp_shape": _whatsapp_state["shape"],
                "whatsapp_enabled": _whatsapp_state["enabled"],
                "whatsapp_auth_dir": _whatsapp_state["auth_dir"],
            })

        # ── Pod-level fields for guided onboarding ──────────────────────────
        # podInvariantIntegrations: which integrations every bot should have
        # (default = DEFAULT_POD_INVARIANT_INTEGRATIONS, i.e. ["github"] since
        # brave was demoted; overridable via network.json). load_network()
        # already runs the in-memory brave-demotion migration, so a list read
        # here reflects the post-migration value.
        # pod_default_github_account / _source: discovered via the cascade so
        # the frontend can compute per-bot account-override badges.
        net_for_keys = load_network(network_path)
        pod_invariants = net_for_keys.get("podInvariantIntegrations")
        if not isinstance(pod_invariants, list):
            pod_invariants = list(DEFAULT_POD_INVARIANT_INTEGRATIONS)
        try:
            pod_default = _discover_pat_cascade()
        except Exception:
            pod_default = None
        pod_default_github_account = pod_default[1] if pod_default else None
        pod_default_github_account_source = (
            f"discovered_from_{pod_default[2]}" if pod_default else None
        )

        # Pod-level Google OAuth client config (lets the wizard skip the
        # first-run GCP setup screen on bots after the first).
        try:
            _g_client = _read_google_oauth_client()
        except Exception:
            _g_client = None

        # Q2: manifest-without-credentials warnings. Cross-probe assertion
        # (no probe owns it) — if a row is "missing" but the bot's
        # workspace declares a manifest matching the provider's catalog
        # (e.g. `google_integration.json`, `gmail_fetcher.json`), the
        # bot's runtime expects this integration to work. Surface as a
        # yellow warning so operators can distinguish "intent declared,
        # no creds" from "bot doesn't want this integration."
        #
        # Gated on v2 to match the rest of the discovery work; logging
        # happens whether or not the warning is rendered, so operators
        # can grep the admin-ui log even on instances that haven't
        # flipped the flag yet.
        if MANIFEST_CATALOG:
            try:
                _manifest_files = _module._list_workspace_manifest_files(bot_id) or []
            except Exception:
                _manifest_files = []
            if _manifest_files:
                _emitted_for: set[str] = set()
                for row in result_keys:
                    provider = row.get("provider") or ""
                    if provider in _emitted_for:
                        continue
                    if provider not in MANIFEST_CATALOG:
                        continue
                    if row.get("status") != "missing":
                        continue
                    matched = manifests_matching_provider(
                        provider, _manifest_files,
                    )
                    if not matched:
                        continue
                    _emitted_for.add(provider)
                    display = row.get("display") or provider.replace("_", " ").title()
                    manifests_str = ", ".join(matched)
                    warning_entry = {
                        "kind": "manifest_without_credentials",
                        "probe_name": "manifest_catalog",
                        "reason": (
                            f"Bot has {manifests_str} but no {display} "
                            "credentials are configured."
                        ),
                        "manifests": list(matched),
                        "remediation_hint": (
                            f"Authorize {display} via the dashboard, "
                            "or remove the manifest if the bot no longer "
                            "needs this integration."
                        ),
                    }
                    _provider_warnings.setdefault(provider, []).append(
                        warning_entry,
                    )
                    _log.info(
                        "manifest-without-credentials: bot=%s provider=%s "
                        "manifests=%s",
                        bot_id, provider, ",".join(matched),
                    )

        # Q5: attach probe warnings, then apply the visibility rule (hide
        # optional, never-attempted providers). Both live in
        # credentials_visibility — the single source of truth — so this frozen
        # hot file doesn't grow (file-size ratchet).
        finalize_credential_rows(result_keys, pod_invariants, _provider_warnings)

        return jsonify({
            "keys": result_keys,
            "source_path": source_path,
            "pod_invariants": list(pod_invariants),
            "pod_default_github_account": pod_default_github_account,
            "pod_default_github_account_source": pod_default_github_account_source,
            "google_oauth_client_configured": bool(_g_client),
        })

    @app.get("/api/admin/keys/<bot_id>/<provider>")
    def api_admin_get_keys_one_provider(bot_id: str, provider: str) -> Response:
        """Single-provider variant of /api/admin/keys/<bot> — returns the row
        directly (not wrapped in {keys: [...]}). Catalog probes (KR1, KR2,
        KR7, KR9) call this URL to inspect per-field metadata.
        """
        all_resp = api_admin_get_keys(bot_id)
        # Flask's view return: a Response from jsonify
        try:
            data = all_resp.get_json()
        except Exception:
            return jsonify({"error": "could not parse keys response"}), 500
        for row in (data or {}).get("keys", []):
            if row.get("provider") == provider:
                return jsonify(row)
        return jsonify({"error": f"provider {provider} not found", "provider": provider}), 404

    # Per-provider config-fragment paths the View-config affordance reads.
    # Maps provider → openclaw.json path (dotted) the operator should look at.
    # _VIEW_CONFIG_PATHS and _VIEW_CONFIG_SECRET_FIELDS lifted to
    # routes_admin_shared.py (4.1b Increment 0).

    def _mask_config_secrets(node):
        """Recursively walk a JSON fragment and replace known-secret leaf
        values with "***". Read-only — doesn't mutate the caller's dict.
        """
        if isinstance(node, dict):
            return {
                k: ("***" if (k in _VIEW_CONFIG_SECRET_FIELDS and v) else _mask_config_secrets(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [_mask_config_secrets(v) for v in node]
        return node

    @app.get("/api/admin/keys/<bot_id>/<provider>/config")
    def api_admin_get_keys_config(bot_id: str, provider: str) -> Response:
        """Read-only view of the openclaw.json fragment that drives a
        plugin-managed integration (the VIEW_CONFIG affordance from the
        Phase 3 design). Secrets are masked server-side so the JSON the
        frontend receives never contains live credentials.
        """
        path = _VIEW_CONFIG_PATHS.get(provider)
        if path is None:
            return jsonify({
                "error": f"View config not supported for provider '{provider}'",
                "provider": provider,
            }), 404
        try:
            oc_cfg = _read_oc_json(bot_id) or {}
        except Exception as exc:
            return jsonify({"error": f"could not read openclaw.json: {exc}"}), 500
        node: object = oc_cfg
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
        masked_fragment = _mask_config_secrets(node) if node is not None else None
        return jsonify({
            "bot_id": bot_id,
            "provider": provider,
            "path": path,
            "json_fragment": masked_fragment,
            "masked_fields": list(_VIEW_CONFIG_SECRET_FIELDS),
            "openclaw_json_path": f"/Users/{_resolve_user(bot_id)}/.openclaw/openclaw.json",
        })

    @app.get("/api/admin/integration-token/<bot_id>/github/check")
    def api_admin_github_integration_check(bot_id: str) -> Response:
        """Lightweight presence check for GitHub auth — KR3 hits this to confirm
        the integration-token routes are wired (regression guard). Recognizes
        HTTPS-PAT, plain-HTTPS-with-credhelper, and SSH-deploy-key configurations.
        Returns 200 with `{"ok": true, "configured": <bool>, "auth_type": ...}`
        or 404 if no github remote is discoverable.
        """
        info = _discover_github_remote(bot_id)
        if not info:
            return jsonify({"ok": False, "configured": False, "bot": bot_id}), 404
        auth_type = info["auth_type"]
        if auth_type == "https_pat":
            masked = _mask_key(info["token"])
        elif auth_type == "https_credhelper":
            masked = "https (credential helper)"
        else:
            masked = f"ssh:evolve-backup-{bot_id}"
        return jsonify({
            "ok": True,
            "configured": True,
            "bot": bot_id,
            "auth_type": auth_type,
            "masked": masked,
            "repo": info["repo_slug"],
        })

    @app.post("/api/admin/keys/<bot_id>/<provider>")
    def api_admin_add_key(bot_id: str, provider: str) -> Response:
        """Write an API key directly to auth-profiles.json."""
        body = request.get_json() or {}
        key_value = (body.get("key_value") or "").strip()
        key_type = body.get("key_type", "api_key")
        if not key_value:
            return jsonify({"error": "key_value required"}), 400
        hit = _placeholder_reason(key_value)
        if hit:
            return jsonify({
                "error": f"value rejected: looks like a placeholder ({hit!r})",
            }), 400

        auth_data = _read_auth_profiles(bot_id)
        if not auth_data:
            auth_data = {"profiles": {}}
        profiles = auth_data.setdefault("profiles", {})

        profile_id = _canonical_profile_id(provider, key_type)
        field = _key_field(key_type)
        profiles[profile_id] = {
            "provider": provider,
            "type": key_type,
            field: key_value,
        }

        if not _write_auth_profiles(bot_id, auth_data):
            return jsonify({"error": "Failed to write auth-profiles.json"}), 500
        _prime_auth_store(bot_id)  # only after a successful write
        _module._audit_log_entry("keys.add", bot_id, {"provider": provider, "type": key_type, "profile_id": profile_id})
        return jsonify({"ok": True, "profile_id": profile_id})

    @app.get("/api/admin/keys/borrow-candidates")
    def api_admin_keys_borrow_candidates() -> Response:
        """List bots that have a configured profile for the given provider.

        Query string: ?provider=anthropic[&exclude=<bot_id>]. Used by the
        Add Key modal's "Copy from another bot" picker and by AI
        Optimization's diversity advisory.

        Reads via the same canonical _read_auth_profiles helper as the
        rest of /api/admin/keys/*, so the "<provider>:" prefix scan
        matches what the operator sees in the Credentials tab.
        """
        provider = (request.args.get("provider") or "").strip().lower()
        exclude = (request.args.get("exclude") or "").strip()
        if not provider:
            return jsonify({"error": "provider query parameter required"}), 400

        net = load_network(network_path)
        bot_ids = list(net.get("bots", {}).keys()) or net.get("members", []) or []

        candidates: list[dict] = []
        for bid in bot_ids:
            if bid == exclude:
                continue
            try:
                auth = _read_auth_profiles(bid) or {}
            except Exception:
                continue
            profs = auth.get("profiles") or {}
            if not isinstance(profs, dict):
                continue
            matching = sorted(
                k for k in profs.keys() if k.startswith(f"{provider}:")
            )
            if matching:
                candidates.append({"bot_id": bid, "profile_keys": matching})
        return jsonify({"provider": provider, "candidates": candidates})

    @app.post("/api/admin/keys/<bot_id>/<provider>/borrow")
    def api_admin_keys_borrow(bot_id: str, provider: str) -> Response:
        """Copy a provider profile from another bot into `bot_id`.

        Body: {from_bot: "evo", key_type?: "api_key"}. Returns 400 if
        the source has no matching profile, 500 on write failure.

        The copied entry gets `borrowed_from` + `borrowed_at` audit
        stamps. Independent after copy — rotating the source bot's
        key does not propagate. Same semantics as the wizard's
        /api/credentials/borrow flow.
        """
        body = request.get_json() or {}
        from_bot = (body.get("from_bot") or "").strip()
        key_type = (body.get("key_type") or "").strip() or None

        if not from_bot:
            return jsonify({"error": "from_bot required"}), 400
        if from_bot == bot_id:
            return jsonify({"error": "from_bot must differ from target bot"}), 400

        net = load_network(network_path)
        known_bots = set(net.get("bots", {}).keys()) | set(net.get("members", []) or [])
        if from_bot not in known_bots:
            return jsonify({"error": f"unknown source bot {from_bot!r}"}), 400
        if bot_id not in known_bots:
            return jsonify({"error": f"unknown target bot {bot_id!r}"}), 400

        source_auth = _read_auth_profiles(from_bot) or {}
        src_profiles = source_auth.get("profiles") or {}
        if not isinstance(src_profiles, dict) or not src_profiles:
            return jsonify({
                "error": f"source bot {from_bot!r} has no auth-profiles",
            }), 400

        # Find the first matching profile. If key_type was specified,
        # prefer "<provider>:<key_type>"; otherwise take any "<provider>:*".
        candidates = sorted(
            k for k in src_profiles.keys() if k.startswith(f"{provider}:")
        )
        if not candidates:
            return jsonify({
                "error": f"{from_bot!r} has no {provider} profile to copy",
            }), 400
        preferred = f"{provider}:{key_type}" if key_type else None
        chosen_pkey = (
            preferred if (preferred and preferred in candidates) else candidates[0]
        )
        entry = src_profiles[chosen_pkey]
        if not isinstance(entry, dict):
            return jsonify({
                "error": f"source profile {chosen_pkey!r} is malformed",
            }), 500

        # Reject placeholder/empty key values up front so we don't write
        # nonsense to the destination.
        chosen_field = _key_field(entry.get("type") or key_type or "api_key")
        live_value = entry.get(chosen_field) or entry.get("key") or entry.get("token")
        if not live_value:
            return jsonify({
                "error": f"source profile {chosen_pkey!r} has no key value",
            }), 400

        dest_auth = _read_auth_profiles(bot_id) or {}
        if "profiles" not in dest_auth or not isinstance(dest_auth.get("profiles"), dict):
            dest_auth["profiles"] = {}
        new_entry = dict(entry)
        new_entry["borrowed_from"] = from_bot
        from datetime import datetime as _bdt, timezone as _btz
        new_entry["borrowed_at"] = _bdt.now(_btz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        dest_auth["profiles"][chosen_pkey] = new_entry

        if not _write_auth_profiles(bot_id, dest_auth):
            return jsonify({"error": "Failed to write auth-profiles.json"}), 500

        _module._audit_log_entry(
            "keys.borrow", bot_id,
            {
                "provider": provider,
                "from_bot": from_bot,
                "profile_id": chosen_pkey,
            },
        )
        return jsonify({
            "ok": True,
            "profile_id": chosen_pkey,
            "from_bot": from_bot,
        })

    @app.post("/api/admin/keys/<bot_id>/<provider>/rotate")
    def api_admin_rotate_key(bot_id: str, provider: str) -> Response:
        """Replace a key value directly in auth-profiles.json.

        Body params:
          - key_value (required): the new credential
          - profile_id (optional): pin a specific profile
          - field_key (required for token_pair providers): which field to rotate
                       (e.g. "bot_token" for telegram). Returns 400 if missing.
          - storage (optional, default "auth_profiles"): where to write the
                       new value. "auth_profiles" replays the canonical
                       wizard-managed flow (auth-profiles.json + mirror to
                       openclaw.json). "openclaw_channels" writes directly
                       to `openclaw.json#channels.<provider>.<oc_field>` —
                       used for bots that were configured outside the
                       wizard and have no auth-profiles entry to update.
                       "dotenv" rewrites the matching `<NAME>=<value>`
                       line in `~/.openclaw/workspace/.env` for the
                       team_bot_a-style pattern (`SLACK_BOT_TOKEN=...` etc.) —
                       every other line is preserved verbatim. The probe
                       (OpenclawChannelsTokenProbe / DotenvProbe) flags
                       the row with the right storage so the modal posts
                       back the correct value (decision B: write where
                       the runtime reads).

        For providers in `_RUNTIME_MIRROR_PATH`, the auth_profiles flow also
        mirrors the new value into the bot's openclaw.json so the gateway
        picks it up after restart. Per-field rotation stores
        `_evolve_prev_<field_key>` for token_pair providers (auth-profiles
        only — the openclaw_channels path skips prev-backup so we don't
        risk widening openclaw's strict schema with `_evolve_prev_*` keys).
        """
        body = request.get_json() or {}
        # Accept either `key_value` (legacy / add-key) or `value` (catalog
        # probes use this name). They're equivalent.
        key_value = (body.get("key_value") or body.get("value") or "").strip()
        profile_id = body.get("profile_id", "")
        field_key = (body.get("field_key") or "").strip()
        storage = (body.get("storage") or "auth_profiles").strip().lower()
        if not key_value:
            return jsonify({"error": "key_value required"}), 400
        hit = _placeholder_reason(key_value)
        if hit:
            return jsonify({
                "error": f"value rejected: looks like a placeholder ({hit!r})",
            }), 400

        if storage == "openclaw_channels":
            return _rotate_openclaw_channels(bot_id, provider, field_key, key_value)
        if storage == "dotenv":
            return _rotate_dotenv(bot_id, provider, field_key, key_value)
        if storage not in ("auth_profiles", ""):
            return jsonify({
                "error": f"unsupported storage '{storage}'",
                "valid_storage": ["auth_profiles", "openclaw_channels", "dotenv"],
            }), 400

        auth_data = _read_auth_profiles(bot_id)
        if not auth_data:
            return jsonify({"error": "Could not read auth-profiles.json"}), 500
        profiles = auth_data.get("profiles", {})

        if profile_id and profile_id in profiles:
            pdata = profiles[profile_id]
        else:
            # Find any profile for this provider
            pdata = next((v for v in profiles.values() if v.get("provider") == provider), None)
            if pdata is None:
                return jsonify({"error": f"No existing profile for {provider}"}), 404
            profile_id = next(k for k, v in profiles.items() if v is pdata)

        ptype = pdata.get("type", "api_key")

        # token_pair providers must specify which field to rotate — we cannot
        # silently corrupt the wrong field (KR5).
        if ptype == "token_pair":
            meta = _PROVIDER_META.get(provider, {})
            valid_fields = {f["key"] for f in meta.get("fields", [])}
            if not field_key:
                return jsonify({
                    "error": "field_key required for token_pair providers",
                    "valid_fields": sorted(valid_fields),
                }), 400
            if field_key not in valid_fields:
                return jsonify({
                    "error": f"unknown field_key '{field_key}' for {provider}",
                    "valid_fields": sorted(valid_fields),
                }), 400
            current_val = pdata.get(field_key)
            if current_val:
                pdata[f"_evolve_prev_{field_key}"] = current_val
            pdata[field_key] = key_value
        else:
            field = _key_field(ptype)
            current_val = pdata.get(field)
            if current_val:
                pdata["_evolve_prev_key"] = current_val
            pdata[field] = key_value
            # api_key providers: derive field_key for mirror lookup
            field_key = field_key or "api_key"

        profiles[profile_id] = pdata

        # Rotate-as-migration: if the profile key is in the legacy
        # underscore shape that wizard-verify's `_LEGACY_KEY_RE` flags
        # (anthropic/openai/brave + api_key/auth_token), rename it to
        # the canonical `<provider>:<type>` colon shape derived from
        # the entry's own provider+type fields. OC's resolver matches
        # profile keys by `provider:slot` — underscore-only keys in
        # this set silently fall through and the agent gets no
        # credential. Rotating a key signals operator intent to make
        # the credential work; that's the right moment to fix the
        # shape. Scope matches the verify regex exactly so we don't
        # touch keys verify wouldn't flag (e.g. `telegram_token_pair`).
        # Idempotent: collision with an existing canonical entry
        # (operator manually pre-created it) leaves the current key
        # alone for manual resolution.
        renamed_from: str | None = None
        renamed_to: str | None = None
        if _LEGACY_PROFILE_KEY_RE.match(profile_id):
            prov_field = str(pdata.get("provider") or "").strip()
            type_field = str(pdata.get("type") or "api_key").strip()
            if prov_field:
                canonical_id = f"{prov_field}:{type_field}"
                if canonical_id != profile_id and canonical_id not in profiles:
                    profiles[canonical_id] = profiles.pop(profile_id)
                    renamed_from, renamed_to = profile_id, canonical_id
                    profile_id = canonical_id

        if not _write_auth_profiles(bot_id, auth_data):
            return jsonify({"error": "Failed to write auth-profiles.json"}), 500

        # Mirror to openclaw.json if registry has an entry for this provider/field
        mirror_ok, mirror_err = _mirror_to_openclaw(bot_id, provider, field_key, key_value)

        audit_details: dict = {
            "provider": provider, "profile_id": profile_id,
            "field_key": field_key, "mirrored": mirror_ok,
            "storage": "auth_profiles",
        }
        if renamed_from:
            audit_details["renamed_from"] = renamed_from
            audit_details["renamed_to"] = renamed_to
        _module._audit_log_entry("keys.rotate", bot_id, audit_details,
            oc_keys=_oc_keys_for_storage("auth_profiles", provider, mirrored=mirror_ok))
        resp_body: dict = {
            "ok": True,
            "storage": "auth_profiles",
            "profile_id": profile_id,
            "field_key": field_key,
            "mirrored": mirror_ok,
            "mirror_error": mirror_err,
            "requires_restart": True,
            "restart_endpoint": f"/api/admin/gateway/{bot_id}/restart",
        }
        if renamed_from:
            resp_body["renamed_from"] = renamed_from
            resp_body["renamed_to"] = renamed_to
        return jsonify(resp_body)

    @app.post("/api/admin/integration-token/<bot_id>/github/rotate")
    def api_admin_rotate_github_integration_token(bot_id: str) -> Response:
        """Rotate a GitHub PAT for a bot.

        Behavior depends on the bot's current auth_type:
          - https_pat:        replace the embedded token in the URL (legacy)
          - https_credhelper: convert plain HTTPS to URL-embedded-PAT form so
                              future rotations can edit the URL directly
          - ssh:              400 — SSH deploy keys aren't rotated through
                              this route (regenerate the key on disk +
                              update the GitHub deploy key page)
          - None:             404 — no github remote at all

        We never silently no-op: every accepted auth_type ends with a
        material change to .git/config (verified by `git_cfg_changed`).
        """
        import re as _re, tempfile as _tmpmod, os as _os

        body = request.get_json() or {}
        new_token = (body.get("key_value") or body.get("value") or "").strip()
        if not new_token:
            return jsonify({"error": "key_value required"}), 400

        info = _discover_github_remote(bot_id)
        if not info:
            return jsonify({
                "ok": False,
                "error": f"no github remote configured for {bot_id}",
            }), 404
        auth_type = info["auth_type"]
        if auth_type == "ssh":
            return jsonify({
                "ok": False,
                "auth_type": "ssh",
                "error": (
                    "SSH deploy keys aren't rotated through this UI. "
                    f"To rotate: regenerate /Users/evolve/.ssh/evolve-backup-{bot_id} "
                    "and update the deploy key on the repo's GitHub Settings → Deploy keys page."
                ),
            }), 400

        # ── Step 1: read .git/config ────────────────────────────────────────
        user = _resolve_user(bot_id)
        _rot_paths = resolve_bot_paths(bot_id, user=user)
        git_config_path = str(Path(_rot_paths["workspace"]) / ".git" / "config")
        git_cfg_text: str | None = None
        try:
            git_cfg_text = Path(git_config_path).read_text()
        except PermissionError:
            # ``-n`` so a missing sudoers rule fails fast on the launchd-
            # spawned admin server (no TTY for the password prompt).
            r = _subproc.run(
                ["sudo", "-n", "/bin/cat", git_config_path],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                git_cfg_text = r.stdout
        if not git_cfg_text:
            return jsonify({
                "ok": False,
                "error": f"could not read {git_config_path}",
            }), 500

        # ── Step 2: rewrite the URL based on auth_type ──────────────────────
        git_cfg_updated: str
        auth_type_after = auth_type
        if auth_type == "https_pat":
            old_token = info["token"]
            git_cfg_updated = git_cfg_text.replace(old_token, new_token)
        else:  # https_credhelper — inject token into plain HTTPS URLs
            # Match `https://github.com/...` (no auth segment) and inject the
            # token. The negative lookahead skips URLs that already have an
            # `@`-delimited auth segment — those are https_pat and shouldn't
            # be touched here (defensive: _discover_github_remote already
            # routed us here, but the file might have multiple github URLs).
            git_cfg_updated = _re.sub(
                r"(url\s*=\s*https://)(?!(?:[^:@\s]*:)?[^@\s]+@)(github\.com/)",
                rf"\g<1>{new_token}@\g<2>",
                git_cfg_text,
            )
            auth_type_after = "https_pat"

        if git_cfg_updated == git_cfg_text:
            # Regex didn't match anything — surface this rather than claim success.
            return jsonify({
                "ok": False,
                "auth_type": auth_type,
                "error": (
                    "no github URLs in .git/config matched the rewrite pattern; "
                    "rotation aborted"
                ),
            }), 500

        # ── Step 3: write .git/config ───────────────────────────────────────
        fd, tmp = _tmpmod.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".gitconfig")
        git_cfg_ok = False
        git_cfg_err = ""
        try:
            with _os.fdopen(fd, "w") as f:
                f.write(git_cfg_updated)
            # ``-n`` so a missing NOPASSWD rule fails immediately with a
            # readable stderr (admin server has no TTY for a password prompt).
            r = _subproc.run(
                ["sudo", "-n", "/bin/cp", tmp, git_config_path],
                capture_output=True, text=True, timeout=10,
            )
            git_cfg_ok = r.returncode == 0
            if not git_cfg_ok:
                git_cfg_err = (r.stderr or "").strip()
            else:
                # Restore bot ownership so the bot can read its own .git/config
                # next time a git op runs. Best-effort: write already succeeded.
                _subproc.run(
                    ["sudo", "-n", "/usr/sbin/chown", f"{user}:staff", git_config_path],
                    capture_output=True, text=True, timeout=5,
                )
                # 0600, not 644: on the https_pat path this file now embeds
                # the fresh PAT — token-bearing files follow the
                # secret_config_perms contract (world-readable tokens on a
                # multi-user box otherwise). chmod preserves the evolve read
                # ACL; the deploy self-heal converges drift.
                from ..secret_config_perms import chmod_secret_config
                chmod_secret_config(git_config_path)
        finally:
            try:
                _os.unlink(tmp)
            except OSError:
                pass

        if not git_cfg_ok:
            return jsonify({
                "ok": False,
                "error": f"failed to write .git/config{': ' + git_cfg_err if git_cfg_err else ''}",
            }), 500

        _module._audit_log_entry(
            "integration_token.rotate",
            bot_id,
            {
                "provider": "github",
                "auth_type_before": auth_type,
                "auth_type_after": auth_type_after,
            },
        )
        return jsonify({
            "ok": True,
            "git_config_updated": True,
            "auth_type_before": auth_type,
            "auth_type_after": auth_type_after,
            "requires_restart": True,
            "restart_endpoint": f"/api/admin/gateway/{bot_id}/restart",
        })

    # ── Discord integration-token routes ────────────────────────────────────
    # Discord credentials live in openclaw.json (channels.discord.token), not
    # in auth-profiles.json — same model as github (.git/config). The check
    # route mirrors github/check; the rotate route also handles first-time
    # setup since the operation is identical (write the token into openclaw).

    @app.get("/api/admin/integration-token/<bot_id>/discord/check")
    def api_admin_discord_integration_check(bot_id: str) -> Response:
        """Lightweight presence check for the Discord channel — returns 404
        when channels.discord is missing or token-less, 200 when configured.
        """
        info = _discover_discord_account(bot_id)
        if info is None:
            return jsonify({"ok": False, "configured": False, "bot": bot_id}), 404
        if info["shape"] == "multi_account":
            return jsonify({
                "ok": True,
                "configured": True,
                "bot": bot_id,
                "shape": "multi_account",
                "accounts": info["accounts"],
                "enabled": info["enabled"],
            })
        return jsonify({
            "ok": True,
            "configured": True,
            "bot": bot_id,
            "shape": "single",
            "masked": _mask_key(info["token"]),
            "enabled": info["enabled"],
            "guild_count": info["guild_count"],
        })

    @app.post("/api/admin/integration-token/<bot_id>/discord/rotate")
    def api_admin_rotate_discord_integration_token(bot_id: str) -> Response:
        """Rotate (or first-time set) the Discord bot token for a bot.

        Body params:
          - key_value (required): the bot token to install
          - skip_validation (optional): bypass the discord.com /users/@me probe
                                        (used by tests; defaults to False)

        Behavior:
          - Validates the token against discord.com/api/v10/users/@me unless
            skip_validation=true (so a bad token never silently lands in
            openclaw.json).
          - Refuses to write when channels.discord.accounts is populated
            (multi-account configs need manual editing, not silent rewrite).
          - Otherwise writes channels.discord.token and ensures enabled=true,
            preserving all sibling keys (guilds/groupPolicy/streaming/etc).
        """
        body = request.get_json() or {}
        new_token = (body.get("key_value") or body.get("value") or "").strip()
        skip_validation = bool(body.get("skip_validation"))
        if not new_token:
            return jsonify({"error": "key_value required"}), 400

        # Detect the existing config first so we can decide whether to refuse
        # a multi-account rewrite (and report the auth_type_before for parity
        # with the github rotate response).
        info_before = _discover_discord_account(bot_id)
        was_configured = info_before is not None
        if info_before and info_before["shape"] == "multi_account":
            return jsonify({
                "ok": False,
                "shape": "multi_account",
                "accounts": info_before["accounts"],
                "error": (
                    "channels.discord.accounts is populated (multi-account "
                    "config). Edit openclaw.json directly to avoid accidentally "
                    "rewriting only the default account's token."
                ),
            }), 400

        identity: dict | None = None
        if not skip_validation:
            ok, err, identity = _validate_discord_token(new_token)
            if not ok:
                return jsonify({"ok": False, "error": err or "token validation failed"}), 400

        applied, write_err = _set_discord_token(bot_id, new_token)
        if not applied:
            return jsonify({"ok": False, "error": write_err or "failed to write openclaw.json"}), 500

        # Verify the write actually landed where we expect — never claim
        # success without a side effect we can read back.
        info_after = _discover_discord_account(bot_id)
        if not info_after or info_after.get("shape") != "single" or info_after.get("token") != new_token:
            return jsonify({
                "ok": False,
                "error": "post-write verification failed: channels.discord.token did not match the value we just wrote",
            }), 500

        _module._audit_log_entry(
            "integration_token.rotate",
            bot_id,
            {
                "provider": "discord",
                "was_configured_before": was_configured,
                "validated": not skip_validation,
            },
            oc_keys=_oc_keys_for_storage("openclaw_channels", "discord"),
        )
        return jsonify({
            "ok": True,
            "openclaw_updated": True,
            "was_configured_before": was_configured,
            "validated": not skip_validation,
            "identity": identity,
            "requires_restart": True,
            "restart_endpoint": f"/api/admin/gateway/{bot_id}/restart",
        })

    # ── WhatsApp integration-token routes ───────────────────────────────────
    # WhatsApp has no rotatable token — it uses Baileys session-based auth
    # via QR pairing on the bot host. The "rotate" verb returns a clear 400
    # explaining what to do instead (per the no-silent-no-op rule). The
    # "setup" verb writes the channel scaffold + enabled=true so the bot host
    # can run the openclaw CLI pairing flow.

    @app.get("/api/admin/integration-token/<bot_id>/whatsapp/check")
    def api_admin_whatsapp_integration_check(bot_id: str) -> Response:
        """Presence check for the WhatsApp channel."""
        info = _discover_whatsapp_account(bot_id)
        if info is None:
            return jsonify({"ok": False, "configured": False, "bot": bot_id}), 404
        return jsonify({
            "ok": True,
            "configured": True,
            "bot": bot_id,
            "enabled": info["enabled"],
            "shape": info["shape"],
            "auth_dir": info["auth_dir"],
        })

    @app.post("/api/admin/integration-token/<bot_id>/whatsapp/rotate")
    def api_admin_rotate_whatsapp_integration_token(bot_id: str) -> Response:
        """WhatsApp does not have a rotatable bot token — Baileys auth state
        is session-based (a directory of files paired via QR on the bot host).
        Surface a clear 400 rather than pretending to rotate something."""
        return jsonify({
            "ok": False,
            "error": (
                "WhatsApp via openclaw uses Baileys session-based auth — "
                "there is no token to rotate. To re-pair: ssh to the bot "
                f"host as the bot user and run `oc whatsapp pair` to scan a "
                "fresh QR code, which writes new session files into the "
                "Baileys auth directory."
            ),
        }), 400

    @app.post("/api/admin/integration-token/<bot_id>/whatsapp/setup")
    def api_admin_setup_whatsapp_integration(bot_id: str) -> Response:
        """Enable channels.whatsapp in openclaw.json so the gateway picks it
        up on next restart. This is the prerequisite step before the operator
        runs the QR pairing flow on the bot host.

        Body params:
          - enabled (optional, default true): the value to set
        """
        body = request.get_json() or {}
        enabled = bool(body.get("enabled", True))

        info_before = _discover_whatsapp_account(bot_id)
        was_present = info_before is not None
        was_enabled = bool(info_before["enabled"]) if info_before else False

        if was_present and was_enabled == enabled:
            # Idempotent re-flip with no other change is a no-op — call that
            # out explicitly rather than claim a write that didn't happen.
            return jsonify({
                "ok": True,
                "openclaw_updated": False,
                "noop": True,
                "was_present_before": was_present,
                "enabled": enabled,
                "next_steps": (
                    f"Already at enabled={enabled}. To pair: ssh to the bot "
                    f"host as the bot user and run `oc whatsapp pair`."
                ),
            })

        applied, write_err = _set_whatsapp_enabled(bot_id, enabled)
        if not applied:
            return jsonify({"ok": False, "error": write_err or "failed to write openclaw.json"}), 500

        info_after = _discover_whatsapp_account(bot_id)
        if not info_after or info_after["enabled"] != enabled:
            return jsonify({
                "ok": False,
                "error": "post-write verification failed: channels.whatsapp.enabled did not reflect the requested value",
            }), 500

        _module._audit_log_entry(
            "integration_token.setup",
            bot_id,
            {"provider": "whatsapp", "was_present_before": was_present, "enabled": enabled},
            oc_keys=_oc_keys_for_storage("openclaw_channels", "whatsapp"),
        )
        return jsonify({
            "ok": True,
            "openclaw_updated": True,
            "was_present_before": was_present,
            "enabled": enabled,
            "next_steps": (
                f"channels.whatsapp.enabled set to {enabled}. To complete "
                f"pairing: ssh to the bot host as the bot user and run "
                f"`oc whatsapp pair` — this writes session files into the "
                f"Baileys auth directory. Restart the gateway after pairing."
            ),
            "requires_restart": True,
            "restart_endpoint": f"/api/admin/gateway/{bot_id}/restart",
        })

    @app.post("/api/admin/keys/<bot_id>/<provider>/rollback")
    def api_admin_rollback_key(bot_id: str, provider: str) -> Response:
        """Swap current key with its previous value (undo last rotation).

        Body params:
          - profile_id (optional): pin a specific profile
          - field_key (required for token_pair providers): which field to roll back

        For token_pair providers, restores `_evolve_prev_<field_key>` for the
        named field only — sibling fields are untouched. For api_key providers,
        existing `_evolve_prev_key` semantics apply unchanged.
        """
        body = request.get_json() or {}
        profile_id = body.get("profile_id", "")
        field_key = (body.get("field_key") or "").strip()

        auth_data = _read_auth_profiles(bot_id)
        if not auth_data:
            return jsonify({"error": "Could not read auth-profiles.json"}), 500
        profiles = auth_data.get("profiles", {})

        if profile_id and profile_id in profiles:
            pdata = profiles[profile_id]
        else:
            pdata = next((v for v in profiles.values() if v.get("provider") == provider), None)
            if pdata is None:
                return jsonify({"error": f"No existing profile for {provider}"}), 404
            profile_id = next(k for k, v in profiles.items() if v is pdata)

        ptype = pdata.get("type", "api_key")

        if ptype == "token_pair":
            meta = _PROVIDER_META.get(provider, {})
            valid_fields = {f["key"] for f in meta.get("fields", [])}
            if not field_key:
                return jsonify({
                    "error": "field_key required for token_pair providers",
                    "valid_fields": sorted(valid_fields),
                }), 400
            if field_key not in valid_fields:
                return jsonify({
                    "error": f"unknown field_key '{field_key}' for {provider}",
                    "valid_fields": sorted(valid_fields),
                }), 400
            prev_key = f"_evolve_prev_{field_key}"
            prev_val = pdata.get(prev_key)
            if not prev_val:
                return jsonify({"error": f"No previous value for {field_key}"}), 409
            current_val = pdata.get(field_key)
            pdata[field_key] = prev_val
            if current_val:
                pdata[prev_key] = current_val
            else:
                pdata.pop(prev_key, None)
            mirror_value = prev_val
        else:
            field = _key_field(ptype)
            prev_val = pdata.get("_evolve_prev_key")
            if not prev_val:
                return jsonify({"error": "No previous value available for rollback"}), 409
            current_val = pdata.get(field)
            pdata[field] = prev_val
            if current_val:
                pdata["_evolve_prev_key"] = current_val
            else:
                pdata.pop("_evolve_prev_key", None)
            mirror_value = prev_val
            field_key = field_key or "api_key"

        profiles[profile_id] = pdata

        if not _write_auth_profiles(bot_id, auth_data):
            return jsonify({"error": "Failed to write auth-profiles.json"}), 500

        # Re-mirror to openclaw.json so the runtime sees the rolled-back value.
        mirror_ok, mirror_err = _mirror_to_openclaw(bot_id, provider, field_key, mirror_value)

        _module._audit_log_entry("keys.rollback", bot_id, {
            "provider": provider, "profile_id": profile_id,
            "field_key": field_key, "mirrored": mirror_ok,
        }, oc_keys=_oc_keys_for_storage("auth_profiles", provider, mirrored=mirror_ok))
        return jsonify({
            "ok": True,
            "profile_id": profile_id,
            "field_key": field_key,
            "mirrored": mirror_ok,
            "mirror_error": mirror_err,
            "requires_restart": True,
            "restart_endpoint": f"/api/admin/gateway/{bot_id}/restart",
        })

    @app.delete("/api/admin/keys/<bot_id>/<provider>")
    def api_admin_remove_key(bot_id: str, provider: str) -> Response:
        """Remove a key from auth-profiles.json."""
        body = request.get_json() or {}
        profile_id = body.get("profile_id", "")

        auth_data = _read_auth_profiles(bot_id)
        if not auth_data:
            return jsonify({"error": "Could not read auth-profiles.json"}), 500
        profiles = auth_data.get("profiles", {})

        if profile_id and profile_id in profiles:
            del profiles[profile_id]
        else:
            # Remove all profiles for this provider
            to_del = [k for k, v in profiles.items() if v.get("provider") == provider]
            if not to_del:
                return jsonify({"error": f"No profile found for {provider}"}), 404
            for k in to_del:
                del profiles[k]

        if not _write_auth_profiles(bot_id, auth_data):
            return jsonify({"error": "Failed to write auth-profiles.json"}), 500
        _module._audit_log_entry("keys.remove", bot_id, {"provider": provider, "profile_id": profile_id})
        return jsonify({"ok": True})

    @app.post("/api/admin/keys/<bot_id>/<provider>/disconnect")
    def api_admin_disconnect_provider(bot_id: str, provider: str) -> Response:
        """Disconnect a non-Google provider by clearing its auth-profiles entry.

        The DISCONNECT affordance dispatches POSTs from the dashboard
        (`_dispatchAction`); Google Workspace has its own remote-revoke flow
        at `/api/admin/onboard/google/revoke`, this is the per-provider
        equivalent for everything else (token_pair: telegram/slack/discord;
        api_key: anthropic, openai, …). Returns `{ok, revoked: false}` —
        token revocation at the provider isn't reachable for these
        credential shapes (telegram requires BotFather, slack/api keys
        require admin), so disconnect = clear local credentials.
        """
        auth_data = _read_auth_profiles(bot_id)
        if not auth_data:
            return jsonify({"error": "Could not read auth-profiles.json"}), 500
        profiles = auth_data.get("profiles", {})
        to_del = [k for k, v in profiles.items() if v.get("provider") == provider]
        if not to_del:
            return jsonify({"error": f"No profile found for {provider}"}), 404
        for k in to_del:
            del profiles[k]
        if not _write_auth_profiles(bot_id, auth_data):
            return jsonify({"error": "Failed to write auth-profiles.json"}), 500
        _module._audit_log_entry("keys.disconnect", bot_id, {
            "provider": provider, "profiles_cleared": to_del,
        })
        return jsonify({"ok": True, "revoked": False})

    @app.put("/api/admin/keys/<bot_id>/order")
    def api_admin_order_keys(bot_id: str) -> Response:
        """Update auth.order.{provider} in openclaw.json."""
        body = request.get_json() or {}
        provider = body.get("provider", "")
        order = body.get("order", [])
        if not provider or not isinstance(order, list):
            return jsonify({"error": "provider and order[] required"}), 400

        oc_cfg = _read_oc_json(bot_id)
        if not oc_cfg:
            return jsonify({"error": "Could not read openclaw.json"}), 500
        oc_cfg.setdefault("auth", {}).setdefault("order", {})[provider] = order

        if not _write_oc_json(bot_id, oc_cfg):
            return jsonify({"error": "Failed to write openclaw.json"}), 500
        _module._audit_log_entry("keys.order", bot_id, {"provider": provider, "order": order}, oc_keys={"auth"})
        return jsonify({"ok": True})

    # ── Guided onboarding (github + brave) ──────────────────────────────
    # See feature-2026-05-03-002 spec at issues/features/.
    # Five new endpoints: discover-default-pat, github verify, github
    # onboard, brave verify, brave onboard. The github flow uses a session
    # nonce (stored in _DISCOVERED_PAT_NONCES) to keep the discovered token
    # off the wire; the verify endpoint accepts either a raw PAT or a nonce.

    # GITHUB_API_BASE / BRAVE_API_BASE / HTTP_TIMEOUT_SECONDS and _github_api
    # lifted to routes_admin_shared.py (4.1b Increment 0). _github_api is
    # imported at module scope; the constants resolve there too.

    def _brave_verify(key: str) -> tuple[bool, int]:
        """Probe Brave Search API. Returns (ok, status_code)."""
        import urllib.request, urllib.error
        req = urllib.request.Request(f"{BRAVE_API_BASE}?q=test&count=1")
        req.add_header("X-Subscription-Token", key)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return 200 <= resp.status < 300, resp.status
        except urllib.error.HTTPError as e:
            return False, e.code
        except Exception:
            return False, 0

    def _discover_pat_cascade() -> tuple[str, str, str] | None:
        """Walk primary bot first, then other bots in network.json declaration
        order, returning the first (token, login, source_bot) discovered.
        """
        net = load_network(network_path)
        primary = net.get("primary")
        bots_dict = net.get("bots", {}) if isinstance(net.get("bots"), dict) else {}
        # Build search order: primary first, then everything else
        order: list[str] = []
        if primary and primary in bots_dict:
            order.append(primary)
        for bot_id in bots_dict.keys():
            if bot_id != primary and bot_id not in order:
                order.append(bot_id)
        for bot_id in order:
            try:
                result = _discover_github_token(bot_id)
            except Exception:
                continue
            if not result:
                continue
            token, repo_slug = result
            login = (repo_slug or "").split("/", 1)[0]
            if not login:
                continue
            return token, login, bot_id
        return None

    def _mask_pat(token: str) -> str:
        """Return a masked PAT for display: ghp_•••• with last 4 chars."""
        if not token:
            return ""
        if len(token) <= 8:
            return "ghp_••••"
        prefix = token[:4]
        suffix = token[-4:]
        return f"{prefix}{'•' * 12}{suffix}"

    @app.get("/api/admin/onboard/github/discover-default-pat")
    def api_admin_onboard_discover_default_pat() -> Response:
        """Discover a pod-default PAT and surface per-bot current owners.

        Per primary-first cascade order: walk bots in network.json, return the
        first HTTPS-PAT we find. Even when no PAT is discovered, the response
        still carries per-bot discovered owners (from .git/config, including
        SSH-based bots via _discover_github_remote — feature 2026-05-04-002),
        which the frontend uses to pre-populate the per-bot org dropdown.

        Returns one of:
            {nonce, masked, login, source_bot,
             available_orgs: [{login, type, source}],
             bot_owners: {bot_id: {owner, repo, auth_type}}}     on PAT hit
            {nonce: null, available_orgs: [...], bot_owners: {...}} otherwise

        The plaintext token is held server-side keyed by the nonce; the
        client redeems it via the verify or onboard endpoint within the
        TTL (network.json → onboardingNonceTTLSeconds, default 600s).
        """
        net = load_network(network_path)
        ttl = int(net.get("onboardingNonceTTLSeconds") or 600)

        # Per-bot discovered owners (HTTPS-PAT + SSH). Populated for every bot
        # in network.json so the modal can render "currently at <owner>/<repo>"
        # inline regardless of auth type.
        bots_dict = net.get("bots", {}) if isinstance(net.get("bots"), dict) else {}
        bot_owners: dict[str, dict] = {}
        discovered_owners: set[str] = set()
        for bid in bots_dict.keys():
            try:
                info = _discover_github_remote(bid)
            except Exception:
                info = None
            if not info:
                continue
            slug = info.get("repo_slug") or ""
            owner = slug.split("/", 1)[0] if "/" in slug else ""
            repo = slug.split("/", 1)[1] if "/" in slug else ""
            if owner:
                bot_owners[bid] = {
                    "owner": owner,
                    "repo": repo,
                    "auth_type": info.get("auth_type"),
                }
                discovered_owners.add(owner)

        # PAT cascade — separate from per-bot discovery so SSH-only pods still
        # render an empty-PAT response with bot_owners populated.
        cascade = _discover_pat_cascade()
        token = login = source_bot = None
        nonce: str | None = None
        if cascade:
            token, login, source_bot = cascade
            nonce = _store_discovered_pat_nonce(token, login, source_bot, ttl)

        # available_orgs: union of PAT-accessible logins + discovered bot owners.
        # Source tags let the frontend disambiguate when the PAT does not have
        # access to a discovered owner (e.g. fine-grained PAT scoped to one
        # org, or SSH-only owner the PAT can't reach).
        available_orgs: list[dict] = list(_list_pat_orgs(token) if token else [])
        pat_login_set = {x["login"] for x in available_orgs}
        for owner in discovered_owners:
            if owner in pat_login_set:
                continue
            available_orgs.append({
                "login": owner,
                "type": "unknown",
                "source": "discovered_from_bot",
            })

        resp: dict = {
            "available_orgs": available_orgs,
            "bot_owners": bot_owners,
        }
        if nonce:
            resp.update({
                "nonce": nonce,
                "masked": _mask_pat(token or ""),
                "login": login,
                "source_bot": source_bot,
            })
        else:
            resp["nonce"] = None
        return jsonify(resp)

    def _list_pat_orgs(token: str) -> list[dict]:
        """Return the orgs + personal login a PAT can write to, for the
        per-bot org dropdown (feature 2026-05-04-002).

        Format: [{"login": <name>, "type": "user"|"org",
                  "source": "pat_user"|"pat_orgs"}, ...]. The PAT owner is
        always first when /user resolves; orgs follow in /user/orgs order.
        Errors → empty list (callers treat that as "no enumeration available"
        and fall back to free text). Best-effort; never raises.
        """
        if not token:
            return []
        out: list[dict] = []
        seen: set[str] = set()
        try:
            u_status, u_body, _ = _github_api("GET", "/user", token)
            if u_status == 200 and isinstance(u_body, dict):
                login = (u_body.get("login") or "").strip()
                if login and login not in seen:
                    out.append({"login": login, "type": "user", "source": "pat_user"})
                    seen.add(login)
        except Exception:
            pass
        try:
            o_status, o_body, _ = _github_api("GET", "/user/orgs", token)
            if o_status == 200 and isinstance(o_body, list):
                for o in o_body:
                    if not isinstance(o, dict):
                        continue
                    login = (o.get("login") or "").strip()
                    if login and login not in seen:
                        out.append({"login": login, "type": "org", "source": "pat_orgs"})
                        seen.add(login)
        except Exception:
            pass
        return out

    def _verify_github_pat_for_bot(
        token: str,
        login: str | None,
        repo_name: str,
        bot_id: str | None = None,
    ) -> dict:
        """Run the per-bot github verify probe.

        - GET /user with the token → confirms token validity + reports owner
        - GET /repos/{target_login}/{repo_name} → collision check + has_evolve_pubkey

        `login` is the *target* org/account for the probe. When a multi-org PAT
        is in use, this can legitimately differ from /user's login (e.g. token
        belongs to 'pod_admin' but the bot's repo lives under 'evolve-ops').
        We probe at `login or actual_login` and surface `actual_login` in the
        response so the frontend can warn if needed, but no longer reject
        purely on mismatch (per feature 2026-05-04-002).

        ``bot_id`` is required for byte-accurate has_evolve_pubkey check
        against the local pubkey via _bot_pubkey(bot_id). Pre-2026-06-07
        this fell back to a title-substring match ("evolve" in title),
        which silently disagreed with the run-step's byte comparison in
        _ensure_deploy_key. The disagreement was the silent-failure trap
        behind the 2026-06-07 wizard report: verify said "Will reuse —
        already has evolve deploy key" (green) for bots whose registered
        key was OLD per-bot bytes; run then collision'd because the
        local pubkey (now the shared key from Distribute Key) didn't
        match. The collision banner offered no [Reuse] affordance to
        unstick the user because verify had already declared reuse.
        """
        if not token:
            return {"ok": False, "error": "no token"}
        # 1. /user — confirms token validity + scopes
        u_status, u_body, u_headers = _github_api("GET", "/user", token)
        if u_status != 200 or not isinstance(u_body, dict):
            return {"ok": False, "error": f"github /user returned {u_status}"}
        actual_login = (u_body.get("login") or "").strip()
        if not actual_login:
            return {"ok": False, "error": "github /user response missing login"}
        scopes_raw = u_headers.get("x-oauth-scopes", "") or ""
        scopes = [s.strip() for s in scopes_raw.split(",") if s.strip()]
        # Fine-grained PATs return an empty X-OAuth-Scopes header but are still valid.
        fine_grained = (scopes_raw == "" or scopes_raw is None)
        has_repo_scope = "repo" in scopes
        # 2. /repos/{target_login}/{repo_name} — collision check at the per-bot
        # owner. Falls back to the PAT's own login when no target is given.
        target_login = (login or "").strip() or actual_login
        r_status, r_body, _ = _github_api("GET", f"/repos/{target_login}/{repo_name}", token)
        repo_info: dict
        if r_status == 200 and isinstance(r_body, dict):
            # Check if our evolve deploy key is registered. MUST use the
            # same byte comparison _ensure_deploy_key uses at run time;
            # otherwise verify and run disagree and the wizard locks up
            # with "Unresolved collisions" the user can't resolve (no
            # [Reuse] affordance was offered because verify falsely
            # declared reuse already).
            k_status, k_body, _ = _github_api("GET", f"/repos/{target_login}/{repo_name}/keys", token)
            has_evolve_pubkey = False
            if k_status == 200 and isinstance(k_body, list):
                local_pub = _bot_pubkey(bot_id) if bot_id else None
                target_blob = " ".join(local_pub.split()[:2]) if local_pub else ""
                if target_blob:
                    has_evolve_pubkey = any(
                        " ".join((k.get("key") or "").split()[:2]) == target_blob
                        for k in k_body if isinstance(k, dict)
                    )
                else:
                    # No bot_id (older caller) or no local pubkey
                    # available — fall back to the pre-2026-06-07
                    # title heuristic so behavior is no worse than
                    # before for unknown-bot probes.
                    has_evolve_pubkey = any(
                        "evolve" in (k.get("title") or "").lower()
                        for k in k_body if isinstance(k, dict)
                    )
            repo_info = {
                "name": repo_name,
                "exists": True,
                "url": r_body.get("html_url"),
                "private": r_body.get("private"),
                "default_branch": r_body.get("default_branch"),
                "last_pushed_at": r_body.get("pushed_at"),
                "has_evolve_pubkey": has_evolve_pubkey,
            }
        else:
            repo_info = {"name": repo_name, "exists": False}
        return {
            "ok": True,
            "login": target_login,
            "actual_login": actual_login,
            "fine_grained": fine_grained and not has_repo_scope,
            "has_repo_scope": has_repo_scope or fine_grained,
            "repo": repo_info,
        }

    @app.post("/api/admin/onboard/github/verify")
    def api_admin_onboard_verify_github() -> Response:
        """Per-bot github verify: scope info + collision data per candidate repo.

        Body: {
          default: {token, github_login} | null,
          bots: [{bot_id, repo_name, github_login?, override?: {token, github_login}}]
        }

        Per-bot login precedence: override.github_login > entry.github_login >
        default_login. The top-level entry.github_login lets the operator point
        a bot at a different org without supplying a per-bot PAT, when the
        default PAT already has access to that org (per feature 2026-05-04-002).

        Returns {ok, bots: [...], available_orgs: [...]}. `available_orgs` is
        the set of logins the default PAT can write to (PAT's own login + orgs
        from /user/orgs); empty list when no default token is provided.
        Mutates nothing.
        """
        body = request.get_json() or {}
        default_creds = body.get("default") or {}
        bots = body.get("bots") or []
        if not isinstance(bots, list):
            return jsonify({"error": "bots[] required"}), 400

        default_token_or_nonce = (default_creds.get("token") or "").strip()
        default_login_input = (default_creds.get("github_login") or "").strip()
        default_token, _disc_login, _ = _resolve_credential(default_token_or_nonce)
        # If nonce-resolved login present and no explicit login given, use it.
        if not default_login_input and _disc_login:
            default_login_input = _disc_login

        per_bot: list[dict] = []
        for entry in bots:
            if not isinstance(entry, dict):
                continue
            bot_id = (entry.get("bot_id") or "").strip()
            repo_name = (entry.get("repo_name") or "").strip()
            if not bot_id or not repo_name:
                per_bot.append({
                    "bot_id": bot_id,
                    "ok": False,
                    "error": "bot_id and repo_name required",
                })
                continue
            entry_login = (entry.get("github_login") or "").strip()
            override = entry.get("override") or {}
            override_token_in = (override.get("token") or "").strip()
            if override_token_in:
                tok, _ol, _src = _resolve_credential(override_token_in)
                # Precedence: override.github_login > entry.github_login > nonce-derived login
                login = (
                    (override.get("github_login") or "").strip()
                    or entry_login
                    or _ol
                    or ""
                )
            else:
                tok = default_token
                # Precedence: entry.github_login > default_login_input
                login = entry_login or default_login_input
            if not tok:
                per_bot.append({
                    "bot_id": bot_id,
                    "ok": False,
                    "error": "no token (default missing or nonce expired)",
                })
                continue
            result = _verify_github_pat_for_bot(
                tok, login or None, repo_name, bot_id=bot_id,
            )
            result["bot_id"] = bot_id
            per_bot.append(result)

        available_orgs = _list_pat_orgs(default_token) if default_token else []
        return jsonify({
            "ok": True,
            "bots": per_bot,
            "available_orgs": available_orgs,
        })

    @app.post("/api/admin/onboard/brave/verify")
    def api_admin_onboard_verify_brave() -> Response:
        """Verify a Brave Search API key + per-bot eligibility.

        Body: {key, bots: [bot_id, ...]}
        Returns {ok, brave_ok, status, bots: [{bot_id, eligible,
                 current_provider, already_configured, oc_only}]}.
        `eligible` is true only when the bot has neither opted out (provider
        set to something other than null/"brave") nor already has a key in
        openclaw.json or auth-profiles.json.
        Mutates nothing.
        """
        body = request.get_json() or {}
        key = (body.get("key") or "").strip()
        bots = body.get("bots") or []
        if not key:
            return jsonify({"error": "key required"}), 400
        if not isinstance(bots, list):
            return jsonify({"error": "bots[] required"}), 400
        brave_ok, status = _brave_verify(key)
        per_bot: list[dict] = []
        for bot_id in bots:
            if not isinstance(bot_id, str) or not bot_id.strip():
                continue
            oc_cfg = _read_oc_json(bot_id) or {}
            current = (oc_cfg.get("tools", {}).get("web", {}).get("search", {}) or {}).get("provider")
            # Honour both canonical + legacy openclaw.json key locations (see
            # brave_key_from_oc_config) so onboarding correctly skips a bot that
            # already has brave configured by either path.
            oc_brave_key = brave_key_from_oc_config(oc_cfg)
            auth_profiles = (_read_auth_profiles(bot_id) or {}).get("profiles") or {}
            # Match by provider+type fields rather than profile-key name,
            # so existing bots whose key still lives under the legacy
            # `brave_api_key` underscore form get found before the next
            # rotate-time rename. New onboards land under the canonical
            # `brave:api_key`.
            auth_brave = next(
                (
                    p for p in auth_profiles.values()
                    if isinstance(p, dict)
                    and p.get("provider") == "brave"
                    and p.get("type") == "api_key"
                ),
                {},
            )
            auth_brave_key = (auth_brave.get("key") or "").strip() or None
            already_configured = bool(oc_brave_key or auth_brave_key)
            opted_out = current not in (None, "", "brave")
            eligible = not opted_out and not already_configured
            per_bot.append({
                "bot_id": bot_id,
                "eligible": eligible,
                "current_provider": current,
                "already_configured": already_configured,
                "oc_only": bool(oc_brave_key and not auth_brave_key),
            })
        return jsonify({
            "ok": True,
            "brave_ok": brave_ok,
            "status": status,
            "bots": per_bot,
        })

    @app.post("/api/admin/onboard/brave")
    def api_admin_onboard_brave() -> Response:
        """Onboard one or more bots to brave search.

        Body: {key, bots: [bot_id, ...]}

        For each bot: if `tools.web.search.provider` is set to anything other
        than null/'brave', the bot is moved to `skipped` (per v3 design — opt
        out is respected, not an error). For the rest:
          1. _ensure_brave_wired_in_dict() — scaffolds plugin + sets provider
             if currently null
          2. _apply_credential_to_oc_dict() — writes the api key
          3. POST /api/admin/keys/<bot>/brave — also writes to auth-profiles
             (canonical store) so the rotate endpoint sees a consistent state

        Returns {ok, results: [{bot, ok, error, provider_overridden, current_provider}],
                 skipped: [{bot, reason}]}.
        """
        body = request.get_json() or {}
        key = (body.get("key") or "").strip()
        bots = body.get("bots") or []
        if not key:
            return jsonify({"error": "key required"}), 400
        if not isinstance(bots, list) or not bots:
            return jsonify({"error": "bots[] required"}), 400

        results: list[dict] = []
        skipped: list[dict] = []
        for bot_id in bots:
            if not isinstance(bot_id, str) or not bot_id.strip():
                continue
            oc_cfg = _read_oc_json(bot_id) or {}
            current = (oc_cfg.get("tools", {}).get("web", {}).get("search", {}) or {}).get("provider")
            if current not in (None, "", "brave"):
                skipped.append({
                    "bot": bot_id,
                    "reason": f"provider already set to {current}",
                })
                continue
            # 1+2: scaffold + write key into openclaw.json
            wire_info = _ensure_brave_wired_in_dict(oc_cfg)
            _apply_credential_to_oc_dict(oc_cfg, "brave", "api_key", key)
            if not _write_oc_json(bot_id, oc_cfg):
                results.append({
                    "bot": bot_id,
                    "ok": False,
                    "error": "Failed to write openclaw.json",
                })
                continue
            # 3: write to auth-profiles canonically
            auth_data = _read_auth_profiles(bot_id) or {"profiles": {}}
            profiles = auth_data.setdefault("profiles", {})
            profile_id = _canonical_profile_id("brave", "api_key")
            profiles[profile_id] = {
                "provider": "brave",
                "type": "api_key",
                "key": key,
            }
            auth_ok = _write_auth_profiles(bot_id, auth_data)
            _module._audit_log_entry("onboard.brave", bot_id, {
                "provider_overridden": wire_info.get("provider_overridden"),
                "current_provider": wire_info.get("current_provider"),
                "auth_ok": auth_ok,
            }, oc_keys={"plugins", "tools"})
            results.append({
                "bot": bot_id,
                "ok": auth_ok,
                "provider_overridden": wire_info.get("provider_overridden"),
                "current_provider": wire_info.get("current_provider"),
                "requires_restart": True,
                "restart_endpoint": f"/api/admin/gateway/{bot_id}/restart",
            })
        return jsonify({"ok": True, "results": results, "skipped": skipped})

    def _bot_pubkey(bot_id: str) -> str | None:
        """Return the canonical pod-wide backup pubkey.

        Under the unified shared-key model every bot uses the same
        pubkey, so ``bot_id`` is accepted for back-compat but ignored.
        If the canonical source is missing the helper generates it
        (one-time per pod) so the onboarding wizard's first call lands
        a value the caller can register on GitHub. The reconciler is
        the canonical path for explicit generation; this call site is
        the rare "first-time onboarding before Distribute Key was run"
        path.
        """
        from .. import backup_keys
        pub = backup_keys.read_canonical_pubkey()
        if pub:
            return pub
        ok, _generated, err = backup_keys.ensure_shared_source_generated()
        if not ok:
            _log.warning("onboard.github: shared-key generation failed: %s", err)
            return None
        return backup_keys.read_canonical_pubkey()

    def _ensure_deploy_key(token: str, login: str, repo: str, pubkey: str, bot_id: str) -> tuple[bool, bool, str | None]:
        """Thin wrapper around ``backup_keys.ensure_deploy_key_registered``.

        Kept as a local function so the onboarding wizard (which calls
        it dozens of lines below) doesn't need to change its tuple-shape
        contract. The actual GitHub-keys API logic lives in one place —
        the unified backup-keys module — so the onboarding flow and the
        Distribute Key flow can never drift apart again.
        """
        from .. import backup_keys
        result = backup_keys.ensure_deploy_key_registered(
            token, login, repo, pubkey, bot_id,
            github_api=_github_api,
        )
        if result.error:
            return False, False, result.error
        return True, result.added, None

    def _onboard_one_github_bot(
        bot_id: str, token: str, login: str, repo_name: str, reuse_confirmed: bool,
    ) -> dict:
        """Run the per-bot github onboarding loop. Returns a per-bot result dict."""
        # Pubkey is needed for both deploy-key registration and the silent-reuse check.
        pubkey = _bot_pubkey(bot_id)
        if not pubkey:
            return {"bot": bot_id, "ok": False, "error": "could not generate SSH deploy key"}
        target_blob = " ".join(pubkey.split()[:2])

        # Step 3: ensure the github repo exists
        r_status, r_body, _ = _github_api("GET", f"/repos/{login}/{repo_name}", token)
        repo_reused = False
        if r_status == 200 and isinstance(r_body, dict):
            # Repo exists. Check if it already has our deploy key (silent-reuse signal).
            k_status, k_body, _ = _github_api("GET", f"/repos/{login}/{repo_name}/keys", token)
            already_ours = False
            if k_status == 200 and isinstance(k_body, list):
                for k in k_body:
                    existing = " ".join((k.get("key") or "").split()[:2]) if isinstance(k, dict) else ""
                    if existing == target_blob:
                        already_ours = True
                        break
            if not already_ours and not reuse_confirmed:
                return {
                    "bot": bot_id,
                    "ok": False,
                    "error": "collision: repo exists without evolve deploy key",
                    "collision": True,
                }
            repo_reused = True
        elif r_status == 404:
            create_body = {
                "name": repo_name,
                "private": True,
                "auto_init": False,
                "description": f"Evolve workspace backup for {bot_id}",
            }
            # Route the create under the right owner: POST /user/repos creates
            # under the PAT owner regardless of `login`, so for org-targeted
            # creates we must use POST /orgs/{login}/repos. Identify the PAT
            # owner via /user once and compare; treat 404 from /orgs as
            # "login is actually a user and you don't own them" — surfaces a
            # clearer error than letting GitHub silently create under the
            # wrong account (per feature 2026-05-04-002).
            u_status, u_body, _ = _github_api("GET", "/user", token)
            pat_owner = ""
            if u_status == 200 and isinstance(u_body, dict):
                pat_owner = (u_body.get("login") or "").strip()
            if login and pat_owner and login.lower() != pat_owner.lower():
                create_path = f"/orgs/{login}/repos"
            else:
                create_path = "/user/repos"
            c_status, c_body, _ = _github_api("POST", create_path, token, body=create_body)
            if not (200 <= c_status < 300):
                msg = (c_body or {}).get("message") if isinstance(c_body, dict) else None
                return {
                    "bot": bot_id,
                    "ok": False,
                    "error": f"create repo at {create_path} failed ({c_status}): {msg}",
                }
        else:
            return {"bot": bot_id, "ok": False, "error": f"GET /repos returned {r_status}"}

        # Step 4: ensure deploy key is registered
        dk_ok, dk_added, dk_err = _ensure_deploy_key(token, login, repo_name, pubkey, bot_id)
        if not dk_ok:
            return {"bot": bot_id, "ok": False, "error": dk_err or "deploy key registration failed"}

        # Step 5: ensure git remote is initialized
        rem_ok, rem_err = _ensure_github_remote(bot_id, login, repo_name, token)
        if not rem_ok:
            return {"bot": bot_id, "ok": False, "error": f".git/config update failed: {rem_err}"}

        # Step 6: write backupRepoUrl to network.json (SSH form for production push)
        ssh_url = f"git@github.com:{login}/{repo_name}.git"
        try:
            net = load_network(network_path)
            net.setdefault("bots", {}).setdefault(bot_id, {})["backupRepoUrl"] = ssh_url
            save_network(net, network_path)
            backup_url_set = True
        except Exception as exc:
            _log.warning("onboard.github[%s]: save_network failed: %s", bot_id, exc)
            backup_url_set = False

        return {
            "bot": bot_id,
            "ok": True,
            "repo_url": f"https://github.com/{login}/{repo_name}",
            "repo_reused": repo_reused,
            "deploy_key_added": dk_added,
            "backup_url_set": backup_url_set,
        }

    @app.post("/api/admin/onboard/github")
    def api_admin_onboard_github() -> Response:
        """Onboard one or more bots to github backup.

        Body: {
          default: {token, github_login} | null,
          bots: [{bot_id, repo_name, reuse_confirmed: bool,
                  github_login?, override?: {token, github_login}}]
        }

        Per-bot login precedence: override.github_login > entry.github_login >
        default_login. Top-level entry.github_login points the bot at a
        non-default org while reusing the default PAT (feature 2026-05-04-002).

        Server-side preflight: if any bot has an unresolved collision (repo
        exists, lacks evolve deploy key, no `reuse_confirmed: true`), the
        whole request fails with 409 and a per-bot list of unresolved
        collisions. Otherwise the per-bot loop runs idempotently — one bot
        failing doesn't abort siblings.
        """
        body = request.get_json() or {}
        default_creds = body.get("default") or {}
        bots = body.get("bots") or []
        if not isinstance(bots, list) or not bots:
            return jsonify({"error": "bots[] required"}), 400

        default_token_in = (default_creds.get("token") or "").strip()
        default_login_input = (default_creds.get("github_login") or "").strip()
        default_token, _disc_login, _ = _resolve_credential(default_token_in)
        if not default_login_input and _disc_login:
            default_login_input = _disc_login

        # Resolve per-bot creds + repo_name up front; build the work list.
        work_items: list[dict] = []
        cred_errors: list[dict] = []
        for entry in bots:
            if not isinstance(entry, dict):
                continue
            bot_id = (entry.get("bot_id") or "").strip()
            repo_name = (entry.get("repo_name") or "").strip()
            if not bot_id or not repo_name:
                cred_errors.append({"bot": bot_id, "error": "bot_id and repo_name required"})
                continue
            entry_login = (entry.get("github_login") or "").strip()
            override = entry.get("override") or {}
            override_token_in = (override.get("token") or "").strip()
            if override_token_in:
                tok, _ol, _src = _resolve_credential(override_token_in)
                login = (
                    (override.get("github_login") or "").strip()
                    or entry_login
                    or _ol
                    or ""
                )
            else:
                tok = default_token
                login = entry_login or default_login_input
            if not tok or not login:
                cred_errors.append({
                    "bot": bot_id,
                    "error": "no credentials (default missing/expired and no override)",
                })
                continue
            work_items.append({
                "bot_id": bot_id, "repo_name": repo_name, "token": tok,
                "login": login, "reuse_confirmed": bool(entry.get("reuse_confirmed")),
            })

        if cred_errors:
            return jsonify({"error": "credential resolution failed", "errors": cred_errors}), 400

        # Preflight: collision check for every bot before any side effects.
        unresolved: list[dict] = []
        for w in work_items:
            r_status, r_body, _ = _github_api("GET", f"/repos/{w['login']}/{w['repo_name']}", w["token"])
            if r_status == 200 and isinstance(r_body, dict) and not w["reuse_confirmed"]:
                # Check if the existing repo already has our deploy key.
                pub = _bot_pubkey(w["bot_id"])
                target_blob = " ".join((pub or "").split()[:2])
                k_status, k_body, _ = _github_api("GET", f"/repos/{w['login']}/{w['repo_name']}/keys", w["token"])
                already_ours = False
                if k_status == 200 and isinstance(k_body, list) and target_blob:
                    for k in k_body:
                        existing = " ".join((k.get("key") or "").split()[:2]) if isinstance(k, dict) else ""
                        if existing == target_blob:
                            already_ours = True
                            break
                if not already_ours:
                    unresolved.append({
                        "bot": w["bot_id"],
                        "repo": w["repo_name"],
                        "url": r_body.get("html_url"),
                        "last_pushed_at": r_body.get("pushed_at"),
                    })
        if unresolved:
            return jsonify({
                "error": "unresolved collisions; set reuse_confirmed=true per bot",
                "unresolved": unresolved,
            }), 409

        # Per-bot loop — idempotent, errors don't abort siblings.
        results: list[dict] = []
        for w in work_items:
            try:
                r = _onboard_one_github_bot(
                    w["bot_id"], w["token"], w["login"],
                    w["repo_name"], w["reuse_confirmed"],
                )
            except Exception as exc:
                r = {"bot": w["bot_id"], "ok": False, "error": f"unhandled: {exc}"}
            results.append(r)
            _module._audit_log_entry("onboard.github", w["bot_id"], {
                "repo": w["repo_name"], "login": w["login"],
                "ok": r.get("ok"), "reused": r.get("repo_reused"),
            })

        # Persist the verified default PAT to the KEYSTORE so the periodic
        # backup-visibility monitor (analyzer/backup_signal.py, via
        # backup_visibility.load_pat) can confirm each repo is private.
        # Without this, the wizard succeeds (repo created, deploy key
        # registered, .git/config has the token embedded for push) but the
        # monitor still fires "GitHub PAT missing — N bot backup repos
        # cannot be verified" because that monitor reads only the pod-wide
        # slot, not per-bot .git/config. One pod-wide PAT covers all bots
        # whose repos it can read.
        # Until 2026-06-10 (roadmap 2.8 / decision D2) the slot was
        # plaintext network.json::github.pat; it is now the keystore
        # (Keychain when usable, Fernet file vault otherwise), and any
        # legacy plaintext copy is scrubbed in the same step.
        # Pick the token shared across the most successful bots — that's the
        # one likeliest to satisfy the monitor for the whole pod. Default
        # token first when it succeeded; otherwise the per-bot override that
        # covered the most bots.
        pat_to_persist: str | None = None
        token_success_count: dict[str, int] = {}
        for w, r in zip(work_items, results):
            if r.get("ok") and w.get("token"):
                token_success_count[w["token"]] = token_success_count.get(w["token"], 0) + 1
        if default_token and token_success_count.get(default_token, 0) > 0:
            pat_to_persist = default_token
        elif token_success_count:
            pat_to_persist = max(token_success_count, key=token_success_count.get)
        pat_persisted = False
        if pat_to_persist:
            try:
                from ..keystore import store_github_pat
                net = load_network(network_path)
                shared_dir = Path(net.get("sharedDir") or "/Users/Shared/evolve")
                store_github_pat(shared_dir, pat_to_persist)
                pat_persisted = True
                # Scrub any legacy plaintext copy left by pre-2.8 onboarding.
                gh = net.get("github")
                if isinstance(gh, dict) and gh.get("pat"):
                    del gh["pat"]
                    save_network(net, network_path)
            except Exception as exc:
                _log.warning("onboard.github: failed to persist github PAT: %s", exc)

        return jsonify({
            "ok": True,
            "results": results,
            "pat_persisted": pat_persisted,
        })

    # ── Google Workspace OAuth ────────────────────────────────────────────
    #
    # Five routes implement the dashboard-driven OAuth flow:
    #   POST /api/admin/onboard/google/configure  — first-run GCP client setup
    #   POST /api/admin/onboard/google/begin      — build the authorize URL
    #   GET  /api/admin/onboard/google/callback   — exchange code for tokens
    #   POST /api/admin/onboard/google/poll       — wait for callback by state
    #   POST /api/admin/onboard/google/revoke     — revoke + clear local profile
    #
    # The refresh path is _ensure_fresh_google_access_token (called by the
    # keys API on every render so the row's status reflects refresh failures
    # within one access-token lifetime, ~1h).

    # Legacy: client_secret used to live as a profile entry inside the bot's
    # auth-profiles.json. That file is also openclaw's territory — the
    # gateway's in-memory cache periodically rewrites it from its own state
    # and silently drops anything it doesn't recognize, including our
    # ``_evolve_google_oauth_client`` profile. Storing Evolve credentials
    # there is a layering violation: we don't own the file. PR fixing Bug 7
    # moves the secret to the Evolve-owned credential store under
    # ``{shared_dir}/credentials/<secret_bot>/google_oauth_client.json``.
    # The auth-profiles read path below stays only to support backward-compat
    # reads during the migration window. GOOGLE_CLIENT_SECRET_PROFILE_ID lifted
    # to routes_admin_shared.py (4.1b Increment 0).

    def _google_oauth_client_store_path(secret_bot: str) -> Path:
        # Shim → lifted helper, binding this app's network_path.
        return _shared_google_oauth_client_store_path(secret_bot, network_path)

    def _read_google_oauth_client_secret_from_store(secret_bot: str) -> dict | None:
        # Shim → lifted helper, binding this app's network_path.
        return _shared_read_google_oauth_client_secret_from_store(secret_bot, network_path)

    def _write_google_oauth_client_to_store(
        secret_bot: str, client_id: str, client_secret: str,
    ) -> tuple[bool, str | None]:
        """Persist the OAuth client credentials for ``secret_bot`` to the
        Evolve credential store. Atomic write (temp file + rename) under
        ``{shared_dir}/credentials/<secret_bot>/`` with 0o600 perms.

        Returns ``(ok, error_or_None)``. Failure modes: parent dir
        not creatable, write fails, rename fails.
        """
        if not client_id or not client_secret:
            return False, "client_id and client_secret are required"
        path = _google_oauth_client_store_path(secret_bot)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as e:
            return False, f"cannot create {path.parent}: {e}"
        payload = {
            "schema_version": 1,
            "client_id": client_id,
            "client_secret": client_secret,
            "saved_at": _now_iso(),
        }
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                # Permission bits are best-effort; the parent dir's
                # ownership is the real gate.
                pass
            os.replace(tmp, path)
            return True, None
        except (PermissionError, OSError) as e:
            return False, f"write failed: {e}"

    def _read_google_oauth_client(bot_id: str | None = None) -> dict | None:
        # Shim → lifted helper (routes_admin_shared._read_google_oauth_client).
        # References _read_auth_profiles in the body (not as a default arg) so a
        # test that swaps this closure's _read_auth_profiles cell is honored at
        # call time — preserving the §1.3 monkeypatch-at-call-time contract.
        return _shared_read_google_oauth_client(
            bot_id, network_path=network_path, read_auth_profiles=_read_auth_profiles,
        )

    def _resolve_oauth_client_dict(
        cfg: dict, *, default_secret_bot: str | None,
    ) -> dict | None:
        # Shim → lifted helper, threading network_path + the late-bound reader.
        return _shared_resolve_oauth_client_dict(
            cfg, default_secret_bot=default_secret_bot,
            network_path=network_path, read_auth_profiles=_read_auth_profiles,
        )

    def _save_google_oauth_client(client_id: str, client_secret: str, secret_bot: str) -> tuple[bool, str | None]:
        """Persist a per-bot Google OAuth client.

        The ``client_id`` (non-secret) goes in
        ``network.bots[secret_bot].googleOAuthClient``. The secret goes
        in the Evolve credential store at
        ``{shared_dir}/credentials/<secret_bot>/google_oauth_client.json``
        — an Evolve-owned path that openclaw's auth-profiles
        management never touches. Returns ``(ok, error_or_None)``.

        ``secret_bot`` is load-bearing: under the per-bot redesign
        every bot has its own OAuth app credentials by default.
        Sharing one app across bots is opt-in — set
        ``network.bots[<other>].googleOAuthClient.secret_bot`` to the
        bot that owns the credentials.
        """
        if not client_id or not client_secret:
            return False, "client_id and client_secret required"
        # 1) Write the secret to the Evolve credential store. This is
        # the load-bearing change from the original implementation,
        # which wrote into auth-profiles.json — that file is openclaw's
        # territory and its in-memory cache rewrites silently dropped
        # Evolve's entries (the bug that motivated this PR).
        ok, err = _write_google_oauth_client_to_store(
            secret_bot, client_id, client_secret,
        )
        if not ok:
            return False, f"credential-store write failed: {err}"

        # 2) Write client_id + secret_bot pointer into network.json.
        # ``secret_bot`` defaults to the bot key when reading, so the
        # explicit field is only needed for sharing — keep it explicit
        # here for clarity / round-trip stability.
        try:
            net = load_network(network_path)
            bots = net.setdefault("bots", {})
            if not isinstance(bots, dict):
                return False, "network.bots is not a dict"
            bot_cfg = bots.setdefault(secret_bot, {})
            if not isinstance(bot_cfg, dict):
                return False, f"network.bots[{secret_bot}] is not a dict"
            bot_cfg["googleOAuthClient"] = {
                "mode": "self_hosted",
                "client_id": client_id,
                "secret_bot": secret_bot,
            }
            save_network(net, network_path)
        except Exception as exc:
            return False, f"network.json write failed: {exc}"
        return True, None

    def _read_google_oauth_profile(bot_id: str) -> dict | None:
        # Shim → lifted helper, threading the late-bound auth-profiles reader.
        return _shared_read_google_oauth_profile(
            bot_id, read_auth_profiles=_read_auth_profiles,
        )

    def _write_google_oauth_profile(bot_id: str, profile: dict) -> bool:
        """Save the bot's Google OAuth profile. Always rewrites the entire
        auth-profiles.json file via /tmp staging (per CLAUDE.md).
        """
        auth = _read_auth_profiles(bot_id) or {"profiles": {}}
        profiles = auth.setdefault("profiles", {})
        profiles[_google_oauth_profile_id(bot_id)] = profile
        return _write_auth_profiles(bot_id, auth)

    def _delete_google_oauth_profile(bot_id: str) -> bool:
        # Shim → lifted helper, threading the late-bound reader + writer.
        return _shared_delete_google_oauth_profile(
            bot_id,
            read_auth_profiles=_read_auth_profiles,
            write_auth_profiles=_write_auth_profiles,
        )

    def _scopes_to_services(scopes: list[str]) -> list[str]:
        """Reverse-map a list of granted scopes to the service ids in
        _GOOGLE_SCOPE_REGISTRY. A service is "granted" iff every scope it
        registers is present in the input list.
        """
        granted: list[str] = []
        for service_id, meta in _GOOGLE_SCOPE_REGISTRY.items():
            req = set(meta["scopes"])
            if req.issubset(set(scopes)):
                granted.append(service_id)
        return granted

    def _services_to_scopes(services: list[str]) -> list[str]:
        """Forward-map a list of service ids to the union of their scopes,
        plus the OIDC base scopes (so we always get an id_token + email)."""
        out: set[str] = set(GOOGLE_OAUTH_BASE_SCOPES)
        for s in services:
            meta = _GOOGLE_SCOPE_REGISTRY.get(s)
            if meta:
                out.update(meta["scopes"])
        return sorted(out)

    def _ensure_fresh_google_access_token(bot_id: str) -> tuple[str | None, str | None]:
        # Shim → lifted helper, threading network_path + the late-bound reader
        # and the OAuth-profile writer (which itself rewrites auth-profiles).
        return _shared_ensure_fresh_google_access_token(
            bot_id,
            network_path=network_path,
            read_auth_profiles=_read_auth_profiles,
            write_google_oauth_profile=_write_google_oauth_profile,
        )

    @app.post("/api/admin/onboard/google/configure")
    def api_admin_onboard_google_configure() -> Response:
        """First-run GCP client config. Body:
            {client_id, client_secret, secret_bot?}
        `secret_bot` defaults to network.primary.

        Returns {ok: bool, error?}.
        """
        body = request.get_json() or {}
        client_id = (body.get("client_id") or "").strip()
        client_secret = (body.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            return jsonify({"error": "client_id and client_secret required"}), 400
        net = load_network(network_path)
        secret_bot = (body.get("secret_bot") or net.get("primary") or "").strip()
        if not secret_bot:
            return jsonify({"error": "secret_bot required (no network.primary set)"}), 400
        ok, err = _save_google_oauth_client(client_id, client_secret, secret_bot)
        if not ok:
            return jsonify({"error": err or "save failed"}), 500
        _module._audit_log_entry("onboard.google.configure", "admin", {
            "secret_bot": secret_bot, "client_id_suffix": client_id[-12:],
        })
        return jsonify({"ok": True})

    @app.post("/api/admin/onboard/google/begin")
    def api_admin_onboard_google_begin() -> Response:
        """Build a Google authorization URL for a bot + selected services.

        Body: {bot_id, services: [str]}. Returns {authorize_url, state, scopes}.
        Returns 412 if `googleOAuthClient` not configured.
        """
        body = request.get_json() or {}
        bot_id = (body.get("bot_id") or "").strip()
        services = body.get("services") or []
        if not bot_id:
            return jsonify({"error": "bot_id required"}), 400
        if not isinstance(services, list) or not services:
            return jsonify({"error": "services[] required"}), 400
        # Validate service ids
        unknown = [s for s in services if s not in _GOOGLE_SCOPE_REGISTRY]
        if unknown:
            return jsonify({"error": f"unknown services: {unknown}"}), 400
        # Per-bot OAuth client (with legacy pod-level fallback handled by reader)
        client = _read_google_oauth_client(bot_id)
        if not client:
            return jsonify({
                "error": "google_oauth_client_not_configured",
                "hint": (
                    f"Run `evo setup-google` from {bot_id!r} (or use the "
                    f"admin Plugins/Credentials tab) to wire this bot's "
                    f"OAuth app first."
                ),
            }), 412
        scopes = _services_to_scopes(services)
        # Build redirect_uri from the request host so localhost / mini both work.
        # The wizard has already nudged the user to register this URI in GCP.
        host = request.headers.get("Host") or "localhost:5050"
        scheme = request.headers.get("X-Forwarded-Proto") or ("https" if request.is_secure else "http")
        redirect_uri = f"{scheme}://{host}/api/admin/onboard/google/callback"
        state = _google_state_create(bot_id, services, scopes, redirect_uri)
        import urllib.parse as _up
        params = {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            # `prompt=consent` always: without it Google omits the refresh token
            # for previously-consented scopes — see design risks/§11.
            "prompt": "consent",
            "state": state,
        }
        authorize_url = f"{GOOGLE_AUTHORIZE_URL}?{_up.urlencode(params)}"
        # V2.4-4: emit standardised oauth_started events for each Google skill
        # that the requested services correspond to.  The same Google OAuth
        # credential backs gog (combined), gmail-only, and calendar-only skills.
        # We emit one event per skill whose services are included in the request.
        try:
            from ..oauth import audit_log_provider_event as _alf
            _svc_set = set(services)
            _google_skill_ids: list[str] = []
            # gog covers both gmail + calendar services together
            if _svc_set & {"gmail_readonly", "gmail"}:
                _google_skill_ids.append("gmail")
            if _svc_set & {"calendar_readonly", "calendar"}:
                _google_skill_ids.append("calendar")
            # emit for gog whenever either gmail or calendar is requested
            # (gog = Gmail & Calendar combined skill)
            if _google_skill_ids:
                _google_skill_ids.insert(0, "gog")
            for _gsid in _google_skill_ids:
                _alf(_gsid, bot_id, "oauth_started", {
                    "services": list(_svc_set),
                    "scopes": scopes,
                })
        except Exception:
            pass
        return jsonify({
            "ok": True,
            "authorize_url": authorize_url,
            "state": state,
            "scopes": scopes,
            "redirect_uri": redirect_uri,
        })

    def _close_tab_html(message: str, ok: bool) -> str:
        """Tiny self-closing HTML returned to the OAuth popup. Posts a message
        to the opener (so the wizard can react instantly) and closes the tab."""
        color = "#34a853" if ok else "#ea4335"
        icon = "✅" if ok else "❌"
        # message goes through json.dumps to avoid HTML/JS injection if
        # Google ever sends adversarial error params.
        msg_json = _json.dumps(message)
        ok_json = _json.dumps(ok)
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Google authorization</title>"
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
            "background:#0a0a0a;color:#eee;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0}"
            f".box{{text-align:center;padding:32px;border-radius:8px;border:1px solid #333}}"
            f".icon{{font-size:48px;margin-bottom:12px}}"
            f".msg{{color:{color}}}"
            "</style></head><body>"
            f"<div class='box'><div class='icon'>{icon}</div>"
            f"<div class='msg'>{message}</div>"
            "<div style='font-size:0.78rem;color:#888;margin-top:12px'>"
            "You can close this tab.</div></div>"
            "<script>"
            "try{if(window.opener){window.opener.postMessage("
            f"{{type:'google-oauth',ok:{ok_json},message:{msg_json}}},'*');}}"
            "}catch(e){}"
            "setTimeout(function(){try{window.close();}catch(e){}},800);"
            "</script></body></html>"
        )

    @app.get("/api/admin/onboard/google/callback")
    def api_admin_onboard_google_callback() -> Response:
        """OAuth redirect target. Exchanges `code` for tokens and writes the
        bot's OAuth profile to auth-profiles.json. Always returns a small HTML
        page that closes the popup; result is delivered to the wizard via the
        /poll endpoint or the postMessage from the closing tab.
        """
        state = request.args.get("state") or ""
        code = request.args.get("code") or ""
        error = request.args.get("error") or ""

        if not state:
            return Response(_close_tab_html("Missing state", False), mimetype="text/html"), 400
        entry = _google_state_get(state)
        if not entry:
            return Response(_close_tab_html("Unknown or expired state", False), mimetype="text/html"), 400

        if error:
            _google_state_set_result(state, {
                "status": "denied" if error == "access_denied" else "error",
                "error": error,
            })
            _module._audit_log_entry("onboard.google.callback", entry.get("bot_id") or "?", {
                "ok": False, "error": error,
            })
            human = "Authorization denied" if error == "access_denied" else f"Google error: {error}"
            return Response(_close_tab_html(human, False), mimetype="text/html"), 200

        if not code:
            _google_state_set_result(state, {"status": "error", "error": "no_code"})
            return Response(_close_tab_html("No code returned", False), mimetype="text/html"), 400

        # Per-bot OAuth client. The state token already carries the
        # target bot id; pass it so the reader resolves the right
        # client_id / secret pair (per-bot under the new model, or
        # legacy pod-level if this bot hasn't migrated yet).
        bot_id = entry["bot_id"]
        services = entry.get("services") or []
        client = _read_google_oauth_client(bot_id)
        if not client:
            _google_state_set_result(state, {"status": "error", "error": "client_not_configured"})
            return Response(_close_tab_html("OAuth client not configured", False), mimetype="text/html"), 500
        redirect_uri = entry.get("redirect_uri") or ""
        status, body = _module._google_token_exchange(code, client["client_id"], client["client_secret"], redirect_uri)
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            err = (body or {}).get("error") if isinstance(body, dict) else "exchange_failed"
            _google_state_set_result(state, {"status": "error", "error": f"token_exchange_{status}_{err}"})
            return Response(_close_tab_html(f"Token exchange failed ({err})", False), mimetype="text/html"), 200

        access = body["access_token"]
        refresh = body.get("refresh_token") or ""
        if not refresh:
            # Without a refresh token we can't keep the bot working past the
            # access token's lifetime — treat as a hard failure so the user
            # can retry the wizard (likely needs to revoke previous consent
            # at https://myaccount.google.com/permissions first).
            _google_state_set_result(state, {"status": "error", "error": "no_refresh_token"})
            return Response(_close_tab_html(
                "Google did not return a refresh token. "
                "Try again, or revoke previous consent first at "
                "https://myaccount.google.com/permissions.",
                False,
            ), mimetype="text/html"), 200

        scope_str = body.get("scope") or " ".join(entry.get("scopes") or [])
        granted_scopes = [s for s in scope_str.split() if s]
        # Resolve the Google account email from userinfo.
        u_status, u_body = _module._google_userinfo(access)
        google_account = ""
        if u_status == 200 and isinstance(u_body, dict):
            google_account = u_body.get("email") or ""

        import time
        now = time.time()
        prev = _read_google_oauth_profile(bot_id) or {}
        prof = {
            "provider": "google_workspace",
            "type": "oauth",
            "google_account": google_account or prev.get("google_account", ""),
            "scopes": granted_scopes,
            "services": services,
            "access_token": access,
            "access_token_expires_at": now + int(body.get("expires_in") or 3500),
            "refresh_token": refresh,
            "issued_at": now,
            "status": "active",
            "_evolve_prev_refresh_token": prev.get("refresh_token", ""),
            "_evolve_prev_access_token": prev.get("access_token", ""),
        }
        if not _write_google_oauth_profile(bot_id, prof):
            _google_state_set_result(state, {"status": "error", "error": "auth_profile_write_failed"})
            return Response(_close_tab_html("Could not save credentials", False), mimetype="text/html"), 500

        # Kickstart the gateway so it picks up the new Google credentials.
        # Best-effort: surface the failure but keep the install otherwise
        # complete (credentials are already on disk). Mirrors the slack/discord
        # OAuth callback pattern — without this, fresh Gmail/Calendar/GOG
        # installs sit dormant until the next deploy or a manual launchctl
        # restart. P1 from docs/design/skills-install-roadmap-2026-05-30.md.
        from ..skills import _oc_install_common as _oc_common
        kick_ok, kick_err = _oc_common.kickstart_gateway(bot_id)

        granted_services = _scopes_to_services(granted_scopes)
        _google_state_set_result(state, {
            "status": "success",
            "bot_id": bot_id,
            "google_account": google_account,
            "granted_services": granted_services,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })
        _module._audit_log_entry("onboard.google.callback", bot_id, {
            "ok": True,
            "google_account": google_account,
            "granted_services": granted_services,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })
        # V2.4-4: emit standardised activated events for each Google skill.
        try:
            from ..oauth import audit_log_provider_event as _alf
            _svc_set = set(granted_services)
            _act_skill_ids: list[str] = []
            if _svc_set & {"gmail_readonly", "gmail"}:
                _act_skill_ids.append("gmail")
            if _svc_set & {"calendar_readonly", "calendar"}:
                _act_skill_ids.append("calendar")
            if _act_skill_ids:
                _act_skill_ids.insert(0, "gog")
            for _gsid in _act_skill_ids:
                _alf(_gsid, bot_id, "activated", {
                    "google_account": google_account,
                    "granted_services": list(_svc_set),
                })
        except Exception:
            pass
        services_str = ", ".join(granted_services) or "(no services granted)"
        return Response(_close_tab_html(
            f"Authorized {google_account or bot_id} — {services_str}",
            True,
        ), mimetype="text/html")

    @app.post("/api/admin/onboard/google/poll")
    def api_admin_onboard_google_poll() -> Response:
        """Poll for callback completion by state token.

        Body: {state}. Returns {pending: true} or the result dict
        ({status: success|denied|error, ...}). Successful/terminal results
        consume the state so a single flow can't be polled to completion twice.
        """
        body = request.get_json() or {}
        state = (body.get("state") or "").strip()
        if not state:
            return jsonify({"error": "state required"}), 400
        entry = _google_state_get(state)
        if not entry:
            return jsonify({"status": "expired"}), 410
        result = entry.get("result") or {"status": "pending"}
        if result.get("status") == "pending":
            return jsonify({"pending": True})
        # Terminal — consume and return.
        _google_state_consume(state)
        return jsonify(result)

    @app.post("/api/admin/onboard/google/revoke")
    def api_admin_onboard_google_revoke() -> Response:
        """Revoke the bot's OAuth consent and clear local profile.

        Body: {bot_id}. Returns {ok, revoked: bool, error?}. Always clears the
        local profile, even if the remote revoke fails — Google will revoke
        next time the refresh token is used anyway.
        """
        body = request.get_json() or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"error": "bot_id required"}), 400
        prof = _read_google_oauth_profile(bot_id) or {}
        revoked = False
        revoke_error = None
        token = prof.get("refresh_token") or prof.get("access_token") or ""
        if token:
            status, _ = _google_token_revoke(token)
            revoked = 200 <= status < 300
            if not revoked:
                revoke_error = f"google revoke returned {status}"
        cleared = _delete_google_oauth_profile(bot_id)
        _module._audit_log_entry("onboard.google.revoke", bot_id, {
            "remote_revoked": revoked, "local_cleared": cleared,
        })
        # V2.4-4: emit standardised revoked events for all three Google skills.
        try:
            from ..oauth import audit_log_provider_event as _alf
            for _gsid in ("gog", "gmail", "calendar"):
                _alf(_gsid, bot_id, "revoked", {
                    "remote_revoked": revoked, "local_cleared": cleared,
                })
        except Exception:
            pass
        out = {"ok": cleared, "revoked": revoked}
        if revoke_error:
            out["revoke_error"] = revoke_error
        return jsonify(out)

    @app.get("/api/admin/onboard/google/status")
    def api_admin_onboard_google_status() -> Response:
        """Pod-level status: is the GCP client configured? Used by the wizard
        to decide whether to show the first-run setup screen."""
        client = _read_google_oauth_client()
        net = load_network(network_path)
        cfg = net.get("googleOAuthClient") or {}
        # ``secret_bot`` lives on the new shape directly; falls back to
        # the legacy ``client_secret_ref.bot`` for installs that haven't
        # migrated to the Evolve credential store yet.
        secret_bot = cfg.get("secret_bot") or (cfg.get("client_secret_ref") or {}).get("bot")
        out = {
            "configured": bool(client),
            "mode": cfg.get("mode") or "self_hosted",
            "client_id_suffix": (client["client_id"][-12:]) if client else None,
            "secret_bot": secret_bot,
            "services": [
                {
                    "id": sid,
                    "label": meta["label"],
                    "default_on": meta["default_on"],
                    "advanced": meta["advanced"],
                    "restricted": meta["restricted"],
                }
                for sid, meta in _GOOGLE_SCOPE_REGISTRY.items()
            ],
        }
        return jsonify(out)

    # ── Skills install flow (Spec 11 — GOG skill) ──────────────────────────
    # These routes live inside _register_admin_routes because they re-use the
    # closure-scoped Google OAuth helpers (_read_google_oauth_client,
    # _read_google_oauth_profile, _read_auth_profiles). The skill abstraction
    # itself lives in evolve_admin.skills.gog_install — this is the route
    # layer that wires the readers to it.
    #
    # Trust-chain notes (HIGH-STAKES):
    #   - Tokens are stored only on the bot via _write_google_oauth_profile
    #     (which uses /tmp + sudo /bin/cp per CLAUDE.md). This module never
    #     handles raw tokens.
    #   - The plain-language access panel is in gog_install.GOG_ACCESS_PANEL
    #     and is surfaced verbatim by /api/skills/<id>; the UI must not
    #     re-encode it.
    #   - Plugin enable goes through the standard EnablePluginEntry applier
    #     pipeline → security_warden gates apply unchanged.

    from ..skills import gog_install as _gog
    from ..skills import slack_install as _slack
    from ..skills import imessage_install as _imessage
    # Platform-aware channel honesty (design-linux-port §8): entries whose
    # registry declares ``platforms`` are only offered when the pod host's
    # platform profile matches (data-driven — no skill names in the logic).
    from ..skills import supported_on_host as _skill_supported_on_host
    from ..skills import whatsapp_install as _whatsapp
    from ..skills import signal_install as _signal
    from ..skills import discord_install as _discord
    from ..skills import telegram_install as _telegram
    from ..skills import upstream_plugin_skills as _upstream
    from ..skills import apple_local_install as _apple_local
    from ..skills import autocad_install as _autocad
    # Obsidian was withdrawn 2026-05-30 along with home_assistant / notion /
    # linear / runway because the paste-token install wrote a file no code
    # consumed at runtime. It's the first one re-wired as an MCP install
    # (catalog_id=filesystem + extra_args=[vault_path] + ACL grant). The
    # other four stay withdrawn for now — see
    # docs/design/paste-token-skills-future-2026-05-30.md.
    from ..skills import obsidian_install as _obsidian
    # Dropbox follows the Obsidian pattern (catalog_id=filesystem + extra_args
    # + OS-ACL mode toggle). See docs/design/skills-install-roadmap-2026-05-30.md
    # for the per-skill plan and dropbox_install.py for the design rationale.
    from ..skills import dropbox_install as _dropbox
    # Notion is the THIRD MCP-backed install + the FIRST that's not a
    # filesystem skill. It uses catalog_id=notion (NOT filesystem) +
    # env_bindings with the OPENAPI_MCP_HEADERS keystore reference. The
    # wrapper route hides the JSON headers construction from operators —
    # they paste a plain Internal Integration Secret. See notion_install.py
    # for the design rationale.
    from ..skills import notion_install as _notion
    # Runway is the FIRST bundled-plugin install — distinct pattern from
    # the MCP installs (Obsidian / Dropbox / Notion / GitHub-MCP / Linear).
    # OC ships @openclaw/runway-provider internally; install is purely
    # auth-profiles.json + openclaw.json config + kickstart. The same
    # pattern applies to any future bundled OC provider (Google Veo,
    # Synthesia, etc.). See runway_install.py for the design rationale.
    from ..skills import runway_install as _runway
    # Linear is the FIFTH MCP-backed install (and the second API-key one
    # after Notion). Difference from Notion: linear-mcp takes a plain
    # ``LINEAR_API_KEY`` env var — no JSON headers blob, so the keystore
    # stores the verbatim secret (mirrors GitHub-MCP's verbatim-PAT shape).
    # The MCP server is community ``linear-mcp`` by dvcrn (MIT, candidate
    # vetting status — see catalog.py vetting_notes). Linear is NOT
    # bundled in OC's dist/extensions/, so the MCP path is correct here
    # (vs the bundled-plugin pattern Runway uses).
    from ..skills import linear_install as _linear
    # Google Workspace (Write) — the FIRST install module to close F4 (runtime
    # consumer exists) for Google Workspace. The withdrawn `gog` family
    # (2026-05-30 audit) wrote real OAuth tokens but no OC consumer read
    # them; this module wraps `taylorwilsdon/google_workspace_mcp` (PyPI:
    # workspace-mcp) via InstallMcpServer. See:
    #   * docs/spec-google-workspace-suite-2026-06-04.md
    #   * docs/vetting-workspace-mcp-2026-06-04.md
    # The Read skill (deferred to spec Phase 2) will share this module's
    # `complete_install` path with a narrower scope set + `--read-only`
    # extra_args; same MCP server, same server_id, replaces in place.
    from ..skills import google_workspace_write_install as _gws_write
    # Unified "google" skill — replaces the legacy gog/_read/_write
    # split. The Read module was removed as vestigial (post-PR-#2231
    # cleanup); _write stays because google_install.py imports its
    # shared infrastructure (CompletionResult, keystore helpers,
    # preflight_check). See:
    #   * docs/spec-google-workspace-suite-2026-06-04.md §10.2
    #   * skills/google_install.py for the capability framework
    from ..skills import google_install as _google

    # Route renamed from /api/skills to /api/skills/catalog in PR #1033 to
    # avoid colliding with /api/skills/<bot_id> (per-bot inventory).
    @app.get("/api/skills/catalog")
    def api_skills_catalog_list() -> Response:
        """Return the registry of known skills (id, display, summary).

        MVP ships only GOG; the registry is forward-looking so the Spec 12
        skills inventory view can iterate skills cleanly when more land.

        Route is namespaced under /catalog/ to avoid colliding with
        /api/skills/<bot_id> from the Spec 12 inventory endpoints.
        """
        skills = []
        # Unified "google" skill — replaces gog / google_workspace_read /
        # google_workspace_write as the catalog presentation. The wizard
        # negotiates per-capability scope; legacy gog-installed bots are
        # picked up by the resolver and reported under this row.
        google_reg = _google.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": google_reg["id"],
            "display_name": google_reg["display_name"],
            "summary": google_reg["summary"],
            # Carry the category through so the frontend groups under
            # "Productivity" instead of falling through to "Other".
            "category": google_reg.get("category", "other"),
            "default_capabilities": list(google_reg.get("default_capabilities", [])),
        })
        # Slack (V2.1-2) — uses SKILL_REGISTRY_ENTRY shape (no plugin_name/provider_id)
        slack_reg = _slack.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": slack_reg["id"],
            "display_name": slack_reg["display_name"],
            "summary": slack_reg["summary"],
            "default_scopes": list(slack_reg.get("default_scopes", [])),
        })
        # iMessage — RE-ADDED 2026-06-04 with the bundled-plugin rewire
        # (see docs/openclaw-coverage-audit-2026-06-04.md). Wiring now
        # goes through OC's bundled @openclaw/imessage plugin via
        # channels.imessage block + plugins.entries.imessage.enabled.
        # The home-rolled poller / send helper are deprecated dead code.
        # Status only reports ``active`` when the live OC probe returns
        # connected — never from config presence alone.
        imessage_reg = _imessage.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": imessage_reg["id"],
            "display_name": imessage_reg["display_name"],
            "summary": imessage_reg["summary"],
            "kind": imessage_reg.get("kind"),
            # Platform constraint rides through as data so the generic
            # filter below (and any API consumer) can act on it.
            "platforms": list(imessage_reg.get("platforms") or []),
        })
        # WhatsApp — added 2026-06-04 (Phase 1.2 of the OC coverage audit).
        # Wires OC's @openclaw/whatsapp bundled-clawhub plugin via QR pairing
        # (Baileys / WhatsApp Web). Multi-channel sibling to imessage; same
        # status-correctness rule (active only after live probe).
        whatsapp_reg = _whatsapp.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": whatsapp_reg["id"],
            "display_name": whatsapp_reg["display_name"],
            "summary": whatsapp_reg["summary"],
        })
        # Signal — added 2026-06-04 (Phase 1.3 of the OC coverage audit).
        # **LICENSING REVIEW REQUIRED BEFORE MERGE** — see signal_install
        # module docstring. Wires OC's @openclaw/signal bundled-clawhub
        # plugin via signal-cli linked-device QR pairing.
        signal_reg = _signal.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": signal_reg["id"],
            "display_name": signal_reg["display_name"],
            "summary": signal_reg["summary"],
        })
        # Discord (V2.3-2) — OAuth2 bot-token skill, guild-invite flow
        discord_reg = _discord.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": discord_reg["id"],
            "display_name": discord_reg["display_name"],
            "summary": discord_reg["summary"],
            "default_scopes": list(discord_reg.get("default_scopes", [])),
        })
        # Telegram (V2.3-1) — BotFather token skill, no OAuth dance
        tg_reg = _telegram.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": tg_reg["id"],
            "display_name": tg_reg["display_name"],
            "summary": tg_reg["summary"],
        })
        # Obsidian — rewired 2026-05-30 as the first paste-token-skill ported
        # to the MCP install pipeline. Catalog stays "filesystem" + extra_args=
        # [vault_path] + ACL grant for read/read_write. The other four
        # withdrawn skills (home_assistant / notion / linear / runway) are
        # still off the list — see docs/design/paste-token-skills-future-2026-05-30.md.
        obs_reg = _obsidian.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": obs_reg["id"],
            "display_name": obs_reg["display_name"],
            "summary": obs_reg["summary"],
            "kind": obs_reg.get("kind"),
            "config_keys": list(obs_reg.get("config_keys", [])),
        })
        # Dropbox — second filesystem-MCP rewire (2026-05-30). Same shape as
        # Obsidian — see docs/design/skills-install-roadmap-2026-05-30.md.
        # Previously listed via upstream_plugin_skills with no real install
        # path; now installs via /api/skills/install/dropbox/set-folder-path.
        dbx_reg = _dropbox.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": dbx_reg["id"],
            "display_name": dbx_reg["display_name"],
            "summary": dbx_reg["summary"],
            "kind": dbx_reg.get("kind"),
            "config_keys": list(dbx_reg.get("config_keys", [])),
        })
        # Notion — third MCP rewire (first non-filesystem). API-key skill;
        # credential lives in pod keystore (per-bot slot notion-<bot>). The
        # MCP server is @notionhq/notion-mcp-server. Install via
        # /api/skills/install/notion/set-token (plain Internal Integration
        # Secret paste; wrapper builds the JSON headers blob).
        notion_reg = _notion.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": notion_reg["id"],
            "display_name": notion_reg["display_name"],
            "summary": notion_reg["summary"],
        })
        # Runway — first BUNDLED-PLUGIN rewire (distinct from the MCP
        # pattern). OC ships @openclaw/runway-provider internally; the
        # install writes auth-profiles.json + openclaw.json model
        # default + kickstarts. No MCP server, no keystore. Install via
        # /api/skills/install/runway/set-token (plain API key paste).
        runway_reg = _runway.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": runway_reg["id"],
            "display_name": runway_reg["display_name"],
            "summary": runway_reg["summary"],
        })
        # Linear — fifth MCP rewire (second API-key one). Per-bot keystore
        # slot ``linear-<bot>`` carrying the verbatim PAT. MCP server is
        # the community ``linear-mcp`` package (dvcrn/MIT — vetting_status
        # ``candidate`` until ~2 weeks of real-bot use). Install via
        # /api/skills/install/linear/set-token (plain PAT paste).
        linear_reg = _linear.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": linear_reg["id"],
            "display_name": linear_reg["display_name"],
            "summary": linear_reg["summary"],
        })
        # NOTE: the two split google_workspace_{read,write} catalog
        # entries are intentionally NOT listed here. They were collapsed
        # into the unified ``google`` entry above (see PR #2154 review).
        # The install modules stay on disk as internal helpers; the
        # catalog list, status, plan, complete, and revoke dispatchers
        # all route through ``_google`` instead.

        # Upstream-OpenClaw-plugin skills (Brave, GitHub, Drive).
        # These ride on plugins that the deploy step already wires; the
        # catalog entry surfaces them so users can discover + check install
        # state per bot.
        for _us in _upstream.SKILLS.values():
            skills.append({
                "id": _us.id,
                "display_name": _us.display_name,
                "summary": _us.summary,
            })
        # Apple Contacts + Calendar — WITHDRAWN 2026-05-30 (Phase 1c of
        # the deep skills audit; see docs/skills-deep-audit-2026-05-30.md).
        # Classic probe-is-the-only-consumer dead-end: install grants TCC
        # for the evolve service user, but no bot has a plugin / MCP /
        # channel entry that consumes Contacts/Calendar/Reminders/Notes.
        # Verified live on all 7 pod bots: zero relevant entries; OC
        # bundle has no apple-* extension; packages/plugin/src has zero
        # AppleScript wrappers; TCC is per-user so even if a consumer
        # existed, grants on `evolve` wouldn't help team_bot_a/team_bot_c/etc. Access
        # panel promised 5 capabilities, all false post-install.
        # Re-add via either: (a) apple-mcp-server in mcp_admin/catalog.py
        # rewired through InstallMcpServer per-bot with per-bot-user TCC
        # grants, OR (b) Apple tool surfaces in packages/plugin/src using
        # osascript wrappers (mirror imessage_helpers.py shape).
        # AutoCAD / APS — v1 ships as a catalog stub; full OAuth flow lands
        # in a follow-up PR (see autocad_install module docstring).
        autocad_reg = _autocad.SKILL_REGISTRY_ENTRY
        skills.append({
            "id": autocad_reg["id"],
            "display_name": autocad_reg["display_name"],
            "summary": autocad_reg["summary"],
        })

        # ── Platform filter (design-linux-port-2026-06-10.md §8) ─────────────
        # Channel-matrix honesty: an entry that declares ``platforms`` is
        # offered only when the pod host's platform profile matches — a
        # Linux pod never renders an iMessage card (the dead-affordance
        # anti-pattern). The constraint is catalog data on the registry
        # entries (carried through the dicts above); this is the single
        # filter point for the list surface. Entries without the field are
        # platform-neutral and pass untouched.
        skills = [s for s in skills if _skill_supported_on_host(s)]

        # ── Categorical metadata (2026-06-04 catalog-UX polish) ───────────────
        # Skills are appended in chronological add-order above (gog first,
        # autocad last). The pre-2026-06-04 UI rendered them in exactly that
        # order, which read as random. Inject a `category` field + a
        # `category_order` integer here so the UI can group by section and
        # sort within section alphabetically. Categories chosen to match
        # how other skill marketplaces organise (Slack App Directory, Notion
        # gallery, Zapier): five buckets, ordered roughly by Plex-test user
        # priority (messaging is the daily-driver, creative tools sit
        # furthest from the household user's first install).
        _CATEGORY_BY_SKILL: dict[str, str] = {
            # Messaging (6 once Signal lands)
            "slack":          "Messaging",
            "telegram":       "Messaging",
            "discord":        "Messaging",
            "imessage":       "Messaging",
            "whatsapp":       "Messaging",
            "signal":         "Messaging",
            # Productivity — email, calendar, notes, tasks, project mgmt (4)
            "google":         "Productivity",
            "gog":            "Productivity",
            "gmail":          "Productivity",
            "calendar":       "Productivity",
            "notion":         "Productivity",
            "linear":         "Productivity",
            "obsidian_vault": "Productivity",
            # Storage (1) — file sync/cloud storage
            "dropbox":        "Storage",
            # Tools (2) — power-user surfaces: code, search APIs
            "github":         "Tools",
            "brave":          "Tools",
            # Creative (2) — content + design generation
            "runway":         "Creative",
            "autocad":        "Creative",
        }
        _CATEGORY_ORDER: list[str] = [
            "Messaging", "Productivity", "Storage", "Tools", "Creative",
        ]
        _CATEGORY_RANK = {name: i for i, name in enumerate(_CATEGORY_ORDER)}
        _UNKNOWN_RANK = len(_CATEGORY_ORDER)  # uncategorised skills tail-sort

        for s in skills:
            s["category"] = _CATEGORY_BY_SKILL.get(s["id"], "Other")
            s["category_order"] = _CATEGORY_RANK.get(s["category"], _UNKNOWN_RANK)

        # Server-side sort by (category_order, display_name lowercased). The
        # UI can re-group from the flat list but the sort guarantees a
        # stable canonical order for any consumer (catalog list endpoint,
        # CLI tooling, the orchestrator's plan-builder, etc.).
        skills.sort(key=lambda s: (
            s.get("category_order", _UNKNOWN_RANK),
            (s.get("display_name") or s.get("id") or "").lower(),
        ))

        return jsonify({"skills": skills, "category_order": _CATEGORY_ORDER})

    @app.get("/api/skills/catalog/<skill_id>")
    def api_skills_catalog_get(skill_id: str) -> Response:
        """Return one skill's metadata + plain-language access panel.

        Surfaces ``access_panel`` verbatim so the UI renders the "Will/Won't"
        lists straight from the source — no client-side re-encoding.
        """
        # Slack uses SKILL_REGISTRY_ENTRY directly (no plugin_name/provider_id).
        if skill_id == _slack.SLACK_SKILL_ID:
            reg = _slack.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "default_scopes": list(reg.get("default_scopes", [])),
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        # WhatsApp — added 2026-06-04 (Phase 1.2 OC coverage audit).
        if skill_id == _whatsapp.WHATSAPP_SKILL_ID:
            reg = _whatsapp.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "config_keys": list(reg.get("config_keys", [])),
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        # Signal — added 2026-06-04 (Phase 1.3 OC coverage audit).
        if skill_id == _signal.SIGNAL_SKILL_ID:
            reg = _signal.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "config_keys": list(reg.get("config_keys", [])),
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        # iMessage — re-added 2026-06-04 with the bundled-plugin rewire.
        if skill_id == _imessage.IMESSAGE_SKILL_ID:
            reg = _imessage.SKILL_REGISTRY_ENTRY
            # Hidden from the catalog list on non-macOS hosts (§8 channel
            # honesty); a direct deep-link gets an honest 404 instead of a
            # detail card for a skill this host can never install.
            if not _skill_supported_on_host(reg):
                return jsonify({
                    "ok": False,
                    "error": "skill_unavailable_on_platform",
                    "detail": (
                        "iMessage requires a macOS pod host (upstream "
                        "OpenClaw constraint) and is not offered on this "
                        "host's platform."
                    ),
                }), 404
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "kind": reg.get("kind"),
                "platforms": list(reg.get("platforms") or []),
                "config_keys": list(reg.get("config_keys", [])),
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        # Discord uses SKILL_REGISTRY_ENTRY (OAuth2, guild-invite flow).
        if skill_id == _discord.DISCORD_SKILL_ID:
            reg = _discord.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "default_scopes": list(reg.get("default_scopes", [])),
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        # Telegram (V2.3-1) — BotFather token skill.
        if skill_id == _telegram.TELEGRAM_SKILL_ID:
            reg = _telegram.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        # Upstream-plugin skills (Brave, GitHub, Drive, Dropbox).
        _up = _upstream.get_skill(skill_id)
        if _up is not None:
            return jsonify({
                "id": _up.id,
                "display_name": _up.display_name,
                "summary": _up.summary,
                "access_panel": dict(_up.access_panel),
            })

        # Apple Contacts + Calendar — withdrawn 2026-05-30; falls through
        # to the unknown-skill 404 branch below. See the catalog-list
        # withdrawal comment above for the rationale.

        # Obsidian — MCP-server-backed filesystem skill (rewired 2026-05-30).
        # The access panel exposes ``mode_choices`` so the UI's install modal
        # can render the read/read_write radio. home_assistant / notion /
        # linear / runway stay withdrawn and fall through to the 404 below.
        if skill_id == _obsidian.OBSIDIAN_SKILL_ID:
            reg = _obsidian.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "kind": reg.get("kind"),
                "access_panel": dict(reg.get("access_panel") or {}),
                "config_keys": list(reg.get("config_keys", [])),
            })

        if skill_id == _dropbox.DROPBOX_SKILL_ID:
            reg = _dropbox.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "kind": reg.get("kind"),
                "access_panel": dict(reg.get("access_panel") or {}),
                "config_keys": list(reg.get("config_keys", [])),
            })

        if skill_id == _notion.NOTION_SKILL_ID:
            reg = _notion.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        if skill_id == _runway.RUNWAY_SKILL_ID:
            reg = _runway.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        if skill_id == _linear.LINEAR_SKILL_ID:
            reg = _linear.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        # Unified "google" — the canonical Google skill (PR #2155).
        if skill_id == _google.GOOGLE_SKILL_ID:
            reg = _google.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "default_capabilities": list(reg.get("default_capabilities", [])),
                "capabilities_catalog": list(reg.get("capabilities_catalog", [])),
                "rate_limits": dict(reg.get("rate_limits", {})),
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        # NOTE: ``google_workspace_read`` was the narrower-scope sibling of
        # Write — it shipped briefly in PR #2154, was hidden from the
        # catalog list in PR #2231 (replaced by the unified ``google``
        # skill's capability picker), and removed entirely as vestigial
        # in this PR. Old deep-links to /api/skills/catalog/google_workspace_read
        # will now 404 (no surviving consumers — the IA pivot happened
        # within hours of #2154 shipping, before any operator reached
        # those routes).

        # Google Workspace (Write) — first F4-honest Workspace install.
        if skill_id == _gws_write.GOOGLE_WORKSPACE_WRITE_SKILL_ID:
            reg = _gws_write.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "default_services": list(reg.get("default_services", [])),
                "required_scopes": list(reg.get("required_scopes", [])),
                "rate_limits": dict(reg.get("rate_limits", {})),
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        # AutoCAD / Autodesk Platform Services — catalog stub for v1.
        if skill_id == _autocad.AUTOCAD_SKILL_ID:
            reg = _autocad.SKILL_REGISTRY_ENTRY
            return jsonify({
                "id": reg["id"],
                "display_name": reg["display_name"],
                "summary": reg["summary"],
                "access_panel": dict(reg.get("access_panel") or {}),
            })

        meta = _gog.get_skill(skill_id)
        if not meta:
            return jsonify({"ok": False, "error": f"unknown skill {skill_id!r}"}), 404
        return jsonify({
            "id": meta["id"],
            "display_name": meta["display_name"],
            "summary": meta["summary"],
            "plugin_name": meta["plugin_name"],
            "provider_id": meta["provider_id"],
            "default_services": list(meta["default_services"]),
            "access_panel": dict(meta["access_panel"]),
        })

    @app.post("/api/admin/gateway/<bot_id>/restart")
    def api_admin_restart_gateway(bot_id: str) -> "Response | tuple[Response, int]":
        """Restart bot's openclaw gateway via launchctl.
        Uses oc_gateway_restart() (Evolve-native, direct launchctl call).
        """
        body = request.get_json() or {}
        if not body.get("confirm"):
            return jsonify({"error": "confirm required"}), 400
        _prime_auth_store(bot_id)  # reconcile sqlite←JSON before the bounce
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        oc_gateway_restart = _rt.gateway_restart
        result = oc_gateway_restart(bot_id)
        if not result.get("ok"):
            return jsonify(result), 500
        _module._audit_log_entry("gateway.restart", bot_id, {"service": result.get("service", "")})
        return jsonify(result)

    @app.post("/api/admin/gateway/<bot_id>/stop")
    def api_admin_stop_gateway(bot_id: str) -> "Response | tuple[Response, int]":
        """Stop (bootout) bot's openclaw gateway via launchctl — does NOT restart.

        Useful for stopping a crash-looping gateway without triggering launchd
        respawn.  Call /restart afterwards to bring it back up intentionally.

        Body: {"confirm": true}
        """
        body = request.get_json() or {}
        if not body.get("confirm"):
            return jsonify({"error": "confirm required"}), 400
        network = load_network(network_path)
        bots = network.get("bots", {})
        bot_cfg = bots.get(bot_id, {})
        svc = bot_cfg.get("service") or f"ai.openclaw.{bot_id}-gateway"

        # Try system domain first (LaunchDaemon), then user GUI domain
        import pwd as _pwd
        user = bot_cfg.get("user", bot_id)
        domains: list[str] = ["system"]
        try:
            uid = _pwd.getpwnam(user).pw_uid
            domains.insert(0, f"gui/{uid}")
        except Exception:
            pass

        errors: list[str] = []
        for domain in domains:
            # raw(): bare bootout that must NOT delete the plist (Scheduler.
            # remove() would) — /restart needs it on disk to bring the
            # gateway back; the domain also varies per iteration (gui/<uid>
            # vs system) while an adapter is fixed to one domain.
            rc, out, err = get_launchd_scheduler().raw("bootout", f"{domain}/{svc}")
            if rc == 0:
                _module._audit_log_entry("gateway.stop", bot_id, {"service": svc, "domain": domain})
                return jsonify({"ok": True, "bot": bot_id, "service": svc, "domain": domain})
            errors.append(f"{domain}: {(err or out).strip()}")

        return jsonify({"ok": False, "bot": bot_id, "service": svc, "errors": errors}), 500

    @app.get("/api/admin/usage/<bot_id>")
    def api_admin_get_usage(bot_id: str) -> Response:
        """Bot usage summary — delegates to /api/analytics/turns (same data, Evolve-native)."""
        days = min(int(request.args.get("days", 7)), 30)
        # Reuse the existing analytics turns logic via internal redirect
        from flask import redirect
        return redirect(f"/api/analytics/turns?bot={bot_id}&days={days}")


# ── /api/launchd, /api/oc/version, /api/gateway/logs routes ─────────────────

