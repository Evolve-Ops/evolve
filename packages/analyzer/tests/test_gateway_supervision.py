"""tests/test_gateway_supervision.py — label-agnostic gateway crash-loop net.

`gateway_supervision` is the safety net for the failure class where two gateway
units fratricidally crash-loop on the same port and the admin UI never alerts
because the only gateway-health check was keyed to a hardcoded label. These
pins lock the three crash-loop prongs (auto-restart substate, systemd restart
rate, launchd PID churn), port-collision detection, the label-agnostic
enumeration, state persistence, and the "a flaky probe is never a finding"
contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import gateway_supervision as gs  # noqa: E402

CFG = gs.SupervisionConfig(restart_threshold=5, window_secs=180, churn_threshold=3)
L = "ai.openclaw.evo-gateway"


def _m(*, n=None, sub="running", pid=None, env=None, err=None) -> dict:
    return {
        "n_restarts": n,
        "sub_state": sub,
        "active_state": sub,
        "pid": pid,
        "last_exit": "1",
        "env": env or {},
        "status_error": err,
    }


# ── enumeration is label-agnostic ─────────────────────────────────────────────


class _ListOnly:
    def __init__(self, labels):
        self._labels = labels

    def list(self, *, prefix=None, timeout=None):
        return [x for x in self._labels if prefix is None or x.startswith(prefix)]


def test_discover_matches_any_openclaw_gateway_not_a_hardcoded_name():
    sched = _ListOnly([
        "ai.openclaw.evo-gateway",       # primary
        "ai.openclaw.team-bot-b-gateway",     # per-bot
        "ai.openclaw.evolve-gateway",    # the orphan from the incident
        "ai.openclaw.team-bot-b-audit",       # not a gateway
        "ai.evolve.evolve.admin-ui",     # not openclaw
    ])
    assert gs.discover_gateway_labels(sched) == [
        "ai.openclaw.evo-gateway",
        "ai.openclaw.evolve-gateway",
        "ai.openclaw.team-bot-b-gateway",
    ]


def test_discover_survives_a_raising_scheduler():
    class _Boom:
        def list(self, *, prefix=None, timeout=None):
            raise RuntimeError("systemctl exploded")
    assert gs.discover_gateway_labels(_Boom()) == []


# ── prong 1: auto-restart substate is instant ─────────────────────────────────


def test_auto_restart_substate_fires_immediately():
    state: dict = {}
    findings = gs.classify({L: _m(sub="auto-restart")}, state, config=CFG, now=1000.0)
    assert [f.kind for f in findings] == [gs.CRASHLOOP]
    assert findings[0].scope_key == L
    assert "auto-restart" in findings[0].detail


# ── prong 2: systemd restart-rate over the window ─────────────────────────────


def test_restart_rate_fires_when_counter_climbs_within_window():
    state: dict = {}
    # First sighting anchors; no rate is knowable from one point.
    assert gs.classify({L: _m(n=100)}, state, config=CFG, now=0.0) == []
    # +20 restarts 60s later → well past threshold 5.
    findings = gs.classify({L: _m(n=120)}, state, config=CFG, now=60.0)
    assert [f.kind for f in findings] == [gs.CRASHLOOP]
    assert "20 restarts" in findings[0].detail


def test_slow_restart_growth_within_window_does_not_fire():
    state: dict = {}
    gs.classify({L: _m(n=100)}, state, config=CFG, now=0.0)
    # Only +3 in the window — under the threshold of 5.
    assert gs.classify({L: _m(n=103)}, state, config=CFG, now=60.0) == []


def test_window_expiry_reanchors_so_old_restarts_dont_accumulate():
    state: dict = {}
    gs.classify({L: _m(n=100)}, state, config=CFG, now=0.0)
    # 200s later (> 180s window) with +3 → window slid, fresh anchor, no fire.
    assert gs.classify({L: _m(n=103)}, state, config=CFG, now=200.0) == []
    assert state[L].anchor_restarts == 103


def test_counter_reset_reanchors_without_a_false_positive():
    state: dict = {}
    gs.classify({L: _m(n=2266)}, state, config=CFG, now=0.0)
    # Unit reinstalled → NRestarts drops. Must NOT read as a huge negative/rate.
    assert gs.classify({L: _m(n=1)}, state, config=CFG, now=60.0) == []
    assert state[L].anchor_restarts == 1


# ── prong 3: launchd PID churn (best-effort) ──────────────────────────────────


def test_pid_churn_fires_after_consecutive_respawns():
    state: dict = {}
    assert gs.classify({L: _m(n=None, pid=10)}, state, config=CFG, now=0.0) == []
    assert gs.classify({L: _m(n=None, pid=11)}, state, config=CFG, now=60.0) == []
    assert gs.classify({L: _m(n=None, pid=12)}, state, config=CFG, now=120.0) == []
    findings = gs.classify({L: _m(n=None, pid=13)}, state, config=CFG, now=180.0)
    assert [f.kind for f in findings] == [gs.CRASHLOOP]
    assert "consecutive" in findings[0].detail


def test_stable_pid_resets_churn():
    state: dict = {}
    gs.classify({L: _m(n=None, pid=10)}, state, config=CFG, now=0.0)
    gs.classify({L: _m(n=None, pid=11)}, state, config=CFG, now=60.0)
    gs.classify({L: _m(n=None, pid=11)}, state, config=CFG, now=120.0)
    assert state[L].consecutive_respawns == 0


# ── port collision ────────────────────────────────────────────────────────────


def test_two_units_on_one_port_fire_a_single_collision_finding():
    a, b = "ai.openclaw.evo-gateway", "ai.openclaw.evolve-gateway"
    metrics = {
        a: _m(n=0, env={"OPENCLAW_GATEWAY_PORT": "19030"}),
        b: _m(n=0, env={"OPENCLAW_GATEWAY_PORT": "19030"}),
    }
    findings = [f for f in gs.classify(metrics, {}, config=CFG, now=0.0)
                if f.kind == gs.PORT_COLLISION]
    assert len(findings) == 1
    assert findings[0].scope_key == "gateway_port_19030"
    assert a in findings[0].detail and b in findings[0].detail


def test_distinct_ports_do_not_collide():
    metrics = {
        "ai.openclaw.evo-gateway": _m(n=0, env={"OPENCLAW_GATEWAY_PORT": "19030"}),
        "ai.openclaw.team-bot-b-gateway": _m(n=0, env={"OPENCLAW_GATEWAY_PORT": "19031"}),
    }
    assert [f for f in gs.classify(metrics, {}, config=CFG, now=0.0)
            if f.kind == gs.PORT_COLLISION] == []


# ── a flaky probe is never a finding ──────────────────────────────────────────


def test_status_error_label_is_skipped_but_state_preserved():
    state: dict = {L: gs.LabelState(consecutive_respawns=2, last_pid=10)}
    findings = gs.classify({L: _m(err="cannot_escalate")}, state, config=CFG, now=0.0)
    assert findings == []
    # State preserved so a later good probe resumes from where it was.
    assert state[L].consecutive_respawns == 2


def test_status_error_label_excluded_from_collision_set():
    metrics = {
        "ai.openclaw.evo-gateway": _m(n=0, env={"OPENCLAW_GATEWAY_PORT": "19030"}),
        "ai.openclaw.evolve-gateway": _m(env="ignored", err="error"),
    }
    metrics["ai.openclaw.evolve-gateway"]["env"] = {"OPENCLAW_GATEWAY_PORT": "19030"}
    assert [f for f in gs.classify(metrics, {}, config=CFG, now=0.0)
            if f.kind == gs.PORT_COLLISION] == []


# ── state bookkeeping ─────────────────────────────────────────────────────────


def test_state_prunes_labels_no_longer_present():
    state: dict = {"ai.openclaw.gone-gateway": gs.LabelState()}
    gs.classify({L: _m(n=0)}, state, config=CFG, now=0.0)
    assert "ai.openclaw.gone-gateway" not in state
    assert L in state


def test_state_round_trips_through_disk(tmp_path):
    state = {L: gs.LabelState(anchor_ts=5.0, anchor_restarts=7, last_restarts=9,
                              last_pid=42, consecutive_respawns=2, last_seen_ts=10.0)}
    gs.save_state(tmp_path, state)
    loaded = gs.load_state(tmp_path)
    assert loaded[L].anchor_restarts == 7
    assert loaded[L].last_pid == 42
    assert loaded[L].consecutive_respawns == 2


def test_load_state_tolerates_missing_and_corrupt(tmp_path):
    assert gs.load_state(tmp_path) == {}
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "gateway-supervision.json").write_text("{not json")
    assert gs.load_state(tmp_path) == {}


# ── config ────────────────────────────────────────────────────────────────────


def test_config_from_network_clamps_out_of_range():
    cfg = gs.config_from_network({"alerts": {"gateway_supervision": {
        "restart_threshold": 0, "window_secs": 10**9, "churn_threshold": 1,
    }}})
    assert cfg.restart_threshold == 1          # clamped up from 0
    assert cfg.window_secs == 3600             # clamped down
    assert cfg.churn_threshold == 2            # clamped up from 1


def test_config_defaults_on_garbage():
    cfg = gs.config_from_network({"alerts": {"gateway_supervision": {"window_secs": "nope"}}})
    assert cfg.window_secs == gs.DEFAULT_WINDOW_SECS


def test_config_on_empty_network():
    assert gs.config_from_network(None) == gs.SupervisionConfig()


# ── evaluate orchestration (with a per-label raising probe) ───────────────────


class _FlakyProbe:
    """Lists two gateways; supervision() raises for one of them."""
    def list(self, *, prefix=None, timeout=None):
        return ["ai.openclaw.evo-gateway", "ai.openclaw.team-bot-b-gateway"]

    def supervision(self, label, *, timeout=None):
        if label.endswith("team-bot-b-gateway"):
            raise RuntimeError("probe blew up")
        return _m(sub="auto-restart")


def test_evaluate_isolates_a_raising_probe(tmp_path):
    findings = gs.evaluate(_FlakyProbe(), tmp_path, config=CFG, now=1000.0)
    # The healthy-probe gateway still fires; the raising one is swallowed.
    kinds = [(f.kind, f.scope_key) for f in findings]
    assert (gs.CRASHLOOP, "ai.openclaw.evo-gateway") in kinds
    assert all("team-bot-b" not in k for _, k in kinds)
