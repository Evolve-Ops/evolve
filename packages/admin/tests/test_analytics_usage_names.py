"""End-to-end test for /api/analytics/usage name enrichment (Usage Bite 1).

The route runs compute_summary, then enriches By User rows with a cache-only
display name (+ a categorized fallback label) and By Channel DMs with
"DM · <name>" when the cache already carries the participant. The hard
contract: enrichment is CACHE-ONLY — it reads network.json + the identity
cache, never a live Slack/Telegram API call at render time. We prove that by
making the live-API layer raise and asserting the route still resolves names.

We stub load_turns (so the test doesn't read /Users/Shared) and let the real
roster_resolver + compute_summary run.

Placeholder bot/user/channel ids + names only — no real pod names — so the
scrub guard stays green.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask


def _now_z() -> str:
    """A FRESH cached_at, computed at call time so the conversation-cache
    fixtures never go stale (no hardcoded date → no clock-coupling rot)."""
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web.server import _register_analytics_routes  # noqa: E402


def _turn(*, source, channel, user_id, instance="bot_one", cost=0.5,
          ts="2026-06-12T10:00:00Z", session_id="s1"):
    return {
        "ts": ts,
        "instance": instance,
        "model": "provx/used-lo",
        "provider": "provx",
        "source": source,
        "channel": channel,
        "user_id": user_id,
        "cost": cost,
        "input_tokens": 5,
        "output_tokens": 200,
        "cache_read_tokens": 10000,
        "cache_write_tokens": 0,
        "session_id": session_id,
        "auth_mode": "api_key",
    }


# A person we CAN resolve from cache, a person we CANNOT, a DM with a cached
# participant name, a channel WITH a conversation-cache label, a U-id shown as a
# channel, a negative-cached group, and a slice of system traffic.
_POOLED = [
    _turn(source="human", channel="C0FAKECHAN", user_id="U0KNOWNUSR", session_id="s1"),
    _turn(source="human", channel="C0FAKECHAN", user_id="U0KNOWNUSR", session_id="s2"),
    _turn(source="human", channel="C0FAKECHAN", user_id="U0NONAME01", session_id="s3"),
    # A user named ONLY by the background user-name cache (pod.users.resolved),
    # not by any admin-keyed source — the operator's "Usage shows the raw id but
    # Users resolves it" case, now at parity.
    _turn(source="human", channel="C0FAKECHAN", user_id="U0USRCACHE", session_id="s10"),
    _turn(source="human", channel="D0FAKEDM11", user_id="U0KNOWNUSR", session_id="s4"),
    # Channel id WITH a conversation-name cache entry → "#team-chat".
    _turn(source="human", channel="C0CACHEDCH", user_id="U0KNOWNUSR", session_id="s7"),
    # A Slack user id that landed in the channel column (self/own DM) → resolve
    # as a USER → "DM · Sam Sample" (the operator's U0PLKKXV0 case).
    _turn(source="human", channel="U0SELFDM01", user_id="U0SELFDM01", session_id="s8"),
    # A group id NEGATIVE-cached (bot can't see it) → raw id fallback.
    _turn(source="human", channel="G0NEGGRP01", user_id="U0KNOWNUSR", session_id="s9"),
    _turn(source="heartbeat", channel="heartbeat", user_id="?", session_id="s5"),
    _turn(source="forge", channel="unknown", user_id="?", session_id="s6"),
]


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "primary": "evolve",
        "members": ["bot_one", "bot_two"],
        "pod": {
            "admins": {
                # Cache-only resolution source. Keyed <platform>:<id>. The DM
                # channel id is cached too (the participant's name); so is the
                # U-id-as-channel (a person resolved as a user).
                "resolved_names": {
                    "slack:U0KNOWNUSR": {"name": "Pat Example"},
                    "slack:D0FAKEDM11": {"name": "Pat Example"},
                    "slack:U0SELFDM01": {"name": "Sam Sample"},
                },
            },
            # Background user-name cache (this PR). Cache-only consumer surface
            # resolve_display_name reads AFTER all admin-keyed sources. The user
            # below is in NO admin source, so only this cache can name them.
            "users": {
                "resolved": {
                    "slack:U0USRCACHE": {
                        "resolved": True, "name": "Ada Tester",
                        "username": "ada", "cached_at": _now_z(),
                    },
                },
            },
            # Bite 2 (#2990) conversation-name cache. Cache-only consumer
            # surface the render reads via cached_conversation().
            "conversations": {
                "resolved": {
                    "slack:C0CACHEDCH": {
                        "resolved": True, "kind": "channel",
                        "name": "team-chat", "label": "#team-chat",
                        "cached_at": _now_z(),
                    },
                    "slack:G0NEGGRP01": {
                        "resolved": False, "kind": None,
                        "name": None, "label": None,
                        "cached_at": _now_z(),
                    },
                },
            },
        },
    }))

    import usage_analytics as ua

    def _fake_load_turns(bot_id, **kw):
        return list(_POOLED)

    monkeypatch.setattr(ua, "load_turns", _fake_load_turns)

    # Hard cache-only guard: if the render path EVER reaches the live Slack API
    # layer OR the live conversation resolver, this raises and fails the test.
    import evolve_admin.evo.name_resolver as nr
    import evolve_admin.evo.conversation_resolver as cr
    import evolve_admin.evo.user_resolver as ur

    def _boom(*a, **k):
        raise AssertionError("render path made a live API call — must be cache-only")

    monkeypatch.setattr(nr, "_http_get_json", _boom)
    monkeypatch.setattr(cr, "resolve_conversation", _boom)
    # A render that reaches the live user resolver also fails — proves the
    # By-User name path (resolve_display_name → pod.users.resolved) is
    # cache-only too.
    monkeypatch.setattr(ur, "resolve_user", _boom)
    # The fire-and-forget warm kicks run in daemon threads; stub them to no-ops
    # here so render tests don't spawn real (token-less) warm work. Their own
    # time-boxed/non-blocking behavior is covered separately below.
    monkeypatch.setattr(cr, "warm_conversation_cache", lambda *a, **k: False)
    monkeypatch.setattr(ur, "warm_user_cache", lambda *a, **k: False)

    a = Flask(__name__)
    _register_analytics_routes(a, network_path)
    a.config["TESTING"] = True
    return a


def _usage(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/usage?days=7")
    assert resp.status_code == 200
    return resp.get_json()


def test_by_user_resolved_name_from_cache(app):
    body = _usage(app)
    by_user = {r["user_id"]: r for r in body["by_user"]}
    known = by_user["U0KNOWNUSR"]
    assert known["display_name"] == "Pat Example"
    assert known["name_source"] == "resolved_names"
    assert known["label"] == "Pat Example"
    assert known["platform"] == "slack"
    # Raw id preserved for debugging.
    assert known["user_id"] == "U0KNOWNUSR"


def test_by_user_unresolved_gets_categorized_fallback(app):
    body = _usage(app)
    by_user = {r["user_id"]: r for r in body["by_user"]}
    noname = by_user["U0NONAME01"]
    assert noname["display_name"] is None
    assert noname["name_source"] == "allowlist_only"
    # Never a bare opaque id — a categorized, shortened label.
    assert noname["label"].startswith("Slack user · ")
    assert "U0NONAME01" not in noname["label"]  # shortened in the label
    assert noname["label"] != "U0NONAME01"


def test_by_user_excludes_system_traffic(app):
    body = _usage(app)
    ids = {r["user_id"] for r in body["by_user"]}
    # Only the real people; heartbeat/forge "?" never appear as users.
    assert ids == {"U0KNOWNUSR", "U0NONAME01", "U0SELFDM01", "U0USRCACHE"}
    assert "?" not in ids


def test_by_user_resolved_name_from_user_cache(app):
    """A user named ONLY by pod.users.resolved (no admin-keyed source) now
    renders the real name with source ``users_resolved`` — parity with the
    Users page, cache-only (the fixture booms on any live user lookup)."""
    body = _usage(app)
    by_user = {r["user_id"]: r for r in body["by_user"]}
    row = by_user["U0USRCACHE"]
    assert row["display_name"] == "Ada Tester"
    assert row["name_source"] == "users_resolved"
    assert row["label"] == "Ada Tester"
    assert row["platform"] == "slack"
    assert row["user_id"] == "U0USRCACHE"


def test_unadmitted_marker_suppressed_when_admission_unreadable(app):
    """The fixture network has no pod admins and no bots/allowFrom, so the
    admission index is unreadable → the marker is SUPPRESSED for every row (a
    read failure must never paint everyone "not admitted")."""
    body = _usage(app)
    assert body["by_user"], "expected some by_user rows"
    assert all(r.get("unadmitted") is False for r in body["by_user"])


def test_by_channel_dm_resolves_to_named_label(app):
    body = _usage(app)
    by_chan = {r["channel"]: r for r in body["by_channel"] if not r.get("system")}
    dm = by_chan["D0FAKEDM11"]
    assert dm["is_dm"] is True
    assert dm["label"] == "DM · Pat Example"
    assert dm["platform"] == "slack"


def test_by_channel_cache_miss_keeps_raw_id(app):
    body = _usage(app)
    by_chan = {r["channel"]: r for r in body["by_channel"] if not r.get("system")}
    pub = by_chan["C0FAKECHAN"]
    # Channel with NO conversation-cache entry (Bite 3 cache miss) → raw id
    # stands, tagged with the inferred platform and is_dm=False.
    assert pub["label"] == "C0FAKECHAN"
    assert pub["is_dm"] is False
    assert pub["platform"] == "slack"


def test_by_channel_cached_conversation_uses_label(app):
    """Bite 3: a row whose conversation id is in the cache renders the cached
    label (here a public channel → "#team-chat")."""
    body = _usage(app)
    by_chan = {r["channel"]: r for r in body["by_channel"] if not r.get("system")}
    ch = by_chan["C0CACHEDCH"]
    assert ch["label"] == "#team-chat"
    assert ch["platform"] == "slack"
    assert ch["is_dm"] is False
    # Raw id preserved for debugging / the dim mono secondary line.
    assert ch["channel"] == "C0CACHEDCH"


def test_by_channel_user_id_as_channel_resolves_to_user(app):
    """Bite 3: a Slack user id (U…) that landed in the channel column resolves
    as a PERSON → "DM · <name>" — the operator's "U0PLKKXV0 shows as a
    channel" case. is_dm keeps its Bite 1 definition (D-prefixed only)."""
    body = _usage(app)
    by_chan = {r["channel"]: r for r in body["by_channel"] if not r.get("system")}
    self_dm = by_chan["U0SELFDM01"]
    assert self_dm["label"] == "DM · Sam Sample"
    assert self_dm["platform"] == "slack"
    assert self_dm["is_dm"] is False  # a U-id is not a D-channel


