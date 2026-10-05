"""tests/test_name_resolver.py — Telegram + Slack username lookup.

The resolver makes real-looking HTTP calls via urllib; we monkey-patch
``urllib.request.urlopen`` so tests don't hit the network. The
``_channel_token`` helper reads the primary bot's openclaw.json from
``/Users/<bot>/.openclaw/`` — also monkey-patched so we don't need
filesystem fixtures.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin.evo import name_resolver  # noqa: E402


class _FakeResp:
    """Mimics urllib's HTTPResponse just enough for the resolver."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _fake_urlopen(body: str | dict):
    if isinstance(body, dict):
        body = json.dumps(body)
    return lambda url, timeout=None: _FakeResp(body)


def _patch_channel_token(monkeypatch, channel: str, token: str = "TOK_TEST"):
    """Stub `_channel_token` so tests don't read real openclaw.json.

    Accepts **kwargs so the new ``prefer_bot_id`` kwarg added in
    2026-06-08 (workspace-scoped token fix) doesn't trip this stub.
    Tests that don't care about per-bot token routing just want any
    token; tests that DO care patch _channel_token themselves.
    """
    def fake(network, ch, **_kw):
        if ch == channel:
            return token
        return None
    monkeypatch.setattr(name_resolver, "_channel_token", fake)


def _net() -> dict:
    return {
        "primary": "evolve",
        "bots": {"evolve": {"role": "primary"}},
        "pod": {"admins": {"external_ids": {}}},
    }


# ─────────────────────────────────────────────────────────────────────────────
# SUPPORTED_CHANNELS
# ─────────────────────────────────────────────────────────────────────────────


def test_supported_channels_includes_telegram_slack_discord():
    assert "telegram" in name_resolver.SUPPORTED_CHANNELS
    assert "slack" in name_resolver.SUPPORTED_CHANNELS
    assert "discord" in name_resolver.SUPPORTED_CHANNELS


def test_unsupported_channel_returns_none(monkeypatch):
    net = _net()
    # WhatsApp doesn't have resolver coverage yet; resolve() bails early.
    assert name_resolver.resolve(
        net, channel="whatsapp", external_id="+15551234",
    ) is None


def test_empty_external_id_returns_none():
    net = _net()
    assert name_resolver.resolve(
        net, channel="telegram", external_id="",
    ) is None


# ─────────────────────────────────────────────────────────────────────────────
# Telegram resolution
# ─────────────────────────────────────────────────────────────────────────────


def test_telegram_resolve_happy_path(monkeypatch):
    net = _net()
    _patch_channel_token(monkeypatch, "telegram")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        _fake_urlopen({
                            "ok": True,
                            "result": {
                                "id": 1260193629,
                                "type": "private",
                                "username": "cjalden",
                                "first_name": "Pod_admin",
                                "last_name": "Alden",
                            },
                        }))
    resolved = name_resolver.resolve(
        net, channel="telegram", external_id="1260193629",
    )
    assert resolved is not None
    assert resolved["username"] == "cjalden"
    assert resolved["name"] == "Pod_admin Alden"
    assert resolved["cached_at"].endswith("Z")


def test_telegram_resolve_falls_back_to_username_when_name_missing(monkeypatch):
    net = _net()
    _patch_channel_token(monkeypatch, "telegram")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        _fake_urlopen({
                            "ok": True,
                            "result": {
                                "id": 999,
                                "type": "private",
                                "username": "anon99",
                            },
                        }))
    resolved = name_resolver.resolve(
        net, channel="telegram", external_id="999",
    )
    assert resolved["name"] == "anon99"
    assert resolved["username"] == "anon99"


def test_telegram_resolve_handles_api_error(monkeypatch):
    """Telegram API returns ok:false for unknown users / bad tokens.
    Resolver should return None, not crash."""
    net = _net()
    _patch_channel_token(monkeypatch, "telegram")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        _fake_urlopen({
                            "ok": False,
                            "error_code": 400,
                            "description": "chat not found",
                        }))
    assert name_resolver.resolve(
        net, channel="telegram", external_id="000",
    ) is None


