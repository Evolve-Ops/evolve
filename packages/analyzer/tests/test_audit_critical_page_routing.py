"""audit's CRITICAL page-on-transition batch must actually page.

Regression cover for the routing mismatch found reviewing the delivery-gate
work on the page-on-transition path: ``_send_via_dispatcher`` annotated the
CRITICAL batch with ``security.audit_finding``, whose catalog default is
``DAILY_DIGEST`` (workstream D1 — the umbrella carries the flap-prone
WARN+ERROR traffic). On a default-configured pod the dispatcher therefore
returned ``DEFERRED`` for every newly-firing CRITICAL and the message went to
the daily digest queue: "page on transition" did not page, and the operator
saw a CRITICAL security finding up to ~24h later.

Delivery cadence is a property of the catalog EVENT, not of the individual
message, so the fix is a second event (``security.audit_critical``,
CRITICAL / IMMEDIATE-only) rather than a flag on the umbrella.

These run against the REAL dispatcher — only the wire send is stubbed — so
they exercise the actual catalog → subscription → frequency resolution rather
than a mock of it. The differential pair is the point: the same message under
the umbrella key still defers, which is what makes the first test meaningful.

Design: internal/design-security-alert-fatigue-2026-08-31.md (R-1, R-3).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import audit  # noqa: E402


CRITICAL_BATCH = (
    "🔴 <b>Evolve Security Audit — CRITICAL Findings</b>\n"
    "\n"
    "• team_bot_a openclaw.json is world-readable (mode 644)"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Real dispatcher, real catalog, stubbed wire.

    No subscriptions.json is written — that is the whole point: this is a
    default-configured pod, resolving straight to catalog defaults.
    """
    from evolve_admin.alerts import dispatcher

    wire: list[tuple[str, str]] = []

    def fake_wire(chat_id, message):
        wire.append((chat_id, message))
        return True, None

    monkeypatch.setattr(dispatcher, "_dispatch_via_telegram_http", fake_wire)

    shared = tmp_path / "evolve"
    shared.mkdir()
    return {
        "dispatcher": dispatcher,
        "wire": wire,
        "shared_dir": shared,
        "config": {
            "alerts": {"channel": "telegram", "chatId": "12345"},
            "sharedDir": str(shared),
        },
    }


def test_critical_batch_reaches_the_operator_on_a_default_pod(env):
    """The batch pushes immediately — it does not land in a digest queue."""
    assert audit._send_via_dispatcher(
        CRITICAL_BATCH, env["shared_dir"], env["config"],
    ) == audit._DISPATCH_DELIVERED

    # NB: the return-value assertion above is NOT the guard for this test.
    # #3930 maps DEFERRED onto _DISPATCH_DELIVERED (the digest flusher owns it
    # from there), so it still passes if the key is reverted to the umbrella.
    # The wire check below is what goes red — keep both.
    assert env["wire"], (
        "the CRITICAL batch never reached the channel on a default pod — "
        "it was routed to a digest queue instead of paging"
    )
    assert "CRITICAL Findings" in env["wire"][0][1]

    # And nothing was queued for a later digest flush.
    assert not (env["shared_dir"] / "alerts" / "digest-pending").exists()


def test_umbrella_key_would_still_have_deferred_the_same_message(env):
    """Differential proof that the key is what changed the outcome.

    Same dispatcher, same pod, same message — only the catalog_event differs.
    Under ``security.audit_finding`` this defers; the test above shows it
    sends under ``security.audit_critical``. If a future change made the
    umbrella immediate, this test fails and the split above stops being
    load-bearing — resolve that deliberately, don't delete this.
    """
    disp = env["dispatcher"]
    outcome = disp.send(
        shared_dir=env["shared_dir"],
        network=env["config"],
        source="audit",
        message=CRITICAL_BATCH,
        severity=disp.Severity.CRITICAL,
        dedup_key="audit/batch/umbrella-differential",
        catalog_event="security.audit_finding",
    )
    assert outcome.result is disp.DispatchResult.DEFERRED
    assert not env["wire"], (
        "the umbrella key is DAILY_DIGEST by design — it must not push"
    )


def test_producer_annotates_the_immediate_key(env, monkeypatch):
    """Pin the annotation itself, so a revert is a red test and not a
    silently-quiet security channel."""
    disp = env["dispatcher"]
    seen: list[str | None] = []
    real_send = disp.send

    def spy(*a, **kw):
        seen.append(kw.get("catalog_event"))
        return real_send(*a, **kw)

    # monkeypatch (not a hand-rolled try/finally) so teardown is guaranteed
    # even if the assertion below raises — matches the fixture's style. The
    # patch lands because audit imports `send` INSIDE _send_via_dispatcher,
    # so the lookup is a per-call getattr on the module.
    monkeypatch.setattr(disp, "send", spy)
    audit._send_via_dispatcher(
        CRITICAL_BATCH, env["shared_dir"], env["config"],
    )

    assert seen == ["security.audit_critical"]


