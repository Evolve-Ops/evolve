"""Phase A3 — operator subscription persistence.

Pins the contract Phase A4 (API routes) and Phase B (UI) will rely on:

  - read_subscription returns catalog defaults when no overrides exist
  - write_subscription persists enabled / frequency individually
  - sparse storage: events without overrides aren't in the file
  - validation: unknown event_key raises KeyError; out-of-allowed
    frequency raises ValueError before touching disk
  - reset() wipes overrides cleanly
  - is_overridden flag tracks whether the operator has touched the entry
  - dispatcher integration: an operator-set enabled=False blocks send
    end-to-end (regression guard for the A2→A3 wiring)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.alerts import subscriptions as subs  # noqa: E402
from evolve_admin.alerts import catalog as cat  # noqa: E402
from evolve_admin.alerts.catalog import Frequency  # noqa: E402


@pytest.fixture
def shared(tmp_path):
    s = tmp_path / "evolve"
    s.mkdir()
    return s


# ── Read with no file ───────────────────────────────────────────────────────


def test_read_subscription_returns_catalog_default_when_no_file(shared):
    """Default state — no subscriptions.json yet — read returns the
    catalog default for every event. No file is created on read."""
    sub = subs.read_subscription(shared, "security.audit_finding")
    assert sub is not None
    entry = cat.by_key("security.audit_finding")
    assert entry is not None
    assert sub.enabled is entry.default_enabled
    assert sub.frequency is entry.default_frequency
    assert sub.is_overridden is False
    # No file written by read.
    assert not (shared / "alerts" / "subscriptions.json").exists()


def test_read_subscription_unknown_event_returns_none(shared):
    assert subs.read_subscription(shared, "not.a.real.event") is None


def test_read_all_returns_full_catalog_view(shared):
    """The Subscriptions UI fetches the full list in one shot — every
    catalog event must be present, in catalog order."""
    all_subs = subs.read_all_subscriptions(shared)
    assert len(all_subs) == len(cat.CATALOG)
    assert tuple(s.event_key for s in all_subs) == cat.all_keys()
    # All defaulted (no overrides).
    assert all(s.is_overridden is False for s in all_subs)


# ── Write + read roundtrip ──────────────────────────────────────────────────


def test_write_persists_enabled_change(shared):
    sub = subs.write_subscription(
        shared, "decisions.proposal_applied", enabled=True,
    )
    assert sub.enabled is True
    assert sub.is_overridden is True
    # Re-read confirms persistence.
    again = subs.read_subscription(shared, "decisions.proposal_applied")
    assert again is not None and again.enabled is True
    assert again.is_overridden is True


def test_write_persists_frequency_change(shared):
    sub = subs.write_subscription(
        shared, "security.audit_finding", frequency=Frequency.DAILY_DIGEST,
    )
    assert sub.frequency is Frequency.DAILY_DIGEST
    assert sub.is_overridden is True


def test_write_accepts_frequency_as_string(shared):
    """API handlers will pass strings from JSON. Convenience: accept
    them directly so call sites don't need to import Frequency."""
    sub = subs.write_subscription(
        shared, "cost.daily_threshold", frequency="daily_digest",
    )
    assert sub.frequency is Frequency.DAILY_DIGEST


def test_group_toggle_gates_event_frequency_preserved(shared):
    """Phase 2: on/off is the GROUP's job. Toggling cost_warnings off
    gates cost.daily_threshold; its frequency stays at the catalog default
    (frequency is a per-event property, untouched by the group toggle)."""
    subs.write_subscription_group(shared, "cost_warnings", enabled=False)
    sub = subs.read_subscription(shared, "cost.daily_threshold")
    assert sub is not None and sub.enabled is False
    entry = cat.by_key("cost.daily_threshold")
    assert sub.frequency is entry.default_frequency


