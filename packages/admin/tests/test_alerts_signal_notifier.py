"""tests/test_alerts_signal_notifier.py — Phase 4.

The signal_notifier source watches the Signal store and pushes firing
and resolved transitions through the alert dispatcher. Tests pin:

  - new firing Signal pushes once, but only after the debounce window
    expires (Security_bot-style flap suppression — brief flaps stay silent)
  - same Signal id is not re-pushed on subsequent ticks
  - firing → resolved transition for a previously-pushed Signal pushes
    a recovery message and clears the state slot
  - resolved Signal that was never announced does NOT push a recovery
    (orphan suppression — gated on alerted_for_signal_id)
  - deny-list-by-default: a brand-NEW producer (not named anywhere) reaches
    operator chat with zero config edits
  - deny-list guard: a direct-dispatch producer (cost_watchdog) is NOT routed
    here so the operator doesn't get double-messaged
  - state file persists across runs (cooldown / dedup actually works)
  - master switch off → no pushes
  - failed dispatch leaves state untouched so the next tick can retry
  - signature index keeps clean: signatures with no alerted_for entry
    are pruned on save

Tests drive ``run_once`` directly with a controlled clock and a
monkey-patched ``_dispatcher.send`` so no subprocess fires.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """tmp shared_dir + a fake dispatcher.send + helpers to seed Signals.

    The fixture pre-writes an empty notifier-state.json so tests exercise
    the normal Phase A (fire) / Phase B (recovery) paths. Cold-start
    behavior is covered in its own dedicated tests (see end of file)
    — those construct an env without the state file.
    """
    from evolve_admin.alerts import signal_notifier as sn
    from evolve_admin.alerts import dispatcher as disp

    shared = tmp_path / "evolve"
    (shared / "signals" / "firing").mkdir(parents=True)
    (shared / "signals" / "snoozed").mkdir(parents=True)
    (shared / "signals" / "archived").mkdir(parents=True)
    # Skip cold-start sync by pre-writing an empty state file — tests
    # below assume the notifier has been running for a while already.
    (shared / "signals" / "notifier-state.json").write_text(
        '{"version": 1, "signatures": {}}'
    )

    sent: list[dict] = []   # one entry per dispatcher.send call
    next_result = {"value": "SENT", "is_permanent_failure": False}

    def _fake_send(*, shared_dir, network, source, message, severity,
                   dedup_key=None, cooldown_seconds=None,
                   recipient_override=None, now=None,
                   catalog_event=None, digest_meta=None):
        sent.append({
            "source": source, "message": message,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event, "digest_meta": digest_meta,
        })
        from evolve_admin.alerts.dispatcher import (
            DispatchOutcome, DispatchResult,
        )
        result_map = {
            "SENT": DispatchResult.SENT,
            "DEFERRED": DispatchResult.DEFERRED,
            "BATCHED_RATE_CAP": DispatchResult.BATCHED_RATE_CAP,
            "SUPPRESSED_DISABLED": DispatchResult.SUPPRESSED_DISABLED,
            "SUPPRESSED_COOLDOWN": DispatchResult.SUPPRESSED_COOLDOWN,
            "FAILED": DispatchResult.FAILED,
            "NO_RECIPIENT": DispatchResult.NO_RECIPIENT,
        }
        return DispatchOutcome(
            result=result_map[next_result["value"]],
            source=source, severity=severity, dedup_key=dedup_key,
            channel="telegram", chat_id="12345",
            is_permanent_failure=next_result.get("is_permanent_failure", False),
            is_delivery_target_down=next_result.get(
                "is_delivery_target_down", False),
        )

    monkeypatch.setattr(disp, "send", _fake_send)

    network = {"alerts": {"channel": "telegram", "chatId": "12345"}}

    return {
        "shared": shared, "network": network,
        "sn": sn, "disp": disp,
        "sent": sent, "next_result": next_result,
    }


def _seed_signal(shared, **overrides):
    """Write a Signal JSON into firing/ via the real signals.store.observe
    so its on-disk shape matches production."""
    import signals.store as signals_store
    base = dict(
        signature="pod_health:pod_health_gateways:team_bot_a:gateway",
        producer="pod_health",
        type="pod_health_gateways",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id="team_bot_a",
        title="team_bot_a gateway down",
        body="HTTP probe failed on port 18789",
    )
    base.update(overrides)
    return signals_store.observe(shared, **base)


def _resolve_signal(shared, sig):
    import signals.store as signals_store
    signals_store.apply_transition(
        sig, "resolved", shared, actor="test", reason="test"
    )


def _state(shared):
    p = shared / "signals" / "notifier-state.json"
    if not p.exists():
        return {"signatures": {}}
    return json.loads(p.read_text())


# ── Per-severity grace (was: debounce) ───────────────────────────────────────


def test_firing_warn_within_grace_does_not_push(env):
    # warn grace defaults to 900s; a warn 60s old is well inside it.
    _seed_signal(env["shared"], severity="warn")
    now = datetime.now(timezone.utc) + timedelta(seconds=60)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 0
    assert stats.debounced == 1
    assert env["sent"] == []
    # State not yet recorded — we never pushed, so no alerted_for slot.
    assert _state(env["shared"])["signatures"] == {}


def test_firing_signal_after_debounce_pushes_once(env):
    # warn grace is 900s; push once the signal has aged past it.
    sig = _seed_signal(env["shared"], severity="warn")
    now = datetime.now(timezone.utc) + timedelta(seconds=1000)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 1
    assert len(env["sent"]) == 1
    pushed = env["sent"][0]
    assert pushed["source"] == "signal_notifier"
    assert "team_bot_a" in pushed["message"]
    assert pushed["dedup_key"] == sig.signature

    # Second tick at the same time — already alerted, no re-push.
    env["sent"].clear()
    stats2 = env["sn"].run_once(env["shared"], env["network"], now=now + timedelta(seconds=30))
    assert stats2.fired == 0
    assert env["sent"] == []


def test_warn_resolves_inside_grace_is_never_pushed(env):
    """L3 spec case (a): a warn that fires and self-resolves inside its
    900s grace must never reach the operator. The transient blip is silent —
    no fire push, no recovery push — and the notifier never recorded an
    alerted_for slot (so Phase B has nothing to announce)."""
    sig = _seed_signal(env["shared"], severity="warn")
    # Tick once at +120s (well inside grace): the fire is held, not pushed.
    t1 = datetime.now(timezone.utc) + timedelta(seconds=120)
    stats1 = env["sn"].run_once(env["shared"], env["network"], now=t1)
    assert stats1.debounced == 1
    assert stats1.fired == 0
    assert env["sent"] == []

    # The condition clears before the grace elapses.
    _resolve_signal(env["shared"], sig)

    # Tick again still inside what would have been the grace: nothing to do.
    t2 = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats2 = env["sn"].run_once(env["shared"], env["network"], now=t2)
    assert stats2.fired == 0
    assert stats2.recovered == 0
    assert env["sent"] == [], "a within-grace warn transient must stay silent"
    # Never announced ⇒ no alerted_for slot was ever written.
    assert _state(env["shared"])["signatures"] == {}


def test_alert_is_pushed_immediately_regardless_of_grace(env):
    """L3 spec case (b) + the invariant: an alert is never delayed. Even one
    second old — far inside any warn/info grace — a critical pages on the
    very next tick (alert grace is clamped to 0)."""
    sig = _seed_signal(env["shared"], severity="alert")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 1
    assert len(env["sent"]) == 1
    assert env["sent"][0]["dedup_key"] == sig.signature
    assert "🔴" in env["sent"][0]["message"]


def test_alert_grace_config_override_cannot_delay_critical(env):
    """The clamp is config-proof: even a fat-fingered
    alerts.grace_seconds_by_severity.alert=3600 cannot hold a critical —
    the alert entry is forced to 0 after the merge."""
    import json
    cfg = env["shared"] / "better-engine-config.json"
    cfg.write_text(json.dumps({
        "pod_defaults": {
            "alerts": {"grace_seconds_by_severity": {"alert": 3600}}
        }
    }))
    _seed_signal(env["shared"], severity="alert")
    now = datetime.now(timezone.utc) + timedelta(seconds=5)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 1
    assert len(env["sent"]) == 1


def test_warn_grace_override_lengthens_window(env):
    """Operator-tunable: bumping warn grace to 1800s holds a warn that the
    default 900s would already release. Pins that the config is read."""
    import json
    cfg = env["shared"] / "better-engine-config.json"
    cfg.write_text(json.dumps({
        "pod_defaults": {
            "alerts": {"grace_seconds_by_severity": {"warn": 1800}}
        }
    }))
    _seed_signal(env["shared"], severity="warn")
    # 1000s is past the 900s default but inside the 1800s override.
    now = datetime.now(timezone.utc) + timedelta(seconds=1000)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.debounced == 1
    assert stats.fired == 0
    assert env["sent"] == []


@pytest.mark.parametrize("handled_result", ["DEFERRED", "BATCHED_RATE_CAP"])
def test_firing_deferred_or_batched_is_marked_and_not_repushed(env, handled_result):
    """Storm-mode regression (fire-side twin of the resolve loop): a FIRE that
    is DEFERRED to the digest or BATCHED by the rate-breaker — not SENT — must
    still be state-marked, or Phase A re-pushes the same '⚠️ …' fire every ~70s
    tick. #3251 fixed the resolve paths; Phase A still listed only
    (SENT, DEFERRED) and missed BATCHED_RATE_CAP, so storm-batched fires
    (evo-vps version-drift + error_spike) looped on the fire side."""
    sig = _seed_signal(env["shared"])
    env["next_result"]["value"] = handled_result
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    # A non-SENT handled fire counts under stats.deferred, not stats.fired.
    assert stats.deferred == 1
    assert len(env["sent"]) == 1                        # announced once
    entry = _state(env["shared"])["signatures"].get(sig.signature)
    assert entry is not None
    assert entry["alerted_for_signal_id"] == sig.id     # marked despite not-SENT

    # Next tick (still storm-batched) — must NOT re-push. This is the loop.
    env["sent"].clear()
    stats2 = env["sn"].run_once(env["shared"], env["network"], now=now + timedelta(seconds=80))
    assert stats2.deferred == 0
    assert env["sent"] == [], "deferred/batched fire must not re-push every tick"


# ── Recovery ────────────────────────────────────────────────────────────────


def test_recovery_pushes_when_signal_resolves_after_announce(env):
    sig = _seed_signal(env["shared"])
    t_fire = datetime.now(timezone.utc) + timedelta(seconds=300)
    env["sn"].run_once(env["shared"], env["network"], now=t_fire)
    assert len(env["sent"]) == 1   # fire push

    # Operator fixes it; signal moves to archived/resolved.
    _resolve_signal(env["shared"], sig)

    env["sent"].clear()
    stats = env["sn"].run_once(env["shared"], env["network"], now=t_fire + timedelta(seconds=10))
    assert stats.recovered == 1
    assert len(env["sent"]) == 1
    msg = env["sent"][0]["message"]
    assert "🟢" in msg
    # Recovery framing is "Cleared on {bot}: …" — not "— resolved" suffix.
    # See test_render_resolve_leads_with_cleared_framing for the rationale.
    assert "Cleared on" in msg
    assert env["sent"][0]["dedup_key"] is None   # recovery skips cooldown
    # State entry retained (not popped) — alerted_for_signal_id cleared,
    # last_resolve_pushed_at set so the flap-window check on any
    # subsequent re-fire works. The retention prune in _save_state ages
    # the entry out after 24h.
    entry = _state(env["shared"])["signatures"].get(sig.signature)
    assert entry is not None
    assert entry["alerted_for_signal_id"] is None
    assert entry["last_resolve_pushed_at"]   # ISO timestamp set


@pytest.mark.parametrize("handled_result", ["DEFERRED", "BATCHED_RATE_CAP"])
def test_recovery_deferred_or_batched_is_marked_and_not_repushed(env, handled_result):
    """Storm-mode regression (the self-sustaining 700/hr loop): a recovery
    that is DEFERRED to the digest or BATCHED by the rate-breaker — not SENT —
    must STILL be state-marked, or Phase B re-pushes the same 'Cleared …'
    every tick. The SENT-only mark gate meant that in storm mode (every send
    deferred/batched) the resolve was never marked, so each ~70s tick
    re-announced the whole resolved backlog, which kept the rate above the
    storm threshold, which kept deferring — a storm that fed itself."""
    sig = _seed_signal(env["shared"])
    t_fire = datetime.now(timezone.utc) + timedelta(seconds=300)
    env["sn"].run_once(env["shared"], env["network"], now=t_fire)   # fire push (SENT)
    assert len(env["sent"]) == 1
    _resolve_signal(env["shared"], sig)

    # Storm mode: the recovery dispatch is DEFERRED/BATCHED, not SENT.
    env["next_result"]["value"] = handled_result
    env["sent"].clear()
    stats = env["sn"].run_once(env["shared"], env["network"], now=t_fire + timedelta(seconds=10))
    assert stats.recovered == 1
    assert len(env["sent"]) == 1                       # announced once
    entry = _state(env["shared"])["signatures"].get(sig.signature)
    assert entry is not None
    assert entry["alerted_for_signal_id"] is None       # marked despite not-SENT
    assert entry["last_resolve_pushed_at"]

    # Next tick (still storm-deferred) — must NOT re-push. This is the loop.
    env["sent"].clear()
    stats2 = env["sn"].run_once(env["shared"], env["network"], now=t_fire + timedelta(seconds=80))
    assert stats2.recovered == 0
    assert env["sent"] == [], "deferred/batched recovery must not re-push every tick"


def test_resolved_signal_never_announced_does_not_push_recovery(env):
    """If a Signal flaps inside the debounce window — fires and resolves
    before the notifier ever announced it — the resolution is silent.
    Security_bot-style flap suppression. The fire was never seen, so nobody
    cares it cleared."""
    sig = _seed_signal(env["shared"])
    # Brief flap: resolve before any debounce clears.
    _resolve_signal(env["shared"], sig)

    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.recovered == 0
    assert stats.fired == 0
    assert env["sent"] == []


# ── Deny-list-by-default ─────────────────────────────────────────────────────


def test_brand_new_producer_reaches_chat_without_any_config(env):
    """Core invariant of the deny-list-by-default model: a producer that
    is not named anywhere (not in the deny-list, not in any config) MUST
    reach operator chat after the debounce window. No allowlist edit needed.

    This is the regression test for the recurring 'silent monitor' failure
    mode: previously, a new monitor that wrote Signals correctly would stay
    silent because signal_notifier required explicit allowlisting. The
    inversion to deny-list-by-default makes new monitors loud automatically.
    """
    _seed_signal(
        env["shared"],
        signature="brand_new_monitor:some_event:admin_bot",
        producer="brand_new_monitor",
        type="some_event",
        bot_id="admin_bot",
        title="brand new monitor finding",
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 1, (
        "brand-new producer must reach chat without allowlist entry"
    )
    assert stats.skipped_direct_dispatch == 0
    assert len(env["sent"]) == 1


def test_direct_dispatch_producer_does_not_get_second_notification(env):
    """cost_watchdog calls dispatcher.send() directly on its own path.
    Routing it through signal_notifier as well would double-message the
    operator on every cost event. The deny-list must exclude it.

    Verify both that no chat push happens AND that the skipped_direct_dispatch
    counter increments so observability tooling can confirm the guard is active.
    """
    _seed_signal(
        env["shared"],
        signature="cost_watchdog:spend_exceeded:admin_bot",
        producer="cost_watchdog",
        type="spend_exceeded",
        bot_id="admin_bot",
        title="daily spend cap exceeded",
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 0, "cost_watchdog must NOT be routed through signal_notifier"
    assert stats.skipped_direct_dispatch == 1
    assert env["sent"] == []


def test_spend_alert_producer_also_excluded(env):
    """spend_alert is the second direct-dispatch producer. Same guarantee."""
    _seed_signal(
        env["shared"],
        signature="spend_alert:weekly_budget:admin_bot",
        producer="spend_alert",
        type="weekly_budget_exceeded",
        bot_id="admin_bot",
        title="weekly budget alert",
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 0
    assert stats.skipped_direct_dispatch == 1
    assert env["sent"] == []


def test_pod_report_gateway_down_pushes_after_debounce(env):
    """Regression for the 2026-06-03 OC-upgrade outage: pod_report emits
    gateway_down Signals when bot gateways stop responding, and those
    must reach the operator's chat. Before this fix, pod_report was off
    the allowlist and a pod-wide outage produced zero notifier messages
    even though the Alerts UI lit up correctly.
    """
    _seed_signal(
        env["shared"],
        signature="pod_report:gateway_down:team_bot_a",
        producer="pod_report",
        type="gateway_down",
        bot_id="team_bot_a",
        title="team_bot_a gateway unreachable",
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 1
    assert len(env["sent"]) == 1
    # Subscription gating routes through the gateway_state_change catalog
    # event so the operator can mute it independently of other system
    # alerts.
    assert env["sent"][0]["catalog_event"] == "system.gateway_state_change"


# ── Dispatcher integration ──────────────────────────────────────────────────


def test_master_switch_off_blocks_all_pushes(env):
    _seed_signal(env["shared"])
    env["next_result"]["value"] = "SUPPRESSED_DISABLED"
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.disabled_suppressed == 1
    # Dispatcher was called (and returned SUPPRESSED_DISABLED), but state
    # not updated — when the operator re-enables, the next tick can push.
    assert _state(env["shared"])["signatures"] == {}


def test_cooldown_suppressed_does_not_update_state(env):
    """If the dispatcher's per-source cooldown blocks our push (e.g.
    we already pushed for this signature recently from a previous run
    that wasn't yet committed to state), don't pretend we pushed."""
    _seed_signal(env["shared"])
    env["next_result"]["value"] = "SUPPRESSED_COOLDOWN"
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.cooldown_suppressed == 1
    assert _state(env["shared"])["signatures"] == {}


def test_failed_dispatch_does_not_update_state(env):
    _seed_signal(env["shared"])
    env["next_result"]["value"] = "FAILED"
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.failed == 1
    # Next tick (with subprocess healed) should be able to push.
    assert _state(env["shared"])["signatures"] == {}


def test_deferred_updates_state_so_signal_not_re_enqueued(env):
    """DEFERRED means the catalog accepted delivery via the digest
    queue. From signal_notifier's POV the operator handoff is done —
    we record the signature so the next tick doesn't re-enqueue.

    Without this update, any firing Signal whose catalog event maps to
    daily_digest/weekly_digest frequency would generate one queue
    entry per signal_notifier tick (one per minute on the default
    cron), so the daily digest would render N copies of the same
    finding when it flushes.
    """
    sig = _seed_signal(env["shared"])
    env["next_result"]["value"] = "DEFERRED"
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)

    assert stats.deferred == 1
    assert stats.fired == 0  # not a "fire" — separate counter
    # State now has the signature; next tick won't re-fire.
    state = _state(env["shared"])
    assert sig.signature in state["signatures"]
    assert state["signatures"][sig.signature]["alerted_for_signal_id"] == sig.id

    # Second tick at the same time — already alerted, no re-enqueue.
    env["sent"].clear()
    stats2 = env["sn"].run_once(
        env["shared"], env["network"], now=now + timedelta(seconds=60),
    )
    assert stats2.deferred == 0
    assert stats2.fired == 0
    assert env["sent"] == []


# ── Severity rendering ──────────────────────────────────────────────────────


def test_alert_severity_renders_red_circle(env):
    _seed_signal(env["shared"], severity="alert")
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    env["sn"].run_once(env["shared"], env["network"], now=now)
    assert "🔴" in env["sent"][0]["message"]


def test_warn_severity_renders_yellow_circle(env):
    _seed_signal(env["shared"], severity="warn")
    # Past the 900s warn grace so the warn fire actually pushes.
    now = datetime.now(timezone.utc) + timedelta(seconds=1000)
    env["sn"].run_once(env["shared"], env["network"], now=now)
    assert "⚠️" in env["sent"][0]["message"]


# ── State file hygiene ──────────────────────────────────────────────────────


def test_state_file_persists_across_runs(env):
    sig = _seed_signal(env["shared"])
    t = datetime.now(timezone.utc) + timedelta(seconds=300)
    env["sn"].run_once(env["shared"], env["network"], now=t)
    state_file = env["shared"] / "signals" / "notifier-state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert sig.signature in state["signatures"]
    assert state["signatures"][sig.signature]["alerted_for_signal_id"] == sig.id


def test_state_retained_for_flap_window_after_signature_clears(env):
    """Post-recovery the state entry is retained (not popped) so the
    flap-window suppression check on a subsequent re-fire of the same
    signature has a `last_resolve_pushed_at` to read.

    The 24h retention prune in _save_state ages out resolved entries
    that nothing has re-fired against; pinned in
    test_state_retention_prune_ages_out_old_resolves.
    """
    sig = _seed_signal(env["shared"])
    t = datetime.now(timezone.utc) + timedelta(seconds=300)
    env["sn"].run_once(env["shared"], env["network"], now=t)
    assert sig.signature in _state(env["shared"])["signatures"]

    _resolve_signal(env["shared"], sig)
    env["sn"].run_once(env["shared"], env["network"], now=t + timedelta(seconds=10))
    # Recovery push completed but state entry stays (resolved, not popped).
    entry = _state(env["shared"])["signatures"].get(sig.signature)
    assert entry is not None
    assert entry["alerted_for_signal_id"] is None
    assert entry["last_resolve_pushed_at"]


# ── Phase D: catalog-event mapping ───────────────────────────────────────


def test_catalog_event_mapping_for_pod_health_gateway(env):
    """A pod_health gateway Signal must map to system.gateway_state_change
    so the operator's "Gateway up/down" subscription toggle takes effect."""
    _seed_signal(env["shared"])  # default: producer=pod_health, type=pod_health_gateways
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    env["sn"].run_once(env["shared"], env["network"], now=now)
    assert len(env["sent"]) == 1
    assert env["sent"][0]["catalog_event"] == "system.gateway_state_change"


