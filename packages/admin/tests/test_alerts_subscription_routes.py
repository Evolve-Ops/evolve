"""Phase A4 — HTTP routes for the Subscriptions tab.

Pins the contract Phase B (UI) will consume:

  GET  /api/alerts/subscriptions             full payload
  POST /api/alerts/subscriptions             single or batch updates
  POST /api/alerts/subscriptions/test        fire sample message
  POST /api/alerts/subscriptions/reset       wipe overrides
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture
def app(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app
    from evolve_admin.alerts import dispatcher

    shared = tmp_path / "evolve"
    (shared / "signals").mkdir(parents=True)
    network = {
        "members": [],
        "bots": {},
        "sharedDir": str(shared),
        "alerts": {"channel": "telegram", "chatId": "12345"},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Stub the openclaw subprocess so the /test endpoint doesn't shell out.
    # _dispatch_via_openclaw now takes a 4th gateway_port arg — accept it
    # via **kwargs / extra positional so the stub stays signature-tolerant.
    sent: list = []
    def fake_dispatch(channel, chat_id, message, gateway_port=None):
        sent.append((channel, chat_id, message))
        return True, None
    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", fake_dispatch)

    # Also stub the irrelevant signal/host_health probes from server.py so
    # create_app doesn't try to walk a real shared dir.
    from evolve_admin import host_health as _host_health
    from evolve_admin import reporter as _reporter
    from evolve_admin import health as _health

    monkeypatch.setattr(_host_health, "collect_host_health", lambda: {"ok": True})
    monkeypatch.setattr(_host_health, "emit_signals_from_snapshot", lambda s, p: 0)
    monkeypatch.setattr(_reporter, "emit_error_signals", lambda p: 0)
    class _R:
        checks = []
        def as_dict(self): return {"checks": []}
    monkeypatch.setattr(_health, "run_health_check", lambda **kw: _R())

    app = create_app(network_path)
    app.config["TESTING"] = True
    return {"app": app, "sent": sent, "shared": shared}


def _client(app_fixture):
    return app_fixture["app"].test_client()


# ── GET ─────────────────────────────────────────────────────────────────────


def _groups(body):
    return body["subscriptions"]


def _group(body, gid):
    for g in body["subscriptions"]:
        if g["id"] == gid:
            return g
    raise KeyError(gid)


def _event(body, key):
    for g in body["subscriptions"]:
        for e in g.get("events", []):
            if e["key"] == key:
                return e
    raise KeyError(key)


def test_get_returns_thirteen_subscription_groups(app):
    """Phase 2: GET returns the 13 operator-facing Subscription groups in
    registry order, not the ~65 per-event toggles."""
    c = _client(app)
    resp = c.get("/api/alerts/subscriptions")
    assert resp.status_code == 200
    body = resp.get_json()
    # Channel info plumbed from network.json.
    assert body["channel"] == "telegram"
    assert body["chat_id"] == "12345"
    assert body["digest_hour_local"] == 8
    assert isinstance(body["dispatcher_enabled"], bool)
    assert isinstance(body["pwa_push_enabled"], bool)

    from evolve_admin.alerts import subscriptions_catalog as _subcat
    expected_ids = [s.id for s in _subcat.all_subscriptions()]
    got_ids = [g["id"] for g in body["subscriptions"]]
    # Fixture leaves every source enabled → all 13 groups present, in order.
    assert got_ids == expected_ids

    for g in body["subscriptions"]:
        assert isinstance(g["enabled"], bool)
        assert isinstance(g["is_safety_critical"], bool)
        assert isinstance(g["is_overridden"], bool)
        assert g["member_event_count"] >= 1
        for e in g["events"]:
            assert "key" in e and "label" in e
            assert e["subscription_id"] == g["id"]

    # event_index maps member event keys → group ids; internal events
    # (subscription=None) are absent.
    assert body["event_index"]["security.audit_finding"] == "security_findings"
    assert "meta.unclassified" not in body["event_index"]


def test_safety_critical_groups_flagged(app):
    """The five safety-critical groups carry is_safety_critical=True so the
    UI (chip 2) can warn on mute."""
    body = _client(app).get("/api/alerts/subscriptions").get_json()
    safety = {g["id"] for g in body["subscriptions"] if g["is_safety_critical"]}
    assert safety == {
        "security_findings", "cost_enforcement", "pod_health",
        "app_delivery", "alerting_system",
    }


def test_cve_finding_member_of_security_findings_group(app):
    """security.cve_finding surfaces as a member of the security_findings
    group; the group defaults enabled and safety-critical."""
    body = _client(app).get("/api/alerts/subscriptions").get_json()
    sec = _group(body, "security_findings")
    assert sec["enabled"] is True
    assert sec["is_safety_critical"] is True
    assert "security.cve_finding" in sec["member_event_keys"]
    cve = _event(body, "security.cve_finding")
    assert cve["default_frequency"] == "immediate"


def test_get_allowed_frequencies_includes_labels(app):
    """Per-event frequency is still tunable; the deprecated frequencies must
    not leak into the API response."""
    body = _client(app).get("/api/alerts/subscriptions").get_json()
    audit_finding = _event(body, "security.audit_finding")
    freqs = audit_finding["allowed_frequencies"]
    assert all("key" in f and "label" in f for f in freqs)
    keys = {f["key"] for f in freqs}
    assert not (keys & {"off", "once_per_day_max", "once_per_week_max"}), (
        f"deprecated frequencies leaked into API response: {keys}"
    )


def test_get_reflects_persisted_group_override(app):
    """When the operator has toggled a group off, GET shows the group as
    disabled + overridden, and every member event resolves disabled."""
    from evolve_admin.alerts import subscriptions as subs
    subs.write_subscription_group(app["shared"], "cost_warnings", enabled=False)
    body = _client(app).get("/api/alerts/subscriptions").get_json()
    cost = _group(body, "cost_warnings")
    assert cost["enabled"] is False
    assert cost["is_overridden"] is True
    # member events resolve disabled through the group
    daily = _event(body, "cost.daily_threshold")
    assert daily["subscription"]["enabled"] is False


# ── POST single + batch ─────────────────────────────────────────────────────


def test_post_group_toggle(app):
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions", json={
        "subscription_id": "cost_warnings", "enabled": False,
    })
    assert resp.status_code == 200
    out = resp.get_json()
    assert out["updated_groups"][0]["id"] == "cost_warnings"
    assert out["updated_groups"][0]["enabled"] is False
    assert out["updated_groups"][0]["is_overridden"] is True
    # Re-read: the group is off.
    body = _client(app).get("/api/alerts/subscriptions").get_json()
    assert _group(body, "cost_warnings")["enabled"] is False


def test_post_batch_group_toggles(app):
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions", json={
        "updates": [
            {"subscription_id": "cost_warnings", "enabled": False},
            {"subscription_id": "updates", "enabled": False},
        ],
    })
    assert resp.status_code == 200
    ids = {g["id"] for g in resp.get_json()["updated_groups"]}
    assert ids == {"cost_warnings", "updates"}


def test_post_per_event_enabled_rejected(app):
    """Per-event enabled is no longer the on/off surface — caller must
    toggle the group instead."""
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions", json={
        "event_key": "cost.daily_threshold", "enabled": False,
    })
    assert resp.status_code == 400


def test_post_event_frequency_still_accepted(app):
    """A per-event frequency-only override is still honored (digest cadence
    is event-shaped)."""
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions", json={
        "event_key": "cost.daily_threshold", "frequency": "daily_digest",
    })
    assert resp.status_code == 200
    out = resp.get_json()["updated_events"]
    assert out[0]["event_key"] == "cost.daily_threshold"
    assert out[0]["frequency"] == "daily_digest"


def test_post_unknown_group_returns_404(app):
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions", json={
        "subscription_id": "not_a_real_group", "enabled": True,
    })
    assert resp.status_code == 404


def test_post_unknown_event_returns_404(app):
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions", json={
        "event_key": "not.a.real.event", "frequency": "daily_digest",
    })
    assert resp.status_code == 404


def test_post_invalid_frequency_returns_400(app):
    """`updates.evolve_repo` doesn't allow IMMEDIATE — UI safety net."""
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions", json={
        "event_key": "updates.evolve_repo", "frequency": "immediate",
    })
    assert resp.status_code == 400