def test_telegram_resolve_handles_network_error(monkeypatch):
    """urlopen raises (e.g., DNS failure). Resolver returns None."""
    import urllib.error
    net = _net()
    _patch_channel_token(monkeypatch, "telegram")

    def boom(*a, **k):
        raise urllib.error.URLError("DNS unreachable")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen", boom)
    assert name_resolver.resolve(
        net, channel="telegram", external_id="1",
    ) is None


def test_telegram_resolve_returns_none_when_no_token(monkeypatch):
    """No bot has telegram configured → resolver returns None.
    The route translates this into ok:false with a clear reason."""
    net = _net()
    monkeypatch.setattr(name_resolver, "_channel_token", lambda n, c, **_kw: None)
    assert name_resolver.resolve(
        net, channel="telegram", external_id="1",
    ) is None


# ─────────────────────────────────────────────────────────────────────────────
# Slack resolution
# ─────────────────────────────────────────────────────────────────────────────


def test_slack_resolve_happy_path(monkeypatch):
    net = _net()
    _patch_channel_token(monkeypatch, "slack")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        _fake_urlopen({
                            "ok": True,
                            "user": {
                                "id": "U0PLKKXV0",
                                "name": "pod_admin",
                                "real_name": "Pod_admin Alden",
                                "profile": {
                                    "real_name": "Pod_admin Alden",
                                    "display_name": "pod_admin.a",
                                },
                            },
                        }))
    resolved = name_resolver.resolve(
        net, channel="slack", external_id="U0PLKKXV0",
    )
    assert resolved is not None
    assert resolved["username"] == "pod_admin"
    assert resolved["name"] == "Pod_admin Alden"


def test_slack_resolve_prefers_display_name_when_no_real_name(monkeypatch):
    net = _net()
    _patch_channel_token(monkeypatch, "slack")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        _fake_urlopen({
                            "ok": True,
                            "user": {
                                "id": "U123", "name": "anon",
                                "profile": {"display_name": "anon-display"},
                            },
                        }))
    resolved = name_resolver.resolve(
        net, channel="slack", external_id="U123",
    )
    assert resolved["name"] == "anon-display"


def test_slack_resolve_handles_api_error(monkeypatch):
    net = _net()
    _patch_channel_token(monkeypatch, "slack")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        _fake_urlopen({"ok": False, "error": "user_not_found"}))
    assert name_resolver.resolve(
        net, channel="slack", external_id="U_unknown",
    ) is None


# ─────────────────────────────────────────────────────────────────────────────
# Caching
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_caches_in_network(monkeypatch):
    net = _net()
    _patch_channel_token(monkeypatch, "telegram")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        _fake_urlopen({
                            "ok": True,
                            "result": {
                                "id": 1, "type": "private",
                                "username": "u", "first_name": "User",
                            },
                        }))
    name_resolver.resolve(
        net, channel="telegram", external_id="1",
    )
    cache = net["pod"]["admins"]["resolved_names"]
    assert "telegram:1" in cache
    entry = cache["telegram:1"]
    assert entry["username"] == "u"
    assert entry["name"] == "User"
    assert "cached_at" in entry


def test_resolve_uses_cache_on_repeat_call(monkeypatch):
    """A second resolve(...) with use_cache=True (default) returns
    the cached entry without an API call."""
    net = _net()
    call_count = {"n": 0}

    def counting_urlopen(url, timeout=None):
        call_count["n"] += 1
        return _FakeResp(json.dumps({
            "ok": True,
            "result": {
                "id": 1, "type": "private",
                "username": "u", "first_name": "User",
            },
        }))

    _patch_channel_token(monkeypatch, "telegram")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        counting_urlopen)

    name_resolver.resolve(net, channel="telegram", external_id="1")
    assert call_count["n"] == 1
    name_resolver.resolve(net, channel="telegram", external_id="1")
    assert call_count["n"] == 1, "second call should use cache"