def test_storage_is_sparse(shared):
    """Only events the operator has touched land in the file. Events
    that match the catalog default are not stored — small file, lean
    diffs, easy to grep."""
    subs.write_subscription(shared, "decisions.proposal_applied", enabled=True)
    state = json.loads((shared / "alerts" / "subscriptions.json").read_text())
    stored = state["subscriptions"]
    # Only the touched event is in the file.
    assert "decisions.proposal_applied" in stored
    # Other catalog events are absent.
    assert "security.audit_finding" not in stored
    assert "cost.daily_threshold" not in stored


# ── Validation ──────────────────────────────────────────────────────────────


def test_write_unknown_event_raises_keyerror(shared):
    with pytest.raises(KeyError, match="unknown catalog event"):
        subs.write_subscription(shared, "not.a.real.event", enabled=True)


def test_write_disallowed_frequency_raises_valueerror(shared):
    """`updates.evolve_repo` does not allow IMMEDIATE — confirm the
    UI can't put the dispatcher in a state the catalog rejects."""
    with pytest.raises(ValueError, match="not in allowed_frequencies"):
        subs.write_subscription(
            shared, "updates.evolve_repo", frequency=Frequency.IMMEDIATE,
        )


def test_write_unknown_frequency_string_raises_valueerror(shared):
    with pytest.raises(ValueError, match="unknown frequency"):
        subs.write_subscription(
            shared, "security.audit_finding", frequency="not-a-frequency",
        )


def test_write_validation_does_not_touch_disk(shared):
    """If validation raises, no partial write should land. Stops a
    bad UI form from corrupting the operator's existing prefs."""
    # First, set a good preference.
    subs.write_subscription(shared, "cost.daily_threshold", enabled=False)
    pre = (shared / "alerts" / "subscriptions.json").read_text()

    # Then, attempt an invalid update.
    with pytest.raises(ValueError):
        subs.write_subscription(
            shared, "updates.evolve_repo", frequency=Frequency.IMMEDIATE,
        )

    post = (shared / "alerts" / "subscriptions.json").read_text()
    assert pre == post, "validation failure must not modify the file"


# ── Reset ───────────────────────────────────────────────────────────────────


def test_reset_wipes_all_overrides(shared):
    subs.write_subscription(shared, "cost.daily_threshold", enabled=False)
    subs.write_subscription(
        shared, "security.audit_finding", frequency=Frequency.DAILY_DIGEST,
    )
    # Both overrides present pre-reset.
    assert subs.read_subscription(shared, "cost.daily_threshold").is_overridden is True
    assert subs.read_subscription(shared, "security.audit_finding").is_overridden is True

    subs.reset(shared)

    # All overrides gone; reads return catalog defaults.
    s1 = subs.read_subscription(shared, "cost.daily_threshold")
    s2 = subs.read_subscription(shared, "security.audit_finding")
    assert s1.is_overridden is False
    assert s2.is_overridden is False


def test_reset_when_no_file_is_a_noop(shared):
    """Reset before any write must not blow up — operator clicks
    Reset on a fresh install and gets a clean no-op."""
    subs.reset(shared)
    assert not (shared / "alerts" / "subscriptions.json").exists()


# ── Digest hour ─────────────────────────────────────────────────────────────


def test_digest_hour_default_is_8(shared):
    assert subs.read_digest_hour_local(shared) == 8


def test_write_digest_hour_persists(shared):
    subs.write_digest_hour_local(shared, 18)
    assert subs.read_digest_hour_local(shared) == 18


def test_write_digest_hour_rejects_invalid(shared):
    with pytest.raises(ValueError):
        subs.write_digest_hour_local(shared, 24)
    with pytest.raises(ValueError):
        subs.write_digest_hour_local(shared, -1)


# ── Atomic write contract ───────────────────────────────────────────────────


def test_write_uses_atomic_temp_then_rename(shared):
    """No leftover .tmp files after a successful write."""
    subs.write_subscription(shared, "cost.daily_threshold", enabled=False)
    leftover = list((shared / "alerts").glob(".*.tmp"))
    assert leftover == []


