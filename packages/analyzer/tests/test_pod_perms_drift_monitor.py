"""Tests for pod_perms_drift_monitor.

The monitor wraps ensure_pod_perms(check_only=True) in a Signal-
emitting loop. We unit-test the loop here without invoking the real
ensure_pod_perms (that would require sudo and a real macOS host with
the pod-perms contract on disk). The Signal-emission path is locked
in; the contract-check path is already locked in by deploy.py's own
tests.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pod_perms_drift_monitor as monitor  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────


class _FakePermCheck:
    """Mimics deploy._PermCheck without depending on the dataclass."""
    def __init__(self, *, category, target, ok, detail="", fix_description=None):
        self.category = category
        self.target = target
        self.ok = ok
        self.detail = detail
        self.fix_description = fix_description


class _FakePermResult:
    """Mimics deploy.PodPermsResult.checks shape."""
    def __init__(self, checks: list[_FakePermCheck]) -> None:
        self.checks = checks


@pytest.fixture
def network_json(tmp_path):
    """Tiny network.json + shared dir so the monitor's load path works."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "signals").mkdir()
    (shared / "signals" / "firing").mkdir()
    (shared / "signals" / "archived").mkdir()
    (shared / "signals" / "snoozed").mkdir()
    (shared / "signals" / "log").mkdir()
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(shared)}))
    return net, shared


@pytest.fixture
def stub_admin_imports(monkeypatch, network_json):
    """Stub evolve_admin.deploy.ensure_pod_perms + load_network so the
    monitor's lazy imports resolve without the admin package on path."""
    net, shared = network_json

    class _StubModule:
        """Fake module object suitable for monkeypatching sys.modules entries."""
        pass

    # Track calls
    calls: dict[str, Any] = {"ensure_pod_perms": []}

    def _stub_ensure(bot_id=None, network_path=None, check_only=True):
        calls["ensure_pod_perms"].append({
            "bot_id": bot_id,
            "network_path": str(network_path) if network_path else None,
            "check_only": check_only,
        })
        # Default: no drift. Tests override via _set_drift below.
        return _FakePermResult(calls.get("_planned_checks", []))

    def _stub_load_network(path):
        return json.loads(Path(path).read_text())

    # Per-bot evolve-access reassert: default no-op (no bots in the tiny
    # network → empty dict). Tests that exercise the heal wiring set
    # calls["_reassert_result"] to a {bot_id: (ok, failures)} dict.
    def _stub_reassert(bot_pairs):
        calls["reassert_pairs"] = list(bot_pairs)
        return calls.get("_reassert_result", {})

    # Inject as sys.modules so the lazy import inside run() finds them.
    deploy_mod = _StubModule()
    deploy_mod.ensure_pod_perms = _stub_ensure  # type: ignore[attr-defined]
    deploy_mod._PermCheck = _FakePermCheck  # type: ignore[attr-defined]
    config_mod = _StubModule()
    config_mod.load_network = _stub_load_network  # type: ignore[attr-defined]
    config_mod.get_bot_user = lambda bot_id, network: bot_id  # type: ignore[attr-defined]
    scp_mod = _StubModule()
    scp_mod.reassert_pod_evolve_access = _stub_reassert  # type: ignore[attr-defined]
    evolve_admin = _StubModule()
    evolve_admin.deploy = deploy_mod  # type: ignore[attr-defined]
    evolve_admin.config = config_mod  # type: ignore[attr-defined]
    evolve_admin.secret_config_perms = scp_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "evolve_admin", evolve_admin)
    monkeypatch.setitem(sys.modules, "evolve_admin.deploy", deploy_mod)
    monkeypatch.setitem(sys.modules, "evolve_admin.config", config_mod)
    monkeypatch.setitem(
        sys.modules, "evolve_admin.secret_config_perms", scp_mod)
    return calls, net, shared


# ── Happy path: no drift → no Signal ──────────────────────────────────────


def test_no_drift_emits_no_signal(stub_admin_imports):
    calls, net, shared = stub_admin_imports
    calls["_planned_checks"] = [
        _FakePermCheck(
            category="dir-mode",
            target=str(shared / "proposals"),
            ok=True,
            detail="mode 1777",
        ),
    ]
    report = monitor.run(net)
    assert report["drifted"] == 0
    assert report["signals_fired"] == 0
    # ensure_pod_perms invoked once with check_only=True
    assert len(calls["ensure_pod_perms"]) == 1
    assert calls["ensure_pod_perms"][0]["check_only"] is True
    assert calls["ensure_pod_perms"][0]["bot_id"] is None


# ── Drift detected → one Signal fires ─────────────────────────────────────


