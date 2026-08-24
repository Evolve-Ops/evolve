"""Tests for evolve_admin.oc_deps + safe_upgrade.gate_oc_data_dependencies.

The manifest declares every OpenClaw artifact Evolve reads out-of-band; the
gate runs the real readers against the live pod. The headline case is the
2026-06-22 incident: an auth-store artifact present on disk but unreadable by
Evolve's reader → the auth_store probe returns ``missing`` → the gate FAILS
(blocking), the exact verdict the safe-upgrade preflight should have produced
the instant the auth migration landed.

Covers:
- ``oc_store.auth_store_present`` (present / absent / unresolvable + read-only)
- each oc_deps probe's verdict from the underlying reader's state
- the gate's missing→FAIL(load-bearing) / WARN / PASS routing
- probes are exception-safe and the new presence helper is read-only/pure
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))
# primary_bot (the auth-store parser) resolves the same way the scanner sets up.
_ANALYZER_DIR = Path(__file__).resolve().parents[2] / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin import oc_deps  # noqa: E402
from evolve_admin import oc_store  # noqa: E402
from evolve_admin import safe_upgrade as su  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────────────


def _agent_dir(home: Path) -> Path:
    d = home.joinpath(".openclaw", "agents", "main", "agent")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_sqlite_store(agent_dir: Path) -> Path:
    db = agent_dir / "openclaw-agent.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE auth_profile_store (store_key TEXT PRIMARY KEY, "
            "store_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO auth_profile_store VALUES (?, ?, ?)",
            ("primary", json.dumps({"profiles": {}}), 1_700_000_000),
        )
        conn.commit()
    finally:
        conn.close()
    return db


# ── oc_store.auth_store_present ────────────────────────────────────────────────


def test_auth_store_present_true_for_sqlite(tmp_path):
    home = tmp_path / "bot"
    _build_sqlite_store(_agent_dir(home))
    assert oc_store.auth_store_present("bot", home=home) is True


def test_auth_store_present_true_for_legacy_json(tmp_path):
    home = tmp_path / "bot"
    (_agent_dir(home) / "auth-profiles.json").write_text("{}")
    assert oc_store.auth_store_present("bot", home=home) is True


def test_auth_store_present_true_for_bak(tmp_path):
    home = tmp_path / "bot"
    (_agent_dir(home) / "auth-profiles.json.sqlite-import.1700000000000.bak").write_text("{}")
    assert oc_store.auth_store_present("bot", home=home) is True


def test_auth_store_present_false_when_nothing_on_disk(tmp_path):
    home = tmp_path / "bot"
    _agent_dir(home)  # empty agent dir, no artifacts
    assert oc_store.auth_store_present("bot", home=home) is False


def test_auth_store_present_none_when_home_unresolvable():
    # No home, no user, no bot_id → cannot resolve a home dir.
    assert oc_store.auth_store_present(None) is None


def test_auth_store_present_fails_safe_to_true_under_unreadable_parent(tmp_path):
    """The incident-defense line: a store PRESENT on disk but under a parent
    Evolve can't traverse must read as present (True), never as absent — so the
    auth_store probe classifies it as `missing` (drift), not `not_applicable`.
    Uses os.lstat so the fail-safe holds on Python 3.13+ where Path.exists()
    swallows EACCES (the pod runs 3.14)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    _build_sqlite_store(agent)
    os.chmod(agent, 0o000)
    try:
        assert oc_store.auth_store_present("bot", home=home) is True
    finally:
        os.chmod(agent, 0o755)  # restore so tmp cleanup can recurse in


def test_auth_store_present_is_read_only(tmp_path):
    """The presence probe must not mutate the store tree."""
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    db = _build_sqlite_store(agent)
    before = {p.name: p.stat().st_mtime_ns for p in agent.iterdir()}
    assert oc_store.auth_store_present("bot", home=home) is True
    assert oc_store.auth_store_present("bot", home=home) is True
    after = {p.name: p.stat().st_mtime_ns for p in agent.iterdir()}
    assert before == after
    assert db.exists()


# ── auth_store probe — the incident-defining dependency ───────────────────────


