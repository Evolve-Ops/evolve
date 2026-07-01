"""tests/test_alerts_notify_gate.py — notify gate (subscription-completeness
Phase 2 chip 3, spec-subscription-completeness-2026-06-24.md
"Removed/internalized").

Pins the contract that the six internalized events no longer reach the
operator's CHAT channel, while the two internal-but-still-delivering events
(meta.digest, meta.unclassified) keep dispatching. The mechanism under test:

  - CatalogEvent gains a ``notify: bool = True`` field.
  - The six removed events set ``notify=False``; digest/unclassified (and
    everything else) keep ``notify=True``.
  - ``dispatcher.send`` consults ``catalog.notify_for`` before the chat push
    and returns SUPPRESSED_DISABLED (reason ``notify_off:<key>``) for
    notify=False events — without firing the openclaw / telegram send.

We monkeypatch ``_dispatch_via_openclaw`` so no real subprocess fires; the
contract under test is the gate, not the transport.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# The six events internalized by this chip — produced, but no longer pushed
# to chat. Must match the notify=False entries in catalog.py exactly.
STOPPED_EVENTS = (
    "decisions.proposal_rejected",
    "decisions.proposal_outcome_checkin",
    "decisions.proposal_applied",
    "decisions.briefing_activated",
    "meta.send_probe",
    "meta.alert_repeat_loop",
)

# The two internal events that legitimately keep delivering.
KEPT_EVENTS = (
    "meta.digest",
    "meta.unclassified",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """tmp shared_dir + a fake openclaw subprocess + a default-recipient network.

    Mirrors test_alerts_dispatcher.env so the chat-send path is observable
    via ``sent`` and never hits a real subprocess.
    """
    from evolve_admin.alerts import dispatcher

    shared = tmp_path / "evolve"
    shared.mkdir()

    sent: list[tuple[str, str, str]] = []  # [(channel, chat_id, message)]

    def _fake_dispatch(channel, chat_id, message, gateway_port=None):
        sent.append((channel, chat_id, message))
        return True, None

    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _fake_dispatch)
    # Force the openclaw path (no real telegram token on the test box) so a
    # send that DOES go through is captured in ``sent``.
    monkeypatch.setattr(
        dispatcher, "_dispatch_via_telegram_http",
        lambda chat_id, message: (False, "no-telegram-token: test"),
    )

    network = {
        "alerts": {"channel": "telegram", "chatId": "12345"},
        "bots": {},
    }
    return {
        "shared": shared,
        "network": network,
        "dispatcher": dispatcher,
        "sent": sent,
    }


# ── Catalog flag ────────────────────────────────────────────────────────────


def test_six_removed_events_are_notify_false():
    """Each of the six internalized events carries notify=False."""
    from evolve_admin.alerts import catalog

    for key in STOPPED_EVENTS:
        entry = catalog.by_key(key)
        assert entry is not None, f"{key} missing from catalog"
        assert entry.notify is False, f"{key} should be notify=False"
        # These are internal/hidden-from-Configure events.
        assert entry.subscription is None, f"{key} should be subscription=None"


def test_kept_events_are_notify_true():
    """meta.digest and meta.unclassified keep notify=True (they deliver)."""
    from evolve_admin.alerts import catalog

    for key in KEPT_EVENTS:
        entry = catalog.by_key(key)
        assert entry is not None, f"{key} missing from catalog"
        assert entry.notify is True, f"{key} should stay notify=True"


def test_notify_for_resolves_legacy_and_unknown_keys():
    """notify_for fails open: unknown keys → True (louder, safe default)."""
    from evolve_admin.alerts import catalog

    assert catalog.notify_for("decisions.proposal_rejected") is False
    assert catalog.notify_for("meta.digest") is True
    # Unknown key resolves to True so a typo never silently swallows a send.
    assert catalog.notify_for("does.not.exist") is True


def test_every_other_event_still_notifies():
    """Only the six internalized events are notify=False; nothing else."""
    from evolve_admin.alerts import catalog

    notify_false = {e.key for e in catalog.CATALOG if e.notify is False}
    assert notify_false == set(STOPPED_EVENTS)


# ── Dispatch path ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("event_key", STOPPED_EVENTS)
def test_stopped_event_is_not_chat_dispatched(env, event_key):
    """A notify=False event suppresses the chat push (no send fires)."""
    d = env["dispatcher"]
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="review", message="should not reach chat",
        catalog_event=event_key,
    )
    assert out.result == d.DispatchResult.SUPPRESSED_DISABLED
    assert out.catalog_event == event_key
    # The transport never ran — nothing was pushed to chat.
    assert env["sent"] == [], f"{event_key} should not have sent a chat message"


@pytest.mark.parametrize("event_key", KEPT_EVENTS)
def test_kept_event_still_dispatches(env, event_key):
    """meta.digest / meta.unclassified are notify=True, so the notify gate
    lets them through to the operator. The gate is about suppression, not
    cadence: meta.digest pushes immediately (IMMEDIATE-only envelope), while
    meta.unclassified now lands in the daily digest
    (spec-subscription-digest-default-2026-06-28, workstreams D1+D4) — both
    reach the operator, neither is swallowed by the notify gate."""
    d = env["dispatcher"]
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="digest_dispatcher" if event_key == "meta.digest" else "signal_notifier",
        message="digest body" if event_key == "meta.digest" else "unmapped alert",
        catalog_event=event_key,
    )
    # The notify gate did NOT suppress it (that would be SUPPRESSED_DISABLED).
    assert out.result != d.DispatchResult.SUPPRESSED_DISABLED
    assert out.catalog_event == event_key
    if out.result == d.DispatchResult.SENT:
        assert len(env["sent"]) == 1
        assert env["sent"][0][0] == "telegram"
    else:
        # Deferred into the digest queue — delivered later, not dropped.
        assert out.result == d.DispatchResult.DEFERRED
        assert env["sent"] == []


def test_notify_gate_runs_before_subscription_state(env):
    """notify=False holds even if the operator never set any subscription.

    The gate is a fixed catalog property, not an operator override — an
    internalized event must stay chat-silent regardless of subscription
    state (which doesn't exist for subscription=None events anyway).
    """
    d = env["dispatcher"]
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="outcome", message="did this help?",
        catalog_event="decisions.proposal_outcome_checkin",
        dedup_key="outcome/checkin/prop_1",
    )
    assert out.result == d.DispatchResult.SUPPRESSED_DISABLED
    assert env["sent"] == []


def test_proposal_rejected_chat_suppressed_internal_record_preserved(env, tmp_path):
    """proposal_rejected's chat message is stopped, but the internal
    proposals/rejected/ record (written by review.py, NOT the dispatcher)
    is untouched — the two paths are independent.

    This pins the preservation invariant: suppressing the chat push must
    not touch any internal record the rejection feeds. We assert the
    independence directly — review.py writes the rejected proposal to disk
    and only THEN calls _alert_rejection, so the dispatcher suppressing the
    alert cannot affect the on-disk record.
    """
    d = env["dispatcher"]
    # The chat side: suppressed.
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="review", message="📋 Proposal prop_1 rejected",
        catalog_event="decisions.proposal_rejected",
        dedup_key="review/rejected/prop_1",
    )
    assert out.result == d.DispatchResult.SUPPRESSED_DISABLED
    assert env["sent"] == []

    # The internal record side: a caller can still write + read its own
    # rejected-proposal record independent of the (now-suppressed) alert.
    rejected_dir = tmp_path / "proposals" / "rejected"
    rejected_dir.mkdir(parents=True)
    rec = rejected_dir / "prop_1.json"
    rec.write_text('{"id": "prop_1", "status": "rejected"}', encoding="utf-8")
    assert rec.exists(), "rejected-proposal record must survive chat suppression"