def test_drift_fires_one_pod_signal(stub_admin_imports):
    """The motivating scenario: pending/ owned by a bot user (not
    evolve). The check pass returns a single drifted record; monitor
    emits one pod-scope Signal."""
    calls, net, shared = stub_admin_imports
    calls["_planned_checks"] = [
        _FakePermCheck(
            category="dir-owner",
            target=str(shared / "proposals" / "pending"),
            ok=False,
            detail="owner=team_bot_d != expected evolve",
            fix_description=f"chown evolve:wheel {shared}/proposals/pending",
        ),
    ]
    # Flap hysteresis (docs/spec-transient-signal-suppression-2026-06-23.md):
    # the first tick dwells (1/2) and withholds; a second consecutive tick with
    # the same drift promotes (2/2) and the pod Signal fires.
    r1 = monitor.run(net)
    assert r1["signals_fired"] == 0
    assert r1["signals_withheld"] == 1
    report = monitor.run(net)
    assert report["drifted"] == 1
    assert report["signals_fired"] == 1

    # The Signal landed in firing/
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    sig = json.loads(firing[0].read_text())
    assert sig["producer"] == "pod_perms_drift"
    assert sig["type"] == "pod_perms_drift"
    assert sig["scope"] == "pod"
    assert "perm contract drifted" in sig["title"]
    # Body should include the drifted target + the fix command
    assert "proposals/pending" in sig["body"]
    assert "chown evolve:wheel" in sig["body"]
    # Details payload is structured for downstream consumers
    assert sig["details"]["drift_count"] == 1
    assert len(sig["details"]["drifted"]) == 1


def test_multi_drift_groups_by_category_in_body(stub_admin_imports):
    """Multiple drift records grouped by category for readability —
    operator scans by layer (ACL / dir-owner / dir-mode) rather than
    a flat list."""
    calls, net, shared = stub_admin_imports
    calls["_planned_checks"] = [
        _FakePermCheck(
            category="dir-owner",
            target=str(shared / "proposals" / "pending"),
            ok=False,
            detail="owner=team_bot_d != expected evolve",
            fix_description="chown evolve:wheel proposals/pending",
        ),
        _FakePermCheck(
            category="dir-owner",
            target=str(shared / "proposals" / "archived"),
            ok=False,
            detail="owner=team_bot_a != expected evolve",
            fix_description="chown evolve:wheel proposals/archived",
        ),
        _FakePermCheck(
            category="ACL",
            target=str(shared / "proposals"),
            ok=False,
            detail="ACL missing",
            fix_description="chmod +a ...",
        ),
    ]
    monitor.run(net)  # dwell (1/2)
    report = monitor.run(net)  # promote (2/2) → fire
    assert report["drifted"] == 3
    assert report["signals_fired"] == 1
    firing = list((shared / "signals" / "firing").glob("*.json"))
    body = json.loads(firing[0].read_text())["body"]
    # Section headers appear once per category
    assert body.count("**dir-owner**") == 1
    assert body.count("**ACL**") == 1


# ── Sweep-resolve: drift cleared → Signal auto-archives ───────────────────


def test_drift_cleared_auto_resolves_prior_signal(stub_admin_imports):
    """After the operator runs ensure-pod-perms, the next monitor tick
    sees clean state and the prior Signal sweeps to resolved/archived.
    """
    calls, net, shared = stub_admin_imports

    # Tick 1: drift detected → Signal fires
    calls["_planned_checks"] = [
        _FakePermCheck(
            category="dir-owner",
            target=str(shared / "proposals" / "pending"),
            ok=False,
            detail="owner=team_bot_d != expected evolve",
            fix_description="chown evolve:wheel proposals/pending",
        ),
    ]
    monitor.run(net)  # dwell (1/2)
    r1 = monitor.run(net)  # promote (2/2) → fire
    assert r1["signals_fired"] == 1
    firing_after_1 = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing_after_1) == 1

    # Tick 2: no drift (operator ran ensure-pod-perms) → resolves
    calls["_planned_checks"] = [
        _FakePermCheck(
            category="dir-mode",
            target=str(shared / "proposals"),
            ok=True,
            detail="mode 1777",
        ),
    ]
    r2 = monitor.run(net)
    assert r2["signals_fired"] == 0
    assert r2["signals_resolved"] >= 1  # the prior fire got swept

    # firing/ now empty
    firing_after_2 = list((shared / "signals" / "firing").glob("*.json"))
    assert firing_after_2 == []


# ── Stable signature across runs (sweep-resolve dependency) ───────────────


def test_signature_stable_when_drift_persists(stub_admin_imports):
    """Two consecutive runs with the same drift → same Signal signature
    so the second observe() find-or-creates rather than emitting a
    duplicate."""
    calls, net, shared = stub_admin_imports
    calls["_planned_checks"] = [
        _FakePermCheck(
            category="dir-owner",
            target=str(shared / "proposals" / "pending"),
            ok=False,
            detail="owner=team_bot_d != expected evolve",
            fix_description="chown evolve:wheel proposals/pending",
        ),
    ]

    # Two ticks to clear the flap dwell and promote the Signal.
    monitor.run(net)
    monitor.run(net)
    sigs_1 = list((shared / "signals" / "firing").glob("*.json"))
    assert len(sigs_1) == 1
    sig_1_id = sigs_1[0].name

    # A further run with identical drift — already firing, so it bumps the
    # same Signal (find-or-create) rather than emitting a duplicate.
    monitor.run(net)
    sigs_2 = list((shared / "signals" / "firing").glob("*.json"))
    assert len(sigs_2) == 1
    assert sigs_2[0].name == sig_1_id  # same file, find-or-created