def test_resolve_use_cache_false_refetches(monkeypatch):
    """When ``use_cache=False`` (the refresh path), even a cached
    entry triggers a fresh API call."""
    net = _net()
    call_count = {"n": 0}

    def counting_urlopen(url, timeout=None):
        call_count["n"] += 1
        return _FakeResp(json.dumps({
            "ok": True,
            "result": {
                "id": 1, "type": "private",
                "username": "u", "first_name": "User",
            },
        }))

    _patch_channel_token(monkeypatch, "telegram")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        counting_urlopen)

    name_resolver.resolve(net, channel="telegram", external_id="1")
    name_resolver.resolve(
        net, channel="telegram", external_id="1", use_cache=False,
    )
    assert call_count["n"] == 2


def test_cached_name_returns_none_when_absent():
    net = _net()
    assert name_resolver.cached_name(
        net, "telegram", "missing",
    ) is None


def test_cached_name_returns_entry_when_present(monkeypatch):
    net = _net()
    _patch_channel_token(monkeypatch, "telegram")
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen",
                        _fake_urlopen({
                            "ok": True,
                            "result": {
                                "id": 1, "username": "u",
                                "first_name": "User",
                            },
                        }))
    name_resolver.resolve(net, channel="telegram", external_id="1")
    entry = name_resolver.cached_name(net, "telegram", "1")
    assert entry is not None
    assert entry["username"] == "u"


# ─────────────────────────────────────────────────────────────────────────────
# Discord resolution
# ─────────────────────────────────────────────────────────────────────────────


def test_discord_resolve_prefers_global_name(monkeypatch):
    """Discord display-name preference: global_name > username."""
    net = _net()
    _patch_channel_token(monkeypatch, "discord")
    monkeypatch.setattr(
        name_resolver.urllib.request, "urlopen",
        _fake_urlopen({
            "id": "987654321012345678",
            "username": "samdev",
            "global_name": "Sam Carter",
            "discriminator": "0",  # post-username-rollout accounts
        }),
    )
    result = name_resolver.resolve(
        net, channel="discord", external_id="987654321012345678",
    )
    assert result is not None
    assert result["name"] == "Sam Carter"
    assert result["username"] == "samdev"


def test_discord_resolve_falls_back_to_username_when_no_global_name(
        monkeypatch):
    """Pre-2023 accounts may not have a global_name set."""
    net = _net()
    _patch_channel_token(monkeypatch, "discord")
    monkeypatch.setattr(
        name_resolver.urllib.request, "urlopen",
        _fake_urlopen({
            "id": "111", "username": "olduser",
            "global_name": None, "discriminator": "1234",
        }),
    )
    result = name_resolver.resolve(
        net, channel="discord", external_id="111",
    )
    assert result is not None
    assert result["name"] == "olduser"


def test_discord_resolve_returns_none_on_error_envelope(monkeypatch):
    """Discord returns {code, message} on errors (e.g. unknown user);
    treat any non-user payload as a miss."""
    net = _net()
    _patch_channel_token(monkeypatch, "discord")
    monkeypatch.setattr(
        name_resolver.urllib.request, "urlopen",
        _fake_urlopen({"code": 10013, "message": "Unknown User"}),
    )
    assert name_resolver.resolve(
        net, channel="discord", external_id="999",
    ) is None