def test_by_channel_negative_cache_falls_back_to_raw_id(app):
    """A negative cache entry (bot can't see the conversation) yields no usable
    label → raw id fallback, NOT a re-hit of the live API."""
    body = _usage(app)
    by_chan = {r["channel"]: r for r in body["by_channel"] if not r.get("system")}
    neg = by_chan["G0NEGGRP01"]
    assert neg["label"] == "G0NEGGRP01"
    assert neg["platform"] == "slack"
    assert neg["is_dm"] is False


def test_by_channel_system_rows_passthrough(app):
    body = _usage(app)
    sys_rows = {r["category"]: r for r in body["by_channel"] if r.get("system")}
    assert "Heartbeat" in sys_rows
    assert "Forge builds" in sys_rows
    # System rows label = their category; platform tagged "system".
    assert sys_rows["Heartbeat"]["label"] == "Heartbeat"
    assert sys_rows["Heartbeat"]["platform"] == "system"


def test_enrichment_is_cache_only_no_network(app):
    # The fixture patches BOTH the live Slack API layer and the live
    # conversation resolver to raise; a clean 200 with resolved names (user +
    # channel) proves the render path stayed entirely cache-only.
    body = _usage(app)
    assert any(r.get("display_name") == "Pat Example" for r in body["by_user"])
    labels = {r["channel"]: r["label"]
              for r in body["by_channel"] if not r.get("system")}
    assert labels["C0CACHEDCH"] == "#team-chat"      # from the conv cache
    assert labels["U0SELFDM01"] == "DM · Sam Sample"  # from the user cache


