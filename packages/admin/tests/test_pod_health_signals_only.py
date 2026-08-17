"""tests/test_pod_health_signals_only.py — high-cadence liveness runner.

`run_pod_health_signals_only` is the entrypoint for the 1-min
`ai.evolve.evolve.pod-health` LaunchDaemon (alert-notifier Phase 0a).
It runs the gateway probe and the repo-puller freshness check, then
writes/sweeps pod_health_gateways and pod_health_repo_puller Signals;
a partial-coverage sweep must not auto-resolve Signals from other
pod_health categories the runner did not re-check.

Test pins:
  - a failing gateway emits a firing Signal of type pod_health_gateways
  - when the gateway recovers, the firing Signal is auto-resolved
  - a pre-existing pod_health Signal of a *different* type
    (e.g. pod_health_launchd) is untouched by the scoped sweep
  - a stale repo-puller log emits a pod_health_repo_puller Signal
  - when the puller log goes fresh, the firing puller Signal resolves
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


def _write_network(tmp_path: Path, shared_dir: Path) -> Path:
    network = {
        "networkId": "test",
        "members": ["team_bot_a", "team_bot_b"],
        "sharedDir": str(shared_dir),
        "bots": {
            "team_bot_a":  {"port": 18789, "user": "team_bot_a"},
            "team_bot_b": {"port": 18790, "user": "team_bot_b"},
        },
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    return p


@pytest.fixture
def signals_env(tmp_path, monkeypatch):
    """Set up tmp_path/evolve as shared_dir + return (network_path, gateway_state).

    `gateway_state` is a mutable dict[bot_id_or_admin -> bool] that controls
    which gateways pass the probe. Tests flip values between calls to
    simulate down→up transitions.
    """
    shared = tmp_path / "evolve"
    (shared / "signals" / "firing").mkdir(parents=True)
    (shared / "signals" / "snoozed").mkdir(parents=True)
    (shared / "signals" / "archived").mkdir(parents=True)
    network_path = _write_network(tmp_path, shared)

    # Default: everything passes. Tests override before invoking.
    state: dict[str, bool] = {"team_bot_a": True, "team_bot_b": True, "admin_ui": True}

    def _fake_http_ok(url: str, timeout: int = 3) -> bool:
        # _check_gateways probes f"http://127.0.0.1:{port}/evolve/status" per bot
        # and "http://127.0.0.1:5050/api/health" for admin UI.
        if "5050" in url:
            return state["admin_ui"]
        if "18789" in url:
            return state["team_bot_a"]
        if "18790" in url:
            return state["team_bot_b"]
        return False

    from evolve_admin import health as _health
    monkeypatch.setattr(_health, "_http_ok", _fake_http_ok)
    # _check_users -> dscl/getent calls in CI fail; we don't run that path
    # because run_pod_health_signals_only doesn't call _check_users. But
    # _flag_urgent_refresh writes to shared_dir which we already created.

    # Inject an empty FakeScheduler so _check_gateway_supervision never shells
    # out to a real launchctl/systemctl in tests. Gateway-supervision tests
    # seed gateways onto it via get_scheduler(); the rest get an empty list
    # (no findings), exactly as a healthy pod with no crash-loops would.
    import runtime.scheduler as _sched_mod
    _sched_mod.set_scheduler(_sched_mod.FakeScheduler())
    try:
        yield network_path, shared, state
    finally:
        _sched_mod.set_scheduler(None)


def test_failing_gateway_emits_firing_signal(signals_env):
    network_path, shared, state = signals_env
    from evolve_admin.health import run_pod_health_signals_only
    import signals.store as signals_store

    state["team_bot_a"] = False  # team_bot_a gateway is down
    run_pod_health_signals_only(network_path=network_path)

    firing = list(signals_store.iter_signals(shared, subdirs=("firing",)))
    team_bot_a_sigs = [
        s for s in firing
        if s.producer == "pod_health"
        and s.type == "pod_health_gateways"
        and s.bot_id == "team_bot_a"
    ]
    assert len(team_bot_a_sigs) == 1, f"expected one firing team_bot_a gateway Signal, got: {team_bot_a_sigs}"
    assert team_bot_a_sigs[0].severity == "alert"


def test_recovered_gateway_is_auto_resolved(signals_env):
    network_path, shared, state = signals_env
    from evolve_admin.health import run_pod_health_signals_only
    import signals.store as signals_store

    # First tick: team_bot_a down — fires.
    state["team_bot_a"] = False
    run_pod_health_signals_only(network_path=network_path)
    firing_after_down = list(signals_store.iter_signals(shared, subdirs=("firing",)))
    team_bot_a_firing = [
        s for s in firing_after_down
        if s.bot_id == "team_bot_a" and s.type == "pod_health_gateways"
    ]
    assert len(team_bot_a_firing) == 1
    fire_id = team_bot_a_firing[0].id

    # Second tick: team_bot_a up again — auto-resolves via scoped sweep.
    state["team_bot_a"] = True
    run_pod_health_signals_only(network_path=network_path)

    archived = list(signals_store.iter_signals(shared, subdirs=("archived",)))
    resolved_team_bot_a = [
        s for s in archived
        if s.id == fire_id and s.state == "resolved"
    ]
    assert len(resolved_team_bot_a) == 1, (
        f"expected team_bot_a gateway Signal {fire_id} to be auto-resolved, "
        f"archived state: {[(s.id, s.state) for s in archived]}"
    )


def _write_puller_log(shared: Path, *, stale: bool) -> Path:
    """Helper: create shared/logs/repo-puller.log with a one-line healthy
    body and an mtime in the past iff ``stale`` is True."""
    import os
    import time as _time

    log_dir = shared / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "repo-puller.log"
    log_path.write_text("[repo-puller] stashes=0\n[repo-puller] advanced abc..def (1 commits)\n")
    if stale:
        # 91 minutes ago — past the 90-min stale_after_seconds default.
        old = _time.time() - (91 * 60)
        os.utime(log_path, (old, old))
    return log_path


def test_stale_puller_log_emits_firing_signal(signals_env):
    network_path, shared, _state = signals_env
    from evolve_admin.health import run_pod_health_signals_only
    import signals.store as signals_store

    _write_puller_log(shared, stale=True)
    run_pod_health_signals_only(network_path=network_path)

    firing = list(signals_store.iter_signals(shared, subdirs=("firing",)))
    puller_sigs = [
        s for s in firing
        if s.producer == "pod_health" and s.type == "pod_health_repo_puller"
    ]
    assert len(puller_sigs) == 1, (
        f"expected one firing puller Signal, got: "
        f"{[(s.type, s.title) for s in puller_sigs]}"
    )
    # _check_repo_puller_freshness emits WARN for the stale case →
    # _emit_health_signals maps WARN → severity='warn'.
    assert puller_sigs[0].severity == "warn"


def test_fresh_puller_log_resolves_firing_signal(signals_env):
    network_path, shared, _state = signals_env
    from evolve_admin.health import run_pod_health_signals_only
    import signals.store as signals_store

    # First tick: stale log → fires.
    _write_puller_log(shared, stale=True)
    run_pod_health_signals_only(network_path=network_path)
    firing = [
        s for s in signals_store.iter_signals(shared, subdirs=("firing",))
        if s.type == "pod_health_repo_puller"
    ]
    assert len(firing) == 1
    fire_id = firing[0].id

    # Second tick: fresh log → sweep_resolve clears it.
    _write_puller_log(shared, stale=False)
    run_pod_health_signals_only(network_path=network_path)

    archived = list(signals_store.iter_signals(shared, subdirs=("archived",)))
    resolved = [s for s in archived if s.id == fire_id and s.state == "resolved"]
    assert len(resolved) == 1, (
        f"expected puller Signal {fire_id} to be auto-resolved, "
        f"archived states: {[(s.id, s.state) for s in archived]}"
    )


def test_partial_sweep_does_not_resolve_other_pod_health_types(signals_env):
    """A pre-existing pod_health Signal of a different type must survive a
    liveness-only sweep — that's the whole point of the type filter."""
    network_path, shared, state = signals_env
    import signals.store as signals_store
    from evolve_admin.health import run_pod_health_signals_only

    # Pre-existing launchd Signal that this runner does NOT re-check.
    launchd_sig = signals_store.observe(
        shared,
        signature="pod_health:pod_health_launchd:team_bot_a:plist",
        producer="pod_health",
        type="pod_health_launchd",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id="team_bot_a",
        title="team_bot_a launchd plist missing",
    )

    # Liveness tick with all gateways healthy — sweep should NOT touch the
    # launchd signal even though its signature is not in `kept`.
    run_pod_health_signals_only(network_path=network_path)

    found = signals_store.find_signal(shared, launchd_sig.id)
    assert found is not None
    sig_after, _path, _subdir = found
    assert sig_after.state == "firing", (
        f"launchd Signal must survive the gateway-only sweep; got state={sig_after.state}"
    )