def test_catalog_event_mapping_uses_watchdog_for_non_gateway_pod_health(env):
    """Other pod_health types fall back to system.watchdog_event."""
    _seed_signal(
        env["shared"],
        signature="pod_health:pod_health_launchd:team_bot_a:plist",
        type="pod_health_launchd",
        title="team_bot_a launchd plist missing",
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    env["sn"].run_once(env["shared"], env["network"], now=now)
    assert env["sent"][0]["catalog_event"] == "system.watchdog_event"


def test_catalog_event_mapping_for_host_health_and_error_reporter(env):
    """Each producer in the default allowlist maps to a catalog event so
    operator subscriptions can gate per-event. Unknown producers leave
    catalog_event=None (dispatcher falls back to source-level gating)."""
    from evolve_admin.alerts.signal_notifier import _catalog_event_for_signal

    class _S:
        def __init__(self, producer, sig_type=""):
            self.producer = producer
            self.type = sig_type

    assert _catalog_event_for_signal(_S("pod_health", "pod_health_gateways")) == "system.gateway_state_change"
    # Crash-loop / port-collision: gateway is down and won't self-recover →
    # the "didn't auto-restart" event, more apt than the up/down toggle.
    assert _catalog_event_for_signal(_S("pod_health", "pod_health_gateway_crashloop")) == "system.gateway_autorestart_failed"
    assert _catalog_event_for_signal(_S("pod_health", "pod_health_gateway_port_collision")) == "system.gateway_autorestart_failed"
    assert _catalog_event_for_signal(_S("pod_health", "pod_health_launchd")) == "system.watchdog_event"
    assert _catalog_event_for_signal(_S("host_health")) == "system.watchdog_event"
    assert _catalog_event_for_signal(_S("integration_probe")) == "system.watchdog_event"
    assert _catalog_event_for_signal(_S("error_reporter")) == "system.daemon_error_spike"
    assert _catalog_event_for_signal(_S("audit")) == "security.audit_finding"
    assert _catalog_event_for_signal(_S("security_warden")) == "security.audit_finding"
    assert _catalog_event_for_signal(_S("watchdog")) == "system.watchdog_event"
    # pod_report (added post-2026-06-03 outage) maps by signal type:
    # gateway types → gateway_state_change so the operator can mute it
    # without losing other pod_report alerts; audit_* → security finding;
    # the rest fall through to source-level gating.
    assert _catalog_event_for_signal(_S("pod_report", "gateway_down")) == "system.gateway_state_change"
    assert _catalog_event_for_signal(_S("pod_report", "audit_critical")) == "security.audit_finding"
    assert _catalog_event_for_signal(_S("pod_report", "metrics_outage")) == "system.watchdog_event"
    # Totality (spec-subscription-completeness-2026-06-24): unmapped pod_report
    # types no longer return None — they fall through to the watchdog umbrella
    # so the operator keeps a per-event subscription handle.
    assert _catalog_event_for_signal(_S("pod_report", "cost_spike")) == "system.watchdog_event"
    # Producers added 2026-06-03 to close the silent-monitor gap. Each
    # maps to the closest existing catalog event (umbrella) or a
    # bespoke catalog entry added in the follow-up PR (plugin /
    # exec_outcome / stuck_proposal / session_cost).
    assert _catalog_event_for_signal(_S("permission_monitor", "perm_config_drift")) == "security.audit_finding"
    assert _catalog_event_for_signal(_S("monitor_coverage", "monitor_silent")) == "system.watchdog_event"
    # repo_puller_sudoers maps to a DEDICATED immediate event, NOT
    # system.watchdog_event (DAILY_DIGEST) — routing it there left a firing
    # signal undelivered for 24h+. Dormant sudo grants need a prompt ping.
    assert _catalog_event_for_signal(_S("repo_puller_sudoers", "sudoers_refresh_failed")) == "system.sudoers_refresh_failed"
    assert _catalog_event_for_signal(_S("oc_cli", "cli_misinvocation")) == "system.oc_cli_misinvocation"
    assert _catalog_event_for_signal(_S("bot_log_monitor", "max_auth_failure")) == "system.daemon_error_spike"
    assert _catalog_event_for_signal(_S("bot_recovery_monitor", "bot_recovered")) == "system.watchdog_event"
    assert _catalog_event_for_signal(_S("plugin_monitor", "plugin_missing_required")) == "system.plugin_health_issue"
    assert _catalog_event_for_signal(_S("plugin_monitor", "plugin_config_drift")) == "system.plugin_health_issue"
    assert _catalog_event_for_signal(_S("exec_outcome_watchdog", "tool_error_burst")) == "system.exec_outcome_failure"
    assert _catalog_event_for_signal(_S("exec_outcome_watchdog", "approval_timeout")) == "system.exec_outcome_failure"
    assert _catalog_event_for_signal(_S("stuck_proposal_monitor", "stuck_proposal")) == "system.stuck_proposal"
    assert _catalog_event_for_signal(_S("session_cost_monitor", "session_budget_exceeded")) == "cost.session_budget_exceeded"
    # delivery_monitor (U2.1) — bespoke per-type events so the operator
    # can mute missed-delivery alerts without losing the unmeasurable
    # advisories (or vice versa).
    assert _catalog_event_for_signal(_S("delivery_monitor", "app_delivery_missed")) == "system.app_delivery_missed"
    assert _catalog_event_for_signal(_S("delivery_monitor", "app_delivery_unmeasurable")) == "system.app_delivery_unmeasurable"
    # Subscription-completeness producers (spec-subscription-completeness-
    # 2026-06-24): previously-unmapped producers that returned None now carry
    # a handle so no dispatched message reaches a channel unsubscribable.
    assert _catalog_event_for_signal(_S("content_scan", "content_scan_file_disappeared")) == "system.identity_doc_missing"
    # "evolve can't read it" splits off the louder "file is gone" event into a
    # quieter, separately-tunable access-flap class — the producer-side fix for
    # the digest screaming about files that are fine.
    assert _catalog_event_for_signal(_S("content_scan", "content_scan_file_unreadable")) == "system.bot_file_unreadable"
    # Other content_scan types (structural/match) keep the identity-doc handle.
    assert _catalog_event_for_signal(_S("content_scan", "content_scan_structural_anomaly")) == "system.identity_doc_missing"
    assert _catalog_event_for_signal(_S("alerts_loop_monitor", "alert_repeat_loop")) == "meta.alert_repeat_loop"
    assert _catalog_event_for_signal(_S("alerts_loop_monitor", "dispatcher_failures")) == "meta.dispatcher_health"
    assert _catalog_event_for_signal(_S("pod_perms_drift", "pod_perms_drift")) == "security.config_drift"
    # Digest-default classification (D4, spec-subscription-digest-default-
    # 2026-06-28): four former meta.unclassified contributors now bind to real
    # classes. cascade_audit binds by type — only the telemetry-silence type
    # maps to the system class; its other types stay on the (now-digested)
    # catch-all.
    assert _catalog_event_for_signal(_S("deploy_drift_monitor", "deploy_drift")) == "updates.version_skew"
    assert _catalog_event_for_signal(_S("session_economics", "cache_invalidation_elevated")) == "cost.session_economics"
    assert _catalog_event_for_signal(_S("session_economics", "cache_hit_rate_low")) == "cost.session_economics"
    assert _catalog_event_for_signal(_S("code_quality_monitor", "fix_heavy_scope")) == "meta.dev_health"
    assert _catalog_event_for_signal(_S("code_quality_monitor", "revert_rate_high")) == "meta.dev_health"
    assert _catalog_event_for_signal(_S("cascade_audit", "plugin_telemetry_failure")) == "system.plugin_telemetry_silent"
    assert _catalog_event_for_signal(_S("cascade_audit", "tier_routing_disagreement")) == "meta.unclassified"
    # Totality catch-all: an unknown producer NO LONGER returns None — it
    # routes to meta.unclassified (loud-by-default) so every dispatch carries
    # a subscription handle. This is the keystone invariant.
    assert _catalog_event_for_signal(_S("not_a_real_producer")) == "meta.unclassified"


# Every producer ``_catalog_event_for_signal`` knows about, with a
# representative type. The totality test asserts each maps to a NON-None,
# real catalog key. Add a row here when you teach the mapper a new producer.
_KNOWN_SIGNAL_PRODUCERS: tuple[tuple[str, str], ...] = (
    ("pod_health", "pod_health_gateways"),
    ("pod_health", "pod_health_gateway_crashloop"),
    ("pod_health", "pod_health_launchd"),
    ("host_health", "disk_low"),
    ("integration_probe", "probe_failed"),
    ("error_reporter", "error_spike"),
    ("audit", "finding"),
    ("security_warden", "finding"),
    ("watchdog", "event"),
    ("pod_report", "gateway_down"),
    ("pod_report", "audit_critical"),
    ("pod_report", "metrics_outage"),
    ("pod_report", "cost_spike"),            # unmapped pod_report type → catch
    ("permission_monitor", "perm_config_drift"),
    ("permission_monitor", "autonomy_posture_drift"),
    ("permission_monitor", "autonomy_backfill_review"),
    ("permission_monitor", "autonomy_limit_hit"),
    ("permission_monitor", "autonomy_demoted"),
    ("permission_monitor", "autonomy_promotion_candidate"),
    ("monitor_coverage", "monitor_silent"),
    ("oc_cli", "cli_misinvocation"),
    ("bot_log_monitor", "max_auth_failure"),
    ("bot_recovery_monitor", "bot_recovered"),
    ("plugin_monitor", "plugin_missing_required"),
    ("exec_outcome_watchdog", "tool_error_burst"),
    ("stuck_proposal_monitor", "stuck_proposal"),
    ("session_cost_monitor", "session_budget_exceeded"),
    ("forge_cost_guard", "forge_session_cap"),
    ("app_structural_verifier", "openclaw_cron_missing"),
    ("app_structural_verifier", "file_missing"),
    ("app_script_failure_audit", "script_failed"),
    ("repo_puller_sudoers", "sudoers_refresh_failed"),
    ("cron_exit_monitor", "cron_job_failed"),
    ("delivery_monitor", "app_delivery_missed"),
    ("delivery_monitor", "app_delivery_unmeasurable"),
    ("delivery_monitor", "pod_delivery_regression"),
    ("send_surface_probe", "send_surface_broken"),
    ("oc_surface_drift", "openclaw_surface_drift"),
    # Subscription-completeness producers (2026-06-24).
    ("content_scan", "content_scan_file_disappeared"),
    ("content_scan", "content_scan_file_unreadable"),
    ("alerts_loop_monitor", "alert_repeat_loop"),
    ("alerts_loop_monitor", "dispatcher_failures"),
    ("pod_perms_drift", "pod_perms_drift"),
    # Digest-default classification (D4, 2026-06-28).
    ("deploy_drift_monitor", "deploy_drift"),
    ("session_economics", "cache_invalidation_elevated"),
    ("code_quality_monitor", "fix_heavy_scope"),
    ("cascade_audit", "plugin_telemetry_failure"),
    ("cascade_audit", "tier_routing_disagreement"),  # → digested catch-all
    # The catch-all itself: a never-seen producer.
    ("brand_new_unmapped_producer", "some_type"),
)


def test_catalog_event_mapping_is_total():
    """KEYSTONE INVARIANT (spec-subscription-completeness-2026-06-24):
    ``_catalog_event_for_signal`` NEVER returns None. Every dispatched
    message must carry a ``catalog_event`` so the operator has a
    subscription handle for it — no message reaches a channel
    unsubscribable. This is the producer-side ratchet that makes "every
    message tied to a subscription" enforced, not aspirational.

    For every known producer (+ a brand-new unmapped one) the mapper
    returns a non-None key that resolves to a REAL catalog entry."""
    from evolve_admin.alerts.signal_notifier import _catalog_event_for_signal
    from evolve_admin.alerts import catalog as cat

    class _S:
        def __init__(self, producer, sig_type=""):
            self.producer = producer
            self.type = sig_type

    failures: list[str] = []
    for producer, sig_type in _KNOWN_SIGNAL_PRODUCERS:
        key = _catalog_event_for_signal(_S(producer, sig_type))
        if key is None:
            failures.append(f"{producer}/{sig_type} → None")
            continue
        if cat.by_key(key) is None:
            failures.append(f"{producer}/{sig_type} → {key!r} (no catalog entry)")
    assert not failures, (
        "signal→catalog mapping is not total (catalog_event=None reaches a "
        "channel unsubscribable):\n  - " + "\n  - ".join(failures)
    )


def test_digest_default_classification_binds_former_unclassified_contributors():
    """D4 (spec-subscription-digest-default-2026-06-28): the four loud
    contributors from the 2026-06-28 evo-vps flood each now bind to a real,
    properly-categorized catalog class that DEFAULTS TO DAILY_DIGEST — out of
    the loud meta.unclassified catch-all (never loud-immediate). And the
    residual catch-all itself now defaults to digest, so an unmapped producer
    still reaches the operator without driving a push flood."""
    from evolve_admin.alerts.signal_notifier import _catalog_event_for_signal
    from evolve_admin.alerts import catalog as cat

    class _S:
        def __init__(self, producer, sig_type=""):
            self.producer = producer
            self.type = sig_type

    cases = [
        ("deploy_drift_monitor", "deploy_drift",
         "updates.version_skew", cat.Category.UPDATES),
        ("session_economics", "cache_invalidation_elevated",
         "cost.session_economics", cat.Category.COST),
        ("code_quality_monitor", "fix_heavy_scope",
         "meta.dev_health", cat.Category.META),
        ("cascade_audit", "plugin_telemetry_failure",
         "system.plugin_telemetry_silent", cat.Category.SYSTEM),
    ]
    for producer, sig_type, expected_key, expected_cat in cases:
        key = _catalog_event_for_signal(_S(producer, sig_type))
        assert key == expected_key, f"{producer}/{sig_type} → {key} != {expected_key}"
        entry = cat.by_key(key)
        assert entry is not None, f"{expected_key} has no catalog entry"
        assert entry.category is expected_cat
        assert entry.default_frequency is cat.Frequency.DAILY_DIGEST, (
            f"{expected_key} must default to DAILY_DIGEST (never loud-immediate)"
        )
        assert entry.default_enabled is True
        # Heads-up / maintenance / trend classes — never a critical page.
        assert entry.severity is not cat.Severity.CRITICAL

    # Residual unclassified now batches into the daily digest, not a loud push.
    catchall = cat.by_key("meta.unclassified")
    assert catchall is not None
    assert catchall.default_frequency is cat.Frequency.DAILY_DIGEST


def test_no_alerts_send_passes_literal_catalog_event_none():
    """No-None-send ratchet (spec-subscription-completeness-2026-06-24):
    no ``dispatcher.send`` / ``_dispatcher.send`` / ``_dispatch.send`` call
    site under ``alerts/`` may pass a literal ``catalog_event=None`` for a
    real message. Combined with the totality test above (which covers the
    callers that pass ``catalog_event=_catalog_event_for_signal(...)``),
    this enforces that every dispatched message carries a subscription
    handle.

    Source-level grep — cheap, catches the regression where someone
    re-introduces a hardcoded None send. If a future call site legitimately
    needs a None (none currently do), it must be added to the allowlist
    here with a comment justifying why it can never carry a real message."""
    import re
    from pathlib import Path

    alerts_dir = Path(__file__).parent.parent / "evolve_admin" / "alerts"
    # No legitimate literal-None sends remain after the keystone work. The
    # one defensive fallback (digest_dispatcher.py:432 skip-gating-on-None)
    # is a READER, not a send call, so it isn't matched here.
    allowlisted: set[tuple[str, int]] = set()

    send_re = re.compile(r"\b(?:_?dispatch(?:er)?)\.send\s*\(")
    none_re = re.compile(r"catalog_event\s*=\s*None")

    failures: list[str] = []
    for py in sorted(alerts_dir.glob("*.py")):
        lines = py.read_text(encoding="utf-8").splitlines()
        i = 0
        while i < len(lines):
            if send_re.search(lines[i]):
                # Scan the call's argument block (until the matching close
                # at column 0-ish / a line that's just ")") for a literal
                # catalog_event=None.
                start = i
                depth = lines[i].count("(") - lines[i].count(")")
                j = i
                while depth > 0 and j + 1 < len(lines):
                    j += 1
                    depth += lines[j].count("(") - lines[j].count(")")
                block = "\n".join(lines[start:j + 1])
                if none_re.search(block) and (py.name, start + 1) not in allowlisted:
                    failures.append(f"{py.name}:{start + 1} passes catalog_event=None")
                i = j + 1
                continue
            i += 1
    assert not failures, (
        "dispatcher.send call sites passing literal catalog_event=None "
        "(every dispatched message must carry a subscription handle):\n  - "
        + "\n  - ".join(failures)
    )


def test_delivery_monitor_not_in_direct_dispatch_denylist():
    """Spec §10.2: delivery_monitor emits via signals.store.observe() only.
    Adding it to the deny-list is the modern allowlist-miss — Alerts page
    lights up, chat stays silent."""
    from evolve_admin.alerts.signal_notifier import _DIRECT_DISPATCH_PRODUCERS

    assert "delivery_monitor" not in _DIRECT_DISPATCH_PRODUCERS


def test_delivery_monitor_missed_signal_reaches_chat(env):
    """The test_brand_new_producer family, pinned for this producer (spec
    §10.6): a firing app_delivery_missed Signal reaches operator chat with
    zero config edits once the (warn) grace window passes, gated under its
    bespoke catalog event."""
    _seed_signal(
        env["shared"],
        signature="delivery_monitor:app_delivery_missed:team_bot:app:act",
        producer="delivery_monitor",
        type="app_delivery_missed",
        severity="warn",
        title="Test App didn't run on schedule",
        body="team_bot's 07:00 Test App missed its delivery window.",
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=1000)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 1
    assert len(env["sent"]) == 1
    assert env["sent"][0]["catalog_event"] == "system.app_delivery_missed"


# ── M2: announce_unannounced_resolve (delivery monitor §9.1/§9.2) ───────────


def _seed_delivery_signal(shared, **overrides):
    import signals.store as signals_store
    base = dict(
        signature=(
            "delivery_monitor:app_delivery_missed:"
            "team_bot:app_morning_briefing:morning-briefing"
        ),
        producer="delivery_monitor",
        type="app_delivery_missed",
        flavor="activity",
        severity="warn",
        scope="bot",
        bot_id="team_bot",
        title="Morning Briefing didn't run on schedule",
        body=(
            "team_bot's 07:00 Morning Briefing missed its delivery window "
            "(07:00–07:30). Evolve restarted it and is watching for the delivery."
        ),
        details={
            "app_name": "Morning Briefing",
            "schedule_human": "07:00",
            "heal": {"attempted": True, "action": "kickstart", "result": "restarted"},
            "recovery": None,
        },
    )
    base.update(overrides)
    return signals_store.observe(shared, **base)


def _write_recovery_and_resolve(shared, sig):
    """The monitor's fast-heal shape (§7): recovery details are written
    onto the Signal before sweep_resolve archives it."""
    import signals.store as signals_store
    sig = _seed_delivery_signal(
        shared,
        details={
            "app_name": "Morning Briefing",
            "schedule_human": "07:00",
            "recovery": {
                "delivered_at": "2026-06-09T07:40:00-04:00",
                "summary": "Evolve restarted it, and it was delivered at 07:40.",
                "healed": True,
            },
        },
    )
    _resolve_signal(shared, sig)
    return sig


def test_m2_fast_heal_unannounced_resolve_pushes_single_green(env):
    """The flagship §9.2 message: miss → heal → resolve all inside the
    debounce window ⇒ exactly ONE 🟢, rendered from details.recovery."""
    sig = _seed_delivery_signal(env["shared"])
    _write_recovery_and_resolve(env["shared"], sig)

    now = datetime.now(timezone.utc) + timedelta(seconds=60)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.unannounced_recovered == 1
    assert stats.fired == 0
    assert len(env["sent"]) == 1
    msg = env["sent"][0]["message"]
    assert msg == (
        "🟢 Morning Briefing — late today, now delivered\n"
        "team_bot's 07:00 Morning Briefing didn't go out on time. "
        "Evolve restarted it, and it was delivered at 07:40.\n"
        "No action needed."
    )
    assert env["sent"][0]["catalog_event"] == "system.app_delivery_missed"

    # Exactly once: subsequent ticks stay quiet.
    env["sent"].clear()
    stats2 = env["sn"].run_once(
        env["shared"], env["network"], now=now + timedelta(seconds=60),
    )
    assert stats2.unannounced_recovered == 0
    assert env["sent"] == []


def test_m2_announced_fire_still_gets_exactly_one_green(env):
    """Slow outage: ⚠️ fire then 🟢 resolve — Phase B renders the same
    recovery copy and Phase B2 must not double-push."""
    sig = _seed_delivery_signal(env["shared"])
    # Past the warn grace so the ⚠️ fire is announced (then it resolves).
    t_fire = datetime.now(timezone.utc) + timedelta(seconds=1000)
    env["sn"].run_once(env["shared"], env["network"], now=t_fire)
    assert len(env["sent"]) == 1  # the ⚠️ fire

    _write_recovery_and_resolve(env["shared"], sig)
    env["sent"].clear()
    stats = env["sn"].run_once(
        env["shared"], env["network"], now=t_fire + timedelta(seconds=60),
    )
    assert stats.recovered == 1
    assert stats.unannounced_recovered == 0
    assert len(env["sent"]) == 1
    assert "late today, now delivered" in env["sent"][0]["message"]
    # And nothing more on the next tick.
    env["sent"].clear()
    env["sn"].run_once(
        env["shared"], env["network"], now=t_fire + timedelta(seconds=120),
    )
    assert env["sent"] == []


def test_m2_unflagged_event_resolve_stays_silent(env):
    """The orphan-suppression default still holds for everything that
    didn't opt in (pod_health → system.gateway_state_change)."""
    sig = _seed_signal(env["shared"])
    _resolve_signal(env["shared"], sig)
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.unannounced_recovered == 0
    assert env["sent"] == []


def test_m2_escalation_to_alert_reannounces_once(env):
    """§8 heal_wait: the monitor escalates the SAME Signal to alert with
    'the restart didn't work' copy — for the flagged event that follow-up
    must reach chat (once), not vanish behind the same-signal-id skip."""
    import signals.store as signals_store
    sig = _seed_delivery_signal(env["shared"])
    # Past the warn grace so the warn fire is announced before it escalates.
    t_fire = datetime.now(timezone.utc) + timedelta(seconds=1000)
    env["sn"].run_once(env["shared"], env["network"], now=t_fire)
    assert len(env["sent"]) == 1  # warn fire announced

    # Monitor escalates: same signature ⇒ same signal id, new severity+copy.
    escalated = _seed_delivery_signal(
        env["shared"],
        severity="alert",
        title="Morning Briefing didn't arrive",
        body=(
            "team_bot's 07:00 Morning Briefing didn't go out as scheduled, "
            "and an automatic restart didn't fix it.\n"
            "Check team_bot's Apps page for details."
        ),
    )
    assert escalated.id == sig.id

    env["sent"].clear()
    stats = env["sn"].run_once(
        env["shared"], env["network"], now=t_fire + timedelta(seconds=60),
    )
    assert stats.escalation_announced == 1
    assert len(env["sent"]) == 1
    msg = env["sent"][0]["message"]
    assert "🔴" in msg
    assert "didn't fix it" in msg
    # …and only once.
    env["sent"].clear()
    stats2 = env["sn"].run_once(
        env["shared"], env["network"], now=t_fire + timedelta(seconds=120),
    )
    assert stats2.escalation_announced == 0
    assert env["sent"] == []


def test_escalation_not_reannounced_for_unflagged_events(env):
    """pod_health (unflagged) keeps escalate-without-re-paging."""
    _seed_signal(env["shared"], severity="warn")
    # Past the warn grace so the warn fire is announced before it escalates.
    t_fire = datetime.now(timezone.utc) + timedelta(seconds=1000)
    env["sn"].run_once(env["shared"], env["network"], now=t_fire)
    assert len(env["sent"]) == 1

    _seed_signal(env["shared"], severity="alert")  # same signature escalates
    env["sent"].clear()
    stats = env["sn"].run_once(
        env["shared"], env["network"], now=t_fire + timedelta(seconds=60),
    )
    assert stats.escalation_announced == 0
    assert env["sent"] == []


def test_render_fire_keeps_possessive_bot_body():
    """{bot}'s is prose (the §9.2 copy), not a redundant bot prefix —
    stripping it would mangle the body to \"'s 07:00 …\"."""
    from evolve_admin.alerts.signal_notifier import _strip_bot_prefix

    body = "team_bot's 07:00 Morning Briefing missed its delivery window."
    assert _strip_bot_prefix(body, "team_bot") == body
    # The bare-prefix defense still works.
    assert _strip_bot_prefix("team_bot: gateway down", "team_bot") == "gateway down"


def test_permission_monitor_pushes_after_debounce(env):
    """Regression — permission_monitor is an observe-only producer (no direct
    dispatcher.send). Under the deny-list model it is loud by default; the
    catalog mapping routes it to security.audit_finding so the operator can
    mute it independently via subscription gating."""
    _seed_signal(
        env["shared"],
        signature="permission_monitor:perm_config_drift:team_bot_a",
        producer="permission_monitor",
        type="perm_config_drift",
        bot_id="team_bot_a",
        title="permissions drifted from baseline",
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 1
    assert env["sent"][0]["catalog_event"] == "security.audit_finding"


# ── Cold-start protection ───────────────────────────────────────────────────


def _cold_env(tmp_path, monkeypatch):
    """Variant of `env` without the pre-written notifier-state.json so
    the cold-start branch executes. Returns the same shape."""
    from evolve_admin.alerts import signal_notifier as sn
    from evolve_admin.alerts import dispatcher as disp

    shared = tmp_path / "evolve"
    (shared / "signals" / "firing").mkdir(parents=True)
    (shared / "signals" / "snoozed").mkdir(parents=True)
    (shared / "signals" / "archived").mkdir(parents=True)
    # Intentionally NO notifier-state.json — first ever run.

    sent: list[dict] = []

    def _fake_send(*, shared_dir, network, source, message, severity,
                   dedup_key=None, cooldown_seconds=None,
                   recipient_override=None, now=None, catalog_event=None):
        sent.append({"source": source, "message": message})
        from evolve_admin.alerts.dispatcher import DispatchOutcome, DispatchResult
        return DispatchOutcome(
            result=DispatchResult.SENT,
            source=source, severity=severity, dedup_key=dedup_key,
            channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(disp, "send", _fake_send)
    network = {"alerts": {"channel": "telegram", "chatId": "12345"}}
    return {"shared": shared, "network": network, "sn": sn, "sent": sent}


def test_cold_start_sync_records_state_without_pushing(tmp_path, monkeypatch):
    """First ever run after enable: every backlogged firing Signal is
    recorded as already-alerted so subsequent ticks don't dump the
    backlog to chat. The debounce window only protects against
    signal-side flap, not against operator-side first-enable; without
    this guard, enabling signal_notifier on a pod with months of
    quietly-accumulating Signals would page the operator hundreds of
    times in one tick.
    """
    env = _cold_env(tmp_path, monkeypatch)
    # Seed three old, well-past-debounce firing signals.
    for bot_id in ("team_bot_a", "admin_bot", "team_bot_b"):
        _seed_signal(env["shared"], bot_id=bot_id,
                     signature=f"pod_health:pod_health_gateways:{bot_id}:gateway",
                     title=f"{bot_id} gateway down")

    # State file does not exist yet — this is the very first tick.
    assert not (env["shared"] / "signals" / "notifier-state.json").exists()

    stats = env["sn"].run_once(env["shared"], env["network"])

    # No pushes — the whole point.
    assert stats.fired == 0
    assert env["sent"] == []

    # But state file now exists with all three signatures marked.
    state = _state(env["shared"])
    assert len(state["signatures"]) == 3
    for bot_id in ("team_bot_a", "admin_bot", "team_bot_b"):
        sig_key = f"pod_health:pod_health_gateways:{bot_id}:gateway"
        assert sig_key in state["signatures"]
        assert state["signatures"][sig_key].get("cold_start_synced") is True

    # Second tick (state file now present) returns to normal behavior —
    # no new pushes for the same signatures (they're already alerted).
    stats2 = env["sn"].run_once(env["shared"], env["network"])
    assert stats2.fired == 0
    assert env["sent"] == []


def test_cold_start_marks_recent_flagged_resolves_without_pushing(tmp_path, monkeypatch):
    """M2 + cold start: a delivery resolve from just before enable is
    backlog, not news — marked as announced, never pushed (neither on
    the first tick nor by Phase B2 on the second)."""
    env = _cold_env(tmp_path, monkeypatch)
    sig = _seed_delivery_signal(env["shared"])
    _write_recovery_and_resolve(env["shared"], sig)

    stats = env["sn"].run_once(env["shared"], env["network"])
    assert stats.unannounced_recovered == 0
    assert env["sent"] == []
    entry = _state(env["shared"])["signatures"].get(sig.signature)
    assert entry and entry["resolve_pushed_for_signal_id"] == sig.id

    stats2 = env["sn"].run_once(env["shared"], env["network"])
    assert stats2.unannounced_recovered == 0
    assert env["sent"] == []


def test_announce_unannounced_resolve_flag_is_pinned_to_the_missed_event():
    """The M2 flag lives on system.app_delivery_missed (spec §9.1/§10.5)
    and nowhere else until another producer deliberately opts in."""
    from evolve_admin.alerts.catalog import CATALOG

    flagged = [e.key for e in CATALOG if e.announce_unannounced_resolve]
    assert flagged == ["system.app_delivery_missed"]


def test_cold_start_does_not_record_direct_dispatch_producers(tmp_path, monkeypatch):
    """Cold-start only records signatures for producers that would push
    through this notifier. Direct-dispatch producers are excluded so their
    Signals don't occupy a state slot (they message via their own path)."""
    env = _cold_env(tmp_path, monkeypatch)
    _seed_signal(env["shared"], producer="cost_watchdog",
                 signature="cost_watchdog:spend_exceeded:admin_bot",
                 title="daily spend cap exceeded")
    stats = env["sn"].run_once(env["shared"], env["network"])
    assert env["sent"] == []
    assert stats.skipped_direct_dispatch == 1
    state = _state(env["shared"])
    assert state["signatures"] == {}


# ── Render dedup (bot_id-in-title + body == title) ──────────────────────────


def test_render_fire_strips_bot_prefix_with_colon():
    """Producers that bake "{bot_id}:" into the title would otherwise
    yield "team_bot_c: team_bot_c: ..." once _render_fire prepends its own head."""
    from evolve_admin.alerts.signal_notifier import _render_fire

    class _Sig:
        severity = "warn"
        bot_id = "team_bot_c"
        title = "team_bot_c: gateway down"
        body = ""

    out = _render_fire(_Sig())
    assert out == "⚠️ team_bot_c: gateway down"


def test_render_fire_strips_bot_prefix_with_parenthetical():
    """OC security audit findings use "{bot_id} ({check_id}): {desc}" —
    the bot_id is a space-separated prefix, not colon-separated. Must
    still strip cleanly, AND the trailing "({check_id}):" parenthetical
    that's left behind also gets stripped so chat reads as a sentence."""
    from evolve_admin.alerts.signal_notifier import _render_fire

    class _Sig:
        severity = "warn"
        bot_id = "team_bot_c"
        title = "team_bot_c (gateway.trusted_proxies_missing): Reverse proxy headers are not trusted"
        body = ""

    out = _render_fire(_Sig())
    # No leading "(check.id):" paren — operator sees just the description.
    assert out == "⚠️ team_bot_c: Reverse proxy headers are not trusted"


def test_render_fire_skips_body_when_identical_to_title():
    """Several producers write Signal.body identical to Signal.title.
    Without the dedup, the chat message reads:
        ⚠️ team_bot_c: X
        X
    With the dedup, the body is dropped."""
    from evolve_admin.alerts.signal_notifier import _render_fire

    class _Sig:
        severity = "warn"
        bot_id = "team_bot_c"
        title = "team_bot_c: gateway down"
        body = "team_bot_c: gateway down"

    out = _render_fire(_Sig())
    assert "\n" not in out  # body line was dropped
    assert out == "⚠️ team_bot_c: gateway down"


def test_render_fire_keeps_distinct_body():
    """When the body genuinely differs from the title (the normal case
    for well-behaved producers), it stays."""
    from evolve_admin.alerts.signal_notifier import _render_fire

    class _Sig:
        severity = "warn"
        bot_id = "team_bot_a"
        title = "team_bot_a: CPU load excursion"
        body = "1-min load average: 8.42 (crit threshold 4.0)"

    out = _render_fire(_Sig())
    assert out == (
        "⚠️ team_bot_a: CPU load excursion\n"
        "1-min load average: 8.42 (crit threshold 4.0)"
    )


def test_render_fire_html_escapes_dangerous_title():
    """Under HTML parse mode (PR 2 of dispatcher-safety rework),
    signal_notifier must HTML-escape Signal.title and Signal.body
    before splicing them into the chat message. Otherwise a producer
    that writes ``<`` or ``&`` in a title would break Telegram's HTML
    parser the same way the catalog event keys broke Markdown."""
    from evolve_admin.alerts.signal_notifier import _render_fire

    class _Sig:
        severity = "warn"
        bot_id = "team_bot_a"
        title = "Found <script>alert(1)</script> in plugin output"
        body = "Vector: A & B & C"

    out = _render_fire(_Sig())
    # Dangerous chars escaped.
    assert "&lt;script&gt;" in out
    assert "A &amp; B &amp; C" in out
    # No raw HTML tags reach the wire.
    assert "<script>" not in out


def test_render_resolve_html_escapes_dangerous_title():
    """Same protection on the recovery path."""
    from evolve_admin.alerts.signal_notifier import _render_resolve

    class _Sig:
        severity = "alert"
        bot_id = "<admin_bot>"
        title = "Issue with <plugin>"
        body = ""

    out = _render_resolve(_Sig())
    assert "&lt;admin_bot&gt;" in out
    assert "&lt;plugin&gt;" in out
    assert "<admin_bot>" not in out


def test_strip_check_id_paren_drops_leading_check_id():
    """The "({check_id}):" leading paren left over after bot-prefix
    stripping is what made resolved messages read as gibberish
    ("🟢 team_bot_c: (gateway.probe_failed): Gateway probe failed ..." —
    user saw this in chat 2026-05-20). Conservative match: only word
    chars, dots, hyphens between the parens — won't eat a legitimate
    English parenthetical aside that happens to lead a title.
    """
    from evolve_admin.alerts.signal_notifier import _strip_check_id_paren

    assert (
        _strip_check_id_paren("(gateway.probe_failed): Gateway probe failed (deep)")
        == "Gateway probe failed (deep)"
    )
    assert (
        _strip_check_id_paren("(plugins.installs_unpinned_npm_specs): Plugin index includes unpinned npm specs")
        == "Plugin index includes unpinned npm specs"
    )
    # Conservative — only strips check_id-shaped tokens. A legitimate
    # English parenthetical at the head stays.
    assert (
        _strip_check_id_paren("(see the next line): something explanatory")
        == "(see the next line): something explanatory"
    )
    # No paren at all → unchanged.
    assert _strip_check_id_paren("Plain title") == "Plain title"
    assert _strip_check_id_paren("") == ""


# ── Recovery rendering (_render_resolve) ────────────────────────────────────


def test_render_resolve_leads_with_cleared_framing():
    """Recovery messages frame the news affirmatively: "Cleared on
    {bot}: {title}". The producer-authored title is for the broken
    state ("Gateway probe failed (deep)") — pairing it with a green
    dot and trailing "— resolved" reads as a contradiction. Leading
    with "Cleared on {bot}:" puts the recovery framing before the
    failure-tense title.
    """
    from evolve_admin.alerts.signal_notifier import _render_resolve

    class _Sig:
        severity = "alert"
        bot_id = "team_bot_c"
        title = "team_bot_c (gateway.probe_failed): Gateway probe failed (deep)"
        body = ""

    out = _render_resolve(_Sig())
    # 🟢 + "Cleared on" + bot_id + producer's title — no orphan check_id
    # paren, no trailing "— resolved" suffix tacked onto a "failed" title.
    assert out == "🟢 Cleared on team_bot_c: Gateway probe failed (deep)"
    # Sanity — operator-facing wording, no "resolved" jargon.
    assert "resolved" not in out.lower()


def test_render_resolve_pod_scoped_signal():
    """Pod-scoped Signals (no bot_id) use "Cleared:" without a bot
    chip — the source/scope are surfaced separately on the Alerts
    page, and chat doesn't have a bot to anchor on."""
    from evolve_admin.alerts.signal_notifier import _render_resolve

    class _Sig:
        severity = "warn"
        bot_id = None
        title = "Repo puller wedged on origin/main"
        body = ""

    out = _render_resolve(_Sig())
    assert out == "🟢 Cleared: Repo puller wedged on origin/main"


# ── Flap suppression (post-recovery re-fire within window) ──────────────────


def test_flap_suppression_silences_refire_within_window(env):
    """Real-world case (team_bot_c gateway.probe_failed, 2026-05-20):
    signature fires, resolves, then re-fires 5 min later because the
    underlying degradation persists. Without flap suppression each
    cycle generated a ⚠️ + 🟢 pair in chat. With it, the second fire
    inside the 30-min window stays silent (Alerts page still records
    the transition).
    """
    sig = _seed_signal(env["shared"])
    t0 = datetime.now(timezone.utc) + timedelta(seconds=300)

    # First fire announces.
    env["sn"].run_once(env["shared"], env["network"], now=t0)
    assert len(env["sent"]) == 1
    env["sent"].clear()

    # Resolves 10s later → recovery push.
    _resolve_signal(env["shared"], sig)
    env["sn"].run_once(env["shared"], env["network"], now=t0 + timedelta(seconds=10))
    assert len(env["sent"]) == 1
    env["sent"].clear()

    # 5 min after recovery, same signature re-fires (fresh signal id).
    refired = _seed_signal(env["shared"])
    t_refire = t0 + timedelta(seconds=300 + 300)   # +5 min past resolve
    stats = env["sn"].run_once(env["shared"], env["network"], now=t_refire)

    assert stats.flap_suppressed == 1
    assert stats.fired == 0
    assert env["sent"] == []   # chat stays clean

    # State entry not corrupted — alerted_for_signal_id is still None
    # (we didn't announce), last_resolve_pushed_at is unchanged.
    entry = _state(env["shared"])["signatures"].get(refired.signature)
    assert entry is not None
    assert entry["alerted_for_signal_id"] is None


def test_flap_suppression_allows_refire_after_window_expires(env):
    """Once the flap window has passed, a re-fire of the same signature
    announces normally. Otherwise a persistent intermittent failure
    would stay silent forever."""
    sig = _seed_signal(env["shared"])
    t0 = datetime.now(timezone.utc) + timedelta(seconds=300)
    env["sn"].run_once(env["shared"], env["network"], now=t0)
    env["sent"].clear()

    _resolve_signal(env["shared"], sig)
    t_resolve = t0 + timedelta(seconds=10)
    env["sn"].run_once(env["shared"], env["network"], now=t_resolve)
    env["sent"].clear()

    # Re-fire 31 min after the resolve — past the default 30-min window.
    _seed_signal(env["shared"])
    t_late = t_resolve + timedelta(minutes=31)
    stats = env["sn"].run_once(env["shared"], env["network"], now=t_late)

    assert stats.flap_suppressed == 0
    assert stats.fired == 1
    assert len(env["sent"]) == 1


def test_flap_window_disabled_when_zero(tmp_path, monkeypatch):
    """Operators who want every transition surfaced can set
    alerts.signal_notifier.flap_window_seconds = 0 in
    better-engine-config.json. Suppression turns off."""
    import json as _json
    from evolve_admin.alerts import signal_notifier as sn

    shared = tmp_path / "evolve"
    (shared / "signals" / "firing").mkdir(parents=True)
    (shared / "signals" / "snoozed").mkdir(parents=True)
    (shared / "signals" / "archived").mkdir(parents=True)
    (shared / "signals" / "notifier-state.json").write_text(
        '{"version": 1, "signatures": {"sig1": {'
        '"alerted_for_signal_id": null,'
        '"last_resolve_pushed_at": "2999-01-01T00:00:00Z"'
        '}}}'
    )
    (shared / "better-engine-config.json").write_text(_json.dumps({
        "schema_version": 1,
        "pod_defaults": {"alerts": {"signal_notifier": {"flap_window_seconds": 0}}},
        "bots": {},
    }))

    # Lookup must return 0 — confirms the wiring.
    assert sn._read_flap_window_seconds(shared) == 0


def test_state_retention_prune_ages_out_old_resolves(tmp_path):
    """_save_state drops resolved-only entries older than 24h so the
    file doesn't grow unboundedly across a long-running install."""
    from evolve_admin.alerts.signal_notifier import _save_state, _load_state

    shared = tmp_path / "evolve"
    (shared / "signals").mkdir(parents=True)
    now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(days=2)).isoformat()
    fresh_ts = (now - timedelta(hours=1)).isoformat()

    state = {
        "version": 1,
        "signatures": {
            "old:resolved":   {"alerted_for_signal_id": None, "last_resolve_pushed_at": old_ts},
            "fresh:resolved": {"alerted_for_signal_id": None, "last_resolve_pushed_at": fresh_ts},
            "still:firing":   {"alerted_for_signal_id": "sig_xyz",
                               "last_resolve_pushed_at": None},
        },
    }
    _save_state(shared, state, now=now)
    out = _load_state(shared)["signatures"]
    assert "old:resolved" not in out             # aged out
    assert "fresh:resolved" in out                # within 24h
    assert "still:firing" in out                  # currently alerted


# ── Permanent-failure tracking (PR 1 of dispatcher-safety rework) ───────────


def test_permanent_failure_records_state_and_skips_next_tick(env):
    """When the dispatcher returns FAILED with is_permanent_failure=True
    (e.g., Telegram returned HTTP 400 for a parse error), signal_notifier
    records the offending signal id and skips it on subsequent ticks.

    Without this, the 1-min cron retries the same un-sendable message
    forever — the per-minute retry loop that broke chat on 2026-05-21
    when catalog event keys with underscores tripped Telegram's
    legacy-Markdown parser.
    """
    sig = _seed_signal(env["shared"])
    env["next_result"]["value"] = "FAILED"
    env["next_result"]["is_permanent_failure"] = True
    t = datetime.now(timezone.utc) + timedelta(seconds=300)

    stats = env["sn"].run_once(env["shared"], env["network"], now=t)
    assert stats.permanent_failure == 1
    assert stats.failed == 0
    # State now carries the permanent-failure marker so the next tick
    # skips this signal id.
    entry = _state(env["shared"])["signatures"].get(sig.signature)
    assert entry is not None
    assert entry["permanent_failure_signal_id"] == sig.id
    assert entry["last_permanent_failure_at"]

    # Next tick — same signal id, same broken message — must NOT retry.
    env["sent"].clear()
    stats2 = env["sn"].run_once(
        env["shared"], env["network"], now=t + timedelta(seconds=60),
    )
    assert stats2.permanent_failure_skipped == 1
    assert stats2.permanent_failure == 0
    assert env["sent"] == []   # no dispatcher call at all


def test_transient_failure_still_retries_next_tick(env):
    """Non-permanent FAILED outcomes (network blip, 5xx, timeout) leave
    state in place so the next tick retries. Only HTTP 4xx-class
    failures get the permanent-skip treatment."""
    sig = _seed_signal(env["shared"])
    env["next_result"]["value"] = "FAILED"
    env["next_result"]["is_permanent_failure"] = False
    t = datetime.now(timezone.utc) + timedelta(seconds=300)

    stats = env["sn"].run_once(env["shared"], env["network"], now=t)
    assert stats.failed == 1
    assert stats.permanent_failure == 0
    # No permanent_failure_signal_id recorded.
    entry = _state(env["shared"])["signatures"].get(sig.signature) or {}
    assert entry.get("permanent_failure_signal_id") is None

    # Next tick — dispatcher is called again (we retry).
    env["sent"].clear()
    env["next_result"]["value"] = "SENT"
    env["next_result"]["is_permanent_failure"] = False
    env["sn"].run_once(env["shared"], env["network"], now=t + timedelta(seconds=60))
    assert len(env["sent"]) == 1   # retry happened


def test_permanent_failure_skipped_when_signal_id_changes(env):
    """Once the audit cycle's sweep_resolve archives the old signal and
    a new fire creates a fresh signal id, signal_notifier retries —
    the new id might have a different message body that doesn't trip
    the parse error. Recovery via natural signal lifecycle."""
    sig1 = _seed_signal(env["shared"], signature="audit:gateway:team_bot_c:probe")
    env["next_result"]["value"] = "FAILED"
    env["next_result"]["is_permanent_failure"] = True
    t = datetime.now(timezone.utc) + timedelta(seconds=300)
    env["sn"].run_once(env["shared"], env["network"], now=t)
    assert _state(env["shared"])["signatures"][sig1.signature]["permanent_failure_signal_id"] == sig1.id

    # Audit resolves the old signal (so it leaves firing/) and creates a
    # fresh one with a new id but same signature. Bypass the 1h reopen
    # window so observe() makes a brand new Signal instead of reviving
    # the archived one.
    _resolve_signal(env["shared"], sig1)
    sig2 = _seed_signal(
        env["shared"], signature="audit:gateway:team_bot_c:probe",
        reopen_window_seconds=0,
    )
    assert sig2.id != sig1.id

    # Next tick — different signal id, so the permanent-failure skip
    # doesn't match. Dispatcher gets called again; this time SENT.
    env["sent"].clear()
    env["next_result"]["value"] = "SENT"
    env["next_result"]["is_permanent_failure"] = False
    env["sn"].run_once(env["shared"], env["network"], now=t + timedelta(seconds=120))
    assert len(env["sent"]) == 1   # fresh attempt for the new signal id


def test_state_retention_keeps_recent_permanent_failures(tmp_path):
    """Permanent-failure entries are retained for the 24h retention
    window so the next minute's tick doesn't lose the marker and
    retry the broken signal again."""
    from evolve_admin.alerts.signal_notifier import _save_state, _load_state

    shared = tmp_path / "evolve"
    (shared / "signals").mkdir(parents=True)
    now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    fresh_perm = (now - timedelta(hours=1)).isoformat()
    old_perm = (now - timedelta(days=2)).isoformat()

    state = {
        "version": 1,
        "signatures": {
            "fresh:perm-fail": {
                "alerted_for_signal_id": None,
                "permanent_failure_signal_id": "sig_a",
                "last_permanent_failure_at": fresh_perm,
            },
            "old:perm-fail": {
                "alerted_for_signal_id": None,
                "permanent_failure_signal_id": "sig_b",
                "last_permanent_failure_at": old_perm,
            },
        },
    }
    _save_state(shared, state, now=now)
    out = _load_state(shared)["signatures"]
    assert "fresh:perm-fail" in out      # retained
    assert "old:perm-fail" not in out    # aged out after 24h


# ── Source-disabled early-exit (2026-06-01) ─────────────────────────────────


def test_run_once_short_circuits_when_source_disabled(env, monkeypatch):
    """When the operator has set ``alerts.signal_notifier.enabled = false``
    in better-engine-config.json, ``run_once`` must NOT iterate firing
    Signals or call the dispatcher.

    Anchor: 2026-06-01 disk-fillup incident. The signal_notifier source
    was disabled but the runner still ticked every 60s, dispatching ~25
    firing Signals per tick. The dispatcher correctly suppressed every
    one — and wrote a record per suppression. 36k lines/day filled
    ``alerts/dispatcher-suppressed.jsonl`` to 306 MB in 22 days.

    This test seeds a firing Signal and disables the source via the
    config-lookup layer. Expected behavior: zero dispatcher calls, no
    state mutation, no log writes."""
    _seed_signal(env["shared"])
    # Disable via the same lookup the source uses.
    from evolve_admin.alerts import _config_lookup, signal_notifier as sn
    monkeypatch.setattr(
        _config_lookup, "lookup",
        lambda shared_dir, path, default: (
            False if path == "alerts.signal_notifier.enabled" else default
        ),
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = sn.run_once(env["shared"], env["network"], now=now)
    assert env["sent"] == [], (
        "dispatcher.send must not be called when the source is disabled"
    )
    # Stats object is the default-zeros NotifierStats — nothing to
    # report because nothing ran.
    assert stats.fired == 0
    assert stats.disabled_suppressed == 0
    assert stats.debounced == 0


def test_run_once_proceeds_when_source_enabled_default(env):
    """Sanity check: with default config (no override), the source is
    enabled and the normal Phase A path runs. Pins that the early-exit
    isn't accidentally always-on."""
    _seed_signal(env["shared"])
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)
    assert stats.fired == 1
    assert len(env["sent"]) == 1


# ── Digest-on-recovery — channel-level outage (#3152) ───────────────────────


def _seed_n_firing(shared, n):
    """Seed ``n`` distinct firing Signals (distinct signature + bot_id)."""
    sigs = []
    for i in range(n):
        sigs.append(_seed_signal(
            shared,
            signature=f"pod_health:pod_health_gateways:bot_{i}:gateway",
            bot_id=f"bot_{i}",
            title=f"bot_{i} gateway down",
        ))
    return sigs


def test_target_down_marks_delivery_down_and_bounds_the_burst(env):
    """A channel-level permanent failure (chat not found / blocked / bad
    token) flips the pod-level delivery-down marker and stops per-signal
    sends *within the same tick* — only the one send that detected the
    outage goes out, the rest are skipped. This is the #3152 fix: without
    it, every firing Signal hammered the dead target each tick.
    """
    _seed_n_firing(env["shared"], 3)
    env["next_result"]["value"] = "FAILED"
    env["next_result"]["is_permanent_failure"] = True
    env["next_result"]["is_delivery_target_down"] = True
    t = datetime.now(timezone.utc) + timedelta(seconds=300)

    stats = env["sn"].run_once(env["shared"], env["network"], now=t)

    # Exactly ONE send was attempted before the marker bounded the burst.
    assert len(env["sent"]) == 1
    assert stats.permanent_failure == 1
    assert stats.delivery_down_skipped == 2   # the other two firing signals
    state = _state(env["shared"])
    assert state.get("delivery_down")          # marker persisted
    assert "down_since" in state["delivery_down"]


def test_message_level_permanent_failure_does_not_mark_target_down(env):
    """A per-message permanent failure (HTML parse error, bare http 4xx —
    is_permanent_failure=True but is_delivery_target_down=False) keeps the
    existing per-signal skip behavior and must NOT flip the whole channel
    into digest-on-recovery mode. Guards against over-suppression: one bad
    message body is not a channel outage."""
    _seed_n_firing(env["shared"], 3)
    env["next_result"]["value"] = "FAILED"
    env["next_result"]["is_permanent_failure"] = True
    env["next_result"]["is_delivery_target_down"] = False
    t = datetime.now(timezone.utc) + timedelta(seconds=300)

    stats = env["sn"].run_once(env["shared"], env["network"], now=t)

    # Every signal was attempted individually (no channel-down short-circuit).
    assert len(env["sent"]) == 3
    assert stats.permanent_failure == 3
    assert stats.delivery_down_skipped == 0
    assert _state(env["shared"]).get("delivery_down") is None


def test_delivery_down_stays_silent_while_target_remains_down(env):
    """Once down, each tick fires a single digest *probe*. While the target
    is still down that probe FAILS and reaches no one — no per-signal
    replay, no flood — and the marker stays set."""
    sigs = _seed_n_firing(env["shared"], 2)
    # Hand-set the down marker (as if a prior tick detected the outage).
    p = env["shared"] / "signals" / "notifier-state.json"
    state = json.loads(p.read_text())
    state["delivery_down"] = {"down_since": "2026-06-23T00:00:00+00:00"}
    p.write_text(json.dumps(state))

    env["next_result"]["value"] = "FAILED"   # probe still fails
    env["next_result"]["is_permanent_failure"] = True
    env["next_result"]["is_delivery_target_down"] = True
    t = datetime.now(timezone.utc) + timedelta(seconds=300)

    stats = env["sn"].run_once(env["shared"], env["network"], now=t)

    # Exactly one probe attempt; no per-signal fires.
    assert len(env["sent"]) == 1
    assert env["sent"][0]["message"].startswith("📦")
    assert stats.delivery_recovered == 0
    assert stats.fired == 0
    # Still down — marker retained, backlog NOT yet marked known.
    state2 = _state(env["shared"])
    assert state2.get("delivery_down")
    for sig in sigs:
        assert sig.signature not in state2["signatures"]


def test_delivery_recovery_emits_single_digest_and_resyncs_backlog(env):
    """The down→up transition: the digest probe succeeds, exactly ONE
    digest message goes out (NOT one per backlogged signal), the firing
    backlog is re-synced as already-known, and the marker clears. New
    fires after recovery announce normally."""
    sigs = _seed_n_firing(env["shared"], 3)
    p = env["shared"] / "signals" / "notifier-state.json"
    state = json.loads(p.read_text())
    state["delivery_down"] = {"down_since": "2026-06-23T00:00:00+00:00"}
    p.write_text(json.dumps(state))

    env["next_result"]["value"] = "SENT"   # operator fixed the channel
    t = datetime.now(timezone.utc) + timedelta(seconds=300)

    stats = env["sn"].run_once(env["shared"], env["network"], now=t)

    # ONE digest, not three individual fires.
    assert len(env["sent"]) == 1
    assert "3 alerts accumulated" in env["sent"][0]["message"]
    assert stats.delivery_recovered == 1
    assert stats.delivery_digest_count == 3
    assert stats.fired == 0
    state2 = _state(env["shared"])
    assert state2.get("delivery_down") is None
    for sig in sigs:
        entry = state2["signatures"][sig.signature]
        assert entry["alerted_for_signal_id"] == sig.id
        assert entry.get("cold_start_synced") is True

    # Next tick: the backlog is known — no replay. A NEW signal announces.
    env["sent"].clear()
    new_sig = _seed_signal(
        env["shared"], signature="pod_health:new:bot_new:gateway",
        bot_id="bot_new", title="bot_new gateway down",
    )
    stats2 = env["sn"].run_once(
        env["shared"], env["network"], now=t + timedelta(seconds=60),
    )
    assert stats2.fired == 1
    assert len(env["sent"]) == 1
    assert "bot_new" in env["sent"][0]["message"]


# ── Auto-remediating suppression (spec-delta-transient-delivery-grace L2) ────
#
# A registered self-healing condition (pod_perms_drift — the catalog calls it
# "config drift") must not page until it has outlived its self-heal window and
# is STILL firing: "page on the FAILURE of the auto-fix, not on the condition."
# The window defaults to 1800s (two ~15-min deploy cycles). These tests drive
# run_once with a clock relative to the Signal's created_at (its firing-since
# stamp) to land inside / outside the window. alert-severity is never delayed,
# even if its type were registered.


def _seed_config_drift(shared, **overrides):
    """A pod_perms_drift warn Signal — the first auto-remediating member."""
    base = dict(
        signature="pod_perms_drift:pod_perms_drift:pod",
        producer="pod_perms_drift",
        type="pod_perms_drift",
        flavor="maintenance",
        severity="warn",
        scope="pod",
        bot_id=None,
        title="pod perm contract drifted: 3 targets need ensure-pod-perms",
        body="The pod-perms contract has drifted on 3 targets.",
    )
    base.update(overrides)
    return _seed_signal(shared, **base)


def _self_heal_band(shared):
    """(warn delivery-grace, pod_perms_drift self-heal window) in seconds —
    read from the live constants, not hardcoded.

    The two thresholds live in different modules (``alerts.grace`` and
    ``signals.auto_remediating``) and have drifted apart before: #3285 wrote
    these tests against a 240s hardcoded debounce; #3286 generalized it to a
    900s per-severity grace ~14s later and the literals went stale (600s, once
    'past debounce', fell INSIDE the 900s grace → debounced, not suppressed).
    Deriving the firing ages from the source of truth keeps the band-edge tests
    honest across future tuning of either threshold."""
    from evolve_admin.alerts import grace as _grace
    from signals import auto_remediating as _auto_remediating

    grace_warn = _grace.read_grace_seconds_by_severity(shared)["warn"]
    window = _auto_remediating.self_heal_window("pod_perms_drift")
    assert grace_warn < window, (
        "test premise broken: warn grace must sit below the self-heal window "
        "for a 'past debounce, inside window' age to exist "
        f"(grace={grace_warn}, window={window})"
    )
    return grace_warn, window


def test_config_drift_within_self_heal_window_is_not_pushed(env):
    """(a) A config_drift warn that clears inside its self-heal window is never
    pushed — the next deploy's ensure_pod_perms will silently fix it. We fire it
    PAST the warn delivery-grace (so debounce can't be what holds it) but inside
    the self-heal window, so the auto-remediating gate is what suppresses it.
    Age derived from the live constants (see ``_self_heal_band``)."""
    grace_warn, window = _self_heal_band(env["shared"])
    # Midpoint of the (grace, window) band: comfortably past debounce, well
    # inside the window.
    firing_age = (grace_warn + window) // 2

    sig = _seed_config_drift(env["shared"])
    now = datetime.now(timezone.utc) + timedelta(seconds=firing_age)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)

    assert stats.auto_remediating_suppressed == 1
    assert stats.fired == 0
    assert stats.debounced == 0   # debounce passed; the auto-remediating gate held it
    assert env["sent"] == []

    # Delivery-only: we never marked it as alerted (it could still page later
    # once it outlives the window), and we never touched the store JSON.
    assert _state(env["shared"])["signatures"].get(sig.signature) in (None, {})
    found = env["sn"]._signals_store().find_signal(env["shared"], sig.id)
    assert found is not None and found[0].state == "firing"


def test_config_drift_past_self_heal_window_is_pushed(env):
    """(b) A config_drift warn that persists PAST its self-heal window IS pushed
    — the scheduled auto-fix demonstrably failed, so the operator is paged.
    Age derived from the live window (see ``_self_heal_band``) so it stays past
    the boundary even if the window is retuned."""
    _grace_warn, window = _self_heal_band(env["shared"])
    sig = _seed_config_drift(env["shared"])
    # Just past the self-heal window — the auto-fix has demonstrably failed.
    now = datetime.now(timezone.utc) + timedelta(seconds=window + 120)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)

    assert stats.auto_remediating_suppressed == 0
    assert stats.fired == 1
    assert len(env["sent"]) == 1
    assert env["sent"][0]["catalog_event"] == "security.config_drift"
    # Now marked as alerted so it isn't re-pushed next tick.
    entry = _state(env["shared"])["signatures"].get(sig.signature)
    assert entry is not None and entry["alerted_for_signal_id"] == sig.id


