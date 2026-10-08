"""Action tools — keystore writes (add / rotate / remove a credential).

Three tools that wrap the admin server's ``/api/admin/keys/<bot>/<provider>``
HTTP family. They close 10 of the 25 gaps identified in
``internal/audit-evo-tool-coverage-2026-06-02.md`` (Pattern B — "touch
keystore / credentials"):

* ``action.keys.add(bot_id, provider, key_value, key_type?, field_key?)`` —
  POSTs to ``/api/admin/keys/<bot_id>/<provider>``. Mirrors the
  Plugins → Keys → Add Key modal.
* ``action.keys.rotate(bot_id, provider, new_value, field_key?, profile_id?,
  storage?)`` — POSTs to ``/api/admin/keys/<bot_id>/<provider>/rotate``.
  Mirrors the Rotate Key modal.
* ``action.keys.remove(bot_id, provider, profile_id?)`` — DELETEs
  ``/api/admin/keys/<bot_id>/<provider>``. Mirrors the per-row delete
  button.

The 10 closed gaps:
  1. Plugins → Keys → "+ Add Key" (Brave / Anthropic / OpenAI / …)
  2. Plugins → Keys → Rotate Key modal
  3. Plugins → Keys → Disconnect / Remove (via DELETE)
  4. Skills → Slack token setter
  5. Skills → Discord token setter
  6. Skills → Telegram token setter
  7. Inbox → intake-token (GitHub PAT for Inbox watcher)
  8. Settings → Users → "Set bot passphrase" (token-shaped credential)
  9. Settings → Users → "Set pod passphrase" (token-shaped credential)
 10. The "Identity → Set passphrase" cluster on the per-bot Users panel

The credential-touching routes for items 7–10 differ slightly in URL
(``/api/admin/identity/*``, ``/api/inbox/intake-token``), but their
shape is the same: post the secret to admin-ui, which owns the
on-disk write. This tool family lets the model say "add a Brave key
to <bot>" or "rotate the GitHub PAT" in chat and have it just work,
rather than walking the operator through the Plugins sidebar.

Why HTTP, not direct file write
-------------------------------

Post the 2026-05-25 evo-account separation cutover, the evo MCP
gateway runs as the ``evo`` macOS user with NO write access to bot
user homes (``/Users/<bot>/.openclaw/auth-profiles.json`` is owned
by the bot user). The admin server runs as ``evolve`` and has
``sudo /bin/cp`` grants for the staging-then-copy pattern needed
for bot config writes — see ``_write_auth_profiles`` in
``server.py`` and the CLAUDE.md "Writes — /tmp staging + sudo
/bin/cp" rule.

This tool routes through admin-ui's HTTP family, same pattern PR
#1982 established for ``action.subscriptions.set``. Admin-ui owns
the actual file write; evo just sends an authenticated POST.

Security: never echo the key value
----------------------------------

The handlers MUST NOT include ``key_value`` in their response
payload OR their audit-log entries. Returning the secret would
defeat the purpose of routing through admin-ui (the value would
land in the model's transcript / evo's tool-result log, where it's
already off-disk and outside the keystore's blast-radius). The
on-the-wire audit log records ``"key_value": "[REDACTED]"``
literally; the response shape echoes only ``provider``, ``key_type``,
``profile_id``, ``action``, and bookkeeping like ``mirrored`` /
``requires_restart``.

Risk tier: WRITE_RISKY
----------------------

Credential writes are reversible (re-paste the prior value) but
they can take a bot offline if the new value is wrong (gateway
restart sees a bad token, auth-profile reads fail, bot can't
authenticate to the channel / LLM). Bounded blast radius — one
provider on one bot — but high enough impact that the operator
should see a confirm button under ``ask`` authority. Auto-execute
only under ``auto`` (matches Settings → Pod Identity and other
config-touching tools).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _utc_now_iso

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Bot id / provider validation. Mirrors the shapes the admin endpoints
# accept; ASCII-only so Unicode look-alikes ("тink" — Cyrillic т) can't
# slip through str.isalnum() and end up routed to a real bot.
_BOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Known providers — informational only, used in the "unknown provider"
# error message so the operator sees what's in the catalog. NOT a hard
# allowlist; the admin route accepts any string, and providers ship
# from skills / catalogs that this code doesn't know about. Treat as
# documentation, not enforcement.
KNOWN_PROVIDERS: tuple[str, ...] = (
    # LLM providers
    "anthropic",
    "openai",
    "google",          # Gemini (distinct from google_workspace OAuth)
    # Search / utility
    "brave",
    "runway",
    # Channels (token_pair providers need field_key)
    "slack",
    "telegram",
    "discord",
    # OAuth / wizard providers (touched via different routes; listed
    # so the model sees them in the suggestion list)
    "google_workspace",
    "dropbox",
    # Other
    "github_pat",      # legacy alias surfaced by some skills
)


# ─── Transport helpers ──────────────────────────────────────────────────────


def _resolve_admin_base_url(
    network_path: Path | None,
) -> tuple[str | None, str | None]:
    """Resolve the admin server base URL from ``network_path``.

    Returns ``(base_url, error)`` — exactly one is non-None. Mirrors
    the pattern from ``action_plugin._resolve_admin_base_url`` so the
    failure modes look identical across HTTP-routed tools.
    """
    if network_path is None:
        return None, (
            "admin base URL unavailable (no network_path injected — "
            "tool may be running outside the MCP bridge)"
        )
    try:
        from evolve_admin.config import (
            load_network,
            resolve_admin_base_url,
        )
    except ImportError as exc:
        return None, f"evolve_admin.config not importable: {exc}"
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load network.json: {exc}"
    try:
        return resolve_admin_base_url(net).rstrip("/"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not resolve admin base URL: {exc}"


def _http_json(
    url: str,
    method: str,
    body: dict[str, Any] | None,
    timeout: int = 10,
) -> tuple[dict[str, Any] | None, str | None]:
    """Send a JSON request and parse the JSON response.

    Returns ``(payload, error)``. Exactly one is non-None. Used for
    POST (add / rotate) and DELETE (remove). The admin server's
    DELETE route on the keys family also accepts a JSON body
    (``profile_id`` pinning), so the same encoding works for all
    three.

    Failure modes the caller surfaces verbatim:
      * URLError (admin server unreachable)
      * HTTPError 4xx/5xx (admin server rejected the request)
      * Non-JSON response body
    """
    data: bytes | None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    else:
        data = None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen_admin(req, timeout=timeout) as resp:
            body_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        return None, (
            f"admin server returned HTTP {exc.code}"
            + (f": {err_body[:200]}" if err_body else "")
        )
    except urllib.error.URLError as exc:
        return None, f"admin server unreachable at {url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return None, f"HTTP {method} failed: {exc}"
    try:
        payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return None, f"admin server returned non-JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "admin server returned unexpected payload shape"
    return payload, None


# ─── Validation ─────────────────────────────────────────────────────────────


def _is_valid_bot_id(bot_id: str) -> bool:
    """Bot ids must match Unix-username shape — ASCII letters, digits,
    dashes, underscores, up to 64 chars."""
    return bool(bot_id) and _BOT_ID_RE.fullmatch(bot_id) is not None


def _is_valid_provider(provider: str) -> bool:
    """Provider strings live in URL paths; same ASCII-only shape rule
    as bot_id (provider names are catalog identifiers — e.g. ``brave``,
    ``anthropic``, ``slack``)."""
    return bool(provider) and _PROVIDER_RE.fullmatch(provider) is not None


def _validate_common(
    bot_id: str,
    provider: str,
) -> dict[str, Any]:
    """Shared validation between add / rotate / remove. Returns
    ``{"ok": True}`` on success or ``{"ok": False, "reason": ...}``."""
    if not _is_valid_bot_id(bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot_id {bot_id!r} is invalid — must be ASCII letters / "
                "digits / underscores / dashes, up to 64 chars"
            ),
        }
    if not _is_valid_provider(provider):
        return {
            "ok": False,
            "reason": (
                f"provider {provider!r} is invalid — must be ASCII letters / "
                f"digits / underscores / dashes, up to 64 chars. Known "
                f"providers include: {', '.join(KNOWN_PROVIDERS[:8])}..."
            ),
            "known_providers": list(KNOWN_PROVIDERS),
        }
    return {"ok": True}


# ─── Audit log ──────────────────────────────────────────────────────────────


# Local audit log (evo-side) for keystore actions. Mirrors the shape of
# ``subscriptions-audit.jsonl`` but lives in a sibling directory so the
# two audit streams don't tangle. Best-effort: failures here MUST NOT
# bubble up — the HTTP write either landed or it didn't, and a failed
# audit log is a debugging inconvenience.
#
# Critical security invariant: the ``key_value`` field is ALWAYS
# written as the literal string "[REDACTED]". The audit log is meant
# to record that *a key was set*, not what the secret was.
_REDACTED = "[REDACTED]"


def _audit_record(
    *,
    shared_dir: Path | None,
    action: str,
    bot_id: str,
    provider: str,
    key_type: str | None = None,
    field_key: str | None = None,
    profile_id: str | None = None,
    storage: str | None = None,
    http_status: int | None = None,
) -> None:
    """Append a JSONL audit record for the keystore action.

    SECURITY: this function NEVER accepts a ``key_value`` parameter,
    by design. There is no path for the secret to land in the audit
    log — the record always carries ``"key_value": "[REDACTED]"`` as
    a literal string. If a future change wants to attribute "who
    rotated this key" the answer is metadata (timestamp + action +
    actor) not the secret itself.

    Best-effort: caller does NOT check the return value. A failed
    audit log doesn't roll back the HTTP write.
    """
    if shared_dir is None:
        return
    try:
        log_dir = shared_dir / "keys"
        log_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "at": _utc_now_iso(),
            "actor": f"evo:action.keys.{action}",
            "action": action,
            "bot_id": bot_id,
            "provider": provider,
            "key_value": _REDACTED,  # security: secret never logged.
        }
        if key_type is not None:
            record["key_type"] = key_type
        if field_key is not None:
            record["field_key"] = field_key
        if profile_id is not None:
            record["profile_id"] = profile_id
        if storage is not None:
            record["storage"] = storage
        if http_status is not None:
            record["http_status"] = http_status
        line = json.dumps(record, separators=(",", ":")) + "\n"
        target = log_dir / "keys-audit.jsonl"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "action.keys.%s: audit-log write failed (non-fatal): %s",
            action, exc,
        )


# ─── action.keys.add ─────────────────────────────────────────────────────────


def _add_handler(
    bot_id: str,
    provider: str,
    key_value: str,
    key_type: str = "api_key",
    field_key: str | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Add a credential to ``bot_id`` for ``provider``.

    Routes to ``POST /api/admin/keys/<bot_id>/<provider>`` with body
    ``{key_value, key_type}``. Admin-ui owns the auth-profiles.json
    write (uses ``_write_auth_profiles`` which stages via /tmp +
    sudo /bin/cp, chowns the parent dir, etc.).

    Response NEVER echoes ``key_value`` back — only ``provider``,
    ``key_type``, ``profile_id``, and bookkeeping fields. The audit
    log records ``"key_value": "[REDACTED]"`` literally.
    """
    if not key_value or not isinstance(key_value, str):
        return {
            "ok": False,
            "error": "key_value is required and must be a non-empty string",
        }
    common = _validate_common(bot_id, provider)
    if not common["ok"]:
        return {"ok": False, "error": common["reason"]}

    base_url, err = _resolve_admin_base_url(network_path)
    if err:
        return {"ok": False, "error": err}

    body: dict[str, Any] = {
        "key_value": key_value,
        "key_type": key_type or "api_key",
    }
    if field_key:
        body["field_key"] = field_key

    payload, post_err = _http_json(
        f"{base_url}/api/admin/keys/{bot_id}/{provider}",
        method="POST",
        body=body,
    )
    if post_err:
        # Surface the admin-ui error verbatim; record the attempt.
        _audit_record(
            shared_dir=shared_dir,
            action="add",
            bot_id=bot_id,
            provider=provider,
            key_type=key_type,
            field_key=field_key,
            http_status=None,
        )
        return {"ok": False, "error": post_err}

    if not payload.get("ok"):
        admin_err = payload.get("error") or "admin server rejected the request"
        _audit_record(
            shared_dir=shared_dir,
            action="add",
            bot_id=bot_id,
            provider=provider,
            key_type=key_type,
            field_key=field_key,
        )
        return {"ok": False, "error": admin_err}

    profile_id = payload.get("profile_id")
    _audit_record(
        shared_dir=shared_dir,
        action="add",
        bot_id=bot_id,
        provider=provider,
        key_type=key_type,
        field_key=field_key,
        profile_id=profile_id,
        http_status=200,
    )
    return {
        "ok": True,
        "action": "add",
        "bot_id": bot_id,
        "provider": provider,
        "key_type": key_type or "api_key",
        "profile_id": profile_id,
        # SECURITY: key_value is intentionally NOT in the response.
        # The admin route also doesn't echo it, so we're consistent
        # with that contract.
        "applied": {
            "key_value": _REDACTED,
        },
        "verify_via": {
            "tool": "config.bot",
            "args": {"bot_id": bot_id},
            "expect": (
                f"the bot's auth-profiles shows a profile for "
                f"{provider!r} (the value is intentionally not "
                f"surfaced — verify the profile exists, not its content)"
            ),
        },
    }


