"""PWA Phase 1.2 — push notifications.

Pins the contract that the admin UI, dispatcher integration, and the
test endpoints rely on:

  - VAPID keypair lazy-init is idempotent: read twice → same key.
  - The vapid.json file is 0600.
  - Subscription add/list/remove roundtrips cleanly.
  - Re-subscribing the same endpoint updates the existing row (no
    duplicates) and preserves the subscription id.
  - mark_invalid flips the active flag without deleting the row, so
    the operator can see the failure in the Devices UI.
  - Dispatcher fans out to PWA push subscribers when the pwa_push
    channel is on, and skips fanout when off.
  - 410 Gone marks the subscription invalid; other errors increment
    last_failure but leave the subscription active.
  - Push fanout for a catalog-event SENT path records a {sent: N,
    attempted: N} entry in the dispatcher's audit log.
  - sw.js exposes both ``push`` and ``notificationclick`` listeners
    (regression guard for the Phase 1.1 TODO scaffolds being lost).

The pywebpush dependency is mocked end-to-end — these tests never hit
Apple/Google/Mozilla push services. The contract under test is the
admin-side glue around pywebpush, not pywebpush itself.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def shared(tmp_path):
    s = tmp_path / "evolve"
    s.mkdir()
    return s


@pytest.fixture
def fake_pywebpush(monkeypatch):
    """Inject a fake ``pywebpush`` module with controllable behaviour.

    Tests configure ``mod.next`` to (status_code, raise?) before each
    send; ``mod.calls`` collects every webpush() invocation so tests
    can assert payload shape.
    """
    calls: list[dict] = []
    next_results: list[dict] = []

    class WebPushException(Exception):
        def __init__(self, message="", response=None):
            super().__init__(message)
            self.response = response

    class _FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

    def webpush(*, subscription_info, data, vapid_private_key,
                vapid_claims, timeout=None):
        calls.append({
            "subscription_info": subscription_info,
            "data": data,
            "vapid_private_key": vapid_private_key,
            "vapid_claims": vapid_claims,
            "timeout": timeout,
        })
        if next_results:
            r = next_results.pop(0)
            if r.get("raise"):
                raise WebPushException(
                    r.get("message", "fake"),
                    response=_FakeResponse(r.get("status_code", 500)),
                )

    mod = types.ModuleType("pywebpush")
    mod.webpush = webpush  # type: ignore[attr-defined]
    mod.WebPushException = WebPushException  # type: ignore[attr-defined]
    mod.calls = calls  # type: ignore[attr-defined]
    mod.next_results = next_results  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywebpush", mod)
    return mod


# ── VAPID idempotency + 0600 ───────────────────────────────────────────────


def test_vapid_get_or_create_is_idempotent(shared):
    from evolve_admin.web import push_vapid

    k1 = push_vapid.get_or_create(shared)
    k2 = push_vapid.get_or_create(shared)
    assert k1.private_key_pem == k2.private_key_pem
    assert k1.public_key_b64url == k2.public_key_b64url


def test_vapid_file_mode_is_0600(shared):
    from evolve_admin.web import push_vapid

    push_vapid.get_or_create(shared)
    p = push_vapid.vapid_path(shared)
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600, f"vapid.json must be 0600, got {oct(mode)}"


def test_vapid_public_key_is_b64url_no_padding(shared):
    from evolve_admin.web import push_vapid

    pub = push_vapid.get_public_key_b64url(shared)
    # base64url alphabet: A-Z a-z 0-9 - _ ; no '=', no '+/' chars.
    assert pub
    assert "=" not in pub, f"public key still padded: {pub!r}"
    assert "+" not in pub and "/" not in pub
    # P-256 uncompressed point = 65 bytes → ~88 base64url chars (87
    # after stripping the single padding byte). Belt-and-braces sanity check.
    assert 80 <= len(pub) <= 100


# ── Subscription storage roundtrip ─────────────────────────────────────────


def test_subscription_add_list_remove(shared):
    from evolve_admin.web import push_subscriptions as subs

    assert subs.list_subscriptions(shared) == []

    sub = subs.add_subscription(
        shared,
        endpoint="https://push.apple.com/abc",
        keys={"p256dh": "PPP", "auth": "AAA"},
        device_label="iPhone Safari",
        user_agent="Mozilla/5.0 (iPhone)",
    )
    assert sub.id.startswith("sub_")
    assert sub.device_label == "iPhone Safari"

    listed = subs.list_subscriptions(shared)
    assert len(listed) == 1
    assert listed[0].endpoint == "https://push.apple.com/abc"

    assert subs.remove_subscription(shared, sub.id) is True
    assert subs.list_subscriptions(shared) == []


def test_subscription_dedupe_on_endpoint(shared):
    """Re-subscribing the same browser updates the existing row instead
    of creating a duplicate. The id is preserved so the UI's per-device
    toggles don't bounce."""
    from evolve_admin.web import push_subscriptions as subs

    s1 = subs.add_subscription(
        shared, endpoint="https://push.apple.com/abc",
        keys={"p256dh": "OLD", "auth": "OLD"},
        device_label="iPhone",
    )
    s2 = subs.add_subscription(
        shared, endpoint="https://push.apple.com/abc",
        keys={"p256dh": "NEW", "auth": "NEW"},
        device_label="Pod_admin iPhone",
    )
    assert s1.id == s2.id
    listed = subs.list_subscriptions(shared)
    assert len(listed) == 1
    assert listed[0].keys == {"p256dh": "NEW", "auth": "NEW"}
    assert listed[0].device_label == "Pod_admin iPhone"