def test_alert_severity_pushed_immediately_even_if_type_registered(env):
    """(c) An alert-severity signal is pushed immediately even if its type is
    registered as auto-remediating. alert/critical is NEVER suppressed or
    delayed — the keystone composition invariant. Here we force the registered
    pod_perms_drift type onto an alert-severity signal: it must still page the
    moment it clears debounce, well inside the self-heal window."""
    sig = _seed_config_drift(env["shared"], severity="alert")
    # Only 5 min old — inside the 1800s window, but alert bypasses it.
    now = datetime.now(timezone.utc) + timedelta(seconds=300)
    stats = env["sn"].run_once(env["shared"], env["network"], now=now)

    assert stats.auto_remediating_suppressed == 0
    assert stats.fired == 1
    assert len(env["sent"]) == 1


def test_delivery_recovery_with_empty_backlog_clears_marker_silently(env):
    """If the whole backlog resolved while the target was down, recovery
    sends NO digest (nothing accumulated) — it just clears the marker and
    resumes normal processing. Guards against a pointless 'recovered' ping."""
    p = env["shared"] / "signals" / "notifier-state.json"
    state = json.loads(p.read_text())
    state["delivery_down"] = {"down_since": "2026-06-23T00:00:00+00:00"}
    p.write_text(json.dumps(state))
    # No firing signals at all.
    t = datetime.now(timezone.utc) + timedelta(seconds=300)

    stats = env["sn"].run_once(env["shared"], env["network"], now=t)

    assert env["sent"] == []
    assert stats.delivery_recovered == 0
    assert _state(env["shared"]).get("delivery_down") is None