# ── Background warm kick (fire-and-forget, never on the render path) ──────────


def test_warm_kick_non_blocking_time_boxed_and_serialized(tmp_path, monkeypatch):
    """The kick returns immediately (NON-blocking), budgets the warm with a
    bounded wall-clock (time-boxed), and serializes (a second kick while one is
    in flight does not spawn an overlapping warm)."""
    import threading
    import time as _time
    import evolve_admin.evo.conversation_resolver as cr
    from evolve_admin.web import routes_analytics as ra

    # Drain any lingering warm from a prior test, then clear the throttle.
    assert ra._CONV_WARM_LOCK.acquire(timeout=5)
    ra._CONV_WARM_LOCK.release()
    ra._CONV_WARM_STATE["last_started"] = None
    started = threading.Event()
    release = threading.Event()
    captured = {"max_seconds": None, "days": None, "calls": 0}

    def _slow_warm(network, *, days, max_seconds, network_path):
        captured["calls"] += 1
        captured["max_seconds"] = max_seconds
        captured["days"] = days
        started.set()
        release.wait(timeout=5)  # block — proves the caller didn't wait on us
        return False

    monkeypatch.setattr(cr, "warm_conversation_cache", _slow_warm)
    monkeypatch.setattr(ra, "load_network", lambda p: {"sharedDir": str(tmp_path)})

    net_path = tmp_path / "network.json"
    t0 = _time.monotonic()
    ra._kick_conversation_warm(net_path, days=7)
    elapsed = _time.monotonic() - t0

    assert elapsed < 1.0, "kick blocked the caller — must be fire-and-forget"
    assert started.wait(timeout=5), "warm thread never started"
    # Time-boxed: a bounded wall-clock budget was threaded into the warm.
    assert captured["max_seconds"] is not None and captured["max_seconds"] <= 20.0
    assert captured["days"] == 7

    # Serialized: a second kick (throttle reset) while the first warm still
    # holds the lock must NOT spawn an overlapping warm.
    ra._CONV_WARM_STATE["last_started"] = None
    ra._kick_conversation_warm(net_path, days=7)
    assert captured["calls"] == 1

    # Let the worker finish and release the lock before the test exits.
    release.set()
    assert ra._CONV_WARM_LOCK.acquire(timeout=5)
    ra._CONV_WARM_LOCK.release()


