"""Unit tests for evo/user_resolver.py — the background user-name cache.

The user-name twin of conversation_resolver: a ``pod.users.resolved`` cache
(positive + negative sentinels), a cache-then-API ``resolve_user``, a
time-boxed ``enrich_user_names``, and a pod-wide ``warm_user_cache`` sweep that
reuses identity_discovery's discovery path. We patch name_resolver's HTTP /
token primitives so nothing hits the network, and assert the negative-cache
contract (an unresolvable id is written once and never re-hit).

Placeholder ids/names only — no real pod names — so the scrub guard stays green.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import evolve_admin.evo.name_resolver as nr  # noqa: E402
import evolve_admin.evo.user_resolver as ur  # noqa: E402


def _net():
    return {
        "primary": "bot_one",
        "bots": {"bot_one": {"user": "bot_one"}},
        "pod": {},
    }


# ── cache read / write roundtrip ─────────────────────────────────────────────


def test_write_and_read_positive():
    net = _net()
    ur.write_user_cache(net, "slack", "U0AAA111",
                        {"name": "Pat Example", "username": "pat"})
    entry = ur.cached_user(net, "slack", "U0AAA111")
    assert entry["resolved"] is True
    assert entry["name"] == "Pat Example"
    assert entry["username"] == "pat"
    assert entry["cached_at"].endswith("Z")
    # Stored under the sibling cache, NOT pod.admins.resolved_names.
    assert "U0AAA111" in str(net["pod"]["users"]["resolved"].keys())
    assert "resolved_names" not in (net["pod"].get("admins") or {})


def test_write_and_read_negative_sentinel():
    net = _net()
    ur.write_user_cache(net, "slack", "U0NOBODY1", None)
    entry = ur.cached_user(net, "slack", "U0NOBODY1")
    assert entry is not None
    assert entry["resolved"] is False
    assert entry["name"] is None and entry["username"] is None


def test_cache_key_platform_lowercased_id_verbatim():
    net = _net()
    ur.write_user_cache(net, "Slack", "U0MiXeD22", {"name": "X"})
    # Platform lower-cased, id verbatim — a lookup with the same id resolves.
    assert ur.cached_user(net, "slack", "U0MiXeD22") is not None
    assert "slack:U0MiXeD22" in net["pod"]["users"]["resolved"]


def test_cached_user_respects_ttl():
    net = _net()
    net["pod"] = {"users": {"resolved": {
        "slack:U0OLD0001": {"resolved": True, "name": "Old",
                            "cached_at": "2000-01-01T00:00:00Z"},
    }}}
    # Stale by default TTL → miss; max_age_days=0 → returned regardless of age.
    assert ur.cached_user(net, "slack", "U0OLD0001") is None
    assert ur.cached_user(net, "slack", "U0OLD0001", max_age_days=0) is not None


# ── resolve_user: cache-then-API + negative caching ──────────────────────────


def test_resolve_user_positive_writes_cache(monkeypatch):
    net = _net()
    monkeypatch.setattr(nr, "_channel_token", lambda *a, **k: "tok")
    monkeypatch.setattr(
        nr, "_resolve_slack",
        lambda token, uid: {"name": "Ada Tester", "username": "ada",
                            "email": "ada@example.test"})
    out = ur.resolve_user(net, "slack", "U0AAA111", bot_id="bot_one")
    assert out["resolved"] is True
    assert out["name"] == "Ada Tester"
    assert out["email"] == "ada@example.test"
    # Persisted positive.
    assert ur.cached_user(net, "slack", "U0AAA111", max_age_days=0)["name"] == "Ada Tester"


def test_resolve_user_unresolvable_writes_negative(monkeypatch):
    net = _net()
    monkeypatch.setattr(nr, "_channel_token", lambda *a, **k: "tok")
    monkeypatch.setattr(nr, "_resolve_slack", lambda token, uid: None)
    out = ur.resolve_user(net, "slack", "U0GHOST01", bot_id="bot_one")
    assert out is None
    # A negative sentinel is now cached so the next sweep skips it.
    entry = ur.cached_user(net, "slack", "U0GHOST01", max_age_days=0)
    assert entry is not None and entry["resolved"] is False


def test_resolve_user_cache_hit_makes_no_api_call(monkeypatch):
    net = _net()
    ur.write_user_cache(net, "slack", "U0CACHED1", {"name": "Cached"})

    def _boom(*a, **k):
        raise AssertionError("cache hit must not call the live API")

    monkeypatch.setattr(nr, "_channel_token", _boom)
    monkeypatch.setattr(nr, "_resolve_slack", _boom)
    out = ur.resolve_user(net, "slack", "U0CACHED1")
    assert out["name"] == "Cached"


def test_resolve_user_negative_cache_hit_returns_none_no_api(monkeypatch):
    net = _net()
    ur.write_user_cache(net, "slack", "U0NEG0001", None)

    def _boom(*a, **k):
        raise AssertionError("negative cache hit must not call the live API")

    monkeypatch.setattr(nr, "_channel_token", _boom)
    out = ur.resolve_user(net, "slack", "U0NEG0001")
    assert out is None


def test_resolve_user_no_token_does_not_poison_cache(monkeypatch):
    net = _net()
    monkeypatch.setattr(nr, "_channel_token", lambda *a, **k: None)
    out = ur.resolve_user(net, "slack", "U0NOTOKEN", bot_id="bot_one")
    assert out is None
    # No token = transient → no cache entry (positive OR negative).
    assert ur.cached_user(net, "slack", "U0NOTOKEN", max_age_days=0) is None


def test_resolve_user_unsupported_platform():
    net = _net()
    assert ur.resolve_user(net, "whatsapp", "+15555550100") is None
    assert ur.resolve_user(net, "slack", "") is None


# ── enrich_user_names: batch, skip-fresh, count negatives ────────────────────


def test_enrich_user_names_resolves_and_skips_fresh(monkeypatch):
    net = _net()
    # One id already cached (fresh) → skipped; one new → resolved.
    ur.write_user_cache(net, "slack", "U0FRESH01", {"name": "Fresh"})
    calls: list[str] = []

    monkeypatch.setattr(nr, "_channel_token", lambda *a, **k: "tok")

    def _slack(token, uid):
        calls.append(uid)
        return {"name": f"Name {uid}", "username": None}

    monkeypatch.setattr(nr, "_resolve_slack", _slack)

    changed = ur.enrich_user_names(
        net, [("slack", "U0FRESH01"), ("slack", "U0NEW0001")], bot_id="bot_one")
    assert changed is True
    assert calls == ["U0NEW0001"]  # the fresh one was skipped
    assert ur.cached_user(net, "slack", "U0NEW0001", max_age_days=0)["name"] == "Name U0NEW0001"


def test_enrich_user_names_negative_counts_as_change(monkeypatch):
    net = _net()
    monkeypatch.setattr(nr, "_channel_token", lambda *a, **k: "tok")
    monkeypatch.setattr(nr, "_resolve_slack", lambda token, uid: None)
    changed = ur.enrich_user_names(net, [("slack", "U0GHOST02")], bot_id="bot_one")
    # A negative sentinel was written — worth persisting → True.
    assert changed is True
    assert ur.cached_user(net, "slack", "U0GHOST02", max_age_days=0)["resolved"] is False


def test_enrich_user_names_empty_worklist_returns_false(monkeypatch):
    net = _net()
    # Unsupported platform + empty id → nothing to do, no API.
    monkeypatch.setattr(nr, "_channel_token",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert ur.enrich_user_names(net, [("whatsapp", "x"), ("slack", "")]) is False


# ── warm_user_cache: reuses identity_discovery discovery ─────────────────────


def test_warm_user_cache_uses_discovery(monkeypatch):
    net = _net()
    import evolve_admin.evo.identity_discovery as idsc

    monkeypatch.setattr(
        idsc, "discover_candidates",
        lambda b, **k: [{"channel": "slack", "external_id": "U0DISC001",
                         "turn_count": 5}])
    monkeypatch.setattr(
        idsc, "_rewrite_slack_dm_candidates", lambda net_, c, **k: c)
    monkeypatch.setattr(nr, "_channel_token", lambda *a, **k: "tok")
    monkeypatch.setattr(
        nr, "_resolve_slack",
        lambda token, uid: {"name": "Sam Sample", "username": "sam"})

    changed = ur.warm_user_cache(net, max_seconds=5.0)
    assert changed is True
    assert ur.cached_user(net, "slack", "U0DISC001", max_age_days=0)["name"] == "Sam Sample"


def test_warm_user_cache_no_bots_returns_false():
    assert ur.warm_user_cache({"pod": {}}, max_seconds=5.0) is False
