"""tests/test_conversation_resolver.py — conversation id → label resolution.

The sibling test to ``test_name_resolver.py``. The resolver makes its HTTP
calls through ``name_resolver._http_get_json`` (reused primitive), so every
call — Slack ``conversations.info``, Slack ``users.info`` (for DM peers, via
``name_resolver.resolve``), and Telegram ``getChat`` — routes through
``name_resolver.urllib.request.urlopen``. We monkey-patch that single surface
(URL-routed for multi-call scenarios) so nothing hits the network, mirroring
how the name-resolver tests patch ``urlopen`` + ``_channel_token``.

All ids and names below are FAKE (the public-launch scrub ratchet rejects
real identifiers).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin.evo import conversation_resolver as cr  # noqa: E402
from evolve_admin.evo import name_resolver as nr  # noqa: E402


# ─── fakes ──────────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _route_urlopen(routes, captured=None):
    """Build a urlopen stub that returns a body keyed by URL substring.

    ``routes`` is a list of ``(substring, body_dict)`` — the first substring
    found in the request URL wins. ``captured`` (optional list) collects every
    requested URL so tests can assert on what was actually called.
    """
    def fake(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if captured is not None:
            captured.append(url)
        for sub, body in routes:
            if sub in url:
                return _FakeResp(json.dumps(body))
        return _FakeResp(json.dumps({"ok": False, "error": "no_route"}))
    return fake


def _patch_token(monkeypatch, *, token="TOK_TEST"):
    """Stub ``_channel_token`` so we never read a real openclaw.json.

    Returns ``token`` for any supported conversation platform; covers both
    ``conversation_resolver``'s call and ``name_resolver.resolve``'s call,
    since both go through ``name_resolver._channel_token``.
    """
    def fake(network, ch, **_kw):
        return token if ch in cr.SUPPORTED_CONVERSATION_PLATFORMS else None
    monkeypatch.setattr(nr, "_channel_token", fake)


def _net() -> dict:
    return {
        "primary": "fakebot",
        "bots": {"fakebot": {"role": "primary"}},
        "pod": {"admins": {"external_ids": {}}},
    }


# ─── platform gate ───────────────────────────────────────────────────────────


def test_supported_platforms_are_slack_and_telegram():
    assert cr.SUPPORTED_CONVERSATION_PLATFORMS == frozenset({"slack", "telegram"})


def test_unsupported_platform_returns_none_no_cache(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    assert cr.resolve_conversation(net, "discord", "999000111") is None
    # No negative cache for an unsupported platform — a future impl populates it.
    assert not (net.get("pod", {}).get("conversations"))


def test_empty_conv_id_returns_none(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    assert cr.resolve_conversation(net, "slack", "") is None


def test_no_token_returns_none_without_poisoning_cache(monkeypatch):
    net = _net()
    monkeypatch.setattr(nr, "_channel_token", lambda n, c, **_kw: None)
    assert cr.resolve_conversation(net, "slack", "C0FAKE01") is None
    assert not (net.get("pod", {}).get("conversations"))


# ─── Slack channels ──────────────────────────────────────────────────────────


def test_slack_public_channel(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "C0FAKE01", "name": "product-team",
                        "is_channel": True, "is_private": False},
        }),
    ]))
    out = cr.resolve_conversation(net, "slack", "C0FAKE01")
    assert out is not None
    assert out["kind"] == "channel"
    assert out["name"] == "product-team"
    assert out["label"] == "#product-team"
    assert out["resolved"] is True
    assert out["cached_at"].endswith("Z")


def test_slack_private_channel(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "G0FAKE02", "name": "leadership",
                        "is_group": True, "is_private": True},
        }),
    ]))
    out = cr.resolve_conversation(net, "slack", "G0FAKE02")
    assert out["kind"] == "private"
    assert out["label"] == "#leadership"


def test_slack_dm_resolves_peer_name(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "D0FAKE03", "is_im": True, "user": "U0FAKEUSR"},
        }),
        ("users.info", {
            "ok": True,
            "user": {"id": "U0FAKEUSR", "name": "alice",
                     "profile": {"real_name": "Alice Fakerson"}},
        }),
    ]))
    out = cr.resolve_conversation(net, "slack", "D0FAKE03")
    assert out["kind"] == "dm"
    assert out["name"] == "Alice Fakerson"
    assert out["label"] == "DM · Alice Fakerson"


def test_slack_dm_unresolvable_peer_falls_back_to_generic(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "D0FAKE04", "is_im": True, "user": "U0GONE99"},
        }),
        ("users.info", {"ok": False, "error": "user_not_found"}),
    ]))
    out = cr.resolve_conversation(net, "slack", "D0FAKE04")
    assert out["kind"] == "dm"
    assert out["name"] is None
    assert out["label"] == "Direct message"


def test_slack_group_dm_mpim(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "G0FAKE05", "is_mpim": True},
        }),
    ]))
    out = cr.resolve_conversation(net, "slack", "G0FAKE05")
    assert out["kind"] == "group_dm"
    assert out["label"] == "Group DM"


def test_slack_lowercase_conversation_id_is_upcased_for_api(monkeypatch):
    """A lower-cased session-key stem (``c7t9fby6s``) is up-cased for the
    conversations.info call, but the cache key keeps the original casing."""
    net = _net()
    _patch_token(monkeypatch)
    captured: list[str] = []
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "C7T9FBY6S", "name": "general",
                        "is_channel": True},
        }),
    ], captured=captured))
    out = cr.resolve_conversation(net, "slack", "c7t9fby6s")
    assert out["label"] == "#general"
    # API was called with the upper-cased id.
    assert any("C7T9FBY6S" in u for u in captured)
    # Cache key preserves the original lower-cased id (what a reader passes).
    assert "slack:c7t9fby6s" in net["pod"]["conversations"]["resolved"]


def test_slack_unclassifiable_writes_negative_cache(monkeypatch):
    """conversations.info succeeds but the channel is neither im/mpim nor has a
    name → unresolvable → negative sentinel cached, resolve returns None."""
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True, "channel": {"id": "C0WEIRD0"},  # no name, no flags
        }),
    ]))
    assert cr.resolve_conversation(net, "slack", "C0WEIRD0") is None
    entry = net["pod"]["conversations"]["resolved"]["slack:C0WEIRD0"]
    assert entry["resolved"] is False
    assert entry["label"] is None


def test_slack_api_error_writes_negative_cache(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {"ok": False, "error": "channel_not_found"}),
    ]))
    assert cr.resolve_conversation(net, "slack", "C0MISSING1") is None
    assert net["pod"]["conversations"]["resolved"][
        "slack:C0MISSING1"]["resolved"] is False


# ─── Telegram chats ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
def test_telegram_group_uses_title(monkeypatch, chat_type):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("getChat", {
            "ok": True,
            "result": {"id": -1009000111, "type": chat_type,
                       "title": "Fake Standup Crew"},
        }),
    ]))
    out = cr.resolve_conversation(net, "telegram", "-1009000111")
    assert out["kind"] == "telegram_group"
    assert out["name"] == "Fake Standup Crew"
    assert out["label"] == "Fake Standup Crew"


def test_telegram_private_chat_is_dm(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("getChat", {
            "ok": True,
            "result": {"id": 1260000000, "type": "private",
                       "first_name": "Bob", "last_name": "Fakeman",
                       "username": "bobf"},
        }),
    ]))
    out = cr.resolve_conversation(net, "telegram", "1260000000")
    assert out["kind"] == "dm"
    assert out["label"] == "DM · Bob Fakeman"


def test_telegram_api_error_writes_negative_cache(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("getChat", {"ok": False, "description": "chat not found"}),
    ]))
    assert cr.resolve_conversation(net, "telegram", "999000222") is None
    assert net["pod"]["conversations"]["resolved"][
        "telegram:999000222"]["resolved"] is False


# ─── cache read / write ──────────────────────────────────────────────────────


def test_cache_stores_under_pod_conversations_resolved(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "C0FAKE06", "name": "random", "is_channel": True},
        }),
    ]))
    cr.resolve_conversation(net, "slack", "C0FAKE06")
    cache = net["pod"]["conversations"]["resolved"]
    assert "slack:C0FAKE06" in cache
    entry = cache["slack:C0FAKE06"]
    assert entry["kind"] == "channel"
    assert entry["label"] == "#random"
    assert "cached_at" in entry


def test_write_conversation_cache_negative_sentinel():
    net = _net()
    cr.write_conversation_cache(net, "slack", "C0FAKE07", None)
    entry = net["pod"]["conversations"]["resolved"]["slack:C0FAKE07"]
    assert entry == {
        "resolved": False, "kind": None, "name": None, "label": None,
        "cached_at": entry["cached_at"],
    }


def test_cached_conversation_absent_returns_none():
    assert cr.cached_conversation(_net(), "slack", "C0NOPE00") is None


def test_resolve_uses_positive_cache_no_api(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    calls = {"n": 0}

    def counting(req, timeout=None):
        calls["n"] += 1
        return _FakeResp(json.dumps({
            "ok": True,
            "channel": {"id": "C0FAKE08", "name": "ops", "is_channel": True},
        }))

    monkeypatch.setattr(nr.urllib.request, "urlopen", counting)
    cr.resolve_conversation(net, "slack", "C0FAKE08")
    assert calls["n"] == 1
    out = cr.resolve_conversation(net, "slack", "C0FAKE08")
    assert calls["n"] == 1, "second call must use the cache"
    assert out["label"] == "#ops"


def test_resolve_uses_negative_cache_no_api(monkeypatch):
    """A cached negative sentinel short-circuits resolve to None — the whole
    point of negative caching is to NOT re-hit the API for unresolvable ids."""
    net = _net()
    _patch_token(monkeypatch)
    calls = {"n": 0}

    def counting(req, timeout=None):
        calls["n"] += 1
        return _FakeResp(json.dumps({"ok": False, "error": "channel_not_found"}))

    monkeypatch.setattr(nr.urllib.request, "urlopen", counting)
    assert cr.resolve_conversation(net, "slack", "C0MISS09") is None
    assert calls["n"] == 1
    assert cr.resolve_conversation(net, "slack", "C0MISS09") is None
    assert calls["n"] == 1, "negative cache must prevent a second API call"


def test_use_cache_false_refetches(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    calls = {"n": 0}

    def counting(req, timeout=None):
        calls["n"] += 1
        return _FakeResp(json.dumps({
            "ok": True,
            "channel": {"id": "C0FAKE10", "name": "design", "is_channel": True},
        }))

    monkeypatch.setattr(nr.urllib.request, "urlopen", counting)
    cr.resolve_conversation(net, "slack", "C0FAKE10")
    cr.resolve_conversation(net, "slack", "C0FAKE10", use_cache=False)
    assert calls["n"] == 2


def test_stale_cache_treated_as_miss():
    from datetime import datetime, timedelta, timezone
    net = _net()
    cr.write_conversation_cache(
        net, "slack", "C0FAKE11",
        {"kind": "channel", "name": "old", "label": "#old"},
    )
    eight_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=8)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    net["pod"]["conversations"]["resolved"][
        "slack:C0FAKE11"]["cached_at"] = eight_days_ago
    assert cr.cached_conversation(net, "slack", "C0FAKE11") is None


# ─── thread normalization ────────────────────────────────────────────────────


def test_normalize_conv_id_strips_thread_suffix():
    assert cr.normalize_conv_id("c7t9fby6s:thread:1700000000.001") == "c7t9fby6s"
    assert cr.normalize_conv_id("C0FAKE12") == "C0FAKE12"
    assert cr.normalize_conv_id("  C0FAKE12  ") == "C0FAKE12"


def test_threaded_id_shares_parent_cache_entry(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "C0FAKE13", "name": "eng", "is_channel": True},
        }),
    ]))
    cr.resolve_conversation(net, "slack", "C0FAKE13:thread:1700000000.5")
    # Keyed by the thread-stripped parent.
    assert "slack:C0FAKE13" in net["pod"]["conversations"]["resolved"]
    # A reader passing either the parent or a thread child hits the same entry.
    assert cr.cached_conversation(net, "slack", "C0FAKE13") is not None
    assert cr.cached_conversation(
        net, "slack", "C0FAKE13:thread:9999.9") is not None


# ─── enrich sweep ────────────────────────────────────────────────────────────


def test_enrich_resolves_batch_and_reports_change(monkeypatch):
    net = _net()
    _patch_token(monkeypatch)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "C0FAKE14", "name": "marketing",
                        "is_channel": True},
        }),
    ]))
    changed = cr.enrich_conversation_names(net, ["C0FAKE14"], max_seconds=5.0)
    assert changed is True
    assert net["pod"]["conversations"]["resolved"][
        "slack:C0FAKE14"]["label"] == "#marketing"


def test_enrich_skips_already_cached_no_api(monkeypatch):
    """A freshly cached id is not re-resolved — enrich makes NO live call and
    reports no change."""
    net = _net()
    _patch_token(monkeypatch)
    cr.write_conversation_cache(
        net, "slack", "C0FAKE15",
        {"kind": "channel", "name": "sales", "label": "#sales"},
    )

    def boom(req, timeout=None):
        raise AssertionError("enrich must not hit the API for a cached id")

    monkeypatch.setattr(nr.urllib.request, "urlopen", boom)
    changed = cr.enrich_conversation_names(net, ["C0FAKE15"], max_seconds=5.0)
    assert changed is False


def test_enrich_skips_unsupported_platform_ids(monkeypatch):
    """Ids whose inferred platform isn't a supported conversation platform
    (here: the ``unknown`` sentinel) are dropped before any API call."""
    net = _net()
    _patch_token(monkeypatch)

    def boom(req, timeout=None):
        raise AssertionError("must not call API for unsupported-platform ids")

    monkeypatch.setattr(nr.urllib.request, "urlopen", boom)
    changed = cr.enrich_conversation_names(net, ["unknown", ""], max_seconds=5.0)
    assert changed is False


def test_enrich_no_token_reports_no_change(monkeypatch):
    """When no bot has the channel configured, resolve writes nothing (it is
    transient, not a negative result) — enrich must report no change so the
    caller doesn't persist a no-op."""
    net = _net()
    monkeypatch.setattr(nr, "_channel_token", lambda n, c, **_kw: None)

    def boom(req, timeout=None):
        raise AssertionError("no token → no API call expected")

    monkeypatch.setattr(nr.urllib.request, "urlopen", boom)
    changed = cr.enrich_conversation_names(net, ["C0FAKE17"], max_seconds=5.0)
    assert changed is False
    assert not (net.get("pod", {}).get("conversations"))