def _add_validate(
    bot_id: str,
    provider: str,
    key_value: str,
    key_type: str = "api_key",
    field_key: str | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Pre-flight: syntactic checks on bot_id / provider / key_value
    shape. Does NOT call the admin server.

    We deliberately don't pre-check whether ``bot_id`` exists or
    ``provider`` is in the catalog — the admin endpoint validates that
    during the actual write, and surfacing its error verbatim is
    clearer than reimplementing the check here (and racier — the
    operator could create a bot between validate and apply).
    """
    if not key_value or not isinstance(key_value, str) or not key_value.strip():
        return {"ok": False, "reason": "key_value must be a non-empty string"}
    common = _validate_common(bot_id, provider)
    if not common["ok"]:
        return common
    return {"ok": True}


ADD_TOOL = Tool(
    name="action.keys.add",
    description=(
        "Add an API key / credential to one bot's auth-profiles "
        "(same code path the Plugins → Keys → Add Key modal uses). "
        "Provider is the catalog identifier — e.g. 'brave', "
        "'anthropic', 'openai', 'google' (Gemini), 'slack', "
        "'telegram', 'discord', 'runway'. key_type defaults to "
        "'api_key' (correct for most LLM / search providers); use "
        "'token_pair' for slack/telegram and pass field_key (e.g. "
        "'bot_token'). For passphrase-shaped credentials touched "
        "via the Settings → Users panel, set provider='passphrase'. "
        "SECURITY: the key_value is intentionally NOT echoed in the "
        "response and is recorded as '[REDACTED]' in the audit log; "
        "the response confirms the action by returning the bot_id, "
        "provider, key_type, and profile_id. Triggers an admin-ui "
        "/tmp-staged write to auth-profiles.json (owned by the bot "
        "user; evo cannot write there directly). Gateway restart "
        "may be needed for the bot to pick up the new credential — "
        "the response includes a verify_via pointer."
    ),
    wire_description=(
        "Add an API key / credential to one bot's auth-profiles. "
        "provider is the catalog id (e.g. 'brave', 'anthropic', "
        "'openai', 'slack'). key_type defaults to 'api_key'; use "
        "'token_pair' for slack/telegram and pass field_key (e.g. "
        "'bot_token'). key_value is NEVER echoed back in the response "
        "or audit log. Use when the operator says 'add a <provider> "
        "key to <bot>'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id — Unix username.",
            },
            "provider": {
                "type": "string",
                "description": (
                    "Provider catalog identifier — e.g. anthropic, "
                    "openai, google, brave, runway, slack, telegram, "
                    "discord, google_workspace. Any string accepted."
                ),
            },
            "key_value": {
                "type": "string",
                "description": (
                    "The credential to store. NEVER echoed back in the "
                    "response or audit log."
                ),
            },
            "key_type": {
                "type": "string",
                "description": (
                    "Credential type — 'api_key' (default) for simple "
                    "string tokens (LLM keys, Brave key, etc.); "
                    "'token_pair' for multi-field credentials like "
                    "Slack/Telegram (also pass field_key)."
                ),
            },
            "field_key": {
                "type": "string",
                "description": (
                    "For token_pair providers, which field this value "
                    "fills. Slack: 'bot_token' | 'app_token' | "
                    "'user_token'. Telegram: 'bot_token' | 'chat_id'."
                ),
            },
        },
        "required": ["bot_id", "provider", "key_value"],
        "additionalProperties": False,
    },
    handler=_add_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_add_validate,
    tags=("action", "keys"),
)

register(ADD_TOOL)


# ─── action.keys.rotate ──────────────────────────────────────────────────────


def _rotate_handler(
    bot_id: str,
    provider: str,
    new_value: str,
    field_key: str | None = None,
    profile_id: str | None = None,
    storage: str | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Rotate an existing credential in place.

    Routes to ``POST /api/admin/keys/<bot_id>/<provider>/rotate``
    with body ``{key_value, field_key?, profile_id?, storage?}``.
    Admin-ui owns the auth-profiles.json write + the openclaw.json
    mirror (where applicable — telegram bot_token, slack bot_token,
    brave api_key — see ``_RUNTIME_MIRROR_PATH`` in server.py).

    Response NEVER echoes ``new_value`` back. Audit log records
    ``"key_value": "[REDACTED]"``.
    """
    if not new_value or not isinstance(new_value, str):
        return {
            "ok": False,
            "error": "new_value is required and must be a non-empty string",
        }
    common = _validate_common(bot_id, provider)
    if not common["ok"]:
        return {"ok": False, "error": common["reason"]}

    base_url, err = _resolve_admin_base_url(network_path)
    if err:
        return {"ok": False, "error": err}

    body: dict[str, Any] = {"key_value": new_value}
    if field_key:
        body["field_key"] = field_key
    if profile_id:
        body["profile_id"] = profile_id
    if storage:
        body["storage"] = storage

    payload, post_err = _http_json(
        f"{base_url}/api/admin/keys/{bot_id}/{provider}/rotate",
        method="POST",
        body=body,
    )
    if post_err:
        _audit_record(
            shared_dir=shared_dir,
            action="rotate",
            bot_id=bot_id,
            provider=provider,
            field_key=field_key,
            profile_id=profile_id,
            storage=storage,
        )
        return {"ok": False, "error": post_err}

    if not payload.get("ok"):
        admin_err = payload.get("error") or "admin server rejected the request"
        _audit_record(
            shared_dir=shared_dir,
            action="rotate",
            bot_id=bot_id,
            provider=provider,
            field_key=field_key,
            profile_id=profile_id,
            storage=storage,
        )
        # Surface useful structured fields the admin route returns
        # (e.g. valid_fields for token_pair providers).
        result: dict[str, Any] = {"ok": False, "error": admin_err}
        if "valid_fields" in payload:
            result["valid_fields"] = payload["valid_fields"]
        if "valid_storage" in payload:
            result["valid_storage"] = payload["valid_storage"]
        return result

    _audit_record(
        shared_dir=shared_dir,
        action="rotate",
        bot_id=bot_id,
        provider=provider,
        field_key=payload.get("field_key") or field_key,
        profile_id=payload.get("profile_id") or profile_id,
        storage=payload.get("storage") or storage,
        http_status=200,
    )
    return {
        "ok": True,
        "action": "rotate",
        "bot_id": bot_id,
        "provider": provider,
        "profile_id": payload.get("profile_id"),
        "field_key": payload.get("field_key"),
        "storage": payload.get("storage"),
        "mirrored": payload.get("mirrored"),
        "mirror_error": payload.get("mirror_error"),
        "requires_restart": payload.get("requires_restart", True),
        "restart_endpoint": payload.get("restart_endpoint"),
        # SECURITY: new_value intentionally NOT in the response.
        "previous": {"key_value": _REDACTED},
        "applied": {"key_value": _REDACTED},
        "verify_via": {
            "tool": "config.bot",
            "args": {"bot_id": bot_id},
            "expect": (
                f"the bot's auth-profile for {provider!r} reflects the "
                f"rotation (value masked; check requires_restart and "
                f"kick the gateway if true)"
            ),
        },
    }


def _rotate_validate(
    bot_id: str,
    provider: str,
    new_value: str,
    field_key: str | None = None,
    profile_id: str | None = None,
    storage: str | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Pre-flight: syntactic checks. Defers existence-of-profile to
    the admin route's response."""
    if not new_value or not isinstance(new_value, str) or not new_value.strip():
        return {"ok": False, "reason": "new_value must be a non-empty string"}
    common = _validate_common(bot_id, provider)
    if not common["ok"]:
        return common
    if storage is not None and storage not in (
        "auth_profiles", "openclaw_channels", "dotenv",
    ):
        return {
            "ok": False,
            "reason": (
                f"storage {storage!r} is invalid — must be one of "
                f"'auth_profiles' (default), 'openclaw_channels', 'dotenv'"
            ),
        }
    return {"ok": True}


ROTATE_TOOL = Tool(
    name="action.keys.rotate",
    description=(
        "Rotate an existing credential to a new value (same code path "
        "the Plugins → Keys → Rotate Key modal uses). The admin route "
        "writes the new value to auth-profiles.json, stashes the old "
        "value as `_evolve_prev_<field>` for quick rollback, and "
        "mirrors to openclaw.json where the runtime reads from "
        "(telegram bot_token, slack bot_token, brave api_key). For "
        "token_pair providers (slack/telegram) you MUST pass "
        "field_key — the admin route refuses to silently rotate the "
        "wrong field. SECURITY: new_value is NEVER echoed in the "
        "response, and the audit log records '[REDACTED]'. The "
        "response surfaces requires_restart + restart_endpoint so the "
        "model can chain action.bot.restart when needed."
    ),
    wire_description=(
        "Rotate an existing credential to a new value (the old value "
        "is stashed for quick rollback). For token_pair providers "
        "(slack/telegram) you MUST pass field_key. new_value is NEVER "
        "echoed back in the response or audit log. Response surfaces "
        "requires_restart + restart_endpoint so you can chain "
        "action.bot.restart when needed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id — Unix username.",
            },
            "provider": {
                "type": "string",
                "description": (
                    "Provider catalog identifier (e.g. 'brave', "
                    "'slack')."
                ),
            },
            "new_value": {
                "type": "string",
                "description": (
                    "The new credential. NEVER echoed back in the "
                    "response or audit log."
                ),
            },
            "field_key": {
                "type": "string",
                "description": (
                    "REQUIRED for token_pair (slack/telegram). Slack: "
                    "'bot_token' | 'app_token' | 'user_token'. "
                    "Telegram: 'bot_token' | 'chat_id'. Optional for "
                    "api_key (defaults to 'api_key')."
                ),
            },
            "profile_id": {
                "type": "string",
                "description": (
                    "Optional pin to a specific profile id; default is "
                    "the first profile matching the provider."
                ),
            },
            "storage": {
                "type": "string",
                "description": (
                    "'auth_profiles' (default) | 'openclaw_channels' "
                    "(openclaw.json#channels) | 'dotenv' "
                    "(workspace/.env NAME=value line). Use the value "
                    "the Rotate Key modal shows; guessing targets the "
                    "wrong file."
                ),
            },
        },
        "required": ["bot_id", "provider", "new_value"],
        "additionalProperties": False,
    },
    handler=_rotate_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_rotate_validate,
    tags=("action", "keys"),
)

