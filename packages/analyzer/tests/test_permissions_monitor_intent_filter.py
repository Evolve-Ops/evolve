"""Phase 5b — permission_monitor consults config_intent before emitting
perm_config_drift.

Spec: internal/spec-config-intent-system-2026-05-21.md §4 (investigate before
propose). The monitor-side companion to
``packages/analyzer/generators/auth_drift_filler/signal_proposals.py``
which already does the same lookup on the proposal side. Without the
monitor-side filter, the warn-severity drift signal kept firing forever
for intentional deviations (a hardened bot's workspaceOnly=true flag,
a bot whose exec=full was set as a plugin install side-effect, etc.) —
the proposal pipeline correctly suppressed the revert but the
operator's Alerts page stayed noisy.

These tests exercise the filter through both the helper directly and the
full ``_diff_one_bot`` path, so a future refactor that moves the filter
call can't silently lose intent-awareness.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# evolve_admin lives in a sibling package; make sure it imports cleanly
# from the analyzer test environment. The config_intent module is the
# canonical writer for intent sidecars, used here to construct test data.
from evolve_admin import config_intent as _ci
from permissions import baseline as _bl
from permissions import inventory as _inv
from permissions import monitor as _mon


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def _seed_bot(tmp_path: Path, bid: str, oc: dict) -> Path:
    home = tmp_path / "bots" / bid
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps(oc))
    return home


def _baseline_with_exec_deny() -> dict:
    """Pod baseline: every bot should have tools.exec.security=deny."""
    return {
        "schema_version": 1,
        "pod_default": {
            "permission_config": {"tools.exec.security": "deny"},
            "approvals_denylist": [],
            "approvals_volume": {"warn_count": 999, "alarm_count": 9999},
        },
        "per_bot_overrides": {},
    }


def _record_intent(
    shared_dir: Path, bot_id: str, field_path: str, value, *,
    set_by: str = "pod_admin (admin UI)",
    reason: str = "test fixture",
    depends_on: dict | None = None,
) -> dict:
    """Write a config_intent sidecar entry via the canonical writer."""
    return _ci.set_intent(
        bot_id=bot_id,
        field_path=field_path,
        value=value,
        set_by=set_by,
        reason=reason,
        depends_on=depends_on,
        shared_dir=shared_dir,
    )


# ── _filter_intentional_deviations (helper, direct) ─────────────────────────


def test_filter_drops_field_when_intent_value_matches_observed(shared_dir: Path):
    """Canonical case: recorded intent for exec=full, observed=full → dropped."""
    _record_intent(
        shared_dir, "team-bot-a", "tools.exec.security", "full",
        set_by="plugin_side_effect:codex",
        reason="codex needs exec",
    )
    diffs = {
        "tools.exec.security": {"expected": "deny", "observed": "full"},
    }

    filtered = _mon._filter_intentional_deviations("team-bot-a", diffs, shared_dir)

    assert filtered == {}, (
        "Intent value matches observed → field is an intentional "
        "deviation; the monitor must drop it before deciding whether "
        "to emit perm_config_drift"
    )


def test_filter_keeps_field_when_intent_value_no_longer_matches(shared_dir: Path):
    """Stale intent — operator changed observed back to something else
    after recording the intent. Real drift; surface it."""
    _record_intent(
        shared_dir, "team-bot-a", "tools.exec.security", "full",
        set_by="plugin_side_effect:codex",
        reason="codex needs exec",
    )
    diffs = {
        # observed=allowlist now, but the intent only blesses 'full'
        "tools.exec.security": {"expected": "deny", "observed": "allowlist"},
    }

    filtered = _mon._filter_intentional_deviations("team-bot-a", diffs, shared_dir)

    assert filtered == diffs


def test_filter_keeps_field_when_no_intent_recorded(shared_dir: Path):
    """No intent → real drift; signal must fire."""
    diffs = {
        "tools.exec.security": {"expected": "deny", "observed": "full"},
    }

    filtered = _mon._filter_intentional_deviations("noone", diffs, shared_dir)

    assert filtered == diffs


def test_filter_handles_partial_intent_coverage(shared_dir: Path):
    """Multi-field drift where ONE field has matching intent → that one
    is dropped, the other survives."""
    _record_intent(
        shared_dir, "team-bot-c", "tools.fs.workspaceOnly", True,
        reason="bot is intentionally workspace-hardened",
    )
    diffs = {
        "tools.fs.workspaceOnly": {"expected": None, "observed": True},
        "tools.exec.security": {"expected": "deny", "observed": "full"},
    }

    filtered = _mon._filter_intentional_deviations("team-bot-c", diffs, shared_dir)

    assert "tools.fs.workspaceOnly" not in filtered
    assert "tools.exec.security" in filtered


def test_filter_returns_empty_dict_unchanged(shared_dir: Path):
    """Empty input is a fast-path; no sidecar read needed."""
    assert _mon._filter_intentional_deviations("team-bot-a", {}, shared_dir) == {}


def test_filter_fails_open_when_get_intent_raises(
    shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Any exception during intent lookup must NOT drop the diff.

    Better to emit an explainable signal than silently suppress real
    drift if the sidecar got corrupted. Symmetric with the convention
    in evolve_admin.config_intent.get_intent itself.
    """
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated sidecar read failure")

    monkeypatch.setattr(_ci, "get_intent", _boom)
    diffs = {"tools.exec.security": {"expected": "deny", "observed": "full"}}

    filtered = _mon._filter_intentional_deviations("team-bot-a", diffs, shared_dir)

    assert filtered == diffs


