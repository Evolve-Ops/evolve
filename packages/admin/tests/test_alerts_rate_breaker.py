"""tests/test_alerts_rate_breaker.py — global outbound circuit-breaker.

Pins the producer-agnostic backstop contract (alerts/rate_breaker.py +
its one call site in dispatcher.send):

  - rolling-window rate cap per channel, across ALL producers
  - over-cap alerts are batched into the daily digest, NEVER dropped
  - exactly ONE "🌊 Alert storm" notice per episode (not per suppressed alert)
  - sustained flood trips digest-only storm mode; auto-resets with
    hysteresis (no flapping)
  - critical/bypass alerts keep delivering during a storm but under their
    own higher secondary ceiling

The dispatcher's openclaw send is faked (no real subprocess); the
contract under test is the breaker decision + the dispatcher's
batch/notice handling.
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


# Small, crisp config so the thresholds are easy to reason about:
#   cap=3, critical ceiling=5, storm trips at 5 arrivals, resets below 2.
def _small_config():
    from evolve_admin.alerts.rate_breaker import BreakerConfig
    return BreakerConfig(
        enabled=True,
        window_seconds=3600,
        max_per_window=3,
        critical_max_per_window=5,
        storm_trip_count=5,
        storm_low_water=2,
        storm_heartbeat_seconds=3600,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    from evolve_admin.alerts import dispatcher, rate_breaker

    shared = tmp_path / "evolve"
    shared.mkdir()

    sent: list[tuple[str, str, str]] = []
    next_result = {"ok": True, "error": None}

    def _fake_dispatch(channel, chat_id, message, gateway_port=None):
        if next_result["ok"]:
            sent.append((channel, chat_id, message))
            return True, None
        return False, next_result["error"] or "fake failure"

    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _fake_dispatch)
    # Force the small config so the dispatcher's breaker call site uses it.
    monkeypatch.setattr(rate_breaker, "_load_config", lambda _sd: _small_config())

    network = {"alerts": {"channel": "telegram", "chatId": "12345"}, "bots": {}}
    return {
        "shared": shared,
        "network": network,
        "dispatcher": dispatcher,
        "rate_breaker": rate_breaker,
        "sent": sent,
        "next_result": next_result,
    }


def _digest_queue(shared) -> list[dict]:
    p = shared / "alerts" / "digest-pending" / "daily.jsonl"
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _storm_notices(sent) -> list[str]:
    return [m for _ch, _id, m in sent if m.startswith("🌊")]


def _base_now() -> datetime:
    return datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


# ── Proof 1: M >> cap → ≤N delivered, rest batched, ONE notice, zero dropped ──


def test_flood_caps_delivery_batches_rest_one_notice_zero_dropped(env):
    D = env["dispatcher"]
    base = _base_now()
    M = 10  # >> cap of 3

    results = []
    for i in range(M):
        out = D.send(
            shared_dir=env["shared"], network=env["network"],
            source="audit", message=f"finding {i}",
            severity=D.Severity.WARNING,
            dedup_key=f"k{i}",                 # unique → no cooldown/identical gate
            now=base + timedelta(seconds=i),    # all inside the 1h window
        )
        results.append(out.result)

    delivered = [r for r in results if r == D.DispatchResult.SENT]
    batched = [r for r in results if r == D.DispatchResult.BATCHED_RATE_CAP]

    # At most the cap (3) delivered immediately.
    assert len(delivered) == 3, results
    # Everything else batched — nothing dropped.
    assert len(batched) == M - 3
    assert len(delivered) + len(batched) == M

    # The batched alerts are accounted for in the daily digest queue.
    queue = _digest_queue(env["shared"])
    assert len(queue) == M - 3
    assert {q["message"] for q in queue} == {f"finding {i}" for i in range(3, M)}

    # Exactly ONE storm notice (episode-start), not one per suppressed alert.
    notices = _storm_notices(env["sent"])
    assert len(notices) == 1, notices
    assert "batching" in notices[0].lower()

    # Recent-messages log records every batched alert (no silent suppression).
    log_dir = env["shared"] / "alerts" / "dispatcher"
    log_lines = []
    for p in log_dir.glob("*.jsonl"):
        log_lines.extend(json.loads(ln) for ln in p.read_text().splitlines() if ln.strip())
    batched_logged = [r for r in log_lines if r.get("result") == "batched_rate_cap"]
    assert len(batched_logged) == M - 3
    assert all(r.get("breaker_state") in ("batching", "storm") for r in batched_logged)


# ── Proof 2: sustained over-threshold trips storm; subsides → auto-reset ──────


def test_storm_trips_then_auto_resets_no_flap(env):
    rb = env["rate_breaker"]
    shared = env["shared"]
    cfg = _small_config()
    base = _base_now()

    # Drive arrivals past the storm trip count (5) within the window.
    last = None
    for i in range(8):
        last = rb.evaluate(
            shared, channel="telegram", is_bypass=False,
            now=base + timedelta(seconds=i), config=cfg,
        )
    assert last.state == "storm"
    assert not last.allow  # digest-only while the flood is sustained

    # Health snapshot surfaces the storm (not silent).
    health = rb.breaker_health(shared, now=base + timedelta(seconds=8), config=cfg)
    assert health["any_storm"] is True
    assert health["channels"]["telegram"]["state"] == "storm"

    # Volume fully subsides: jump past the window so old arrivals age out,
    # then a lone alert sees an empty window (< low_water) and resets.
    later = base + timedelta(hours=2)
    out = rb.evaluate(shared, channel="telegram", is_bypass=False, now=later, config=cfg)
    assert out.state == "normal"
    assert out.allow  # delivers again — episode closed

    health2 = rb.breaker_health(shared, now=later, config=cfg)
    assert health2["any_storm"] is False
    assert health2["channels"]["telegram"]["state"] == "normal"


def test_hysteresis_band_does_not_flap(env):
    """Between low_water (2) and trip (5), an active episode stays active —
    it does NOT bounce back to normal on every dip. Only a drop below
    low_water closes it."""
    rb = env["rate_breaker"]
    shared = env["shared"]
    cfg = _small_config()
    base = _base_now()

    # Trip the storm.
    for i in range(6):
        rb.evaluate(shared, channel="telegram", is_bypass=False,
                    now=base + timedelta(seconds=i), config=cfg)

    # Now an arrival at a moment where arrivals in the trailing window sit
    # in the hysteresis band (above low_water=2, below trip=5). The episode
    # must stay ACTIVE ("batching") — it does NOT bounce back to "normal".
    # (A freed delivery slot may legitimately let one trickle through; the
    # no-flap guarantee is about the episode state, not this single send.)
    t = base + timedelta(seconds=3604)  # earliest arrivals have aged out; a few remain
    out = rb.evaluate(shared, channel="telegram", is_bypass=False, now=t, config=cfg)
    assert out.state == "batching"
    assert out.attempts_count >= cfg.storm_low_water  # still above the reset floor
    assert out.attempts_count < cfg.storm_trip_count   # but below re-trip → no flap


def test_heartbeat_repeats_after_interval_while_episode_active(env):
    """A dropped episode-start notice (or a steadily-batching episode) must
    not go silent forever — a heartbeat re-fires once per interval while the
    episode stays active."""
    from evolve_admin.alerts.rate_breaker import BreakerConfig
    rb = env["rate_breaker"]
    shared = env["shared"]
    # heartbeat (1000s) < window (3600s) so arrivals from episode start are
    # still in-window when the heartbeat interval elapses.
    cfg = BreakerConfig(
        enabled=True, window_seconds=3600, max_per_window=3,
        critical_max_per_window=5, storm_trip_count=5, storm_low_water=2,
        storm_heartbeat_seconds=1000,
    )
    base = _base_now()

    # Trip the storm + emit the one-time start notice.
    notices = []
    for i in range(6):
        out = rb.evaluate(shared, channel="telegram", is_bypass=False,
                          now=base + timedelta(seconds=i), config=cfg)
        if out.storm_notice:
            notices.append(out.storm_notice)
    assert len(notices) == 1  # exactly one episode-start notice so far

    # Same window a few seconds later → no heartbeat yet (interval not elapsed).
    soon = rb.evaluate(shared, channel="telegram", is_bypass=False,
                       now=base + timedelta(seconds=7), config=cfg)
    assert soon.storm_notice is None

    # Past the heartbeat interval, arrivals still keeping the episode active
    # (all within the 3600s window) → a heartbeat fires.
    later = rb.evaluate(shared, channel="telegram", is_bypass=False,
                        now=base + timedelta(seconds=1100), config=cfg)
    assert later.storm_notice is not None
    assert "🌊" in later.storm_notice


def test_catalog_critical_severity_bypasses_even_with_weaker_arg(env, monkeypatch):
    """A catalog event whose own declared severity is CRITICAL must bypass
    the cap even when the caller passes a weaker severity arg."""
    D = env["dispatcher"]

    class _FakeEntry:
        is_safety_critical = False
        severity = D.Severity.CRITICAL

    monkeypatch.setattr("evolve_admin.alerts.catalog.by_key", lambda _k: _FakeEntry())

    # Caller passes only WARNING, but the catalog event is CRITICAL.
    assert D._is_bypass_alert(D.Severity.WARNING, "some.critical_event") is True
    # A non-critical catalog event with a weak arg is NOT a bypass.
    class _Mild:
        is_safety_critical = False
        severity = D.Severity.INFO
    monkeypatch.setattr("evolve_admin.alerts.catalog.by_key", lambda _k: _Mild())
    assert D._is_bypass_alert(D.Severity.WARNING, "some.mild_event") is False


def test_low_water_clamped_below_trip(tmp_path, monkeypatch):
    """A misconfigured storm_low_water >= storm_trip_count is clamped so the
    hysteresis can't invert and wedge a channel in storm mode.

    (Uses the module directly — NOT the env fixture, which stubs out
    _load_config — so the real clamp logic runs.)"""
    from evolve_admin.alerts import rate_breaker as rb
    overrides = {
        "alerts.rate_cap.enabled": True,
        "alerts.rate_cap.storm_trip_count": 10,
        "alerts.rate_cap.storm_low_water": 50,  # >= trip → must be clamped
    }
    monkeypatch.setattr(rb, "_lookup",
                        lambda _sd, path, default: overrides.get(path, default))
    cfg = rb._load_config(tmp_path)
    assert cfg.storm_trip_count == 10
    assert cfg.storm_low_water < cfg.storm_trip_count
    assert cfg.storm_low_water == cfg.storm_trip_count - 1


# ── Proof 3: critical bypass delivers during storm, but is itself ceilinged ───


def test_critical_bypasses_storm_but_has_secondary_ceiling(env):
    D = env["dispatcher"]
    base = _base_now()

    # Trip storm with a flood of non-critical alerts.
    for i in range(8):
        D.send(
            shared_dir=env["shared"], network=env["network"],
            source="audit", message=f"noise {i}",
            severity=D.Severity.WARNING, dedup_key=f"n{i}",
            now=base + timedelta(seconds=i),
        )
    health = env["rate_breaker"].breaker_health(
        env["shared"], now=base + timedelta(seconds=8), config=_small_config(),
    )
    assert health["channels"]["telegram"]["state"] == "storm"

    env["sent"].clear()  # focus on what happens to criticals next

    # CRITICAL alerts bypass the storm — they deliver. critical ceiling = 5.
    crit_results = []
    for i in range(7):  # 7 criticals, ceiling is 5
        out = D.send(
            shared_dir=env["shared"], network=env["network"],
            source="heal", message=f"gateway down {i}",
            severity=D.Severity.CRITICAL, dedup_key=f"c{i}",
            now=base + timedelta(seconds=20 + i),
        )
        crit_results.append(out.result)

    delivered = [r for r in crit_results if r == D.DispatchResult.SENT]
    capped = [r for r in crit_results if r == D.DispatchResult.BATCHED_RATE_CAP]
    # First 5 criticals deliver despite storm; the 6th+ hit the secondary
    # ceiling and batch — even a misfiring "critical" can't firehose.
    assert len(delivered) == 5, crit_results
    assert len(capped) == 2

    # Delivered criticals actually went out on the wire.
    crit_sent = [m for _c, _i, m in env["sent"] if m.startswith("gateway down")]
    assert len(crit_sent) == 5


# ── Disabled / fail-open behavior ─────────────────────────────────────────────


def test_breaker_disabled_is_passthrough(env, monkeypatch):
    rb = env["rate_breaker"]
    from evolve_admin.alerts.rate_breaker import BreakerConfig

    disabled = BreakerConfig(
        enabled=False, window_seconds=3600, max_per_window=3,
        critical_max_per_window=5, storm_trip_count=5, storm_low_water=2,
        storm_heartbeat_seconds=3600,
    )
    monkeypatch.setattr(rb, "_load_config", lambda _sd: disabled)
    D = env["dispatcher"]
    base = _base_now()

    results = [
        D.send(
            shared_dir=env["shared"], network=env["network"],
            source="audit", message=f"m{i}", dedup_key=f"k{i}",
            now=base + timedelta(seconds=i),
        ).result
        for i in range(10)
    ]
    # All delivered — the breaker is off, so the cap doesn't apply.
    assert all(r == D.DispatchResult.SENT for r in results)
    assert _digest_queue(env["shared"]) == []


def test_evaluate_fails_open_on_state_error(env, monkeypatch):
    rb = env["rate_breaker"]
    cfg = _small_config()
    # Make the state read explode — the breaker must fail open, not block.
    monkeypatch.setattr(rb, "_load_state", lambda _sd: (_ for _ in ()).throw(RuntimeError("boom")))
    out = rb.evaluate(env["shared"], channel="telegram", is_bypass=False,
                      now=_base_now(), config=cfg)
    assert out.allow is True
    assert out.state == "disabled"


# ── Unit: per-channel independence ────────────────────────────────────────────


def test_cap_is_per_channel(env):
    rb = env["rate_breaker"]
    shared = env["shared"]
    cfg = _small_config()
    base = _base_now()

    # Fill telegram to its cap.
    for i in range(3):
        out = rb.evaluate(shared, channel="telegram", is_bypass=False,
                          now=base + timedelta(seconds=i), config=cfg)
        assert out.allow
    # 4th telegram batches.
    assert not rb.evaluate(shared, channel="telegram", is_bypass=False,
                           now=base + timedelta(seconds=4), config=cfg).allow
    # A different channel is unaffected.
    out_slack = rb.evaluate(shared, channel="slack", is_bypass=False,
                            now=base + timedelta(seconds=5), config=cfg)
    assert out_slack.allow