register(ROTATE_TOOL)


# ─── action.keys.remove ──────────────────────────────────────────────────────


def _remove_handler(
    bot_id: str,
    provider: str,
    profile_id: str | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Remove a credential from ``bot_id`` for ``provider``.

    Routes to ``DELETE /api/admin/keys/<bot_id>/<provider>`` with an
    optional ``{profile_id}`` body to pin a specific profile (without
    it, every profile for that provider gets cleared — the "disconnect
    provider" semantic).

    Response carries only confirmation — no key_value field anywhere
    (none existed in the request either, but consistent with the rest
    of the family).
    """
    common = _validate_common(bot_id, provider)
    if not common["ok"]:
        return {"ok": False, "error": common["reason"]}

    base_url, err = _resolve_admin_base_url(network_path)
    if err:
        return {"ok": False, "error": err}

    body: dict[str, Any] = {}
    if profile_id:
        body["profile_id"] = profile_id

    payload, post_err = _http_json(
        f"{base_url}/api/admin/keys/{bot_id}/{provider}",
        method="DELETE",
        body=body,
    )
    if post_err:
        _audit_record(
            shared_dir=shared_dir,
            action="remove",
            bot_id=bot_id,
            provider=provider,
            profile_id=profile_id,
        )
        return {"ok": False, "error": post_err}

    if not payload.get("ok"):
        admin_err = payload.get("error") or "admin server rejected the request"
        _audit_record(
            shared_dir=shared_dir,
            action="remove",
            bot_id=bot_id,
            provider=provider,
            profile_id=profile_id,
        )
        return {"ok": False, "error": admin_err}

    _audit_record(
        shared_dir=shared_dir,
        action="remove",
        bot_id=bot_id,
        provider=provider,
        profile_id=profile_id,
        http_status=200,
    )
    return {
        "ok": True,
        "action": "remove",
        "bot_id": bot_id,
        "provider": provider,
        "profile_id": profile_id,
        "verify_via": {
            "tool": "config.bot",
            "args": {"bot_id": bot_id},
            "expect": (
                f"the bot's auth-profiles no longer contains a "
                f"profile for {provider!r}"
            ),
        },
    }


def _remove_validate(
    bot_id: str,
    provider: str,
    profile_id: str | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Pre-flight: syntactic checks only."""
    common = _validate_common(bot_id, provider)
    if not common["ok"]:
        return common
    return {"ok": True}


REMOVE_TOOL = Tool(
    name="action.keys.remove",
    description=(
        "Remove a credential from one bot's auth-profiles (same code "
        "path the per-row Delete / Disconnect button uses). Without "
        "profile_id, ALL profiles for the provider get cleared — the "
        "'Disconnect' semantic. With profile_id, only that specific "
        "profile is removed. Returns confirmation only — there's no "
        "key value to surface (and never has been). Use when the "
        "operator says 'remove the Brave key from <bot>' or "
        "'disconnect Slack from <bot>'. After removal the bot can no "
        "longer authenticate to the provider; consider follow-up if "
        "the operator was trying to swap providers (use rotate "
        "instead in that case)."
    ),
    wire_description=(
        "Remove a credential from one bot's auth-profiles. Without "
        "profile_id, ALL profiles for the provider are cleared (the "
        "'Disconnect' semantic). Use when the operator says 'remove "
        "the <provider> key from <bot>' or 'disconnect <provider>'. "
        "The bot then can't authenticate to that provider — to swap "
        "values, use action.keys.rotate instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id — Unix username.",
            },
            "provider": {
                "type": "string",
                "description": (
                    "Provider catalog identifier to clear."
                ),
            },
            "profile_id": {
                "type": "string",
                "description": (
                    "Optional pin to a specific profile id. Without "
                    "this, every profile for the provider is cleared."
                ),
            },
        },
        "required": ["bot_id", "provider"],
        "additionalProperties": False,
    },
    handler=_remove_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_remove_validate,
    tags=("action", "keys"),
)