def test_discord_request_includes_user_agent_header(monkeypatch):
    """I.1 — Discord rejects requests without a User-Agent with
    Cloudflare 403 + error code 1010. Pin that the resolver sends a
    DiscordBot-format UA so the request actually succeeds."""
    net = _net()
    _patch_channel_token(monkeypatch, "discord")
    captured: dict = {}
    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return _FakeResp(json.dumps({
            "id": "1", "username": "u", "global_name": "U",
        }))
    monkeypatch.setattr(name_resolver.urllib.request, "urlopen", fake_urlopen)
    name_resolver.resolve(net, channel="discord", external_id="1")
    # Header key casing depends on urllib's normalization; check
    # case-insensitively.
    ua = next((v for k, v in captured["headers"].items()
               if k.lower() == "user-agent"), "")
    assert "DiscordBot" in ua


def test_discord_resolve_returns_none_when_no_token(monkeypatch):
    net = _net()
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda n, c, **_kw: None,
    )
    assert name_resolver.resolve(
        net, channel="discord", external_id="111",
    ) is None


# ─────────────────────────────────────────────────────────────────────────────
# Slack email extraction
# ─────────────────────────────────────────────────────────────────────────────


def test_slack_resolve_extracts_email_when_scope_present(monkeypatch):
    """When users:read.email scope is granted, profile.email is returned
    and persisted into the cache entry."""
    net = _net()
    _patch_channel_token(monkeypatch, "slack")
    monkeypatch.setattr(
        name_resolver.urllib.request, "urlopen",
        _fake_urlopen({
            "ok": True,
            "user": {
                "id": "U001", "name": "stephanie",
                "real_name": "Stephanie Smith",
                "profile": {
                    "real_name": "Stephanie Smith",
                    "display_name": "steph",
                    "email": "stephanie@example.com",
                },
            },
        }),
    )
    result = name_resolver.resolve(
        net, channel="slack", external_id="U001",
    )
    assert result is not None
    assert result["email"] == "stephanie@example.com"
    # Cache entry persists email too.
    cached = name_resolver.cached_name(net, "slack", "U001")
    assert cached["email"] == "stephanie@example.com"


def test_slack_resolve_omits_email_when_scope_absent(monkeypatch):
    """No email field in the API response (scope missing) → no email in
    the result dict OR cache entry, not even as null."""
    net = _net()
    _patch_channel_token(monkeypatch, "slack")
    monkeypatch.setattr(
        name_resolver.urllib.request, "urlopen",
        _fake_urlopen({
            "ok": True,
            "user": {
                "id": "U002", "name": "marcus",
                "real_name": "Marcus L",
                "profile": {"real_name": "Marcus L"},
            },
        }),
    )
    result = name_resolver.resolve(
        net, channel="slack", external_id="U002",
    )
    assert result is not None
    assert "email" not in result
    cached = name_resolver.cached_name(net, "slack", "U002")
    assert "email" not in cached


# ─────────────────────────────────────────────────────────────────────────────
# TTL on cached_name
# ─────────────────────────────────────────────────────────────────────────────


def test_cached_name_returns_fresh_entry():
    """An entry from 1 day ago is returned at the 7-day default TTL."""
    from datetime import datetime, timedelta, timezone
    net = _net()
    name_resolver.write_cache_entry(
        net, "telegram", "1",
        {"username": "u", "name": "User"},
    )
    # Backdate by 1 day
    one_day_ago = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    net["pod"]["admins"]["resolved_names"][
        "telegram:1"]["cached_at"] = one_day_ago
    assert name_resolver.cached_name(net, "telegram", "1") is not None


def test_cached_name_treats_stale_entry_as_miss():
    """An entry from 8 days ago is invisible at the 7-day default TTL."""
    from datetime import datetime, timedelta, timezone
    net = _net()
    name_resolver.write_cache_entry(
        net, "telegram", "1",
        {"username": "u", "name": "User"},
    )
    eight_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=8)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    net["pod"]["admins"]["resolved_names"][
        "telegram:1"]["cached_at"] = eight_days_ago
    assert name_resolver.cached_name(net, "telegram", "1") is None


