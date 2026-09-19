"""Tests for roster_allowlist_drift_monitor.

The monitor compares each bot's live on-disk effective group allowlist against
the admin-recorded baseline and fires Signals on divergence. We unit-test the
orchestration (seed-once, fire-on-diff, dedup, sweep-resolve on revert, and the
unreadable fail-safe) with an in-memory fake for ``evolve_admin.roster_baseline``
and the REAL signal store writing to a tmp shared dir. The baseline module's own
persistence/snapshot logic is covered separately in
packages/admin/tests/test_roster_baseline.py.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import roster_allowlist_drift_monitor as monitor  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def network_json(tmp_path):
    shared = tmp_path / "shared"
    for sub in ("firing", "snoozed", "archived", "log"):
        (shared / "signals" / sub).mkdir(parents=True)
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(shared),
        "members": ["bot_a", "bot_b"],
    }))
    return net, shared


@pytest.fixture
def stub_admin(monkeypatch, network_json):
    """Inject fake evolve_admin.config + evolve_admin.roster_baseline so the
    monitor's lazy imports resolve without the admin package, with in-memory
    baseline state the test drives."""
    net, shared = network_json

    state = {
        "baselines": {},   # bot_id -> dict[str, list[str]]
        "current": {},     # bot_id -> dict | UNREADABLE
        "seeds": [],       # (bot_id, source)
    }
    UNREADABLE = object()

    def _read_current(network, bot_id, **kw):
        return state["current"].get(bot_id, {})

    def _read_baseline(shared_dir, bot_id):
        b = state["baselines"].get(bot_id)
        return None if b is None else {k: list(v) for k, v in b.items()}

    def _write_baseline(shared_dir, bot_id, allowlists, *, source):
        state["baselines"][bot_id] = {k: list(v) for k, v in allowlists.items()}
        state["seeds"].append((bot_id, source))
        return allowlists

    rb_mod = types.ModuleType("evolve_admin.roster_baseline")
    rb_mod.UNREADABLE = UNREADABLE
    rb_mod.read_current_allowlists = _read_current
    rb_mod.read_baseline = _read_baseline
    rb_mod.write_baseline = _write_baseline

    config_mod = types.ModuleType("evolve_admin.config")
    config_mod.load_network = lambda p: json.loads(Path(p).read_text())

    ea = types.ModuleType("evolve_admin")
    ea.config = config_mod
    ea.roster_baseline = rb_mod

    monkeypatch.setitem(sys.modules, "evolve_admin", ea)
    monkeypatch.setitem(sys.modules, "evolve_admin.config", config_mod)
    monkeypatch.setitem(sys.modules, "evolve_admin.roster_baseline", rb_mod)
    return state, net, shared, UNREADABLE


def _firing(shared):
    return [json.loads(p.read_text())
            for p in (shared / "signals" / "firing").glob("*.json")]


# ── _diff_channels (pure) ───────────────────────────────────────────────────


def test_diff_widen_only():
    d = monitor._diff_channels({"slack": ["U1"]}, {"slack": ["U1", "U2"]})
    assert d == {"slack": (["U2"], [])}


def test_diff_narrow_only():
    d = monitor._diff_channels({"slack": ["U1", "U2"]}, {"slack": ["U1"]})
    assert d == {"slack": ([], ["U2"])}


def test_diff_channel_appeared_and_disappeared():
    d = monitor._diff_channels({"telegram": ["T1"]}, {"slack": ["U2"]})
    assert d == {"slack": (["U2"], []), "telegram": ([], ["T1"])}


def test_diff_no_change_when_order_differs():
    assert monitor._diff_channels({"slack": ["U2", "U1"]}, {"slack": ["U1", "U2"]}) == {}


# ── Seed on first sight → no Signal ─────────────────────────────────────────


def test_first_sight_seeds_and_does_not_fire(stub_admin):
    state, net, shared, _ = stub_admin
    state["current"] = {"bot_a": {"slack": ["U1"]}, "bot_b": {}}
    report = monitor.run(net)
    assert report["signals_fired"] == 0
    assert report["seeded"] == 2
    assert _firing(shared) == []
    # Both bots seeded, with the monitor_seed provenance.
    assert sorted(state["seeds"]) == [("bot_a", "monitor_seed"), ("bot_b", "monitor_seed")]
    assert state["baselines"]["bot_a"] == {"slack": ["U1"]}


# ── Out-of-band widening fires one drift Signal per (bot, channel) ──────────


def test_out_of_band_widen_fires(stub_admin):
    state, net, shared, _ = stub_admin
    state["baselines"] = {"bot_a": {"slack": ["U1"]}}
    state["current"] = {"bot_a": {"slack": ["U1", "U2"]}, "bot_b": {}}
    report = monitor.run(net)
    assert report["signals_fired"] == 1  # bot_a/slack; bot_b seeds silently
    firing = _firing(shared)
    assert len(firing) == 1
    sig = firing[0]
    assert sig["producer"] == "roster_allowlist_drift"
    assert sig["type"] == "roster_group_allowlist_changed"
    assert sig["scope"] == "bot"
    assert sig["bot_id"] == "bot_a"
    assert sig["details"]["added"] == ["U2"]
    assert sig["details"]["removed"] == []
    assert sig["details"]["direction"] == "widened"
    assert sig["signature"] == "roster_allowlist_drift:roster_group_allowlist_changed:bot_a/slack"


def test_repeat_drift_dedups(stub_admin):
    state, net, shared, _ = stub_admin
    state["baselines"] = {"bot_a": {"slack": ["U1"]}, "bot_b": {}}
    state["current"] = {"bot_a": {"slack": ["U1", "U2"]}, "bot_b": {}}
    monitor.run(net)
    monitor.run(net)
    # Same signature → one firing Signal, not two.
    assert len(_firing(shared)) == 1


# ── Revert clears the Signal via sweep_resolve ─────────────────────────────


def test_revert_sweep_resolves(stub_admin):
    state, net, shared, _ = stub_admin
    state["baselines"] = {"bot_a": {"slack": ["U1"]}, "bot_b": {}}
    state["current"] = {"bot_a": {"slack": ["U1", "U2"]}, "bot_b": {}}
    monitor.run(net)
    assert len(_firing(shared)) == 1
    # Operator reverts the out-of-band edit; on-disk matches baseline again.
    state["current"]["bot_a"] = {"slack": ["U1"]}
    report = monitor.run(net)
    assert report["signals_resolved"] >= 1
    assert _firing(shared) == []


# ── Admin approve (baseline advanced) never false-positives ────────────────


def test_admin_advanced_baseline_no_false_positive(stub_admin):
    state, net, shared, _ = stub_admin
    # Admin approved U2 → the write path recorded the baseline WITH U2, and the
    # on-disk config carries U2 too. No divergence ⇒ no drift.
    state["baselines"] = {"bot_a": {"slack": ["U1", "U2"]}, "bot_b": {}}
    state["current"] = {"bot_a": {"slack": ["U1", "U2"]}, "bot_b": {}}
    report = monitor.run(net)
    assert report["signals_fired"] == 0
    assert _firing(shared) == []


# ── Unreadable config: fires, does not seed, leaves drift untouched ────────


def test_unreadable_fires_and_does_not_seed(stub_admin):
    state, net, shared, UNREADABLE = stub_admin
    state["current"] = {"bot_a": UNREADABLE, "bot_b": {}}
    report = monitor.run(net)
    assert report["unreadable"] == 1
    firing = _firing(shared)
    unreadable = [s for s in firing if s["type"] == "roster_group_allowlist_unreadable"]
    assert len(unreadable) == 1
    assert unreadable[0]["bot_id"] == "bot_a"
    # bot_a was NOT seeded (blind tick); bot_b was.
    assert ("bot_a", "monitor_seed") not in state["seeds"]
    assert ("bot_b", "monitor_seed") in state["seeds"]


def test_unreadable_does_not_resolve_prior_drift(stub_admin):
    state, net, shared, UNREADABLE = stub_admin
    # First: real drift on bot_a fires.
    state["baselines"] = {"bot_a": {"slack": ["U1"]}, "bot_b": {}}
    state["current"] = {"bot_a": {"slack": ["U1", "U2"]}, "bot_b": {}}
    monitor.run(net)
    drift = [s for s in _firing(shared)
             if s["type"] == "roster_group_allowlist_changed"]
    assert len(drift) == 1
    # Now bot_a becomes unreadable. Its prior drift Signal must NOT be swept
    # (we can't confirm it cleared) — only an unreadable Signal is added.
    state["current"]["bot_a"] = UNREADABLE
    monitor.run(net)
    types_now = sorted(s["type"] for s in _firing(shared))
    assert "roster_group_allowlist_changed" in types_now
    assert "roster_group_allowlist_unreadable" in types_now


def test_unreadable_auto_resolves_when_readable_again(stub_admin):
    state, net, shared, UNREADABLE = stub_admin
    state["current"] = {"bot_a": UNREADABLE, "bot_b": {}}
    monitor.run(net)
    assert any(s["type"] == "roster_group_allowlist_unreadable"
               for s in _firing(shared))
    # bot_a readable again (empty, clean) → unreadable Signal sweeps away.
    state["current"]["bot_a"] = {}
    monitor.run(net)
    assert not any(s["type"] == "roster_group_allowlist_unreadable"
                   for s in _firing(shared))


# ── dry-run writes nothing ─────────────────────────────────────────────────


def test_dry_run_emits_and_seeds_nothing(stub_admin):
    state, net, shared, _ = stub_admin
    state["baselines"] = {"bot_a": {"slack": ["U1"]}}
    state["current"] = {"bot_a": {"slack": ["U1", "U2"]}, "bot_b": {"slack": ["U9"]}}
    report = monitor.run(net, dry_run=True)
    assert _firing(shared) == []
    assert state["seeds"] == []  # bot_b not seeded in dry-run
    assert report["signals_fired"] == 0
