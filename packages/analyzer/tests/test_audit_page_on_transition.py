"""tests/test_audit_page_on_transition.py — page-on-transition critical delivery.

R-1+R-5 of internal/design-security-alert-fatigue-2026-08-31.md (the
deferred "Phase 5b" of internal/spec-alerts-signal-store-2026-05-07.md).

The delivery contract under test:

  - A critical finding pages when its mirrored Signal newly ENTERS the
    firing state: created, resolve → reopen, or snooze-expiry wake.
  - A standing finding (still true, still firing) never re-pages — it
    lives on the Alerts page, not in the operator's pocket.
  - The per-run page lists ONLY the newly-firing findings, never the
    whole standing set (the old batch-hash layer re-broadcast all ~25
    bullets whenever any one member changed).
  - Snoozed/dismissed signals suppress the push (R-5) and record the
    suppression on the Signal's delivery audit trail.
  - The dispatcher-first + direct-Telegram fallback shape survives for
    the messages that DO page (admin-package-wedged resilience).
  - A page that FAILS end-to-end (dispatcher FAILED and the fallback
    also failed) does not consume the firing episode: it records a
    marked delivery the predicate excludes, so the next run re-pages.
    Retiring the batch-hash layer removed the 7-day resend that used to
    be the backstop, so this gate must fail toward paging — an unknown
    send outcome counts as failed.

Timestamps in the signal store have seconds precision and these tests
run sub-second, so runs that must be "later" backdate the stored
timestamps instead of sleeping (see _backdate_signals) — no wall-clock
coupling.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
for _dir in (_ANALYZER_DIR, _ANALYZER_DIR.parent / "admin"):
    # The admin dir too: the delivery-mapping tests drive the real
    # `_send_via_dispatcher`, which needs `evolve_admin.alerts.dispatcher`.
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import audit  # noqa: E402
from audit import Finding, dispatch_findings  # noqa: E402
from signals import store as signals_store  # noqa: E402


def _crit(msg: str, bot: str = "admin_bot") -> Finding:
    # finding_kind: R-3 requires an explicit classification on criticals;
    # "event" keeps these synthetic findings on the page-now path.
    return Finding(level="critical", finding_kind="event", category="identity",
                   bot_id=bot, message=msg, detail="")


@pytest.fixture
def pages(monkeypatch):
    """Capture would-be Telegram pages; keeps the suite hermetic.

    The stub returns True — ``_send_security_alert``'s return value is the
    delivery outcome ``dispatch_findings`` records on the Signal, so a stub
    that reported nothing would model a FAILED send.
    """
    sent: list[str] = []

    def _send(msg, *a, **k):
        sent.append(msg)
        return True

    monkeypatch.setattr(audit, "_send_security_alert", _send)
    monkeypatch.setattr(audit, "_send_telegram_direct", lambda *a, **k: True)
    return sent


@pytest.fixture
def failing_pages(monkeypatch):
    """Like ``pages``, but every send fails end-to-end (dispatcher FAILED
    and the direct-Telegram fallback also failed)."""
    attempted: list[str] = []

    def _send(msg, *a, **k):
        attempted.append(msg)
        return False

    monkeypatch.setattr(audit, "_send_security_alert", _send)
    return attempted


def _only_signal(shared: Path):
    sigs = list(signals_store.iter_active(shared, producer="audit"))
    assert len(sigs) == 1
    return sigs[0]


def _backdate_signals(shared: Path, seconds: int) -> None:
    """Shift every stored signal timestamp back by ``seconds``.

    The store stamps at seconds precision; the paging predicate compares
    "entered firing" vs "last paged". Backdating simulates the 15-minute
    gap between real audit runs without sleeping or patching the clock.
    """
    root = shared / "signals"
    for sub in ("firing", "snoozed", "archived"):
        d = root / sub
        if not d.exists():
            continue
        for p in d.glob("*.json"):
            data = json.loads(p.read_text())
            for key in ("state_history", "deliveries"):
                for row in data.get(key) or []:
                    if row.get("at"):
                        dt = datetime.fromisoformat(row["at"])
                        row["at"] = (dt - timedelta(seconds=seconds)).isoformat(
                            timespec="seconds")
            if data.get("resolved_at"):
                dt = datetime.fromisoformat(data["resolved_at"])
                data["resolved_at"] = (dt - timedelta(seconds=seconds)).isoformat(
                    timespec="seconds")
            p.write_text(json.dumps(data))


def _snooze(shared: Path, sig, *, until: datetime) -> None:
    signals_store.apply_transition(
        sig, "snoozed", shared, actor="operator", reason="test snooze",
        snoozed_until=until.isoformat(timespec="seconds"),
    )


# ── R-1: page on create; list only the new finding ───────────────────────────


def test_new_finding_pages_and_second_run_is_silent(tmp_path, pages):
    dispatch_findings([_crit("ssh key wrong perms")], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1
    assert "ssh key wrong perms" in pages[0]

    # Same finding still true on the next run → silent.
    dispatch_findings([_crit("ssh key wrong perms")], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1
    # One Delivery from the original page; no suppression bookkeeping.
    assert len(_only_signal(tmp_path).deliveries) == 1


def test_page_lists_only_newly_firing_not_standing_set(tmp_path, pages):
    standing = _crit("standing sudoers drift")
    dispatch_findings([standing], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1

    new = _crit("world-readable openclaw.json", bot="team_bot_a")
    dispatch_findings([standing, new], tmp_path, config={}, dry_run=False)
    assert len(pages) == 2
    assert "world-readable openclaw.json" in pages[1]
    assert "standing sudoers drift" not in pages[1]


def test_healed_then_refired_pages_again_via_reopen(tmp_path, pages):
    f = _crit("ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1
    _backdate_signals(tmp_path, 120)

    # Finding heals → sweep-resolve archives the Signal, no page.
    dispatch_findings([], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1
    assert list(signals_store.iter_active(tmp_path, producer="audit")) == []

    # Re-fires within the reopen window → resolved → firing IS a transition.
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(pages) == 2
    sig = _only_signal(tmp_path)
    assert sig.state == "firing"
    assert len(sig.deliveries) == 2


# ── R-5: snoozed/dismissed suppress the push, with an audit trail ────────────


def test_snoozed_signal_suppresses_push_and_records_reason(tmp_path, pages):
    f = _crit("ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1
    _snooze(tmp_path, _only_signal(tmp_path),
            until=datetime.now(timezone.utc) + timedelta(hours=24))

    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1  # no push
    sig = _only_signal(tmp_path)
    assert sig.state == "snoozed"
    assert sig.deliveries[-1].suppressed_reason == "signal snoozed by operator"

    # Repeated runs collapse into the one suppression entry (no growth
    # at 4 runs/hour for a week-long snooze).
    n = len(sig.deliveries)
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(_only_signal(tmp_path).deliveries) == n


def test_dismissed_signal_suppresses_push_and_records_reason(tmp_path, pages):
    f = _crit("ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    sig = _only_signal(tmp_path)
    signals_store.apply_transition(
        sig, "dismissed", tmp_path, actor="operator", reason="known issue")

    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1  # only the original page
    located = signals_store.find_signal(tmp_path, sig.id)
    assert located is not None
    archived, _path, subdir = located
    assert subdir == "archived"
    assert archived.deliveries[-1].suppressed_reason == "signal dismissed by operator"


def test_snooze_expiry_wake_pages_again(tmp_path, pages):
    f = _crit("ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1
    _backdate_signals(tmp_path, 120)

    _snooze(tmp_path, _only_signal(tmp_path),
            until=datetime.now(timezone.utc) - timedelta(seconds=1))
    waked = signals_store.wake_due_snoozes(tmp_path)
    assert len(waked) == 1

    # snoozed → firing (timer) is a transition INTO firing → pages again.
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(pages) == 2


# ── Delivery resilience + retired batch-hash layer ───────────────────────────


def test_dispatcher_broken_falls_back_to_direct_telegram(tmp_path, monkeypatch):
    """The dispatcher-first + direct-Telegram fallback survives for
    transition pages (admin-package-wedged resilience)."""
    monkeypatch.setattr(audit, "_send_via_dispatcher",
                        lambda *a, **k: audit._DISPATCH_BROKEN)
    direct: list[tuple] = []

    def _direct(token, chat, msg, shared_dir):
        direct.append((token, chat, msg))
        return True

    monkeypatch.setattr(audit, "_send_telegram_direct", _direct)
    ks = tmp_path / "keystore"
    ks.mkdir()
    (ks / "security-alert-token").write_text("tok\n")
    (ks / "security-alert-chat-id").write_text("42\n")

    dispatch_findings([_crit("ssh key wrong perms")], tmp_path, config={}, dry_run=False)
    assert len(direct) == 1
    assert direct[0][:2] == ("tok", "42")
    assert "ssh key wrong perms" in direct[0][2]
    # The fallback delivered, so the episode is marked paged.
    assert _only_signal(tmp_path).deliveries[-1].suppressed_reason is None


def test_both_channels_failing_leaves_the_episode_unpaged(tmp_path, monkeypatch):
    """Dispatcher FAILED *and* the direct-Telegram fallback failed — the
    exact hole this test file's failure-marker contract closes."""
    monkeypatch.setattr(audit, "_send_via_dispatcher",
                        lambda *a, **k: audit._DISPATCH_BROKEN)
    monkeypatch.setattr(audit, "_send_telegram_direct", lambda *a, **k: False)
    ks = tmp_path / "keystore"
    ks.mkdir()
    (ks / "security-alert-token").write_text("tok\n")
    (ks / "security-alert-chat-id").write_text("42\n")

    f = _crit("ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    sig = _only_signal(tmp_path)
    assert sig.deliveries[-1].suppressed_reason == audit._PAGE_UNDELIVERED_REASON
    assert audit._signal_newly_firing(sig) is True


# ── A failed send must not consume the firing episode ────────────────────────


def test_failed_send_repages_on_the_next_run(tmp_path, failing_pages):
    """The #3919 follow-up: the 7-day batch-hash resend that used to be the
    backstop is gone, so a send that fails end-to-end must leave the episode
    un-paged rather than silencing the critical until it resolves."""
    f = _crit("ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(failing_pages) == 1

    sig = _only_signal(tmp_path)
    assert sig.state == "firing"
    # The attempt is on the audit trail, but marked — not a successful page.
    assert len(sig.deliveries) == 1
    assert sig.deliveries[-1].suppressed_reason == audit._PAGE_UNDELIVERED_REASON

    # Same standing finding, next run: still un-paged → tries again.
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(failing_pages) == 2
    assert "ssh key wrong perms" in failing_pages[1]


def test_successful_send_marks_the_episode_and_next_run_is_silent(tmp_path, pages):
    """The counterpart: a confirmed send records an unmarked delivery, and
    the standing finding stays silent from then on."""
    f = _crit("ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1
    assert _only_signal(tmp_path).deliveries[-1].suppressed_reason is None

    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1
    assert len(_only_signal(tmp_path).deliveries) == 1


def test_repeated_failures_collapse_to_one_marker_entry(tmp_path, failing_pages):
    """A channel that stays down re-pages every run (correct) but must not
    grow the delivery trail by a row every 15 minutes."""
    f = _crit("ssh key wrong perms")
    for _ in range(4):
        dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(failing_pages) == 4
    sig = _only_signal(tmp_path)
    assert len(sig.deliveries) == 1
    assert sig.deliveries[-1].suppressed_reason == audit._PAGE_UNDELIVERED_REASON


def test_send_recovers_then_the_episode_goes_quiet(tmp_path, monkeypatch):
    """Fail, then succeed: the successful page ends the episode's paging —
    the retry loop is bounded by delivery, not by run count."""
    sent: list[str] = []
    ok = {"value": False}

    def _send(msg, *a, **k):
        sent.append(msg)
        return ok["value"]

    monkeypatch.setattr(audit, "_send_security_alert", _send)
    f = _crit("ssh key wrong perms")

    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(sent) == 1
    ok["value"] = True
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(sent) == 2
    assert _only_signal(tmp_path).deliveries[-1].suppressed_reason is None

    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(sent) == 2  # delivered → standing → silent


def test_unknown_send_outcome_fails_toward_paging(tmp_path, monkeypatch):
    """A send whose outcome isn't an explicit success (a helper that returns
    nothing, a future refactor that forgets the result) must re-page rather
    than silently consume the episode. A delivery gate fails toward paging."""
    sent: list[str] = []
    monkeypatch.setattr(
        audit, "_send_security_alert",
        lambda msg, *a, **k: sent.append(msg),  # returns None
    )
    f = _crit("ssh key wrong perms")

    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert _only_signal(tmp_path).deliveries[-1].suppressed_reason == (
        audit._PAGE_UNDELIVERED_REASON)
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(sent) == 2


def _script_dispatcher(monkeypatch, results):
    """Drive the REAL ``_send_via_dispatcher`` with scripted outcomes.

    Faking ``alerts.dispatcher.send`` rather than ``_send_via_dispatcher``
    keeps audit's own DispatchResult → delivered/suppressed/broken mapping
    under test. That mapping is where a *suppression* can masquerade as a
    delivered page, and stubbing one layer higher hides it. The last scripted
    result repeats for any further calls.
    """
    admin_dir = _ANALYZER_DIR.parent / "admin"
    if str(admin_dir) not in sys.path:
        sys.path.insert(0, str(admin_dir))
    from evolve_admin.alerts import dispatcher as _d

    calls: list[str] = []
    seq = list(results)

    def _send(*, shared_dir, network, source, message, severity,
              dedup_key=None, catalog_event=None, **_kw):
        calls.append(message)
        result = seq[min(len(calls), len(seq)) - 1]
        return _d.DispatchOutcome(
            result=result, source=source, severity=severity,
            dedup_key=dedup_key, channel="telegram", chat_id="1",
        )

    monkeypatch.setattr(_d, "send", _send)
    return calls


def test_end_to_end_send_failure_through_the_real_alert_path(tmp_path, monkeypatch):
    """Only ``dispatcher.send`` itself is mocked: it FAILS and no
    security-alert keystore is configured, so nothing reaches the operator
    and the episode must stay eligible to page."""
    from evolve_admin.alerts.dispatcher import DispatchResult as R
    calls = _script_dispatcher(monkeypatch, [R.FAILED, R.SENT])
    assert not (tmp_path / "keystore").exists()  # no fallback credentials
    f = _crit("ssh key wrong perms")

    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(calls) == 1
    assert _only_signal(tmp_path).deliveries[-1].suppressed_reason == (
        audit._PAGE_UNDELIVERED_REASON)

    # Dispatcher comes back → the retry lands and the episode is marked paged.
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(calls) == 2
    sig = _only_signal(tmp_path)
    assert sig.deliveries[-1].suppressed_reason is None
    assert audit._signal_newly_firing(sig) is False


def test_identical_content_suppression_must_not_end_the_retry_loop(tmp_path, monkeypatch):
    """The retry introduced by this contract must not cancel itself.

    The dispatcher records ``(dedup_key, body_hash, ts)`` on the FAILURE path
    too, on purpose, and ``audit`` is a STATE_PERSISTS source that does not
    opt out of the 24h identical-content floor. So the run-2 retry of a page
    that failed at run 1 comes back SUPPRESSED_IDENTICAL with byte-identical
    text. If that counted as delivered, this whole mechanism would buy the
    operator exactly one extra run of visibility before going silent for the
    rest of the firing episode.
    """
    from evolve_admin.alerts.dispatcher import DispatchResult as R
    calls = _script_dispatcher(
        monkeypatch, [R.FAILED, R.SUPPRESSED_IDENTICAL, R.SUPPRESSED_IDENTICAL])
    f = _crit("ssh key wrong perms")

    for expected_calls in (1, 2, 3):
        dispatch_findings([f], tmp_path, config={}, dry_run=False)
        assert len(calls) == expected_calls
        sig = _only_signal(tmp_path)
        assert audit._signal_newly_firing(sig) is True, (
            f"run {expected_calls}: episode was consumed by a suppression")
        # The trail stays at the single collapsed marker; no unmarked entry.
        assert all(d.suppressed_reason for d in sig.deliveries)


def test_dispatcher_suppression_does_not_reach_the_keystore_fallback(tmp_path, monkeypatch):
    """A withholding dispatcher is healthy, not broken. Routing around it via
    the dedicated keystore credentials would be the subscription bypass
    ``_send_security_alert`` disclaims — so suppression returns not-delivered
    WITHOUT a direct-Telegram attempt."""
    from evolve_admin.alerts.dispatcher import DispatchResult as R
    _script_dispatcher(monkeypatch, [R.SUPPRESSED_COOLDOWN])
    direct: list = []
    monkeypatch.setattr(
        audit, "_send_telegram_direct",
        lambda *a, **k: (direct.append(a), True)[1],
    )
    ks = tmp_path / "keystore"
    ks.mkdir()
    (ks / "security-alert-token").write_text("tok\n")
    (ks / "security-alert-chat-id").write_text("42\n")

    assert audit._send_security_alert("msg", tmp_path, config={}) is False
    assert direct == [], "suppression must not escalate to the direct channel"


_DISPATCH_RESULT_EXPECTATIONS = [
    ("SENT", True),
    ("DEFERRED", True),          # queued to a digest the flusher owns
    ("BATCHED_RATE_CAP", True),  # rolled into the digest, not dropped
    ("SUPPRESSED_DISABLED", False),
    ("SUPPRESSED_COOLDOWN", False),
    ("SUPPRESSED_IDENTICAL", False),
    ("NO_RECIPIENT", False),
    ("FAILED", False),
]


@pytest.mark.parametrize("result_name,delivered", _DISPATCH_RESULT_EXPECTATIONS)
def test_dispatch_result_to_delivery_mapping(tmp_path, monkeypatch, result_name, delivered):
    """Pin the whole DispatchResult surface, both directions."""
    from evolve_admin.alerts.dispatcher import DispatchResult as R
    _script_dispatcher(monkeypatch, [getattr(R, result_name)])
    monkeypatch.setattr(audit, "_send_telegram_direct", lambda *a, **k: False)
    assert audit._send_security_alert("msg", tmp_path, config={}) is delivered


def test_every_dispatch_result_has_a_declared_delivery_meaning():
    """A DispatchResult member added upstream must fail HERE.

    Without this, a new member would land in the BROKEN bucket by default and
    silently start attempting the direct-Telegram fallback every run — and if
    the new member were a suppression, that is the subscription bypass
    ``_send_security_alert`` disclaims. Making the omission loud means the
    default never has to be right. Same idiom as the dispatcher's own
    ``test_every_source_has_a_declared_category``.
    """
    from evolve_admin.alerts.dispatcher import DispatchResult
    pinned = {name for name, _ in _DISPATCH_RESULT_EXPECTATIONS}
    assert {m.name for m in DispatchResult} == pinned, (
        "DispatchResult changed upstream — decide whether the new member means "
        "delivered / suppressed / broken in audit._send_via_dispatcher, then "
        "add it to _DISPATCH_RESULT_EXPECTATIONS"
    )


def test_failed_send_does_not_disturb_suppressed_delivery_exclusion(tmp_path, monkeypatch):
    """The operator-suppression path (R-5) is unchanged by the failure
    marker: a snoozed signal records its own reason, sends nothing, and
    the failure marker never appears on it."""
    sent: list[str] = []
    monkeypatch.setattr(
        audit, "_send_security_alert",
        lambda msg, *a, **k: (sent.append(msg), False)[1],  # every send fails
    )
    f = _crit("ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    assert len(sent) == 1

    _snooze(tmp_path, _only_signal(tmp_path),
            until=datetime.now(timezone.utc) + timedelta(hours=24))
    dispatch_findings([f], tmp_path, config={}, dry_run=False)

    assert len(sent) == 1  # snoozed → no send attempt at all
    sig = _only_signal(tmp_path)
    assert sig.state == "snoozed"
    assert sig.deliveries[-1].suppressed_reason == "signal snoozed by operator"


def test_send_telegram_direct_reports_the_post_outcome(monkeypatch, tmp_path):
    """The fallback's own bool contract, at the urllib layer.

    Every other test stubs this function, so without this the hunk that makes
    the direct channel "confirm the POST" is asserted by nothing — and it is
    the last thing standing between a wedged dispatcher and a silent CRITICAL.
    """
    import urllib.request

    posted: list = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: posted.append(a) or object())
    assert audit._send_telegram_direct("tok", "42", "msg", tmp_path) is True
    assert len(posted) == 1

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert audit._send_telegram_direct("tok", "42", "msg", tmp_path) is False


def test_dry_run_pages_nothing_and_writes_nothing(tmp_path, pages):
    dispatch_findings([_crit("ssh key wrong perms")], tmp_path, config={}, dry_run=True)
    assert pages == []
    assert list(signals_store.iter_active(tmp_path, producer="audit")) == []


def test_batch_hash_dedup_layer_is_retired(tmp_path, pages):
    """Transition-gating replaced the batch-hash dedup: no dedup file on
    disk, no vestigial helpers on the module."""
    dispatch_findings([_crit("ssh key wrong perms")], tmp_path, config={}, dry_run=False)
    assert not (tmp_path / "alerts" / "audit-critical-dedup.json").exists()
    for retired in ("_should_send_critical", "_critical_fingerprint",
                    "_record_critical_sent"):
        assert not hasattr(audit, retired)