def test_filter_fails_open_when_config_intent_module_absent(
    shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The lazy ``from evolve_admin.config_intent import …`` inside the
    helper must tolerate the module not being importable (e.g. analyzer
    package running in a test env without the admin package on path).

    Simulated by hiding evolve_admin from sys.modules and intercepting
    the import attempt. Mirrors the auth_drift_filler convention.
    """
    import sys
    # Drop the cached module + its submodules so the lazy import re-runs.
    purged = {name for name in list(sys.modules) if name.startswith("evolve_admin")}
    for name in purged:
        monkeypatch.delitem(sys.modules, name, raising=False)

    # Make any subsequent import raise.
    real_import = __import__

    def _block(name, *a, **kw):
        if name.startswith("evolve_admin"):
            raise ImportError(f"simulated: {name} not available")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _block)

    diffs = {"tools.exec.security": {"expected": "deny", "observed": "full"}}
    filtered = _mon._filter_intentional_deviations("team-bot-a", diffs, shared_dir)

    assert filtered == diffs


def test_filter_keeps_field_when_intent_no_longer_valid(
    shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
):
    """intent_still_valid=False (plugin-coupled intent whose plugin is
    gone, post-Phase-5) → surface the drift so auth_drift_filler can
    emit its stale_flagged proposal.

    Phase 1's intent_still_valid always returns True today; this test
    mocks it to False to exercise the branch in advance.
    """
    _record_intent(
        shared_dir, "team-bot-a", "tools.exec.security", "full",
        set_by="plugin_side_effect:codex",
        reason="codex needs exec",
        depends_on={"plugin": "codex"},
    )
    monkeypatch.setattr(_ci, "intent_still_valid", lambda *_a, **_kw: False)
    diffs = {"tools.exec.security": {"expected": "deny", "observed": "full"}}

    filtered = _mon._filter_intentional_deviations("team-bot-a", diffs, shared_dir)

    assert filtered == diffs


# ── _diff_one_bot end-to-end ─────────────────────────────────────────────────


def test_diff_one_bot_suppresses_drift_signal_when_all_fields_intentional(
    tmp_path: Path, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The full integration check: a bot whose only drifted field has a
    matching intent record must produce ZERO perm_config_drift findings.
    """
    home = _seed_bot(tmp_path, "team-bot-a", {
        "tools": {"exec": {"security": "full"}},
    })
    monkeypatch.setattr(_inv, "bot_home", lambda bid, *a, **kw: home)

    _record_intent(
        shared_dir, "team-bot-a", "tools.exec.security", "full",
        set_by="plugin_side_effect:codex",
        reason="codex needs exec",
    )

    inv = _inv.read_inventory("team-bot-a", home_override=home)
    findings = _mon._diff_one_bot(inv, _baseline_with_exec_deny(), shared_dir)

    drift = [f for f in findings if f["type"] == "perm_config_drift"]
    assert drift == [], (
        f"Expected no perm_config_drift signal (intent is recorded), got: {drift!r}"
    )


def test_diff_one_bot_emits_partial_drift_when_some_fields_unexplained(
    tmp_path: Path, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Two drifted fields where only one has a matching intent → the
    signal fires but its ``details.diffs`` carries the unexplained field
    only. Operator's Alerts row shows the actionable subset.
    """
    home = _seed_bot(tmp_path, "team-bot-a", {
        "tools": {
            "exec": {"security": "full"},
            "fs": {"workspaceOnly": True},
        },
    })
    monkeypatch.setattr(_inv, "bot_home", lambda bid, *a, **kw: home)

    _record_intent(
        shared_dir, "team-bot-a", "tools.exec.security", "full",
        set_by="plugin_side_effect:codex",
        reason="codex needs exec",
    )

    # Baseline expects deny + workspaceOnly=False; bot drifts on both.
    baseline = _baseline_with_exec_deny()
    baseline["pod_default"]["permission_config"]["tools.fs.workspaceOnly"] = False

    inv = _inv.read_inventory("team-bot-a", home_override=home)
    findings = _mon._diff_one_bot(inv, baseline, shared_dir)

    drift = [f for f in findings if f["type"] == "perm_config_drift"]
    assert len(drift) == 1, f"Expected one drift signal, got: {drift!r}"
    diffs = drift[0]["details"]["diffs"]
    assert "tools.fs.workspaceOnly" in diffs
    assert "tools.exec.security" not in diffs, (
        "exec.security is intent-explained; only workspaceOnly should "
        "carry through to the operator-visible diff"
    )


def test_diff_one_bot_emits_drift_when_no_intent_recorded(
    tmp_path: Path, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Baseline expectation: existing per_config_drift behavior is preserved
    when no intent exists for the deviation."""
    home = _seed_bot(tmp_path, "drifter", {
        "tools": {"exec": {"security": "full"}},
    })
    monkeypatch.setattr(_inv, "bot_home", lambda bid, *a, **kw: home)

    inv = _inv.read_inventory("drifter", home_override=home)
    findings = _mon._diff_one_bot(inv, _baseline_with_exec_deny(), shared_dir)

    drift = [f for f in findings if f["type"] == "perm_config_drift"]
    assert len(drift) == 1
    assert drift[0]["details"]["diffs"]["tools.exec.security"] == {
        "expected": "deny", "observed": "full",
    }