def test_cached_name_max_age_zero_disables_ttl():
    """Passing max_age_days=0 returns entries regardless of age."""
    from datetime import datetime, timedelta, timezone
    net = _net()
    name_resolver.write_cache_entry(
        net, "telegram", "1",
        {"username": "u", "name": "User"},
    )
    year_ago = (
        datetime.now(timezone.utc) - timedelta(days=365)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    net["pod"]["admins"]["resolved_names"][
        "telegram:1"]["cached_at"] = year_ago
    assert name_resolver.cached_name(
        net, "telegram", "1", max_age_days=0,
    ) is not None


def test_cached_name_handles_unparseable_cached_at():
    """A garbled timestamp shouldn't make the entry vanish — treat as
    fresh (defensive) so callers see the cached data."""
    net = _net()
    name_resolver.write_cache_entry(
        net, "telegram", "1",
        {"username": "u", "name": "User"},
    )
    net["pod"]["admins"]["resolved_names"][
        "telegram:1"]["cached_at"] = "not-an-iso-timestamp"
    assert name_resolver.cached_name(net, "telegram", "1") is not None


def test_stale_cache_triggers_fresh_resolve(monkeypatch):
    """When cached entry is stale, resolve() bypasses it and re-fetches.
    This is the behavior that closes the TTL gap from the spec."""
    from datetime import datetime, timedelta, timezone
    net = _net()
    _patch_channel_token(monkeypatch, "telegram")
    # Seed with a stale entry.
    name_resolver.write_cache_entry(
        net, "telegram", "1",
        {"username": "old_name", "name": "Old Name"},
    )
    ten_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    net["pod"]["admins"]["resolved_names"][
        "telegram:1"]["cached_at"] = ten_days_ago
    # Re-resolve via the API: returns a new name.
    monkeypatch.setattr(
        name_resolver.urllib.request, "urlopen",
        _fake_urlopen({
            "ok": True,
            "result": {"id": 1, "username": "new_name",
                       "first_name": "New", "last_name": "Name"},
        }),
    )
    result = name_resolver.resolve(
        net, channel="telegram", external_id="1",
    )
    assert result["name"] == "New Name"
    # Cache now has the fresh entry.
    cached = name_resolver.cached_name(net, "telegram", "1")
    assert cached["name"] == "New Name"


# ─────────────────────────────────────────────────────────────────────────────
# Workspace-scoped token routing (Slack fix — 2026-06-08)
# ─────────────────────────────────────────────────────────────────────────────


def _patch_channel_token_per_bot(monkeypatch, tokens: dict[str, str]):
    """Stub that returns a token based on which bot the resolver asks for.

    ``tokens`` maps bot_id → token. When ``prefer_bot_id`` matches one,
    return its token; otherwise return the first bot's token (mimics
    the fallback). None when no tokens are configured.
    """
    def fake(network, ch, **kw):
        if ch != "slack":
            return None
        prefer = kw.get("prefer_bot_id")
        if prefer and prefer in tokens:
            return tokens[prefer]
        # Fallback: primary-then-first, but for the test the iteration
        # order matches dict insertion. Return first available.
        for tok in tokens.values():
            return tok
        return None
    monkeypatch.setattr(name_resolver, "_channel_token", fake)


def test_channel_token_prefers_named_bot_when_supplied(monkeypatch):
    """The prefer_bot_id parameter selects that bot's token first —
    critical for Slack where tokens are workspace-scoped."""
    net = {
        "primary": "team_bot_a",
        "bots": {"team_bot_a": {}, "team_bot_b": {}},
    }
    # Stub _read_bot_oc_config so the real openclaw.json reader isn't
    # invoked. team_bot_a's config has token "A"; team_bot_b's has "B".
    def fake_oc(bot_user):
        return {
            "team_bot_a": {"channels": {"slack": {"botToken": "TOKEN_A"}}},
            "team_bot_b": {"channels": {"slack": {"botToken": "TOKEN_B"}}},
        }.get(bot_user)
    monkeypatch.setattr(name_resolver, "_read_bot_oc_config", fake_oc)

    # No prefer_bot_id → primary wins (team_bot_a).
    assert name_resolver._channel_token(net, "slack") == "TOKEN_A"

    # prefer_bot_id=team_bot_b → team_bot_b's token wins, NOT primary.
    assert name_resolver._channel_token(
        net, "slack", prefer_bot_id="team_bot_b") == "TOKEN_B"


def test_channel_token_falls_back_when_prefer_bot_has_no_token(monkeypatch):
    """If the preferred bot has no token, the search continues with
    primary then other members — never strands the lookup just because
    the operator's bot doesn't have its own Slack app yet."""
    net = {
        "primary": "team_bot_a",
        "bots": {"team_bot_a": {}, "team_bot_b": {}},
    }
    def fake_oc(bot_user):
        return {
            # team_bot_a has a token; team_bot_b does NOT.
            "team_bot_a": {"channels": {"slack": {"botToken": "TOKEN_A"}}},
            "team_bot_b": {"channels": {}},
        }.get(bot_user)
    monkeypatch.setattr(name_resolver, "_read_bot_oc_config", fake_oc)

    # prefer_bot_id=team_bot_b → no token there, falls back to primary's.
    assert name_resolver._channel_token(
        net, "slack", prefer_bot_id="team_bot_b") == "TOKEN_A"


def test_channel_token_returns_none_when_no_tokens_anywhere(monkeypatch):
    net = {
        "primary": "team_bot_a",
        "bots": {"team_bot_a": {}, "team_bot_b": {}},
    }
    monkeypatch.setattr(name_resolver, "_read_bot_oc_config",
                        lambda _user: {"channels": {}})
    assert name_resolver._channel_token(
        net, "slack", prefer_bot_id="team_bot_a") is None


def test_channel_token_de_dupes_when_prefer_is_also_primary(monkeypatch):
    """When prefer_bot_id == primary, the iteration shouldn't try the
    same bot twice. Smoke check — no observable effect, but the search
    list should be tight."""
    net = {
        "primary": "team_bot_a",
        "bots": {"team_bot_a": {}, "team_bot_b": {}},
    }
    visited: list[str] = []
    def fake_oc(bot_user):
        visited.append(bot_user)
        return {"channels": {"slack": {"botToken": f"T_{bot_user}"}}}
    monkeypatch.setattr(name_resolver, "_read_bot_oc_config", fake_oc)
    assert name_resolver._channel_token(
        net, "slack", prefer_bot_id="team_bot_a") == "T_team_bot_a"
    # team_bot_a visited exactly once.
    assert visited.count("team_bot_a") == 1


def test_resolve_threads_bot_id_to_channel_token(monkeypatch):
    """End-to-end: resolve(bot_id=X) → _channel_token(prefer_bot_id=X)
    → that bot's token used for the API call. Regression guard against
    the C.5 cross-workspace bug where the wrong bot's token was used."""
    tokens_seen: list[str] = []
    def fake_token(network, ch, **kw):
        tok = f"TOK_{kw.get('prefer_bot_id') or 'PRIMARY'}"
        tokens_seen.append(tok)
        return tok
    monkeypatch.setattr(name_resolver, "_channel_token", fake_token)
    # Stub the slack call so we just check what token was used.
    seen_calls: list[str] = []
    def fake_resolve_slack(token, ext_id):
        seen_calls.append(token)
        return {"username": "u", "name": "n", "email": None, "raw": {}}
    monkeypatch.setattr(name_resolver, "_resolve_slack", fake_resolve_slack)
    net = _net()
    name_resolver.resolve(net, channel="slack", external_id="U999",
                          bot_id="team-bot-b", use_cache=False)
    assert tokens_seen == ["TOK_team-bot-b"]
    assert seen_calls == ["TOK_team-bot-b"]