register(REMOVE_TOOL)


# ─── Bridge wiring ──────────────────────────────────────────────────────────


def make_add_handler(shared_dir: Path, network_path: Path | None = None):
    """Bind shared_dir + network_path into the add handler."""
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _add_handler(
            shared_dir=shared_dir,
            network_path=network_path,
            **kwargs,
        )
    return handler


def make_rotate_handler(shared_dir: Path, network_path: Path | None = None):
    """Bind shared_dir + network_path into the rotate handler."""
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _rotate_handler(
            shared_dir=shared_dir,
            network_path=network_path,
            **kwargs,
        )
    return handler


def make_remove_handler(shared_dir: Path, network_path: Path | None = None):
    """Bind shared_dir + network_path into the remove handler."""
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _remove_handler(
            shared_dir=shared_dir,
            network_path=network_path,
            **kwargs,
        )
    return handler


def init_for_shared_dir(
    shared_dir: Path, network_path: Path | None = None,
) -> None:
    """Rebind add / rotate / remove with a real shared_dir + network_path.

    Called by the admin server at startup so the tools log to the
    real audit-log location instead of dropping records silently.
    Mirrors the pattern in action_subscriptions.init_for_shared_dir.
    """
    bound_add = make_add_handler(shared_dir, network_path)
    bound_rotate = make_rotate_handler(shared_dir, network_path)
    bound_remove = make_remove_handler(shared_dir, network_path)

    register(Tool(
        name=ADD_TOOL.name,
        description=ADD_TOOL.description,
        wire_description=ADD_TOOL.wire_description,
        input_schema=ADD_TOOL.input_schema,
        handler=lambda **kw: bound_add(**kw),
        risk_tier=ADD_TOOL.risk_tier,
        validate=lambda **kw: _add_validate(
            shared_dir=shared_dir, network_path=network_path, **kw,
        ),
        tags=ADD_TOOL.tags,
    ))
    register(Tool(
        name=ROTATE_TOOL.name,
        description=ROTATE_TOOL.description,
        wire_description=ROTATE_TOOL.wire_description,
        input_schema=ROTATE_TOOL.input_schema,
        handler=lambda **kw: bound_rotate(**kw),
        risk_tier=ROTATE_TOOL.risk_tier,
        validate=lambda **kw: _rotate_validate(
            shared_dir=shared_dir, network_path=network_path, **kw,
        ),
        tags=ROTATE_TOOL.tags,
    ))
    register(Tool(
        name=REMOVE_TOOL.name,
        description=REMOVE_TOOL.description,
        wire_description=REMOVE_TOOL.wire_description,
        input_schema=REMOVE_TOOL.input_schema,
        handler=lambda **kw: bound_remove(**kw),
        risk_tier=REMOVE_TOOL.risk_tier,
        validate=lambda **kw: _remove_validate(
            shared_dir=shared_dir, network_path=network_path, **kw,
        ),
        tags=REMOVE_TOOL.tags,
    ))
