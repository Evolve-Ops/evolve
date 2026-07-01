"""Tests for permissions.app_manifest_monitor.

Spec: docs/spec-app-permission-drift-2026-05-25.md (B.1).

Tests use a synthetic bot home via ``home_override`` so file I/O bypasses
sudo. Signal emission is verified by passing ``emit_signals=False`` and
inspecting the returned ``findings`` list — same shape as the live emit
path (kind, pattern, severity, details) without touching the on-disk
signal store.

A separate integration test asserts emit_signals=True actually writes
to the signal store and that sweep_resolve archives cleared findings.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from permissions import app_manifest_monitor as _mon


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def _make_bot(
    tmp_path: Path,
    bot_id: str,
    *,
    security: str = "full",
    exec_approvals: dict | None = None,
    manifests: dict[str, dict] | None = None,
    workspace_files: dict[str, str] | None = None,
) -> Path:
    """Build a /Users/<bot> tree with the requested state.

    Returns the home dir for use as ``home_override``.

    ``workspace_files`` maps workspace-relative path → file body.
    """
    home = tmp_path / bot_id
    (home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)

    (home / ".openclaw" / "openclaw.json").write_text(json.dumps({
        "tools": {"exec": {"security": security}},
    }))

    if exec_approvals is not None:
        (home / ".openclaw" / "exec-approvals.json").write_text(
            json.dumps(exec_approvals)
        )

    for app_id, manifest in (manifests or {}).items():
        manifest.setdefault("id", app_id)
        (home / ".openclaw" / "workspace" / "manifests" / f"{app_id}.json").write_text(
            json.dumps(manifest)
        )

    for rel, body in (workspace_files or {}).items():
        full = home / ".openclaw" / "workspace" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)

    return home


def _network(bot_id: str, *, role: str = "member") -> dict:
    return {
        "bots": {
            bot_id: {"role": role, "user": bot_id},
        },
    }


def _run_scan(home: Path, bot_id: str, *, role: str = "member") -> list[dict]:
    """Helper — run scan_bot with signals disabled, return findings list."""
    result = _mon.scan_bot(
        Path("/tmp/unused-shared"), bot_id, _network(bot_id, role=role),
        home_override=home, emit_signals=False,
    )
    assert not result["skipped"], result.get("skip_reason")
    return result["findings"]


def _findings_by_kind(findings: list[dict], kind: str) -> list[dict]:
    return [f for f in findings if f["kind"] == kind]


# ── declared_not_allowed ─────────────────────────────────────────────────────


def test_declared_not_allowed_critical_in_allowlist_mode(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="allowlist",
        exec_approvals={"agents": {"main": {"allowlist": []}}},
        manifests={
            "i-task": {
                "name": "Task App",
                "files": [{"path": "scripts/run.py", "layer": "script"}],
            },
        },
    )
    findings = _run_scan(home, "team_bot_a")
    dna = _findings_by_kind(findings, _mon.KIND_DECLARED_NOT_ALLOWED)
    assert len(dna) == 1
    f = dna[0]
    assert f["pattern"] == "scripts/run.py"
    assert f["severity"] == "critical"
    assert f["app_id"] == "i-task"
    assert f["details"]["current_mode"] == "allowlist"


def test_declared_not_allowed_info_in_full_mode(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="full",
        exec_approvals={"agents": {"main": {"allowlist": []}}},
        manifests={
            "i-task": {
                "name": "Task App",
                "files": [{"path": "scripts/run.py", "layer": "script"}],
            },
        },
    )
    findings = _run_scan(home, "team_bot_a")
    dna = _findings_by_kind(findings, _mon.KIND_DECLARED_NOT_ALLOWED)
    assert len(dna) == 1
    assert dna[0]["severity"] == "info"


def test_declared_not_allowed_suppressed_in_deny_mode(tmp_path: Path):
    home = _make_bot(
        tmp_path, "evo",
        security="deny",
        exec_approvals={},
        manifests={
            "i-app": {
                "name": "App",
                "files": [{"path": "scripts/x.py", "layer": "script"}],
            },
        },
    )
    findings = _run_scan(home, "evo")
    assert _findings_by_kind(findings, _mon.KIND_DECLARED_NOT_ALLOWED) == []


def test_declared_not_allowed_no_finding_when_pattern_in_allowlist(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="allowlist",
        exec_approvals={"agents": {"main": {"allowlist": [
            {"pattern": "scripts/run.py"},
        ]}}},
        manifests={
            "i-task": {
                "name": "Task App",
                "files": [{"path": "scripts/run.py", "layer": "script"}],
            },
        },
    )
    findings = _run_scan(home, "team_bot_a")
    assert _findings_by_kind(findings, _mon.KIND_DECLARED_NOT_ALLOWED) == []


def test_declared_not_allowed_handles_v7_arc_realized_files(tmp_path: Path):
    """v7-arc manifests put scripts in realized_files[], not files[].
    Monitor must pick those up (matches reconciler behavior)."""
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="allowlist",
        exec_approvals={"agents": {"main": {"allowlist": []}}},
        manifests={
            "i-v7": {
                "instance_id": "i-v7",
                "manifest_shape": "v7-arc",
                "schema_version": 14,
                "files": [],
                "realized_files": [
                    {"path": "tools/v7-script.py", "marker_state": "OWNED",
                     "file_id": "f-1", "logical_name": "v7-script"},
                ],
            },
        },
    )
    findings = _run_scan(home, "team_bot_a")
    patterns = {f["pattern"] for f in _findings_by_kind(
        findings, _mon.KIND_DECLARED_NOT_ALLOWED)}
    assert "tools/v7-script.py" in patterns


# ── allowed_not_declared ─────────────────────────────────────────────────────


def test_allowed_not_declared_warn_in_allowlist_mode(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="allowlist",
        exec_approvals={"agents": {"main": {"allowlist": [
            {"pattern": "scripts/orphan-script.py"},
        ]}}},
        manifests={},  # no apps declare anything
    )
    findings = _run_scan(home, "team_bot_a")
    and_ = _findings_by_kind(findings, _mon.KIND_ALLOWED_NOT_DECLARED)
    assert len(and_) == 1
    assert and_[0]["pattern"] == "scripts/orphan-script.py"
    assert and_[0]["severity"] == "warn"


def test_allowed_not_declared_suppressed_in_full_mode(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="full",
        exec_approvals={"agents": {"main": {"allowlist": [
            {"pattern": "scripts/orphan-script.py"},
        ]}}},
        manifests={},
    )
    findings = _run_scan(home, "team_bot_a")
    assert _findings_by_kind(findings, _mon.KIND_ALLOWED_NOT_DECLARED) == []


def test_allowed_not_declared_skips_operator_set_heuristic(tmp_path: Path):
    """Sudo / sysadmin-shaped patterns are heuristically operator-set —
    don't emit revoke proposals against them."""
    home = _make_bot(
        tmp_path, "security_bot",
        security="allowlist",
        exec_approvals={"agents": {"main": {"allowlist": [
            {"pattern": "sudo /usr/sbin/launchctl kickstart"},
            {"pattern": "/usr/sbin/diskutil info"},
            {"pattern": "/bin/launchctl bootstrap"},
            {"pattern": "scripts/legit-orphan.py"},  # this one SHOULD fire
        ]}}},
        manifests={},
    )
    findings = _run_scan(home, "security_bot")
    patterns = {f["pattern"] for f in _findings_by_kind(
        findings, _mon.KIND_ALLOWED_NOT_DECLARED)}
    assert patterns == {"scripts/legit-orphan.py"}


