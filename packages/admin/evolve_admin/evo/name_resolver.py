"""Resolve a messaging-platform external_id to a human-readable name.

Operators see Telegram IDs like ``123456789`` and Slack IDs like
``U0PLKKXV0`` in the Identity subtab. Without a name attached, those
strings are operationally opaque — you can't tell whether a recorded
admin or primary user is the right person without cross-checking
against the messaging platform manually.

This module wraps each platform's "who is this user?" API:

  * **Telegram** — ``getChat`` (returns ``username`` + ``first_name`` +
    ``last_name``). Requires the bot to have had at least one prior
    interaction with the user; in our context the admin/primary
    typically DM'd the bot to get their own ID, so this is almost
    always satisfied.

  * **Slack** — ``users.info`` (returns ``name`` + ``real_name`` +
    ``profile.display_name``). Requires the bot token to have
    ``users:read`` scope; default for any bot we've onboarded.

  * **Discord** / **WhatsApp** — not implemented in v1. Discord's
    ``GET /users/{id}`` works but DM lookups have nuance; WhatsApp
    uses phone numbers and doesn't have a real username concept.
    Both return ``None`` from this resolver — caller renders just
    the raw ID with a "name lookup not supported" hint.

The resolved names are cached in ``network.json`` under
``pod.admins.resolved_names[<channel>:<external_id>]`` so we don't hit
the platform API every page render. Cache entries include the
``cached_at`` timestamp so the UI can offer a manual refresh and we
can age them out later if needed.

Bot-token resolution: we read the **primary bot's** openclaw.json for
the channel credentials (``channels.<platform>.botToken``). Rationale:
the primary bot (evo) is where pod-level admin claims happen, so its
token is most likely to be able to look up the admins it sees. If the
primary doesn't have the channel configured, lookup falls back to
scanning every member bot until one with the channel is found.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import channel_registry as _channel_registry

log = logging.getLogger(__name__)


# ─── Channels we know how to resolve ────────────────────────────────────────

# Projection of the channel registry's ``supports_name_resolution`` column.
# DELIBERATELY NARROW — three of the registry's nine channels. WhatsApp and
# Signal are fully supported messaging channels that this module cannot name-
# resolve (no directory API), so they must stay out of this set. Deriving it
# from the capability column rather than from "all channels" is the whole
# reason the registry has columns instead of being one flat list.
SUPPORTED_CHANNELS: frozenset[str] = _channel_registry.name_resolution_channels()


# ─── Bot-token resolution ────────────────────────────────────────────────────


def _read_bot_oc_config(bot_user: str) -> dict | None:
    """Return parsed openclaw.json for a bot's macOS account, or None.

    Mirrors the direct-then-sudo-cat pattern used by the alerts
    dispatcher (alerts/dispatcher.py:623-628). The evolve user has
    ACL read on ``.openclaw/`` from deploy time; sudo fallback covers
    the pre-ACL bots.
    """
    p = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")
    text: str | None = None
    try:
        text = p.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(p)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                text = r.stdout
        except (subprocess.TimeoutExpired, OSError):
            return None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _channel_token(
    network: dict[str, Any], channel: str,
    *,
    prefer_bot_id: "str | None" = None,
) -> str | None:
    """Find a bot whose openclaw.json has ``channels.<channel>.botToken``
    set, prefer ``prefer_bot_id`` (the bot whose roster we're resolving
    for), then the primary bot, then scan members.

    ``prefer_bot_id`` matters because Slack tokens are workspace-scoped:
    a bot's token can only resolve users in that bot's Slack workspace.
    The previous behavior (always return the first matching token) broke
    cross-workspace bots whose roster wasn't the one whose token came
    first — their users would resolve as "[unknown]" because the call
    used the wrong bot's token and got ``user_not_found``.

    Returns the token string or None.
    """
    if channel not in SUPPORTED_CHANNELS:
        return None

    # Build the search order: prefer_bot_id first (when valid), then
    # primary, then other members. De-dupe so a bot doesn't appear
    # twice when it's both the prefer_bot_id and the primary.
    primary = network.get("primary")
    bots_block = network.get("bots") or {}
    ordered: list[str] = []
    seen: set[str] = set()
    if (isinstance(prefer_bot_id, str) and prefer_bot_id
            and prefer_bot_id in bots_block):
        ordered.append(prefer_bot_id)
        seen.add(prefer_bot_id)
    if (isinstance(primary, str) and primary
            and primary in bots_block and primary not in seen):
        ordered.append(primary)
        seen.add(primary)
    for bot_id in bots_block:
        if bot_id not in seen:
            ordered.append(bot_id)
            seen.add(bot_id)

    for bot_id in ordered:
        bot_cfg = bots_block.get(bot_id) or {}
        if not isinstance(bot_cfg, dict):
            continue
        bot_user = bot_cfg.get("user") or bot_id
        oc = _read_bot_oc_config(bot_user)
        if oc is None:
            continue
        channels = oc.get("channels") or {}
        if not isinstance(channels, dict):
            continue
        ch_cfg = channels.get(channel) or {}
        if not isinstance(ch_cfg, dict):
            continue
        # OC's schema is inconsistent across channels: Slack and
        # Telegram store the bot token under ``botToken``; Discord
        # stores it under plain ``token``. Try both so the resolver
        # works across all SUPPORTED_CHANNELS without per-channel
        # special-cases here.
        for key in ("botToken", "token"):
            token = ch_cfg.get(key)
            if isinstance(token, str) and token.strip():
                return token.strip()
    return None


# ─── Platform-specific lookup ────────────────────────────────────────────────


def _http_get_json(
    url: str, timeout: int = 10, headers: "dict[str, str] | None" = None,
) -> tuple[dict | None, str | None]:
    """GET → JSON. Returns ``(payload, error_string)`` — exactly one
    is non-None. Used for both Telegram and Slack callouts.

    ``headers`` is optional for Authorization-bearer-style auth
    (Slack's modern API requires this; URL-token auth is rejected
    with ``invalid_auth``).
    """
    try:
        req = urllib.request.Request(url)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err = ""
        return None, f"http {e.code}: {err[:200] or e.reason}"
    except urllib.error.URLError as e:
        return None, f"network error: {e.reason}"
    except Exception as exc:  # noqa: BLE001
        return None, f"request error: {exc}"
    try:
        return json.loads(body), None
    except json.JSONDecodeError:
        return None, f"non-JSON response: {body[:200]}"


def _resolve_telegram(token: str, external_id: str) -> dict | None:
    """Call Telegram's ``getChat`` and return ``{username, name, raw}``
    on success, or None on any failure.

    Field mapping:
      - ``username`` — Telegram @handle (without the @). Optional in TG.
      - ``name`` — concat ``first_name`` + ``last_name`` when present,
        else the username, else None.
      - ``raw`` — the full result dict for any consumer that wants more
        detail.
    """
    url = f"https://api.telegram.org/bot{token}/getChat?chat_id={external_id}"
    payload, err = _http_get_json(url)
    if payload is None:
        log.debug("telegram getChat failed for %s: %s", external_id, err)
        return None
    if not payload.get("ok"):
        log.debug(
            "telegram getChat returned not-ok for %s: %s",
            external_id, payload.get("description"),
        )
        return None
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        return None
    username = result.get("username") or None
    first = result.get("first_name") or ""
    last = result.get("last_name") or ""
    full_name = (first + " " + last).strip() or username or None
    return {
        "username": username,
        "name": full_name,
        "raw": result,
    }


def slack_im_channel_to_user(token: str, channel_id: str) -> str | None:
    """Resolve a Slack IM channel id (``D...``) to its participating user.

    Slack 1:1 DM channels are bijective with the user on the other side
    of the conversation. ``conversations.info?channel=D...`` returns a
    channel record carrying ``{"is_im": true, "user": "U..."}`` for IM
    types — that's the user we'd actually want to attribute turns / pair
    / resolve a name for.

    Returns the user id (``U...``) on success, None on any failure:
    not an IM channel, missing scope (``conversations:read``), token
    rejected, network error. The caller treats None as "skip this
    candidate" — better to omit than mis-attribute.

    Background: turn records from the plugin TurnObserver write the
    sessionKey trailing id into the ``channel`` field. For Slack that's
    the conversation id, which for DMs is the D-channel — not the user.
    Pre-fix, downstream consumers (Discover-from-history) tried to
    resolve names by passing the D-id to users.info; that returns
    ``user_not_found``. Phase F.1 rewires the resolver path through
    this helper so Slack DMs surface real users + names.
    """
    if not channel_id or not channel_id.startswith(("D", "d")):
        # Not an IM channel — caller passed a user id or a public/private
        # channel id. No rewrite needed.
        return None
    url = f"https://slack.com/api/conversations.info?channel={channel_id}"
    payload, err = _http_get_json(
        url, headers={"Authorization": f"Bearer {token}"},
    )
    if payload is None:
        log.debug("slack conversations.info failed for %s: %s",
                  channel_id, err)
        return None
    if not payload.get("ok"):
        log.debug(
            "slack conversations.info returned not-ok for %s: %s",
            channel_id, payload.get("error"),
        )
        return None
    ch = payload.get("channel") or {}
    if not isinstance(ch, dict):
        return None
    if not ch.get("is_im"):
        # D-prefix but not actually an IM — extremely rare (mpim/shared
        # may share prefix in some workspaces); don't transmute.
        return None
    user = ch.get("user")
    return user if isinstance(user, str) and user else None


def _resolve_slack(token: str, external_id: str) -> dict | None:
    """Call Slack's ``users.info`` and return ``{username, name, email, raw}``.

    Uses ``Authorization: Bearer <token>`` header — the URL-parameter
    ``?token=`` form is rejected by Slack as ``invalid_auth`` on
    modern apps. ``email`` is populated when the bot's app has the
    ``users:read.email`` scope; otherwise it stays None.

    Accepts both user ids (``U...``) and IM channel ids (``D...``). For
    D-ids, transparently resolves the channel → user first via
    ``conversations.info`` so callers don't need per-id-shape special
    cases. Pre-Phase F.1, D-id calls returned None with ``user_not_found``.
    """
    if external_id and external_id.startswith(("D", "d")):
        resolved_user = slack_im_channel_to_user(token, external_id)
        if not resolved_user:
            return None  # not an IM, missing scope, or unresolvable
        external_id = resolved_user

    url = f"https://slack.com/api/users.info?user={external_id}"
    payload, err = _http_get_json(
        url, headers={"Authorization": f"Bearer {token}"},
    )
    if payload is None:
        log.debug("slack users.info failed for %s: %s", external_id, err)
        return None
    if not payload.get("ok"):
        log.debug(
            "slack users.info returned not-ok for %s: %s",
            external_id, payload.get("error"),
        )
        return None
    user = payload.get("user") or {}
    if not isinstance(user, dict):
        return None
    profile = user.get("profile") or {}
    username = user.get("name") or None
    # Prefer real_name, then display_name, then the @handle, then None.
    display = (
        profile.get("real_name")
        or profile.get("display_name")
        or user.get("real_name")
        or username
        or None
    )
    # Email only present when the bot's app has users:read.email scope.
    # When absent or empty, return None — caller doesn't render the row.
    email_raw = profile.get("email")
    email = email_raw.strip() if isinstance(email_raw, str) and email_raw.strip() else None
    return {
        "username": username,
        "name": display,
        "email": email,
        "raw": user,
    }


def _resolve_discord(token: str, external_id: str) -> dict | None:
    """Call Discord's ``GET /users/<id>`` and return ``{username, name, raw}``.

    Auth: ``Authorization: Bot <token>`` header (Discord requires the
    literal "Bot " prefix). Bot-shared-guild requirement: Discord
    permits a bot to fetch any user record so long as it shares a
    guild with that user — which is the case for paired users on
    Evolve's Discord-enabled bots.

    Display-name preference:
      - ``global_name`` — the Discord "display name" (set per-user, may
        be empty for older accounts that never set one).
      - ``username`` — the @handle (always present post-2023; pre-2023
        accounts also carried a ``discriminator`` which Discord has
        since retired).

    No email exposed — Discord's user record only includes email when
    the operator is fetching ``/users/@me`` with the ``email`` OAuth
    scope, which is the user's own access flow, not the bot's.
    """
    url = f"https://discord.com/api/v10/users/{external_id}"
    # I.1 — Discord requires a User-Agent on bot API requests; without
    # one, Cloudflare returns 403 + Discord error code 1010 ("banned
    # based on browser signature"). The spec format is
    # ``DiscordBot ($url, $version)`` — see
    # https://discord.com/developers/docs/reference#user-agent. Curl
    # worked manually because curl sends its own UA; Python urllib
    # without an explicit one gets blocked.
    payload, err = _http_get_json(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/evolve-ops/evolve, 1.0)",
        },
    )
    if payload is None:
        log.debug("discord users/%s failed: %s", external_id, err)
        return None
    if not isinstance(payload, dict) or not payload.get("id"):
        # Discord returns a plain user dict on success; a missing id
        # signals an error envelope ({code, message}) or junk.
        log.debug(
            "discord users/%s returned non-user payload: %s",
            external_id, payload,
        )
        return None
    username = payload.get("username") or None
    global_name = payload.get("global_name") or None
    name = global_name or username
    return {
        "username": username,
        "name": name,
        "raw": payload,
    }


# ─── Public entry point + cache ─────────────────────────────────────────────


def _cache_key(channel: str, external_id: str) -> str:
    return f"{channel.lower()}:{external_id}"


DEFAULT_CACHE_TTL_DAYS: int = 7


def _entry_is_stale(entry: dict, max_age_days: int) -> bool:
    """Treat entries older than ``max_age_days`` as cache misses.

    Future-dated ``cached_at`` (clock skew, corrupted data) is treated
    as fresh — we don't try to repair weird timestamps. Missing or
    unparseable ``cached_at`` is also treated as fresh, since the
    entry might come from a pre-TTL writer; callers that want strict
    expiry can wipe the cache.
    """
    if max_age_days <= 0:
        return False
    cached_at = entry.get("cached_at")
    if not isinstance(cached_at, str) or not cached_at:
        return False
    try:
        ts = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - ts
    return age.total_seconds() > max_age_days * 86400


def cached_name(
    network: dict[str, Any], channel: str, external_id: str,
    max_age_days: int = DEFAULT_CACHE_TTL_DAYS,
) -> dict | None:
    """Look up a cached resolved name in ``pod.admins.resolved_names``,
    without making an API call. Returns the cache entry (or None).

    Entries older than ``max_age_days`` are treated as misses so
    callers re-resolve from the channel API. Pass ``max_age_days=0``
    to skip the freshness check (used by callers that explicitly want
    whatever's cached regardless of age).
    """
    pod = network.get("pod") or {}
    if not isinstance(pod, dict):
        return None
    admins = pod.get("admins") or {}
    if not isinstance(admins, dict):
        return None
    cache = admins.get("resolved_names") or {}
    if not isinstance(cache, dict):
        return None
    entry = cache.get(_cache_key(channel, external_id))
    if not isinstance(entry, dict):
        return None
    if _entry_is_stale(entry, max_age_days):
        return None
    return entry


def write_cache_entry(
    network: dict[str, Any],
    channel: str,
    external_id: str,
    resolved: dict,
) -> None:
    """Persist a resolved-name lookup into ``network``. Caller is
    responsible for ``save_network`` afterward."""
    pod = network.setdefault("pod", {})
    if not isinstance(pod, dict):
        pod = {}
        network["pod"] = pod
    admins = pod.setdefault("admins", {})
    if not isinstance(admins, dict):
        admins = {}
        pod["admins"] = admins
    cache = admins.setdefault("resolved_names", {})
    if not isinstance(cache, dict):
        cache = {}
        admins["resolved_names"] = cache
    entry: dict[str, Any] = {
        "username": resolved.get("username"),
        "name": resolved.get("name"),
        "cached_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ).replace("+00:00", "Z"),
    }
    # Email only carried when the channel exposes it (Slack with
    # users:read.email scope). Keep the key absent when None so the
    # JSON stays small and we don't confuse "absent" with "explicitly
    # null" downstream.
    if resolved.get("email"):
        entry["email"] = resolved["email"]
    cache[_cache_key(channel, external_id)] = entry


def resolve(
    network: dict[str, Any],
    *,
    channel: str,
    external_id: str,
    use_cache: bool = True,
    bot_id: "str | None" = None,
) -> dict | None:
    """Look up a user by (channel, external_id).

    Strategy:
      1. If ``use_cache`` and a cache entry exists, return it.
      2. Find a bot with the channel configured + grab its token —
         preferring ``bot_id`` when provided (the bot whose roster
         this lookup is for).
      3. Call the platform-specific API.
      4. Cache the result in ``network`` (caller persists).

    ``bot_id`` is the bot whose roster we're populating. When provided,
    the token search prefers that bot's own openclaw.json::channels.<ch>.botToken
    over the primary or other members. Critical for Slack because each
    Slack token is workspace-scoped — a bot's token can only resolve
    users in its own Slack workspace. Pre-2026-06-08 the resolver
    used whichever bot's token came first in iteration order, which
    silently failed (``user_not_found``) for cross-workspace bots.

    Returns the cache-shaped dict ``{username, name, cached_at}`` on
    success, or None when:
      - the channel isn't supported (Discord / WhatsApp / etc.)
      - no bot has the channel configured
      - the platform API call fails (user doesn't exist, token
        invalid, network error, etc.)

    Callers should be defensive about the None case — it's not an
    error condition for the operator, just "we couldn't auto-discover
    the name; show the raw ID instead and let the operator type a
    display name manually."
    """
    if channel not in SUPPORTED_CHANNELS:
        return None
    if not external_id:
        return None

    if use_cache:
        cached = cached_name(network, channel, external_id)
        if cached is not None:
            return cached

    token = _channel_token(network, channel, prefer_bot_id=bot_id)
    if not token:
        return None

    if channel == "telegram":
        resolved = _resolve_telegram(token, external_id)
    elif channel == "slack":
        resolved = _resolve_slack(token, external_id)
    elif channel == "discord":
        resolved = _resolve_discord(token, external_id)
    else:
        return None

    if resolved is None:
        return None

    write_cache_entry(network, channel, external_id, resolved)
    # Return the cache-shaped dict (no ``raw``) for consistency with
    # the cached_name path. ``email`` is only set when the channel
    # exposes it (Slack with users:read.email scope) — keep it absent
    # otherwise so downstream stays compact.
    out: dict[str, Any] = {
        "username": resolved.get("username"),
        "name": resolved.get("name"),
        "cached_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ).replace("+00:00", "Z"),
    }
    if resolved.get("email"):
        out["email"] = resolved["email"]
    return out