def test_enrich_stale_entry_plus_no_token_reports_no_change(monkeypatch):
    """Edge: a STALE entry passes the freshness pre-filter (so it enters the
    work list), but the token has since vanished → resolve writes nothing and
    the stale entry remains. enrich must NOT count that leftover stale entry as
    a change (a max_age_days=0 check would have mis-counted it)."""
    from datetime import datetime, timedelta, timezone

    net = _net()
    # Seed a stale positive entry.
    cr.write_conversation_cache(
        net, "slack", "C0STALE1",
        {"kind": "channel", "name": "old", "label": "#old"},
    )
    eight_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=8)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    net["pod"]["conversations"]["resolved"][
        "slack:C0STALE1"]["cached_at"] = eight_days_ago

    # Token now gone → resolve early-returns without writing.
    monkeypatch.setattr(nr, "_channel_token", lambda n, c, **_kw: None)

    def boom(req, timeout=None):
        raise AssertionError("no token → no API call expected")

    monkeypatch.setattr(nr.urllib.request, "urlopen", boom)
    changed = cr.enrich_conversation_names(net, ["C0STALE1"], max_seconds=5.0)
    assert changed is False
    # The stale entry is untouched (still stale).
    assert net["pod"]["conversations"]["resolved"][
        "slack:C0STALE1"]["cached_at"] == eight_days_ago


def test_warm_cache_reads_turns_and_resolves(monkeypatch):
    """The sweep pulls conversation ids from ``load_turns`` and warms the
    cache, grouping by the dominant bot."""
    import usage_analytics

    net = _net()
    _patch_token(monkeypatch)

    def fake_load_turns(bot_id, days=7, network_path=None, **_kw):
        return [
            {"channel": "C0FAKE16", "instance": "fakebot"},
            {"channel": "C0FAKE16:thread:111.2", "instance": "fakebot"},
        ]

    monkeypatch.setattr(usage_analytics, "load_turns", fake_load_turns)
    monkeypatch.setattr(nr.urllib.request, "urlopen", _route_urlopen([
        ("conversations.info", {
            "ok": True,
            "channel": {"id": "C0FAKE16", "name": "support",
                        "is_channel": True},
        }),
    ]))
    changed = cr.warm_conversation_cache(net, days=3, max_seconds=5.0)
    assert changed is True
    assert net["pod"]["conversations"]["resolved"][
        "slack:C0FAKE16"]["label"] == "#support"