def test_probe_auth_store_ok_when_key_resolves(monkeypatch):
    monkeypatch.setattr(oc_store, "read_anthropic_key", lambda *a, **k: "sk-ant-live")
    pr = oc_deps._probe_auth_store("bot", network={})
    assert pr.status == oc_deps.PROBE_OK
    assert pr.ok is True


def test_probe_auth_store_ok_for_gateway_auth_bot(monkeypatch):
    """Store readable but carries no anthropic profile → valid keyless bot."""
    monkeypatch.setattr(oc_store, "read_anthropic_key", lambda *a, **k: "")
    monkeypatch.setattr(oc_store, "read_auth_store", lambda *a, **k: '{"profiles": {}}')
    pr = oc_deps._probe_auth_store("atlas", network={})
    assert pr.status == oc_deps.PROBE_OK
    assert pr.ok is True


def test_probe_auth_store_missing_is_the_incident(monkeypatch):
    """Artifact present on disk but the reader resolves nothing → the
    2026-06-22 migration shape. This is the headline failure."""
    monkeypatch.setattr(oc_store, "read_anthropic_key", lambda *a, **k: "")
    monkeypatch.setattr(oc_store, "read_auth_store", lambda *a, **k: None)
    monkeypatch.setattr(oc_store, "auth_store_present", lambda *a, **k: True)
    pr = oc_deps._probe_auth_store("bot", network={})
    assert pr.status == oc_deps.PROBE_MISSING
    assert pr.ok is False


def test_probe_auth_store_not_applicable_when_no_artifact(monkeypatch):
    monkeypatch.setattr(oc_store, "read_anthropic_key", lambda *a, **k: "")
    monkeypatch.setattr(oc_store, "read_auth_store", lambda *a, **k: None)
    monkeypatch.setattr(oc_store, "auth_store_present", lambda *a, **k: False)
    pr = oc_deps._probe_auth_store("bot", network={})
    assert pr.status == oc_deps.PROBE_NOT_APPLICABLE
    assert pr.ok is True


def test_probe_auth_store_indeterminate_when_home_unresolvable(monkeypatch):
    monkeypatch.setattr(oc_store, "read_anthropic_key", lambda *a, **k: "")
    monkeypatch.setattr(oc_store, "read_auth_store", lambda *a, **k: None)
    monkeypatch.setattr(oc_store, "auth_store_present", lambda *a, **k: None)
    pr = oc_deps._probe_auth_store("bot", network={})
    assert pr.status == oc_deps.PROBE_INDETERMINATE
    assert pr.ok is False


# ── plugin_install_registry probe ─────────────────────────────────────────────


def test_probe_plugin_registry_not_applicable_when_absent(monkeypatch):
    monkeypatch.setattr(su, "_install_registry_exists", lambda user: False)
    monkeypatch.setattr("evolve_admin.ocadmin._gateway_runtime_user", lambda b, n: "bot")
    pr = oc_deps._probe_plugin_install_registry("bot", network={})
    assert pr.status == oc_deps.PROBE_NOT_APPLICABLE


def test_probe_plugin_registry_missing_when_present_but_unreadable(monkeypatch):
    monkeypatch.setattr(su, "_install_registry_exists", lambda user: True)
    monkeypatch.setattr(su, "_read_installs_json", lambda user: None)
    monkeypatch.setattr("evolve_admin.ocadmin._gateway_runtime_user", lambda b, n: "bot")
    pr = oc_deps._probe_plugin_install_registry("bot", network={})
    assert pr.status == oc_deps.PROBE_MISSING


def test_probe_plugin_registry_ok_when_readable(monkeypatch):
    monkeypatch.setattr(su, "_install_registry_exists", lambda user: True)
    monkeypatch.setattr(
        su, "_read_installs_json",
        lambda user: {"installRecords": {"brave": {}}, "_source": "state/openclaw.sqlite"},
    )
    monkeypatch.setattr("evolve_admin.ocadmin._gateway_runtime_user", lambda b, n: "bot")
    pr = oc_deps._probe_plugin_install_registry("bot", network={})
    assert pr.status == oc_deps.PROBE_OK
    assert "1 record" in pr.detail


