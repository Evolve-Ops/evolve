"""tests/test_alerts_flapper.py — recurring-flapper demotion.

Pins the multi-hour-flap layer ABOVE the per-severity grace
(alerts/flapper.py + its one call site in dispatcher.send), workstream D3 of
internal/spec-subscription-digest-default-2026-06-28.md.

Contract under test:
  - a signature that oscillates ``count`` times in the window is demoted to the
    daily digest on the K-th fire (the residual the grace + flap-window layers
    structurally cannot catch);
  - alert-severity is exempt — a must-page condition is never demoted;
  - the demotion self-lifts after the signature is stable for the cooldown;
  - while demoted, standalone clears are taken off the immediate-push path;
  - the demotion is visible (flap_health snapshot) — not silently quiet.

Unit tests drive flapper.evaluate directly; the integration tests drive it
through dispatcher.send with the dispatcher's openclaw send faked.
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


# Small, crisp config: demote on the 4th oscillation within 24h; self-lift
# after 6h of no fire.
def _small_config():
    from evolve_admin.alerts.flapper import FlapperConfig
    return FlapperConfig(
        enabled=True,
        flap_count=4,
        window_seconds=86_400,
        cooldown_seconds=21_600,  # 6h
    )


def _base_now() -> datetime:
    return datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def shared(tmp_path):
    s = tmp_path / "evolve"
    s.mkdir()
    return s


# ── Unit: the 4th oscillation demotes ────────────────────────────────────────


def test_fourth_flap_event_demotes(shared):
    """Fires and clears both count as flap events (each is one operator
    push): the K-th event — here the 2nd cycle's clear — crosses the
    threshold, and everything after routes to the digest."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    base = _base_now()
    sig = "pod_perms_drift:security.config_drift:evo-vps"

    results = []
    for cycle in range(3):
        t_fire = base + timedelta(hours=2 * cycle)        # ~2h apart, inside 24h
        fire = flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="fire", severity="warn", now=t_fire, config=cfg,
        )
        # clear ~15 min later (each cycle self-heals)
        clear = flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="resolve", severity="warn",
            now=t_fire + timedelta(minutes=15), config=cfg,
        )
        results.append((fire, clear))

    # Cycle 1 (events 1-2) and cycle 2's fire (event 3) page normally.
    assert results[0][0].action == flapper.ACTION_PASS
    assert results[0][1].action == flapper.ACTION_PASS
    assert results[1][0].action == flapper.ACTION_PASS
    # Cycle 2's clear is the 4th push in the window — demotion trips there,
    # exactly once.
    assert results[1][1].action == flapper.ACTION_SUPPRESS_CLEAR
    assert results[1][1].newly_demoted is True
    # Cycle 3 is fully quiet: fire deferred, clear suppressed.
    assert results[2][0].action == flapper.ACTION_DEMOTE_FIRE
    assert results[2][0].newly_demoted is False
    assert results[2][1].action == flapper.ACTION_SUPPRESS_CLEAR

    # A later fire while still inside the window stays demoted.
    later = flapper.evaluate(
        shared, source="signal_notifier", coalesce_key=sig,
        kind="fire", severity="warn",
        now=base + timedelta(hours=8), config=cfg,
    )
    assert later.action == flapper.ACTION_DEMOTE_FIRE
    assert later.newly_demoted is False
    assert later.demoted is True


def test_fourth_fire_without_clears_demotes(shared):
    """A signature that only fires (no announced clears) still demotes on
    the K-th fire — the pre-V-1 shape (e.g. a tight producer retry loop)."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    base = _base_now()
    sig = "repo_puller_sudoers:sudoers_refresh_failed:evolve"

    actions = []
    for cycle in range(4):
        out = flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="fire", severity="warn",
            now=base + timedelta(minutes=cycle), config=cfg,
        )
        actions.append(out.action)
    assert actions[:3] == [flapper.ACTION_PASS] * 3, actions
    assert actions[3] == flapper.ACTION_DEMOTE_FIRE, actions


def test_three_cycles_do_not_demote(shared):
    """A genuine fire-clear-fire blip (< K cycles) keeps paging — the layer
    only bites on the recurring case."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    base = _base_now()
    sig = "audit:security.audit_finding:bot_a"

    for cycle in range(3):
        out = flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="fire", severity="warn",
            now=base + timedelta(hours=2 * cycle), config=cfg,
        )
        assert out.action == flapper.ACTION_PASS
        assert out.demoted is False