def test_mark_invalid_keeps_row(shared):
    """mark_invalid is a soft delete — the row stays so the operator
    sees the failure in the Devices UI."""
    from evolve_admin.web import push_subscriptions as subs

    subs.add_subscription(
        shared, endpoint="https://push.apple.com/abc",
        keys={"p256dh": "P", "auth": "A"},
        device_label="iPhone",
    )
    assert subs.mark_invalid(shared, "https://push.apple.com/abc") is True
    listed = subs.list_subscriptions(shared)
    assert len(listed) == 1
    assert listed[0].invalid is True
    assert subs.list_active(shared) == []


def test_subscription_default_label_when_empty(shared):
    """An empty device_label gets a sensible fallback so the UI never
    renders a blank row."""
    from evolve_admin.web import push_subscriptions as subs

    sub = subs.add_subscription(
        shared, endpoint="https://push.example/1",
        keys={"p256dh": "P", "auth": "A"},
        device_label="",
        user_agent="",
    )
    assert sub.device_label == "Unnamed device"


# ── push_sender fanout ─────────────────────────────────────────────────────


def test_fanout_calls_webpush_per_subscription(shared, fake_pywebpush):
    from evolve_admin.alerts import push_sender
    from evolve_admin.web import push_subscriptions as subs

    subs.add_subscription(
        shared, endpoint="https://push.apple.com/one",
        keys={"p256dh": "P1", "auth": "A1"}, device_label="iPhone",
    )
    subs.add_subscription(
        shared, endpoint="https://fcm.googleapis.com/two",
        keys={"p256dh": "P2", "auth": "A2"}, device_label="Pixel",
    )

    result = push_sender.fanout_pwa_push(
        shared,
        title="Gateway down", body="team_bot_a gateway not responding",
        url="/maintenance", event_key="system.gateway_state_change",
        severity="error",
    )
    assert result.attempted == 2
    assert result.sent == 2
    assert result.failed == 0
    assert len(fake_pywebpush.calls) == 2
    call = fake_pywebpush.calls[0]
    payload = json.loads(call["data"])
    assert payload["title"] == "Gateway down"
    assert payload["event_key"] == "system.gateway_state_change"
    assert payload["url"] == "/maintenance"


def test_fanout_410_marks_invalid(shared, fake_pywebpush):
    from evolve_admin.alerts import push_sender
    from evolve_admin.web import push_subscriptions as subs

    sub = subs.add_subscription(
        shared, endpoint="https://push.apple.com/x",
        keys={"p256dh": "P", "auth": "A"}, device_label="iPhone",
    )
    fake_pywebpush.next_results.append({"raise": True, "status_code": 410})

    result = push_sender.fanout_pwa_push(
        shared, title="t", body="b", event_key="security.audit_finding",
    )
    assert result.removed_invalid == 1
    assert result.sent == 0
    assert result.failed == 0
    after = subs.list_subscriptions(shared)
    assert after[0].invalid is True, "410 should mark the row invalid"
    assert sub.id == after[0].id
    assert subs.list_active(shared) == [], "active list excludes invalids"