def test_critical_key_is_immediate_only_so_no_operator_can_digest_it(env):
    """The operator cannot route a CRITICAL audit page into a digest — the
    option does not exist on this event. The supported way to stop these is
    the safety-critical security_findings group toggle, which warns first.

    (Not the only way, and this test does not claim otherwise: a hand-edited
    `frequency: "off"` in the state file silences any event with no warning,
    because read_subscription maps OFF to enabled=False BEFORE the
    allowed_frequencies clamp. Pre-existing and generic — security.cve_finding
    has the same hole — and write_subscription refuses it, so it is
    hand-edit-only. Named here so the sentence above is not read as a
    guarantee.)

    Consequence for the delivery gate in ``dispatch_findings``: DEFERRED is
    unreachable for this send. The audit source passes no ``digest_meta``, so
    the recurring-flapper demotion (the other DEFERRED producer) never
    engages either. The remaining deferral is BATCHED_RATE_CAP from the
    global rate breaker, which — like DEFERRED — enqueues to the daily digest
    rather than dropping, so counting it as delivered stays correct.
    """
    import json

    from evolve_admin.alerts import catalog as cat
    from evolve_admin.alerts import subscriptions as subs

    entry = cat.by_key("security.audit_critical")
    assert entry is not None
    assert entry.allowed_frequencies == (cat.Frequency.IMMEDIATE,)

    # The write path refuses the combo outright.
    with pytest.raises(ValueError):
        subs.write_subscription(
            env["shared_dir"], "security.audit_critical",
            frequency=cat.Frequency.DAILY_DIGEST,
        )

    # And a digest override that reached the state file some other way
    # (hand-edit, a stale file from before this event was immediate-only)
    # resolves back to the catalog default on read rather than silently
    # digesting a CRITICAL page.
    state_path = env["shared_dir"] / "alerts" / "subscriptions.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "version": 1,
        "subscriptions": {
            "security.audit_critical": {"frequency": "daily_digest"},
        },
    }), encoding="utf-8")

    effective = subs.read_subscription(
        env["shared_dir"], "security.audit_critical",
    )
    assert effective is not None
    assert effective.frequency is cat.Frequency.IMMEDIATE


def test_critical_page_survives_a_saturated_rate_cap(env):
    """The page still reaches the wire when the normal rate cap is spent.

    What this pins: `dispatcher._is_bypass_alert` keeps routing this send onto
    the higher `alerts.rate_cap.critical_max_per_window` ceiling (30/hr) rather
    than the normal `max_per_window` (10/hr). A future change that re-keyed
    bypass onto `MUST_PAGE_ALLOWLIST` — which this file's earlier comments
    actively invited — would start batching the CRITICAL security page into
    the daily digest during any busy hour. That mutation turns this red.

    What it does NOT pin, despite an earlier version of this docstring saying
    so: the entry's own `severity`/`is_safety_critical`. Those are two of three
    redundant bypass routes, and the caller's `severity=CRITICAL` alone carries
    this test — stripping the entry-side fields leaves it green. The sibling
    `test_bypass_is_earned_by_the_entry_not_only_by_the_caller` is what covers
    them, which is why both exist.
    """
    disp = env["dispatcher"]

    # Saturate the normal (non-bypass) window with ordinary INFO traffic.
    for i in range(15):
        disp.send(
            shared_dir=env["shared_dir"],
            network=env["config"],
            source="pod_report",
            message=f"routine notice {i}",
            severity=disp.Severity.INFO,
            dedup_key=f"noise/{i}",
            catalog_event="summaries.daily_pod_report",
        )

    from evolve_admin.alerts import rate_breaker

    health = rate_breaker.breaker_health(env["shared_dir"])
    assert health["max_per_window"] == 10
    assert health["critical_max_per_window"] == 30
    # The ordinary traffic really did spend the normal window.
    assert health["channels"]["telegram"]["state"] in ("batching", "storm")

    before = len(env["wire"])
    assert audit._send_via_dispatcher(
        CRITICAL_BATCH, env["shared_dir"], env["config"],
    ) == audit._DISPATCH_DELIVERED
    assert len(env["wire"]) == before + 1, (
        "the CRITICAL audit page must ride the bypass ceiling, not the "
        "normal rate cap — it was batched away by ordinary traffic"
    )

    # Control: an ordinary alert at this same point is capped. Asserted rather
    # than inferred from the episode state — "an episode is open" is weaker
    # than "the normal window is spent" (_evaluate_locked still trickles a
    # non-bypass send through while delivered < max_per_window), so without
    # this the test would be sound only by arithmetic it never shows.
    control = disp.send(
        shared_dir=env["shared_dir"],
        network=env["config"],
        source="pod_report",
        message="routine notice, post-page control",
        severity=disp.Severity.INFO,
        dedup_key="noise/control",
        catalog_event="summaries.daily_pod_report",
    )
    assert control.result is disp.DispatchResult.BATCHED_RATE_CAP, (
        "the normal cap must genuinely be spent, or this test proves nothing "
        f"about the bypass; got {control.result}"
    )
    assert len(env["wire"]) == before + 1, "the control must not reach the wire"


def test_bypass_is_earned_by_the_entry_not_only_by_the_caller(env):
    """`_is_bypass_alert` grants the bypass on the catalog ENTRY alone.

    Three independent routes qualify this event: the producer passes
    `severity=CRITICAL`, the entry is `is_safety_critical`, and the entry's
    own severity is CRITICAL. Assert the entry-only routes hold, so a future
    caller that forgets to mirror the severity still pages.
    """
    disp = env["dispatcher"]

    assert disp._is_bypass_alert(
        disp.Severity.CRITICAL, "security.audit_critical",
    ) is True
    # Entry alone suffices — caller severity deliberately downgraded here.
    assert disp._is_bypass_alert(
        disp.Severity.WARNING, "security.audit_critical",
    ) is True
    # Contrast: the digest umbrella earns no bypass from its entry.
    assert disp._is_bypass_alert(
        disp.Severity.WARNING, "security.audit_finding",
    ) is False