# ── gateway supervision (label-agnostic crash-loop + port-collision net) ──────


def _seed_gateway(sched, label, *, port="19030", running=True, **sup):
    """Register a gateway job + its supervision metrics on the injected fake."""
    from runtime.scheduler import JobSpec
    sched.seed_job(
        JobSpec(label=label, program_args=["/bin/echo"], keep_alive=True),
        running=running,
    )
    sched.seed_supervision(label, env={"OPENCLAW_GATEWAY_PORT": port}, **sup)


def test_crashlooping_gateway_emits_firing_signal(signals_env):
    """A gateway in systemd 'auto-restart' fires a pod_health_gateway_crashloop
    Signal — keyed off the live unit list, NOT a hardcoded label (the original
    blindness)."""
    network_path, shared, _state = signals_env
    from evolve_admin.health import run_pod_health_signals_only
    from runtime.scheduler import get_scheduler
    import signals.store as signals_store

    # An orphan-shaped label whose name is NOT this pod's primary — exactly the
    # case the hardcoded check missed.
    _seed_gateway(get_scheduler(), "ai.openclaw.evolve-gateway", sub_state="auto-restart")
    run_pod_health_signals_only(network_path=network_path)

    firing = [
        s for s in signals_store.iter_signals(shared, subdirs=("firing",))
        if s.producer == "pod_health" and s.type == "pod_health_gateway_crashloop"
    ]
    assert len(firing) == 1, f"expected one crash-loop Signal, got {firing}"
    assert firing[0].severity == "alert"