def test_fanout_other_failure_increments_last_failure(shared, fake_pywebpush):
    from evolve_admin.alerts import push_sender
    from evolve_admin.web import push_subscriptions as subs

    subs.add_subscription(
        shared, endpoint="https://push.apple.com/x",
        keys={"p256dh": "P", "auth": "A"}, device_label="iPhone",
    )
    fake_pywebpush.next_results.append({"raise": True, "status_code": 502})

    result = push_sender.fanout_pwa_push(
        shared, title="t", body="b", event_key="security.audit_finding",
    )
    assert result.failed == 1
    assert result.removed_invalid == 0
    after = subs.list_subscriptions(shared)
    assert after[0].invalid is False, "non-410 must not mark invalid"
    assert after[0].last_failure and "502" in after[0].last_failure


def test_fanout_empty_subscriber_set_returns_clean(shared, fake_pywebpush):
    from evolve_admin.alerts import push_sender

    result = push_sender.fanout_pwa_push(
        shared, title="t", body="b", event_key="security.audit_finding",
    )
    assert result.attempted == 0
    assert result.sent == 0
    assert result.failed == 0
    assert fake_pywebpush.calls == []


def test_fanout_continues_after_per_subscription_error(shared, fake_pywebpush):
    from evolve_admin.alerts import push_sender
    from evolve_admin.web import push_subscriptions as subs

    subs.add_subscription(
        shared, endpoint="https://push.apple.com/a",
        keys={"p256dh": "P", "auth": "A"}, device_label="A",
    )
    subs.add_subscription(
        shared, endpoint="https://push.apple.com/b",
        keys={"p256dh": "P", "auth": "A"}, device_label="B",
    )
    # First fails, second succeeds.
    fake_pywebpush.next_results.extend([
        {"raise": True, "status_code": 502},
        {},
    ])
    result = push_sender.fanout_pwa_push(
        shared, title="t", body="b", event_key="security.audit_finding",
    )
    assert result.attempted == 2
    assert result.sent == 1
    assert result.failed == 1


# ── Dispatcher integration ────────────────────────────────────────────────


@pytest.fixture
def dispatcher_env(tmp_path, monkeypatch):
    """tmp shared_dir + fake openclaw subprocess so dispatcher.send is
    self-contained. Mirrors the pattern in test_alerts_dispatcher."""
    _ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
    if str(_ANALYZER_DIR) not in sys.path:
        sys.path.insert(0, str(_ANALYZER_DIR))
    from evolve_admin.alerts import dispatcher

    shared = tmp_path / "evolve"
    shared.mkdir()

    def _fake_dispatch(channel, chat_id, message, gateway_port=None):
        return True, None

    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _fake_dispatch)
    # Bypass the telegram-http path so the fake openclaw always runs.
    monkeypatch.setattr(
        dispatcher, "_dispatch_via_telegram_http",
        lambda chat_id, message: (False, "no-telegram-token: forced for test"),
    )
    network = {
        "alerts": {"channel": "telegram", "chatId": "12345"},
        "bots": {},
    }
    return {"shared": shared, "network": network, "dispatcher": dispatcher}


def test_dispatcher_skips_push_when_channel_off(
    dispatcher_env, fake_pywebpush,
):
    """Default state — pwa_push channel off — no fanout even if there
    are subscribers."""
    from evolve_admin.alerts import subscriptions as alert_subs
    from evolve_admin.alerts.catalog import Frequency
    from evolve_admin.web import push_subscriptions as subs

    sd = dispatcher_env["shared"]
    # security.audit_finding now defaults to the daily digest (workstream D1);
    # pin an immediate override so this push-fanout test runs on the immediate
    # send path it is exercising.
    alert_subs.write_subscription(sd, "security.audit_finding", frequency=Frequency.IMMEDIATE)
    subs.add_subscription(
        sd, endpoint="https://push.apple.com/x",
        keys={"p256dh": "P", "auth": "A"}, device_label="iPhone",
    )
    out = dispatcher_env["dispatcher"].send(
        shared_dir=sd, network=dispatcher_env["network"],
        source="audit",
        catalog_event="security.audit_finding",
        payload={"bot_id": "team_bot_a", "check_title": "gateway loopback auth missing"},
    )
    assert out.result.value == "sent"
    assert fake_pywebpush.calls == [], "push must not fire while channel is off"