def test_write_recovers_when_file_corrupt(shared):
    """If the file got corrupt somehow, writes still succeed (we
    overwrite from scratch). Operator's not stuck on a bad file."""
    p = shared / "alerts" / "subscriptions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{this isn't json")

    group = subs.write_subscription_group(
        shared, "cost_warnings", enabled=False,
    )
    assert group.enabled is False
    # File is now valid JSON.
    state = json.loads(p.read_text())
    assert "subscription_groups" in state


# ── Dispatcher integration ──────────────────────────────────────────────────


def test_dispatcher_honors_operator_subscription_off(shared, tmp_path, monkeypatch):
    """End-to-end: operator disables the cost_warnings GROUP; dispatcher.send
    for a member event (cost.daily_threshold) returns SUPPRESSED_DISABLED
    with subscription_off reason. Pins the Phase-2 per-group gating
    (read_subscription resolves enabled through the group)."""
    from evolve_admin.alerts import dispatcher

    # Persist an operator group-level mute.
    subs.write_subscription_group(
        shared, "cost_warnings", enabled=False,
    )

    # Stub the openclaw subprocess so a bug doesn't shell out.
    sent: list = []

    def fake_dispatch(channel, chat_id, message):
        sent.append((channel, chat_id, message))
        return True, None
    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", fake_dispatch)

    out = dispatcher.send(
        shared_dir=shared,
        network={"alerts": {"channel": "telegram", "chatId": "12345"}},
        source="spend_alert", message="admin_bot: $5.96 over $5.00",
        catalog_event="cost.daily_threshold",
        dedup_key="spend_alert/threshold/admin_bot/2026-05-10",
    )
    assert out.result == dispatcher.DispatchResult.SUPPRESSED_DISABLED
    assert "subscription_off:cost.daily_threshold" in (out.error or "")
    assert sent == [], "operator-disabled events must not reach openclaw"


def test_dispatcher_honors_operator_frequency_override(shared, monkeypatch):
    """Operator switches security.audit_finding from default IMMEDIATE
    to DAILY_DIGEST — next dispatcher.send for that event lands in the
    digest queue with DEFERRED, not SENT."""
    from evolve_admin.alerts import dispatcher

    subs.write_subscription(
        shared, "security.audit_finding", frequency=Frequency.DAILY_DIGEST,
    )

    sent: list = []

    def fake_dispatch(channel, chat_id, message):
        sent.append((channel, chat_id, message))
        return True, None
    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", fake_dispatch)

    out = dispatcher.send(
        shared_dir=shared,
        network={"alerts": {"channel": "telegram", "chatId": "12345"}},
        source="audit", message="🛡️ team_bot_a: gateway loopback auth missing",
        catalog_event="security.audit_finding",
    )
    assert out.result == dispatcher.DispatchResult.DEFERRED
    assert sent == []
    queue = shared / "alerts" / "digest-pending" / "daily.jsonl"
    assert queue.exists()
    queued = [json.loads(l) for l in queue.read_text().splitlines() if l]
    assert len(queued) == 1
    assert queued[0]["catalog_event"] == "security.audit_finding"