# ── Unit: alert-severity is never demoted ────────────────────────────────────


def test_alert_severity_never_demotes(shared):
    """A must-page (alert) condition is exempt: no matter how often it
    oscillates, it never demotes and never has a clear suppressed."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    base = _base_now()
    sig = "pod_health:system.gateway_state_change:bot_b"

    for cycle in range(8):  # well past the threshold
        fire = flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="fire", severity="alert",
            now=base + timedelta(hours=cycle), config=cfg,
        )
        assert fire.action == flapper.ACTION_PASS
        assert fire.demoted is False
        assert fire.reason == "alert_exempt"
        resolve = flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="resolve", severity="alert",
            now=base + timedelta(hours=cycle, minutes=15), config=cfg,
        )
        assert resolve.action == flapper.ACTION_PASS

    # Never recorded as demoted anywhere.
    health = flapper.flap_health(shared, now=base + timedelta(hours=8), config=cfg)
    assert health["any_demoted"] is False
    assert health["demoted"] == []


# ── Unit: clears are suppressed while demoted ────────────────────────────────


def test_clears_suppressed_while_demoted(shared):
    """Once demoted, a resolve for the signature is taken off the
    immediate-push path (ACTION_SUPPRESS_CLEAR)."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    base = _base_now()
    sig = "pod_perms_drift:security.config_drift:evo-vps"

    # Drive to demotion (4 fires).
    for cycle in range(4):
        flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="fire", severity="warn",
            now=base + timedelta(hours=2 * cycle), config=cfg,
        )

    clear = flapper.evaluate(
        shared, source="signal_notifier", coalesce_key=sig,
        kind="resolve", severity="warn",
        now=base + timedelta(hours=6, minutes=15), config=cfg,
    )
    assert clear.action == flapper.ACTION_SUPPRESS_CLEAR
    assert clear.demoted is True


# ── Unit: the demotion self-lifts after a stable cooldown ────────────────────