def test_dispatcher_fans_out_push_when_channel_on(
    dispatcher_env, fake_pywebpush,
):
    """Channel on → catalog send triggers a push fanout in addition to
    the primary chat send."""
    from evolve_admin.alerts import subscriptions as alert_subs
    from evolve_admin.alerts.catalog import Frequency
    from evolve_admin.web import push_subscriptions as subs

    sd = dispatcher_env["shared"]
    alert_subs.write_pwa_push_enabled(sd, True)
    # Immediate override — audit_finding now digests by default (D1); this
    # test exercises the immediate-send push fanout.
    alert_subs.write_subscription(sd, "security.audit_finding", frequency=Frequency.IMMEDIATE)
    subs.add_subscription(
        sd, endpoint="https://push.apple.com/x",
        keys={"p256dh": "P", "auth": "A"}, device_label="iPhone",
    )
    out = dispatcher_env["dispatcher"].send(
        shared_dir=sd, network=dispatcher_env["network"],
        source="audit",
        catalog_event="security.audit_finding",
        payload={"bot_id": "team_bot_a", "check_title": "gateway loopback auth missing"},
    )
    assert out.result.value == "sent"
    assert len(fake_pywebpush.calls) == 1
    payload = json.loads(fake_pywebpush.calls[0]["data"])
    assert payload["event_key"] == "security.audit_finding"
    # The dispatched message header (first non-blank line of the rendered
    # catalog body) becomes the push title.
    assert payload["title"].startswith("🛡️"), (
        f"push title should start with the catalog emoji; got {payload['title']!r}"
    )
    # Subscription footer must NOT leak into the body — it's noise on a
    # phone notification.
    assert "subscription:" not in payload["body"]


def test_dispatcher_audit_log_records_pwa_push_counts(
    dispatcher_env, fake_pywebpush,
):
    """The dispatcher's JSONL audit log gains a ``pwa_push`` field when
    fanout fires, so the operator's Activity tab can show "+ 1 device"."""
    from evolve_admin.alerts import subscriptions as alert_subs
    from evolve_admin.alerts.catalog import Frequency
    from evolve_admin.web import push_subscriptions as subs

    sd = dispatcher_env["shared"]
    alert_subs.write_pwa_push_enabled(sd, True)
    # Immediate override — audit_finding now digests by default (D1); this
    # test exercises the immediate-send audit-log + push-count record.
    alert_subs.write_subscription(sd, "security.audit_finding", frequency=Frequency.IMMEDIATE)
    subs.add_subscription(
        sd, endpoint="https://push.apple.com/x",
        keys={"p256dh": "P", "auth": "A"}, device_label="iPhone",
    )
    dispatcher_env["dispatcher"].send(
        shared_dir=sd, network=dispatcher_env["network"],
        source="audit",
        catalog_event="security.audit_finding",
        payload={"bot_id": "team_bot_a", "check_title": "gateway loopback auth missing"},
    )
    # Dispatcher writes to alerts/dispatcher/<YYYY-MM-DD>.jsonl post-
    # rotation (2026-06-01). Read whichever date file landed.
    log_dir = sd / "alerts" / "dispatcher"
    lines = []
    for p in sorted(log_dir.glob("*.jsonl")) if log_dir.is_dir() else []:
        lines.extend(json.loads(ln) for ln in p.read_text().splitlines() if ln)
    sent = [r for r in lines if r.get("result") == "sent"]
    assert sent, "expected a SENT audit record"
    rec = sent[-1]
    assert rec.get("pwa_push") == {
        "attempted": 1, "sent": 1, "failed": 0, "removed_invalid": 0,
    }


# ── Service-worker regression guard ────────────────────────────────────────


def test_sw_js_has_push_and_notificationclick_listeners():
    """The Phase 1.2 push + notificationclick handlers must remain
    registered. A future edit that accidentally drops them (e.g. the
    Phase 1.1 TODO scaffolds being restored) would silently disable
    push delivery for every installed PWA."""
    sw_path = _ADMIN_DIR / "evolve_admin" / "web" / "sw.js"
    text = sw_path.read_text(encoding="utf-8")
    assert "addEventListener('push'" in text or 'addEventListener("push"' in text
    assert (
        "addEventListener('notificationclick'" in text
        or 'addEventListener("notificationclick"' in text
    )
    # Both handlers must actually show a notification / focus a client —
    # the bare TODO stubs would only register handlers that do nothing.
    assert "showNotification" in text
    assert "openWindow" in text or "matchAll" in text
