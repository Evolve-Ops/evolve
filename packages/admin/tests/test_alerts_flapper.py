"""tests/test_alerts_flapper.py — recurring-flapper demotion.

Pins the multi-hour-flap layer ABOVE the per-severity grace
(alerts/flapper.py + its one call site in dispatcher.send), workstream D3 of
docs/spec-subscription-digest-default-2026-06-28.md.

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


def test_fourth_flap_demotes(shared):
    """Three fire/clear cycles page normally; the 4th fire crosses the
    threshold and is demoted to the digest."""
    from evolve_admin.alerts import flapper

    cfg = _small_config()
    base = _base_now()
    sig = "pod_perms_drift:security.config_drift:evo-vps"

    actions = []
    for cycle in range(4):
        t_fire = base + timedelta(hours=2 * cycle)        # ~2h apart, inside 24h
        fire = flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="fire", severity="warn", now=t_fire, config=cfg,
        )
        actions.append(fire.action)
        # clear ~15 min later (each cycle self-heals)
        flapper.evaluate(
            shared, source="signal_notifier", coalesce_key=sig,
            kind="resolve", severity="warn",
            now=t_fire + timedelta(minutes=15), config=cfg,
        )

    # First three fires pass; the 4th demotes.
    assert actions[:3] == [flapper.ACTION_PASS] * 3, actions
    assert actions[3] == flapper.ACTION_DEMOTE_FIRE, actions

    # The crossing fire reports newly_demoted exactly once.
    fourth = flapper.evaluate(
        shared, source="signal_notifier", coalesce_key=sig,
        kind="fire", severity="warn",
        now=base + timedelta(hours=8), config=cfg,
    )
    assert fourth.action == flapper.ACTION_DEMOTE_FIRE
    assert fourth.newly_demoted is False  # already demoted by the 4th cycle
    assert fourth.demoted is True


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


def test_demotion_self_lifts_after_stable_cooldown(shared):
    """After the signature goes quiet for the cooldown, the next fire lifts the
    demotion and pages normally again (window reset to a fresh accounting)."""
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

    # A fire 7h after the last one (> 6h cooldown of stability) self-lifts.
    last_fire = base + timedelta(hours=6)
    lifted_fire = flapper.evaluate(
        shared, source="signal_notifier", coalesce_key=sig,
        kind="fire", severity="warn",
        now=last_fire + timedelta(hours=7), config=cfg,
    )
    assert lifted_fire.lifted is True
    assert lifted_fire.demoted is False
    assert lifted_fire.action == flapper.ACTION_PASS
    # Window reset — this lone fire is the only one counted.
    assert lifted_fire.flap_count == 1

    health2 = flapper.flap_health(
        shared, now=last_fire + timedelta(hours=7), config=cfg,
    )
    assert health2["any_demoted"] is False


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

    # Recent-messages log records the demotion with flap fields (visible, not
    # silently quiet).
    log_lines = []
    for p in (shared / "alerts" / "dispatcher").glob("*.jsonl"):
        log_lines.extend(json.loads(ln) for ln in p.read_text().splitlines() if ln.strip())
    demoted_logged = [r for r in log_lines if r.get("flap_demoted")]
    assert len(demoted_logged) == 1
    assert demoted_logged[0]["flap_action"] == "demote_fire"
    assert demoted_logged[0]["flap_newly_demoted"] is True

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