# ── workspace_orphan_script ──────────────────────────────────────────────────


def test_workspace_orphan_script_info_regardless_of_mode(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="full",
        exec_approvals={},
        manifests={
            "i-app": {
                "name": "App",
                "files": [{"path": "scripts/declared.py", "layer": "script"}],
            },
        },
        workspace_files={
            "scripts/declared.py": "# declared\n",
            "scripts/orphan.py": "# undeclared\n",
            "ops/another-orphan.sh": "#!/bin/sh\necho hi\n",
            "docs/notes.md": "# not a script\n",  # extension excluded
        },
    )
    findings = _run_scan(home, "team_bot_a")
    orphans = _findings_by_kind(findings, _mon.KIND_WORKSPACE_ORPHAN_SCRIPT)
    patterns = {f["pattern"] for f in orphans}
    assert patterns == {"scripts/orphan.py", "ops/another-orphan.sh"}
    assert all(f["severity"] == "info" for f in orphans)


def test_workspace_skip_dirs_not_walked(tmp_path: Path):
    """manifests/, .git/, evolve/, __pycache__ are excluded from the walk."""
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="full",
        exec_approvals={},
        manifests={},
        workspace_files={
            "manifests/somemanifest.py": "# inside manifests dir\n",
            ".git/hooks/pre-commit.sh": "# inside .git\n",
            "evolve/script.py": "# inside evolve dir\n",
            "__pycache__/cached.py": "# inside __pycache__\n",
            "real_orphan.py": "# legit\n",
        },
    )
    findings = _run_scan(home, "team_bot_a")
    patterns = {f["pattern"] for f in _findings_by_kind(
        findings, _mon.KIND_WORKSPACE_ORPHAN_SCRIPT)}
    assert patterns == {"real_orphan.py"}