# ── openclaw_json_schema probe ────────────────────────────────────────────────


def test_probe_openclaw_json_ok_when_gateway_present(monkeypatch):
    monkeypatch.setattr(su, "_read_bot_openclaw_json", lambda b, n: {"gateway": {"port": 1}})
    pr = oc_deps._probe_openclaw_json_schema("bot", network={})
    assert pr.status == oc_deps.PROBE_OK


def test_probe_openclaw_json_empty_when_gateway_missing(monkeypatch):
    monkeypatch.setattr(su, "_read_bot_openclaw_json", lambda b, n: {"channels": {}})
    pr = oc_deps._probe_openclaw_json_schema("bot", network={})
    assert pr.status == oc_deps.PROBE_EMPTY


def test_probe_openclaw_json_indeterminate_when_unreadable(monkeypatch):
    monkeypatch.setattr(su, "_read_bot_openclaw_json", lambda b, n: None)
    pr = oc_deps._probe_openclaw_json_schema("bot", network={})
    assert pr.status == oc_deps.PROBE_INDETERMINATE


# ── message_send_cli probe (pod-scope) ────────────────────────────────────────


def test_probe_message_send_ok(monkeypatch):
    monkeypatch.setattr(su, "probe_send_surface", lambda: {"status": su.SEND_SURFACE_OK})
    pr = oc_deps._probe_message_send_cli(None, network={})
    assert pr.status == oc_deps.PROBE_OK


def test_probe_message_send_missing_when_failed(monkeypatch):
    monkeypatch.setattr(
        su, "probe_send_surface",
        lambda: {"status": su.SEND_SURFACE_FAILED, "reason": "cli_not_found", "missing_flags": []},
    )
    pr = oc_deps._probe_message_send_cli(None, network={})
    assert pr.status == oc_deps.PROBE_MISSING


def test_probe_message_send_indeterminate_when_unverified(monkeypatch):
    monkeypatch.setattr(
        su, "probe_send_surface",
        lambda: {"status": su.SEND_SURFACE_UNVERIFIED, "reason": "probe_timeout"},
    )
    pr = oc_deps._probe_message_send_cli(None, network={})
    assert pr.status == oc_deps.PROBE_INDETERMINATE


# ── gate routing ──────────────────────────────────────────────────────────────


def _stub_bot_ids(monkeypatch, bot_ids):
    """Patch _bot_ids on the exact ocadmin module the gate resolves through
    (mirrors the helper in test_safe_upgrade)."""
    import importlib
    importlib.import_module("evolve_admin.ocadmin")
    target = sys.modules["evolve_admin.ocadmin"]
    monkeypatch.setattr(target, "_bot_ids", lambda network: list(bot_ids))


def _fake_dep(key, status, *, load_bearing, scope=oc_deps.SCOPE_PER_BOT):
    return oc_deps.OCDependency(
        key=key,
        artifact=f"{key}-artifact",
        access="json",
        scope=scope,
        load_bearing=load_bearing,
        why=f"{key} matters",
        probe=lambda bot_id, network=None, _s=status: oc_deps.ProbeResult(_s, f"{key}:{_s}"),
    )


def test_gate_passes_when_all_ok(monkeypatch):
    _stub_bot_ids(monkeypatch, ["b1", "b2"])
    monkeypatch.setattr(oc_deps, "OC_DEPENDENCIES", [
        _fake_dep("auth_store", oc_deps.PROBE_OK, load_bearing=True),
    ])
    result, reqs = su.gate_oc_data_dependencies({})
    assert result.ok is True
    assert reqs == []
    assert result.details["checked_bots"] == ["b1", "b2"]


def test_gate_fails_on_load_bearing_missing_simulating_auth_migration(monkeypatch):
    """The headline: a bot whose auth_store reader returns nothing → blocking."""
    _stub_bot_ids(monkeypatch, ["team-bot-a", "team-bot-b"])
    monkeypatch.setattr(oc_deps, "OC_DEPENDENCIES", [
        _fake_dep("auth_store", oc_deps.PROBE_MISSING, load_bearing=True),
    ])
    result, reqs = su.gate_oc_data_dependencies({})
    assert result.ok is False
    blockers = [r for r in reqs if r.blocking]
    assert len(blockers) == 2  # one per bot
    assert all(r.source_gate == "oc_data_dependencies" for r in blockers)
    assert any("team-bot-a" in r.id for r in blockers)