# ── Dry-run path ──────────────────────────────────────────────────────────


def test_dry_run_does_not_emit_signal(stub_admin_imports):
    calls, net, shared = stub_admin_imports
    calls["_planned_checks"] = [
        _FakePermCheck(
            category="dir-owner",
            target=str(shared / "proposals" / "pending"),
            ok=False,
            detail="owner=team_bot_d != expected evolve",
            fix_description="chown evolve:wheel ...",
        ),
    ]
    report = monitor.run(net, dry_run=True)
    # drift detected, but no Signal written to disk
    assert report["drifted"] == 1
    assert report["signals_fired"] == 0
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert firing == []


# ── Graceful degradation when admin package is missing ────────────────────


def test_missing_admin_package_is_silent_noop(monkeypatch, network_json):
    """On a host where evolve_admin isn't on sys.path (early bootstrap,
    misconfigured worker), the monitor should print a status row and
    return without crashing — the launchd daemon's StandardErrorPath
    would otherwise fill with traceback noise."""
    net, _ = network_json
    # Ensure neither stub is present
    monkeypatch.setitem(sys.modules, "evolve_admin", None)
    report = monitor.run(net)
    assert report["checks_run"] == 0
    assert report["drifted"] == 0


# ── Missing network.json: monitor exits cleanly ──────────────────────────


def test_main_skips_when_network_json_missing(tmp_path, capsys):
    """The CLI entry point should skip cleanly when network.json doesn't
    exist — fresh-install hosts before setup-wizard ran."""
    missing = tmp_path / "no-such-network.json"
    rc = monitor.main(["--network", str(missing)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out


# ── Per-bot evolve-access self-heal (the hourly-flap fix) ─────────────────


def _net_with_members(net: Path, members: list[str]) -> None:
    """Rewrite the tiny network.json to include `members` so the monitor's
    per-bot reassert pass has bots to act on."""
    data = json.loads(net.read_text())
    data["members"] = members
    net.write_text(json.dumps(data))


def test_reassert_runs_for_every_bot(stub_admin_imports):
    """Each hourly tick runs the evolve-access reassert over every bot in
    network.json (the periodic re-widen that bounds the gateway's 0700
    re-clamp to ≤1 cycle)."""
    calls, net, _ = stub_admin_imports
    _net_with_members(net, ["evo", "darwin"])
    calls["_reassert_result"] = {"evo": (True, []), "darwin": (True, [])}
    monitor.run(net, dry_run=True)
    assert {b for b, _u in calls["reassert_pairs"]} == {"evo", "darwin"}


def test_self_healed_bot_emits_no_signal(stub_admin_imports):
    """A bot whose mask clamp the reassert repairs must NOT page — that
    silence is the whole point (it ends the hourly ACL-drift flurry)."""
    calls, net, shared = stub_admin_imports
    _net_with_members(net, ["evo", "darwin"])
    # No pod-wide drift; both bots self-heal.
    calls["_reassert_result"] = {"evo": (True, []), "darwin": (True, [])}
    report = monitor.run(net)
    assert report["drifted"] == 0
    assert report["signals_fired"] == 0
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert firing == []


def test_unhealed_bot_rides_the_drift_signal(stub_admin_imports):
    """A bot still locked out AFTER the escalated full re-grant is a genuine
    problem — it must surface as drift so the operator sees it.

    The unhealed-bot signal flows through the same pod_perms_drift fire path,
    so the flap hysteresis (spec-transient-signal-suppression-2026-06-23)
    applies: the first tick dwells (1/2) and a second consecutive unhealed
    tick promotes (2/2). This is the intended behaviour — a genuinely-stuck
    bot still surfaces (tick 2), while a 1-tick transient unhealed state
    self-clears because the reassert reruns every tick (a heal on the next
    tick emits nothing and resets the dwell)."""
    calls, net, shared = stub_admin_imports
    _net_with_members(net, ["evo", "darwin"])
    calls["_reassert_result"] = {
        "evo": (True, []),
        "darwin": (False, ["evolve cannot TRAVERSE /home/darwin/.openclaw"]),
    }
    r1 = monitor.run(net)
    assert r1["drifted"] == 1
    assert r1["signals_fired"] == 0
    assert r1["signals_withheld"] == 1
    report = monitor.run(net)
    assert report["drifted"] == 1
    assert report["signals_fired"] == 1
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    sig = json.loads(firing[0].read_text())
    blob = json.dumps(sig)
    assert "darwin" in blob and "evolve-access" in blob
    # The self-healed bot is NOT named in the signal.
    assert "/home/evo/.openclaw" not in blob