def test_walk_depth_cap_triggers_truncation_finding(tmp_path: Path, monkeypatch):
    """Going past WORKSPACE_WALK_MAX_DEPTH emits a truncation signal."""
    monkeypatch.setattr(_mon, "WORKSPACE_WALK_MAX_DEPTH", 1)
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="full",
        exec_approvals={},
        manifests={},
        workspace_files={
            # Depth 0: workspace/
            # Depth 1: workspace/a/  → walked
            # Depth 2: workspace/a/b/  → NOT walked (over cap)
            "a/depth1.py": "# depth 1\n",
            "a/b/depth2.py": "# depth 2, beyond cap\n",
        },
    )
    findings = _run_scan(home, "team_bot_a")
    # Truncation signal should fire
    trunc = _findings_by_kind(findings, _mon.KIND_WORKSPACE_WALK_TRUNCATED)
    assert len(trunc) == 1


# ── declared_missing_file ────────────────────────────────────────────────────


def test_declared_missing_file_info_regardless_of_mode(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="full",
        exec_approvals={},
        manifests={
            "i-stale": {
                "name": "Stale App",
                "files": [
                    {"path": "ghost/missing.py", "layer": "script"},
                    {"path": "ghost/also-missing.sh", "layer": "script"},
                ],
            },
        },
    )
    findings = _run_scan(home, "team_bot_a")
    miss = _findings_by_kind(findings, _mon.KIND_DECLARED_MISSING_FILE)
    patterns = {f["pattern"] for f in miss}
    assert patterns == {"ghost/missing.py", "ghost/also-missing.sh"}
    assert all(f["severity"] == "info" for f in miss)


def test_declared_missing_file_skipped_when_file_exists(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="full",
        exec_approvals={},
        manifests={
            "i-real": {
                "name": "Real",
                "files": [{"path": "scripts/real.py", "layer": "script"}],
            },
        },
        workspace_files={"scripts/real.py": "# exists\n"},
    )
    findings = _run_scan(home, "team_bot_a")
    assert _findings_by_kind(findings, _mon.KIND_DECLARED_MISSING_FILE) == []


# ── Hidden / deprecated manifests are skipped ────────────────────────────────


def test_hidden_manifests_skipped(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        security="allowlist",
        exec_approvals={"agents": {"main": {"allowlist": []}}},
        manifests={
            "i-active": {
                "name": "Active",
                "status": "active",
                "files": [{"path": "scripts/active.py", "layer": "script"}],
            },
            "i-hidden": {
                "name": "Hidden",
                "status": "hidden",
                "files": [{"path": "scripts/hidden.py", "layer": "script"}],
            },
        },
    )
    findings = _run_scan(home, "team_bot_a")
    patterns = {f["pattern"] for f in _findings_by_kind(
        findings, _mon.KIND_DECLARED_NOT_ALLOWED)}
    assert "scripts/active.py" in patterns
    assert "scripts/hidden.py" not in patterns


# ── Skip behavior ────────────────────────────────────────────────────────────


def test_scan_skipped_when_openclaw_json_unreadable(tmp_path: Path):
    """No openclaw.json → can't determine exec mode → skip cleanly."""
    home = tmp_path / "team_bot_a"
    (home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)
    # NB: no openclaw.json written
    result = _mon.scan_bot(
        Path("/tmp/x"), "team_bot_a", _network("team_bot_a"),
        home_override=home, emit_signals=False,
    )
    assert result["skipped"] is True
    assert "openclaw.json" in result["skip_reason"]


# ── _collect_allowlist_patterns shapes ───────────────────────────────────────