def test_post_missing_event_key_returns_400(app):
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions", json={"enabled": True})
    assert resp.status_code == 400


# ── POST /test ──────────────────────────────────────────────────────────────


def test_test_renders_sample_payload_and_dispatches(app):
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions/test", json={
        "event_key": "cost.daily_threshold",
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "sent"
    # render came from the catalog's sample_payload — phrasing was
    # updated to "admin_bot's daily spend hit $5.96" in a recent catalog refresh.
    assert "admin_bot" in body["rendered"] and "$5.96" in body["rendered"]
    assert "Threshold: $5.00" in body["rendered"]
    # Test marker is prepended in the message that actually goes out.
    assert len(app["sent"]) == 1
    _channel, _chat, sent_msg = app["sent"][0]
    assert sent_msg.startswith("[Subscriptions tab — test message]")
    assert "admin_bot" in sent_msg and "$5.96" in sent_msg


def test_test_unknown_event_returns_404(app):
    c = _client(app)
    resp = c.post("/api/alerts/subscriptions/test", json={
        "event_key": "not.a.real.event",
    })
    assert resp.status_code == 404


def test_test_bypasses_subscription_off(app):
    """Even if the operator has the event muted, the Test button must
    deliver — they're explicitly asking for a preview."""
    from evolve_admin.alerts import subscriptions as subs
    subs.write_subscription(
        app["shared"], "cost.daily_threshold", enabled=False,
    )
    resp = _client(app).post("/api/alerts/subscriptions/test", json={
        "event_key": "cost.daily_threshold",
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "sent", (
        f"test must deliver even when subscription is off; got result={body['result']}"
    )
    assert len(app["sent"]) == 1


def test_test_missing_event_key_returns_400(app):
    resp = _client(app).post("/api/alerts/subscriptions/test", json={})
    assert resp.status_code == 400


# ── POST /reset ─────────────────────────────────────────────────────────────


def test_reset_wipes_group_overrides_and_returns_full_payload(app):
    from evolve_admin.alerts import subscriptions as subs
    subs.write_subscription_group(app["shared"], "cost_warnings", enabled=False)
    subs.write_subscription_group(app["shared"], "security_findings", enabled=False)
    # Pre-reset: two group overrides set.
    pre = _client(app).get("/api/alerts/subscriptions").get_json()
    overridden_count = sum(1 for g in pre["subscriptions"] if g["is_overridden"])
    assert overridden_count == 2

    resp = _client(app).post("/api/alerts/subscriptions/reset")
    assert resp.status_code == 200
    post = resp.get_json()
    overridden_after = sum(1 for g in post["subscriptions"] if g["is_overridden"])
    assert overridden_after == 0


# ── digest-hour endpoint ─────────────────────────────────────────────────


def test_post_digest_hour_persists(app):
    """POST /api/alerts/digest-hour writes through; subsequent GET
    /api/alerts/subscriptions reflects the new value."""
    c = _client(app)
    resp = c.post("/api/alerts/digest-hour", json={"hour": 18})
    assert resp.status_code == 200
    assert resp.get_json() == {"digest_hour_local": 18}

    body = c.get("/api/alerts/subscriptions").get_json()
    assert body["digest_hour_local"] == 18


def test_post_digest_hour_rejects_out_of_range(app):
    c = _client(app)
    for bad in (-1, 24, 100):
        resp = c.post("/api/alerts/digest-hour", json={"hour": bad})
        assert resp.status_code == 400, f"hour={bad} should 400"


def test_post_digest_hour_rejects_non_integer(app):
    c = _client(app)
    resp = c.post("/api/alerts/digest-hour", json={"hour": "morning"})
    assert resp.status_code == 400


def test_post_digest_hour_missing_body_returns_400(app):
    c = _client(app)
    resp = c.post("/api/alerts/digest-hour", json={})
    assert resp.status_code == 400


# ── delivery-failures endpoint + messages-list FAILED filter ───────────────
#
# PWA-polish PR `pwa-polish-alert-messages` splits the dispatcher's
# successful-vs-failed writes into two files and adds a new endpoint
# for the Dispatcher Health panel. These tests pin that split end-to-end
# at the HTTP layer.


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_delivery_failures_returns_records_and_summary(app):
    """GET /api/alerts/delivery-failures returns the 24h-windowed failure
    feed plus a small aggregate the panel renders compact."""
    from datetime import datetime, timezone
    shared = app["shared"]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    _write_jsonl(
        shared / "alerts" / "delivery-failures.jsonl",
        [
            {"ts": now_iso, "source": "audit", "result": "failed",
             "channel": "telegram",
             "error": "telegram http 400: bad parse",
             "message_excerpt": "🛡️ New security finding\nteam_bot_a: ..."},
            {"ts": now_iso, "source": "audit", "result": "failed",
             "channel": "telegram",
             "error": "telegram http 400: bad parse"},
            {"ts": now_iso, "source": "pod_report", "result": "failed",
             "channel": "slack",
             "error": "slack rate-limited"},
        ],
    )
    c = _client(app)
    resp = c.get("/api/alerts/delivery-failures")
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["records"]) == 3
    assert body["summary"]["total"] == 3
    assert body["summary"]["window_hours"] == 24
    by_channel = {s["channel"]: s for s in body["summary"]["by_channel"]}
    assert by_channel["telegram"]["count"] == 2
    assert by_channel["slack"]["count"] == 1
    # The most-frequent channel sorts first.
    assert body["summary"]["by_channel"][0]["channel"] == "telegram"


def test_delivery_failures_handles_missing_file(app):
    """No failures file yet — endpoint returns the empty / all-green
    shape, not a 404 or crash. (Fresh installs hit this on first load.)"""
    c = _client(app)
    resp = c.get("/api/alerts/delivery-failures")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["records"] == []
    assert body["summary"]["total"] == 0
    assert body["summary"]["by_channel"] == []
    assert body["summary"]["most_recent_ts"] is None


def test_delivery_failures_drops_entries_older_than_window(app):
    """Entries older than window_hours are excluded. Defaults to 24h."""
    shared = app["shared"]
    _write_jsonl(
        shared / "alerts" / "delivery-failures.jsonl",
        [
            {"ts": "2026-05-15T12:00:00Z", "result": "failed",
             "channel": "telegram", "error": "old"},
            {"ts": "2099-01-01T12:00:00Z", "result": "failed",
             "channel": "telegram", "error": "future"},
        ],
    )
    c = _client(app)
    body = c.get("/api/alerts/delivery-failures?window_hours=24").get_json()
    # Only the future-dated entry survives the 24h cutoff from today.
    errors = [r["error"] for r in body["records"]]
    assert errors == ["future"]


def test_dispatcher_log_excludes_failed_entries(app):
    """Even when dispatcher.jsonl has historic result=='failed' rows
    (from before the file split landed), the messages feed filters
    them out so the Recent Messages list is clean immediately."""
    shared = app["shared"]
    _write_jsonl(
        shared / "alerts" / "dispatcher.jsonl",
        [
            {"ts": "2099-01-01T12:00:00Z", "source": "audit",
             "result": "sent", "message_excerpt": "🟢 ok"},
            {"ts": "2099-01-01T12:00:01Z", "source": "audit",
             "result": "failed", "channel": "telegram",
             "error": "telegram http 400: bad parse",
             "message_excerpt": "🔴 historic failure"},
            {"ts": "2099-01-01T12:00:02Z", "source": "spend_alert",
             "result": "sent", "message_excerpt": "💰 daily"},
        ],
    )
    c = _client(app)
    body = c.get("/api/alerts/dispatcher-log").get_json()
    results = [r["result"] for r in body["records"]]
    assert "failed" not in results
    assert set(results) == {"sent"}
    assert len(body["records"]) == 2


# ── Queued lane (spec-delta-digest-audit-noise-2026-08-25 D2) ───────────────


def test_dispatcher_log_excludes_deferred_by_default(app):
    """A deferred message is queued for a future digest, not something the
    operator's phone received — so the default feed must not show it, even
    for the historic rows still sitting in dispatcher.jsonl from before the
    lane split. Without this post-filter, months of deferrals (287 of 292
    rows on the live pod, 2026-08-25) leak into "what actually got sent"."""
    shared = app["shared"]
    _write_jsonl(
        shared / "alerts" / "dispatcher.jsonl",
        [
            {"ts": "2099-01-01T12:00:00Z", "source": "audit",
             "result": "sent", "message_excerpt": "🟢 ok"},
            {"ts": "2099-01-01T12:00:01Z", "source": "heal",
             "result": "deferred", "catalog_event": "security.config_drift",
             "message_excerpt": "historic deferral"},
        ],
    )
    _write_jsonl(
        shared / "alerts" / "dispatcher-queued" / "2099-01-02.jsonl",
        [
            {"ts": "2099-01-02T12:00:00Z", "source": "heal",
             "result": "deferred", "catalog_event": "security.config_drift",
             "message_excerpt": "post-split deferral"},
        ],
    )
    c = _client(app)
    body = c.get("/api/alerts/dispatcher-log").get_json()
    assert [r["result"] for r in body["records"]] == ["sent"]


def test_dispatcher_log_queued_param_adds_both_new_and_historic_deferrals(app):
    """``queued=1`` opens the lane: the new dispatcher-queued stream AND the
    pre-split deferrals still physically living in dispatcher.jsonl. Reading
    only the new stream would show an empty lane on a pod with months of
    history."""
    shared = app["shared"]
    _write_jsonl(
        shared / "alerts" / "dispatcher.jsonl",
        [
            {"ts": "2099-01-01T12:00:00Z", "source": "audit",
             "result": "sent", "message_excerpt": "🟢 ok"},
            {"ts": "2099-01-01T12:00:01Z", "source": "heal",
             "result": "deferred", "message_excerpt": "historic deferral"},
        ],
    )
    _write_jsonl(
        shared / "alerts" / "dispatcher-queued" / "2099-01-02.jsonl",
        [
            {"ts": "2099-01-02T12:00:00Z", "source": "heal",
             "result": "deferred", "message_excerpt": "post-split deferral"},
        ],
    )
    c = _client(app)
    body = c.get("/api/alerts/dispatcher-log?queued=1").get_json()
    excerpts = {r["message_excerpt"] for r in body["records"]}
    assert excerpts == {"🟢 ok", "historic deferral", "post-split deferral"}


def test_dispatcher_log_queued_is_independent_of_suppressed(app):
    """Both toggles default off and neither implies the other — deferred
    *will* be delivered, suppressed will not, so collapsing them would
    trade one wrong label for another."""
    shared = app["shared"]
    _write_jsonl(
        shared / "alerts" / "dispatcher-queued" / "2099-01-02.jsonl",
        [{"ts": "2099-01-02T12:00:00Z", "source": "heal",
          "result": "deferred", "message_excerpt": "queued row"}],
    )
    _write_jsonl(
        shared / "alerts" / "dispatcher-suppressed" / "2099-01-02.jsonl",
        [{"ts": "2099-01-02T12:00:01Z", "source": "heal",
          "result": "suppressed_cooldown", "message_excerpt": "muted row"}],
    )
    c = _client(app)

    def _excerpts(qs):
        return {r["message_excerpt"]
                for r in c.get(f"/api/alerts/dispatcher-log{qs}").get_json()["records"]}

    assert _excerpts("") == set()
    assert _excerpts("?queued=1") == {"queued row"}
    assert _excerpts("?suppressed=1") == {"muted row"}
    assert _excerpts("?queued=1&suppressed=1") == {"queued row", "muted row"}


def test_dispatcher_log_last_delivery_stat_reads_an_actual_send(app):
    """The Reports header's "last delivery" stat is ``limit=1`` with both
    toggles at their defaults. Pre-split a fresh deferral could win that
    slot, so the header claimed a delivery the phone never got."""
    shared = app["shared"]
    _write_jsonl(
        shared / "alerts" / "dispatcher.jsonl",
        [
            {"ts": "2099-01-01T12:00:00Z", "source": "audit",
             "result": "sent", "message_excerpt": "🟢 real delivery"},
            {"ts": "2099-01-01T23:59:00Z", "source": "heal",
             "result": "deferred", "message_excerpt": "much newer deferral"},
        ],
    )
    c = _client(app)
    body = c.get("/api/alerts/dispatcher-log?limit=1").get_json()
    assert len(body["records"]) == 1
    assert body["records"][0]["result"] == "sent"


def test_dispatcher_log_surfaces_newest_on_high_volume_day(app):
    """The crux of the storm-day fix.

    On a high-volume day a single date-partitioned day file can hold
    thousands of records. The per-stream cap is ``limit * 4`` (400 for the
    default limit of 100). The OLD head-reader walked the day file from the
    START, hit the cap on the EARLIEST ~400 records, broke, then sorted those
    newest-first — so the "newest" message shown was an early-morning send
    while recent sends (later in the same file) were never read. Operators
    saw "newest 19h ago" while sends were minutes old.

    Synthesize a day file with > per_stream_cap records spanning early→late
    timestamps and assert the API returns the LATEST records (newest-first),
    not the earliest.
    """
    shared = app["shared"]
    day = "2099-03-15"
    n = 1000  # well above per_stream_cap (400) for default limit=100
    records = [
        {"ts": f"{day}T{h:02d}:{m:02d}:00Z", "source": "audit",
         "result": "sent", "message_excerpt": f"msg {i:04d}"}
        for i, (h, m) in enumerate(
            (i // 60 % 24, i % 60) for i in range(n)
        )
    ]
    # Write in file (oldest-first) order to the date-partitioned path the
    # dispatcher actually uses — alerts/dispatcher/<day>.jsonl.
    _write_jsonl(shared / "alerts" / "dispatcher" / f"{day}.jsonl", records)

    c = _client(app)
    body = c.get("/api/alerts/dispatcher-log?limit=100").get_json()
    got = body["records"]
    assert len(got) == 100

    # Newest-first ordering.
    ts_list = [r["ts"] for r in got]
    assert ts_list == sorted(ts_list, reverse=True)

    # The top record must be the LAST-written (most recent), not the first.
    assert got[0]["message_excerpt"] == records[-1]["message_excerpt"]
    # And the 100 returned must be the final 100 written — the recent tail.
    expected_tail = [r["message_excerpt"] for r in records[-100:]][::-1]
    assert [r["message_excerpt"] for r in got] == expected_tail


def test_dispatcher_log_low_volume_unchanged(app):
    """Low-volume days (records < per_stream_cap) behave exactly as before:
    every record surfaces, newest-first. Guards against the tail-read
    regressing the common case."""
    shared = app["shared"]
    day = "2099-03-16"
    records = [
        {"ts": f"{day}T0{i}:00:00Z", "source": "audit",
         "result": "sent", "message_excerpt": f"m{i}"}
        for i in range(5)
    ]
    _write_jsonl(shared / "alerts" / "dispatcher" / f"{day}.jsonl", records)

    c = _client(app)
    body = c.get("/api/alerts/dispatcher-log").get_json()
    got = body["records"]
    assert len(got) == 5
    assert [r["message_excerpt"] for r in got] == ["m4", "m3", "m2", "m1", "m0"]


def test_delivery_failures_same_auth_path_as_dispatcher_log(app):
    """The new endpoint is registered on the same Flask app as
    /api/alerts/dispatcher-log, so whatever auth guard wraps before_request
    or @app.before_request covers both uniformly. Pin that both return
    the same status from the same test client (so a future
    auth-middleware change can't accidentally leave the failures route
    open while locking the messages route)."""
    c = _client(app)
    msg = c.get("/api/alerts/dispatcher-log")
    fails = c.get("/api/alerts/delivery-failures")
    assert msg.status_code == fails.status_code


# ── Dispatcher Health honesty: non-delivery + digest staleness (R3) ─────────
#
# The Dispatcher Health panel must surface every way an alert silently
# fails to reach the operator — not just transport (HTTP/openclaw) failures.
# Before this fix, NO_RECIPIENT outcomes (routed to the *suppressed* log)
# and a stuck digest queue were invisible, so the panel showed "✓ all
# deliveries succeeded" while a whole delivery mode was dead.


def _iso_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_health_all_green_includes_rollup_and_digest(app):
    """Empty stores → summary.healthy True, with the new non_delivery /
    digest fields present (so the UI can rely on them)."""
    body = _client(app).get("/api/alerts/delivery-failures").get_json()
    s = body["summary"]
    assert s["healthy"] is True
    assert s["non_delivery"]["total"] == 0
    assert s["digest"]["stuck"] is False
    assert s["digest"]["daily"]["pending"] == 0


def test_no_recipient_makes_health_report_a_problem(app):
    """Falsifiable invariant #1: a NO_RECIPIENT dispatch result (logged to
    the suppressed file) makes the health endpoint report a delivery
    problem — NOT 'all deliveries succeeded'."""
    shared = app["shared"]
    _write_jsonl(
        shared / "alerts" / "dispatcher-suppressed.jsonl",
        [
            {"ts": _iso_now(), "source": "digest_dispatcher",
             "result": "no_recipient",
             "error": "no recipient configured (network.alerts unset ...)",
             "message_excerpt": "📋 Daily digest — 142 events"},
            # Intended mutes in the same file must NOT count as non-delivery.
            {"ts": _iso_now(), "source": "audit",
             "result": "suppressed_cooldown", "error": "cooldown:300s"},
            {"ts": _iso_now(), "source": "heal",
             "result": "suppressed_disabled", "error": "subscription_off:..."},
        ],
    )
    body = _client(app).get("/api/alerts/delivery-failures").get_json()
    s = body["summary"]
    assert s["healthy"] is False
    assert s["non_delivery"]["total"] == 1
    assert s["non_delivery"]["by_source"][0]["source"] == "digest_dispatcher"
    # The intended suppressions did not leak into the non-delivery tally.
    assert s["total"] == 0  # no transport failures


def test_stuck_digest_queue_makes_health_report_a_problem(app):
    """Falsifiable invariant #2: a non-empty digest queue whose oldest
    event is past the staleness window surfaces on the health data as a
    stuck pipeline."""
    from datetime import datetime, timezone, timedelta
    shared = app["shared"]
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    _write_jsonl(
        shared / "alerts" / "digest-pending" / "daily.jsonl",
        [
            {"ts": stale_ts, "source": "audit",
             "catalog_event": "security.audit_finding",
             "dedup_key": "a", "message": "🛡️ finding"},
        ],
    )
    body = _client(app).get("/api/alerts/delivery-failures").get_json()
    s = body["summary"]
    assert s["digest"]["stuck"] is True
    assert s["digest"]["daily"]["pending"] == 1
    assert s["healthy"] is False


def test_fresh_digest_queue_does_not_trip_health(app):
    """A digest queue with only recent events (normal accumulation between
    flush windows) is NOT a problem — depth alone must not flip degraded."""
    shared = app["shared"]
    _write_jsonl(
        shared / "alerts" / "digest-pending" / "daily.jsonl",
        [
            {"ts": _iso_now(), "source": "audit",
             "catalog_event": "security.audit_finding",
             "dedup_key": "a", "message": "🛡️ finding"},
        ],
    )
    body = _client(app).get("/api/alerts/delivery-failures").get_json()
    s = body["summary"]
    assert s["digest"]["daily"]["pending"] == 1
    assert s["digest"]["stuck"] is False
    assert s["healthy"] is True