def test_repeated_deferred_no_dedup_key_collapses_to_one_at_flush(
    shared, monkeypatch,
):
    """Regression for the 2026-06 digest-queue leak: a recurring digest-mode
    signal whose caller passes ``dedup_key=None`` (the resolve/recovery and
    storm rate-cap paths legitimately do) re-enqueued one line per tick, and
    ``_dedup_records`` collapsed only on an explicit dedup_key — so the queue
    grew unbounded (4804/4827 records had no dedup_key) and the once-daily,
    chunk-capped flush never caught up.

    The fix: ``_enqueue_digest`` stamps a ``digest_collapse_key`` derived from
    ``(source, catalog_event, body_hash)`` when no dedup_key is supplied, and
    ``_dedup_records`` collapses on it. N identical DEFERRED enqueues must
    reduce to exactly ONE record at flush.
    """
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts import digest_dispatcher

    subs.write_subscription(
        shared, "security.audit_finding", frequency=Frequency.DAILY_DIGEST,
    )

    network = {"alerts": {"channel": "telegram", "chatId": "12345"},
               "timezone": "UTC"}

    # N repeated DEFERRED enqueues of the SAME recurring signal, dedup_key=None.
    N = 25
    for _ in range(N):
        out = dispatcher.send(
            shared_dir=shared,
            network=network,
            source="audit",
            message="🛡️ team_bot_a: gateway loopback auth missing",
            catalog_event="security.audit_finding",
            dedup_key=None,   # the leak case — no cooldown identity supplied
        )
        assert out.result == dispatcher.DispatchResult.DEFERRED

    queue = shared / "alerts" / "digest-pending" / "daily.jsonl"
    queued = [json.loads(l) for l in queue.read_text().splitlines() if l]
    # On disk the queue is still append-only (N lines), but every record now
    # carries a stable collapse key so the flush can coalesce them.
    assert len(queued) == N
    assert all(r.get("digest_collapse_key") for r in queued)
    assert len({r["digest_collapse_key"] for r in queued}) == 1

    # Flush collapses the N identical records to ONE delivered line.
    sent: list = []

    def fake_send(*, shared_dir, network, source, severity, message=None,
                  payload=None, dedup_key=None, catalog_event=None,
                  now=None, **_kw):
        sent.append(message)
        return dispatcher.DispatchOutcome(
            result=dispatcher.DispatchResult.SENT, source=source,
            severity=severity, dedup_key=dedup_key,
            channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    from datetime import datetime, timezone
    outcome = digest_dispatcher.flush(
        shared, network, frequency="daily",
        now=datetime(2026, 6, 25, 8, 0, tzinfo=timezone.utc),
    )
    assert outcome is not None
    assert outcome.result == dispatcher.DispatchResult.SENT
    assert len(sent) == 1
    body = sent[0]
    # The recurring finding appears exactly once — collapsed from 25 records
    # to a single digest bullet (without the fix it would be 25 lines).
    assert body.count("gateway loopback auth missing") == 1
    assert body.count("•") == 1


def test_distinct_no_dedup_key_events_stay_uncollapsed_at_flush(
    shared, monkeypatch,
):
    """Companion guard: the collapse-key fallback must NOT over-collapse.
    Genuinely-unique events (distinct bodies, dedup_key=None — e.g. forge
    job completions, per-SHA commit notices) keep distinct body_hashes and
    survive the flush as separate lines, preserving the original
    'don't collapse me' contract for ``dedup_key=None``.
    """
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts import digest_dispatcher

    subs.write_subscription(
        shared, "security.audit_finding", frequency=Frequency.DAILY_DIGEST,
    )
    network = {"alerts": {"channel": "telegram", "chatId": "12345"},
               "timezone": "UTC"}

    for i in range(3):
        out = dispatcher.send(
            shared_dir=shared,
            network=network,
            source="audit",
            message=f"🛡️ team_bot_{i}: distinct finding {i}",
            catalog_event="security.audit_finding",
            dedup_key=None,
        )
        assert out.result == dispatcher.DispatchResult.DEFERRED

    sent: list = []

    def fake_send(*, shared_dir, network, source, severity, message=None,
                  payload=None, dedup_key=None, catalog_event=None,
                  now=None, **_kw):
        sent.append(message)
        return dispatcher.DispatchOutcome(
            result=dispatcher.DispatchResult.SENT, source=source,
            severity=severity, dedup_key=dedup_key,
            channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    from datetime import datetime, timezone
    outcome = digest_dispatcher.flush(
        shared, network, frequency="daily",
        now=datetime(2026, 6, 25, 8, 0, tzinfo=timezone.utc),
    )
    assert outcome is not None
    assert outcome.result == dispatcher.DispatchResult.SENT
    body = sent[0]
    # All three distinct findings survive — none collapsed (3 bullets).
    assert body.count("•") == 3
    for i in range(3):
        assert f"distinct finding {i}" in body