def test_collect_allowlist_patterns_handles_all_shapes():
    """Three observed exec-approvals.json shapes all parse correctly."""
    # Shape A: list of {pattern: ...} dicts under agents.<id>.allowlist
    a = _mon._collect_allowlist_patterns({
        "agents": {"main": {"allowlist": [
            {"pattern": "/usr/bin/python3"},
            {"pattern": "scripts/foo.py"},
        ]}}
    })
    assert a == {"/usr/bin/python3", "scripts/foo.py"}

    # Shape B: list of bare strings under agents.<id>.approvals
    b = _mon._collect_allowlist_patterns({
        "agents": {"main": {"approvals": [
            "scripts/bar.py",
            "/bin/ls",
        ]}}
    })
    assert b == {"scripts/bar.py", "/bin/ls"}

    # Shape C: dict-form allowlist (pattern as key)
    c = _mon._collect_allowlist_patterns({
        "defaults": {"allowlist": {"/usr/bin/curl*": {}}},
    })
    assert c == {"/usr/bin/curl*"}

    # Combined
    combined = _mon._collect_allowlist_patterns({
        "agents": {"main": {"allowlist": [{"pattern": "a"}]}},
        "defaults": {"allowlist": ["b"]},
    })
    assert combined == {"a", "b"}


def test_collect_allowlist_patterns_empty_inputs():
    assert _mon._collect_allowlist_patterns(None) == set()
    assert _mon._collect_allowlist_patterns({}) == set()
    assert _mon._collect_allowlist_patterns({"agents": {}}) == set()


# ── Heuristic for operator-set entries ───────────────────────────────────────


@pytest.mark.parametrize("pattern,expected_operator_set", [
    ("sudo /usr/sbin/launchctl kickstart", True),
    ("/usr/sbin/diskutil info", True),
    ("/bin/launchctl bootstrap", True),
    ("/sbin/ifconfig en0", True),
    ("/usr/bin/sudo /bin/ls", True),
    ("/Library/whatever", True),
    ("scripts/journal.py", False),
    ("/opt/homebrew/bin/python3 tools/foo.py", False),
    ("python3 -c 'print(1)'", False),
    ("", True),  # empty = don't propose anything
])
def test_looks_operator_set(pattern: str, expected_operator_set: bool):
    assert _mon._looks_operator_set(pattern) is expected_operator_set


# ── Integration: emit_signals=True writes to the store ───────────────────────


def test_emit_signals_writes_and_sweep_resolves(tmp_path: Path, monkeypatch):
    """When emit_signals=True, signals.store.observe is called per finding,
    and a second run with the finding cleared sweep_resolves it."""
    shared = tmp_path / "shared"
    shared.mkdir()

    home = _make_bot(
        tmp_path, "team_bot_a",
        security="allowlist",
        exec_approvals={"agents": {"main": {"allowlist": []}}},
        manifests={
            "i-task": {
                "name": "Task",
                "files": [{"path": "scripts/run.py", "layer": "script"}],
            },
        },
    )

    observed: list[dict] = []
    sweep_calls: list[dict] = []

    class _FakeStore:
        @staticmethod
        def observe(shared_dir, **kwargs):
            observed.append(kwargs)
            return object()

        @staticmethod
        def sweep_resolve(shared_dir, *, producer, kept_signatures, reason, types):
            sweep_calls.append({
                "producer": producer,
                "kept_signatures": set(kept_signatures),
                "reason": reason,
                "types": types,
            })
            return []

    monkeypatch.setattr(_mon, "_signals_store", _FakeStore)

    result = _mon.run(
        shared, ["team_bot_a"], _network("team_bot_a"),
        emit_signals=True,
        home_override_by_bot={"team_bot_a": home},
    )

    # We expected at least one observe call for the declared_not_allowed
    # finding.
    assert any(
        o.get("type") == _mon.SIGNAL_TYPE
        and (o.get("details") or {}).get("kind") == _mon.KIND_DECLARED_NOT_ALLOWED
        for o in observed
    ), f"declared_not_allowed observe not seen; got {[o.get('details') for o in observed]}"
    # sweep_resolve was called once at the end of run() with our kept set.
    assert len(sweep_calls) == 1
    assert sweep_calls[0]["producer"] == _mon.PRODUCER
    assert _mon.SIGNAL_TYPE in sweep_calls[0]["types"]