def test_demotion_self_lifts_after_quiet_window(shared):
    """The lift needs BOTH the stability cooldown AND a drained window.

    A gap merely longer than the cooldown — the natural overnight/mid-day
    pause of a cluster-cadence flapper — must NOT lift the demotion while
    the 24h window still holds a threshold's worth of flap events (the V-1
    hole: the original lift fired on the gap alone and wiped the window, so
    every daily flapper was amnestied by its own rhythm). Once the window
    has genuinely drained, the next fire lifts and pages normally."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    base = _base_now()
    sig = "pod_perms_drift:security.config_drift:evo-vps"

    # Demote (4 fires, last at base+6h).
    for cycle in range(4):
        flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="fire", severity="warn",
            now=base + timedelta(hours=2 * cycle), config=cfg,
        )
    health = flapper.flap_health(shared, now=base + timedelta(hours=6), config=cfg)
    assert health["any_demoted"] is True

    # A fire 7h after the last one: past the 6h cooldown, but the window
    # still holds the 4 earlier fires — stays demoted, no page.
    last_fire = base + timedelta(hours=6)
    still = flapper.evaluate(
        shared, source="signal_notifier", coalesce_key=sig,
        kind="fire", severity="warn",
        now=last_fire + timedelta(hours=7), config=cfg,
    )
    assert still.lifted is False
    assert still.demoted is True
    assert still.action == flapper.ACTION_DEMOTE_FIRE

    # A fire 25h after THAT one: the window has drained (everything pruned)
    # and the signature has been stable well past the cooldown → lift, page.
    lifted_fire = flapper.evaluate(
        shared, source="signal_notifier", coalesce_key=sig,
        kind="fire", severity="warn",
        now=last_fire + timedelta(hours=7 + 25), config=cfg,
    )
    assert lifted_fire.lifted is True
    assert lifted_fire.demoted is False
    assert lifted_fire.action == flapper.ACTION_PASS
    # Drained window — this lone fire is the only event counted.
    assert lifted_fire.flap_count == 1

    health2 = flapper.flap_health(
        shared, now=last_fire + timedelta(hours=32), config=cfg,
    )
    assert health2["any_demoted"] is False


# ── Regression: the live evo-vps daemon_error_spike cadence (V-1) ────────────


def _live_cadence_events(day0: datetime) -> list[tuple[datetime, str]]:
    """The verified live pattern (dispatcher JSONL, evo-vps 2026-08-26 →
    09-01): fire/clear pairs 45min apart, clustered mornings + evenings with
    >6h natural gaps, 3-4 episodes/day, every day."""
    fire_offsets = [  # hours from day0, per fire; clear follows 45min later
        3.94, 6.44, 8.46, 20.63,          # day 1 (03:56, 06:26, 08:27, 20:37)
        26.42, 30.1, 44.5,                # day 2 (02:25, 06:06, 20:30)
        51.9, 55.2, 68.3,                 # day 3
    ]
    events: list[tuple[datetime, str]] = []
    for off in fire_offsets:
        t = day0 + timedelta(hours=off)
        events.append((t, "fire"))
        events.append((t + timedelta(minutes=45), "resolve"))
    return sorted(events)


def test_live_daily_flapper_demotes_within_first_day_and_stays_quiet(shared):
    """The exact pattern that escaped D3 on the live pod: a warn-severity
    source flapping in clusters with >6h gaps must demote within the first
    24h and then stop pushing entirely — including across the gaps that used
    to self-lift the demotion and wipe the accounting."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    day0 = _base_now()
    sig = "error_reporter:error_spike:admin:739f435eff3928d4"

    passed, quieted = [], []
    for when, kind in _live_cadence_events(day0):
        out = flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind=kind, severity="warn", now=when, config=cfg,
        )
        (passed if out.action == flapper.ACTION_PASS else quieted).append(
            (when, kind, out),
        )

    # Demotion trips within the first day — on the 4th push (2nd episode's
    # clear), ~7h in — never later.
    assert quieted, "the live cadence must demote"
    first_quiet = quieted[0][0]
    assert first_quiet - day0 <= timedelta(hours=24)
    # At most 3 pushes ever reach the operator; nothing passes after the
    # demotion trips (the old lift-on-gap would have re-paged every cluster).
    assert len(passed) == 3, [(t.isoformat(), k) for t, k, _ in passed]
    assert all(t < first_quiet for t, _, _ in passed)
    # The >6h gaps do NOT lift while the window is still full: every fire
    # after demotion defers, every clear suppresses.
    for _t, kind, out in quieted:
        assert out.action == (
            flapper.ACTION_DEMOTE_FIRE if kind == "fire"
            else flapper.ACTION_SUPPRESS_CLEAR
        )
        assert out.lifted is False