def test_gate_warns_not_fails_on_non_load_bearing_missing(monkeypatch):
    _stub_bot_ids(monkeypatch, ["b1"])
    monkeypatch.setattr(oc_deps, "OC_DEPENDENCIES", [
        _fake_dep("plugin_install_registry", oc_deps.PROBE_MISSING, load_bearing=False),
    ])
    result, reqs = su.gate_oc_data_dependencies({})
    assert result.ok is True  # WARN, not a hard block
    assert len(reqs) == 1
    assert reqs[0].blocking is False


def test_gate_warns_on_indeterminate(monkeypatch):
    _stub_bot_ids(monkeypatch, ["b1"])
    monkeypatch.setattr(oc_deps, "OC_DEPENDENCIES", [
        _fake_dep("auth_store", oc_deps.PROBE_INDETERMINATE, load_bearing=True),
    ])
    result, reqs = su.gate_oc_data_dependencies({})
    assert result.ok is True  # couldn't-verify never hard-blocks
    assert len(reqs) == 1
    assert reqs[0].blocking is False
    assert "unverified" in reqs[0].id


def test_gate_not_applicable_is_pass(monkeypatch):
    _stub_bot_ids(monkeypatch, ["b1"])
    monkeypatch.setattr(oc_deps, "OC_DEPENDENCIES", [
        _fake_dep("auth_store", oc_deps.PROBE_NOT_APPLICABLE, load_bearing=True),
    ])
    result, reqs = su.gate_oc_data_dependencies({})
    assert result.ok is True
    assert reqs == []


def test_gate_pod_scope_dep_runs_once(monkeypatch):
    _stub_bot_ids(monkeypatch, ["b1", "b2", "b3"])
    monkeypatch.setattr(oc_deps, "OC_DEPENDENCIES", [
        _fake_dep("message_send_cli", oc_deps.PROBE_MISSING,
                  load_bearing=False, scope=oc_deps.SCOPE_POD),
    ])
    result, reqs = su.gate_oc_data_dependencies({})
    assert len(reqs) == 1  # pod-scope → one finding regardless of bot count
    assert reqs[0].blocking is False


def test_gate_survives_a_probe_that_raises(monkeypatch):
    _stub_bot_ids(monkeypatch, ["b1"])

    def _boom(bot_id, network=None):
        raise RuntimeError("kaboom")

    dep = oc_deps.OCDependency(
        key="auth_store", artifact="x", access="json", scope=oc_deps.SCOPE_PER_BOT,
        load_bearing=True, why="x", probe=_boom,
    )
    monkeypatch.setattr(oc_deps, "OC_DEPENDENCIES", [dep])
    result, reqs = su.gate_oc_data_dependencies({})
    # A crashing probe degrades to an indeterminate WARN, never a hard block.
    assert result.ok is True
    assert len(reqs) == 1
    assert reqs[0].blocking is False


# ── manifest integrity ────────────────────────────────────────────────────────


def test_manifest_seeds_expected_dependencies():
    keys = {d.key for d in oc_deps.OC_DEPENDENCIES}
    assert {
        "auth_store", "plugin_install_registry", "openclaw_json_schema",
        "gateway_plist", "message_send_cli",
    } <= keys
    # auth_store — the dependency that broke on 2026-06-22 — is load-bearing.
    auth = next(d for d in oc_deps.OC_DEPENDENCIES if d.key == "auth_store")
    assert auth.load_bearing is True
    assert auth.access == "sqlite"


def test_manifest_access_kinds_are_valid():
    valid = {"sqlite", "json", "cli", "plist"}
    for d in oc_deps.OC_DEPENDENCIES:
        assert d.access in valid
        assert d.scope in (oc_deps.SCOPE_PER_BOT, oc_deps.SCOPE_POD)
        assert callable(d.probe)