def test_warm_kick_throttles_repeat_calls(tmp_path, monkeypatch):
    """Back-to-back kicks within the min-interval fire the warm only once, so
    page polling can't spawn a warm per request."""
    import threading
    import evolve_admin.evo.conversation_resolver as cr
    from evolve_admin.web import routes_analytics as ra

    # Drain any lingering warm from a prior test, then clear the throttle.
    assert ra._CONV_WARM_LOCK.acquire(timeout=5)
    ra._CONV_WARM_LOCK.release()
    ra._CONV_WARM_STATE["last_started"] = None
    done = threading.Event()
    calls = []

    def _warm(network, *, days, max_seconds, network_path):
        calls.append(1)
        done.set()
        return False

    monkeypatch.setattr(cr, "warm_conversation_cache", _warm)
    monkeypatch.setattr(ra, "load_network", lambda p: {})

    net_path = tmp_path / "network.json"
    ra._kick_conversation_warm(net_path, days=7)
    assert done.wait(timeout=5), "first warm never ran"
    # Second kick within the 5-min throttle window → no new warm.
    ra._kick_conversation_warm(net_path, days=7)
    assert len(calls) == 1

    # Let the first worker release the lock before the test exits.
    assert ra._CONV_WARM_LOCK.acquire(timeout=5)
    ra._CONV_WARM_LOCK.release()


def test_warm_kick_releases_lock_if_thread_start_fails(tmp_path, monkeypatch):
    """If spawning the worker thread raises (e.g. thread exhaustion), the kick
    must swallow it AND release the lock — otherwise the next kick is wedged
    forever (``_run``'s finally never runs because the worker never started)."""
    import evolve_admin.evo.conversation_resolver as cr
    from evolve_admin.web import routes_analytics as ra

    # Drain any lingering warm, then clear the throttle.
    assert ra._CONV_WARM_LOCK.acquire(timeout=5)
    ra._CONV_WARM_LOCK.release()
    ra._CONV_WARM_STATE["last_started"] = None

    monkeypatch.setattr(ra, "load_network", lambda p: {})
    monkeypatch.setattr(cr, "warm_conversation_cache", lambda *a, **k: False)

    class _BoomThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(ra.threading, "Thread", _BoomThread)

    # Must not propagate to the caller (fire-and-forget).
    ra._kick_conversation_warm(tmp_path / "network.json", days=7)
    # And the lock must be free for the next kick.
    assert ra._CONV_WARM_LOCK.acquire(blocking=False), "lock leaked on start failure"
    ra._CONV_WARM_LOCK.release()


def test_warm_kick_fires_first_time_when_monotonic_below_interval(tmp_path, monkeypatch):
    """Regression: the FIRST kick must fire even when time.monotonic() is below
    the min-interval (a freshly-started process — monotonic's epoch is
    arbitrary). A 0.0 'last_started' sentinel made `now - 0.0 < interval` true
    and wrongly throttled the first kick for the process's first ~5 minutes."""
    import threading
    import evolve_admin.evo.conversation_resolver as cr
    from evolve_admin.web import routes_analytics as ra

    # Drain any lingering warm, then reset to the "never kicked" sentinel.
    assert ra._CONV_WARM_LOCK.acquire(timeout=5)
    ra._CONV_WARM_LOCK.release()
    ra._CONV_WARM_STATE["last_started"] = None

    # Simulate a young process: monotonic() well below the 5-min throttle.
    monkeypatch.setattr(ra.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(ra, "load_network", lambda p: {})
    ran = threading.Event()

    def _warm(network, *, days, max_seconds, network_path):
        ran.set()
        return False

    monkeypatch.setattr(cr, "warm_conversation_cache", _warm)

    ra._kick_conversation_warm(tmp_path / "network.json", days=7)
    assert ran.wait(timeout=5), "first kick was throttled on a young process"

    # Drain before exit.
    assert ra._CONV_WARM_LOCK.acquire(timeout=5)
    ra._CONV_WARM_LOCK.release()