def test_new_distinct_error_source_still_pages(shared):
    """Demoting one flapping signature must not quiet a genuinely new,
    distinct error source — each signature keeps its own window."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    day0 = _base_now()

    # Drive the known flapper into demotion.
    for when, kind in _live_cadence_events(day0)[:8]:
        flapper.evaluate(
            shared, source="signal_notifier",
            coalesce_key="error_reporter:error_spike:admin:739f435eff3928d4",
            kind=kind, severity="warn", now=when, config=cfg,
        )

    # A brand-new fingerprint fires: it pages normally.
    fresh = flapper.evaluate(
        shared, source="signal_notifier",
        coalesce_key="error_reporter:error_spike:admin:aaaaaaaaaaaaaaaa",
        kind="fire", severity="warn",
        now=day0 + timedelta(hours=21), config=cfg,
    )
    assert fresh.action == flapper.ACTION_PASS
    assert fresh.demoted is False
    assert fresh.flap_count == 1


def test_live_flapper_self_lifts_once_actually_fixed(shared):
    """When the flapping condition is genuinely fixed, the demotion lifts
    after the window drains and the next (real, new) fire pages again."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    day0 = _base_now()
    sig = "error_reporter:error_spike:admin:739f435eff3928d4"

    events = _live_cadence_events(day0)
    for when, kind in events:
        flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind=kind, severity="warn", now=when, config=cfg,
        )
    last = events[-1][0]

    # 30h of true quiet — longer than the 24h window — then a fresh fire.
    reborn = flapper.evaluate(
        shared, source="signal_notifier", coalesce_key=sig,
        kind="fire", severity="warn",
        now=last + timedelta(hours=30), config=cfg,
    )
    assert reborn.lifted is True
    assert reborn.action == flapper.ACTION_PASS
    assert reborn.flap_count == 1


def test_legacy_fires_state_is_carried_forward(shared):
    """Pre-V-1 state files keyed the window as "fires"; they are read as the
    event window and migrated on the next write."""
    import json as _json

    from evolve_admin.alerts import flapper

    cfg = _small_config()
    base = _base_now()
    sig = "error_reporter:error_spike:admin:739f435eff3928d4"
    key = f"signal_notifier\x1f{sig}"

    state_path = shared / "alerts" / "flap-demotion-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (base - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    state_path.write_text(_json.dumps({
        "version": 1,
        "signatures": {key: {
            "fires": [stamp, stamp, stamp],
            "demoted": False,
            "last_fire_at": stamp,
            "severity": "warn",
            "demote_count": 0,
        }},
    }))

    out = flapper.evaluate(
        shared, source="signal_notifier", coalesce_key=sig,
        kind="fire", severity="warn", now=base, config=cfg,
    )
    # 3 legacy fires + this one = 4 → demoted; the legacy key is migrated.
    assert out.action == flapper.ACTION_DEMOTE_FIRE
    assert out.flap_count == 4
    persisted = _json.loads(state_path.read_text())["signatures"][key]
    assert "fires" not in persisted
    assert len(persisted["events"]) == 4