def test_recovered_gateway_crashloop_is_auto_resolved(signals_env):
    network_path, shared, _state = signals_env
    from evolve_admin.health import run_pod_health_signals_only
    from runtime.scheduler import get_scheduler
    import signals.store as signals_store

    sched = get_scheduler()
    _seed_gateway(sched, "ai.openclaw.evolve-gateway", sub_state="auto-restart")
    run_pod_health_signals_only(network_path=network_path)
    firing = [
        s for s in signals_store.iter_signals(shared, subdirs=("firing",))
        if s.type == "pod_health_gateway_crashloop"
    ]
    assert len(firing) == 1
    fire_id = firing[0].id

    # Gateway recovers (stable 'running' substate) → scoped sweep resolves it.
    sched.seed_supervision("ai.openclaw.evolve-gateway", sub_state="running",
                           env={"OPENCLAW_GATEWAY_PORT": "19030"})
    run_pod_health_signals_only(network_path=network_path)

    archived = list(signals_store.iter_signals(shared, subdirs=("archived",)))
    assert [s for s in archived if s.id == fire_id and s.state == "resolved"], (
        f"crash-loop Signal {fire_id} should auto-resolve on recovery"
    )


def test_port_collision_emits_firing_signal(signals_env):
    """Two gateways declaring the same port fire one collision Signal — caught
    structurally, before either even crash-loops."""
    network_path, shared, _state = signals_env
    from evolve_admin.health import run_pod_health_signals_only
    from runtime.scheduler import get_scheduler
    import signals.store as signals_store

    sched = get_scheduler()
    _seed_gateway(sched, "ai.openclaw.evo-gateway", port="19030", sub_state="running")
    _seed_gateway(sched, "ai.openclaw.evolve-gateway", port="19030", sub_state="running")
    run_pod_health_signals_only(network_path=network_path)

    firing = [
        s for s in signals_store.iter_signals(shared, subdirs=("firing",))
        if s.type == "pod_health_gateway_port_collision"
    ]
    assert len(firing) == 1, f"expected one port-collision Signal, got {firing}"
    assert firing[0].severity == "alert"


def test_healthy_gateways_emit_no_supervision_signals(signals_env):
    """Distinct ports + stable units → no crash-loop / collision noise."""
    network_path, shared, _state = signals_env
    from evolve_admin.health import run_pod_health_signals_only
    from runtime.scheduler import get_scheduler
    import signals.store as signals_store

    sched = get_scheduler()
    _seed_gateway(sched, "ai.openclaw.evo-gateway", port="19030", sub_state="running")
    _seed_gateway(sched, "ai.openclaw.team-bot-b-gateway", port="19031", sub_state="running")
    run_pod_health_signals_only(network_path=network_path)

    sup_sigs = [
        s for s in signals_store.iter_signals(shared, subdirs=("firing",))
        if s.type in ("pod_health_gateway_crashloop", "pod_health_gateway_port_collision")
    ]
    assert sup_sigs == [], f"healthy pod should emit no supervision Signals, got {sup_sigs}"