def test_flap_health_surfaces_demotion(shared):
    """flap_health exposes the demoted signature so a quiet phone is explained
    by visible state, not silent suppression."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    base = _base_now()
    sig = "pod_perms_drift:security.config_drift:evo-vps"

    for cycle in range(4):
        flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="fire", severity="warn",
            now=base + timedelta(hours=2 * cycle), config=cfg,
        )

    health = flapper.flap_health(shared, now=base + timedelta(hours=6), config=cfg)
    assert health["any_demoted"] is True
    assert len(health["demoted"]) == 1
    row = health["demoted"][0]
    assert row["source"] == "signal_notifier"
    assert row["coalesce_key"] == sig
    assert row["severity"] == "warn"
    assert row["demoted_since"] is not None


def test_disabled_is_a_no_op(shared):
    """With the master switch off, nothing is ever demoted."""
    from evolve_admin.alerts.flapper import FlapperConfig, evaluate, ACTION_PASS

    cfg = FlapperConfig(
        enabled=False, flap_count=4, window_seconds=86_400, cooldown_seconds=21_600,
    )
    base = _base_now()
    sig = "x:y:z"
    for cycle in range(10):
        out = evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="fire", severity="warn",
            now=base + timedelta(hours=cycle), config=cfg,
        )
        assert out.action == ACTION_PASS
        assert out.demoted is False


# ── Integration: through dispatcher.send ─────────────────────────────────────


@pytest.fixture
def disp_env(tmp_path, monkeypatch):
    from evolve_admin.alerts import dispatcher, flapper

    shared = tmp_path / "evolve"
    shared.mkdir()

    sent: list[tuple[str, str, str]] = []

    def _fake_dispatch(channel, chat_id, message, gateway_port=None):
        sent.append((channel, chat_id, message))
        return True, None

    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _fake_dispatch)
    # Force the small flapper config at the dispatcher's call site.
    monkeypatch.setattr(flapper, "_load_config", lambda _sd: _small_config())

    network = {"alerts": {"channel": "telegram", "chatId": "12345"}, "bots": {}}
    return {
        "shared": shared,
        "network": network,
        "dispatcher": dispatcher,
        "flapper": flapper,
        "sent": sent,
    }


def _digest_queue(shared) -> list[dict]:
    p = shared / "alerts" / "digest-pending" / "daily.jsonl"
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _fire_meta(sig: str, severity: str = "warn") -> dict:
    return {"coalesce_key": sig, "kind": "fire", "signal_severity": severity}


def _resolve_meta(sig: str, severity: str = "warn") -> dict:
    return {"coalesce_key": sig, "kind": "resolve", "signal_severity": severity}


def test_dispatcher_demotes_flapping_fire_to_digest(disp_env):
    D = disp_env["dispatcher"]
    shared = disp_env["shared"]
    base = _base_now()
    sig = "pod_perms_drift:security.config_drift:evo-vps"

    results = []
    for cycle in range(4):
        out = D.send(
            shared_dir=shared, network=disp_env["network"],
            source="signal_notifier", message=f"perm drift cycle {cycle}",
            severity=D.Severity.WARNING,
            dedup_key=sig,
            now=base + timedelta(hours=2 * cycle),
            digest_meta=_fire_meta(sig),
        )
        results.append(out.result)

    # First three page (faked SENT); the 4th is demoted to the digest.
    assert results[:3] == [D.DispatchResult.SENT] * 3, results
    assert results[3] == D.DispatchResult.DEFERRED, results

    # The demoted fire lands in the daily digest queue (never dropped).
    queue = _digest_queue(shared)
    assert any(q["message"] == "perm drift cycle 3" for q in queue)

    # The demotion is logged with its flap fields (visible, not silently
    # quiet). It rides the QUEUED lane, not the sent lane — a demoted fire
    # was enqueued to the digest, so it is not "what your phone received"
    # (spec-delta-digest-audit-noise-2026-08-25 D2). The always-visible
    # explanation is flap_health on the Dispatcher Health panel, asserted
    # below; these per-fire rows are one "include queued" click away.
    log_lines = []
    for p in (shared / "alerts" / "dispatcher-queued").glob("*.jsonl"):
        log_lines.extend(json.loads(ln) for ln in p.read_text().splitlines() if ln.strip())
    demoted_logged = [r for r in log_lines if r.get("flap_demoted")]
    assert len(demoted_logged) == 1
    assert demoted_logged[0]["flap_action"] == "demote_fire"
    assert demoted_logged[0]["flap_newly_demoted"] is True
    sent_lane_dir = shared / "alerts" / "dispatcher"
    sent_lane = [
        json.loads(ln)
        for f in sent_lane_dir.glob("*.jsonl")
        for ln in f.read_text().splitlines() if ln.strip()
    ]
    assert not any(r.get("flap_demoted") for r in sent_lane)

    # And flap_health surfaces it for the Reports panel.
    health = disp_env["flapper"].flap_health(
        shared, now=base + timedelta(hours=6), config=_small_config(),
    )
    assert health["any_demoted"] is True


def test_dispatcher_suppresses_clear_while_demoted(disp_env):
    D = disp_env["dispatcher"]
    shared = disp_env["shared"]
    base = _base_now()
    sig = "pod_perms_drift:security.config_drift:evo-vps"

    # Drive to demotion.
    for cycle in range(4):
        D.send(
            shared_dir=shared, network=disp_env["network"],
            source="signal_notifier", message=f"drift {cycle}",
            severity=D.Severity.WARNING, dedup_key=sig,
            now=base + timedelta(hours=2 * cycle),
            digest_meta=_fire_meta(sig),
        )
    sent_before = len(disp_env["sent"])

    # A resolve while demoted is taken off the immediate-push path.
    out = D.send(
        shared_dir=shared, network=disp_env["network"],
        source="signal_notifier", message="🟢 Cleared: perm drift",
        severity=D.Severity.INFO, dedup_key=None,
        now=base + timedelta(hours=6, minutes=15),
        digest_meta=_resolve_meta(sig),
    )
    assert out.result == D.DispatchResult.DEFERRED
    # No immediate chat push happened for the clear.
    assert len(disp_env["sent"]) == sent_before


def test_dispatcher_never_demotes_alert_severity(disp_env):
    D = disp_env["dispatcher"]
    shared = disp_env["shared"]
    base = _base_now()
    sig = "pod_health:system.gateway_state_change:bot_b"

    results = []
    for cycle in range(8):
        out = D.send(
            shared_dir=shared, network=disp_env["network"],
            source="signal_notifier", message=f"gateway down {cycle}",
            severity=D.Severity.ERROR,
            dedup_key=f"{sig}:{cycle}",  # distinct so cooldown never bites
            now=base + timedelta(hours=cycle),
            digest_meta=_fire_meta(sig, severity="alert"),
        )
        results.append(out.result)

    # Every alert-severity fire pages (faked SENT) — none demoted to digest.
    assert all(r == D.DispatchResult.SENT for r in results), results
    assert _digest_queue(shared) == []


# ── D1: the demotion lane is cooldown-gated, THROUGH send ────────────────────
#
# spec-delta-digest-audit-noise-2026-08-25 D1 gave ``_demote_flapper`` the same
# repeat gate as the subscription-digest short-circuit. test_alerts_dispatcher
# pins the helper directly, calling ``_demote_flapper`` with hand-supplied
# ``body_hash`` / ``effective_cooldown``. That proves the helper gates; it
# cannot prove ``send`` HANDS IT the right two values — a call site passing
# ``effective_cooldown=0``, or a hash of something other than the final body,
# would leave those tests green and the live lane ungated. These drive the
# whole path (flapper.evaluate → demotion → gate) so the wiring is pinned too.
#
# Ported from PR #3801, closed as superseded by #3802 (429a9123).


def _drive_to_demotion(disp_env, sig, *, base, cooldown_seconds, cycles=4):
    """Fire ``cycles`` times, 2h apart, until _small_config demotes on the 4th.

    2h spacing keeps every fire outside the cooldowns used below, so the
    pre-demotion fires page normally and the anchor under test is the one the
    DEMOTION wrote.
    """
    D = disp_env["dispatcher"]
    out = []
    for cycle in range(cycles):
        out.append(D.send(
            shared_dir=disp_env["shared"], network=disp_env["network"],
            source="signal_notifier", message=f"perm drift cycle {cycle}",
            severity=D.Severity.WARNING, dedup_key=sig,
            cooldown_seconds=cooldown_seconds,
            now=base + timedelta(hours=2 * cycle),
            digest_meta=_fire_meta(sig),
        ))
    return out


def test_demoted_fire_repeat_within_cooldown_is_suppressed_not_requeued(disp_env):
    """A demoted signature re-firing inside its cooldown lands in the
    suppressed lane instead of appending another copy of a queue line that
    ``digest_dispatcher._dedup_records`` collapses at flush anyway.

    This is the SUPPRESSED_COOLDOWN arm on the demotion path — the bodies
    drift per tick, so the 24h identical-content floor cannot claim these
    (``signal_notifier`` is STATE_TRACKED and opts out of that floor
    entirely). Of the three dispatcher-side demotion tests, one lands on
    SUPPRESSED_IDENTICAL and two never suppress at all (fresh key /
    ``dedup_key=None``) — so without this the cooldown arm of the demotion
    gate is unexercised.
    """
    D = disp_env["dispatcher"]
    shared = disp_env["shared"]
    base = _base_now()
    sig = "pod_perms_drift:security.config_drift:evo-vps"

    driven = _drive_to_demotion(disp_env, sig, base=base, cooldown_seconds=3600)
    assert driven[3].result == D.DispatchResult.DEFERRED, [r.result for r in driven]
    assert len(_digest_queue(shared)) == 1          # the demoted 4th fire

    # Now the demoted signature keeps flapping every 5 minutes.
    repeats = [
        D.send(
            shared_dir=shared, network=disp_env["network"],
            source="signal_notifier", message=f"perm drift retick {i}",
            severity=D.Severity.WARNING, dedup_key=sig,
            cooldown_seconds=3600,
            now=base + timedelta(hours=6, minutes=5 * i),
            digest_meta=_fire_meta(sig),
        )
        for i in range(1, 6)
    ]
    assert [r.result for r in repeats] == [
        D.DispatchResult.SUPPRESSED_COOLDOWN
    ] * 5, [r.result for r in repeats]
    # The elapsed values prove the gate read the anchor the DEMOTION wrote
    # (base+6h), not the last paged fire — and that send handed the demotion
    # path a non-zero cooldown.
    assert [r.error for r in repeats] == [
        f"cooldown:{300 * i}s" for i in range(1, 6)
    ]

    # Nothing new queued, nothing paged…
    assert len(_digest_queue(shared)) == 1
    assert not any("retick" in m for _, _, m in disp_env["sent"])
    # …and every suppressed tick is still LOGGED — the D1 invariant is
    # "degrade to visible-but-quiet", never to silence.
    supp = []
    for p in (shared / "alerts" / "dispatcher-suppressed").glob("*.jsonl"):
        supp.extend(
            json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()
        )
    assert len(supp) == 5
    assert {r["result"] for r in supp} == {"suppressed_cooldown"}
    assert all(r["dedup_key"] == sig for r in supp)


def test_demoted_clear_is_not_gated_by_the_live_fire_anchor(disp_env):
    """Invariant 1 on the demotion path, with the fire's anchor LIVE.

    ``signal_notifier``'s resolve send passes ``dedup_key=None``, so a
    demoted clear has no cooldown identity and must keep flowing — the L1
    fire/clear pairing in the digest keeps its mate. The dispatcher-side
    ``test_flap_demotion_without_dedup_key_still_queues_every_tick`` pins the
    None bypass against an EMPTY state file; this one fires the clears while
    the same signature's fire anchor is recorded and well inside its
    cooldown, so a future "dedup the clears too" change that keyed the gate
    on ``digest_meta['coalesce_key']`` when ``dedup_key`` is None would be
    caught here rather than in production.
    """
    D = disp_env["dispatcher"]
    shared = disp_env["shared"]
    base = _base_now()
    sig = "pod_perms_drift:security.config_drift:evo-vps"

    driven = _drive_to_demotion(disp_env, sig, base=base, cooldown_seconds=3600)
    assert driven[3].result == D.DispatchResult.DEFERRED
    queued_before = len(_digest_queue(shared))
    # The demotion at base+6h stamped an anchor, and the clears below all
    # fall inside its 1h cooldown — so a gate that consulted anything for a
    # ``dedup_key=None`` caller would have a live entry to trip over.
    state = json.loads(
        (shared / "alerts" / "dispatcher-state.json").read_text()
    )["last_dispatch"]
    assert state[f"signal_notifier::{sig}"]["result"] == "deferred"

    clears = [
        D.send(
            shared_dir=shared, network=disp_env["network"],
            source="signal_notifier", message="🟢 Cleared: perm drift",
            severity=D.Severity.INFO, dedup_key=None,
            now=base + timedelta(hours=6, minutes=5 * i),
            digest_meta=_resolve_meta(sig),
        )
        for i in range(3)
    ]
    assert all(c.result == D.DispatchResult.DEFERRED for c in clears), [
        c.result for c in clears
    ]
    assert len(_digest_queue(shared)) == queued_before + 3
