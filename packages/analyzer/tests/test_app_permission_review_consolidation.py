"""Tests for app_permission_review's pod-aware consolidation pass.

The consolidator takes per-app candidate findings and cross-references
them against sibling manifests on the same bot. Three outcomes per
candidate (parent spec §5):

- No sibling reference → emit as-is
- Sibling declares the same resource → annotate "still in effect"
- Sibling uses but doesn't declare → convert to MOVE proposal

For ``*_missing_declaration`` findings, two outcomes (no sibling, or
sibling already declares).

For ``*_overkill_wildcard``, no consolidation — pass through.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from generators.app_permission_review.consolidation import (
    OUTCOME_AS_IS,
    OUTCOME_MOVE_TO_SIBLING,
    OUTCOME_SIBLING_ALREADY_HAS,
    OUTCOME_SIBLING_DECLARES,
    build_sibling_index,
    consolidate,
)
from generators.app_permission_review.review import (
    KIND_EGRESS_MISSING_DECLARATION,
    KIND_EGRESS_OVERKILL_WILDCARD,
    KIND_EXEC_MISSING_DECLARATION,
    KIND_EXEC_OVERKILL_WILDCARD,
    KIND_EXEC_UNUSED,
    KIND_NETWORK_EGRESS_UNUSED,
    Finding,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_finding(
    kind: str,
    *,
    app_id: str = "i-a",
    app_name: str = "App A",
    entry_kind: str = "exec",
    entry_value: str = "scripts/foo.py",
    bot_id: str = "team_bot_a",
    severity: str = "warn",
) -> Finding:
    return Finding(
        kind=kind, bot_id=bot_id, app_id=app_id, app_name=app_name,
        entry_kind=entry_kind, entry_value=entry_value,
        severity=severity, rationale="test",
    )


def _make_workspace(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    for rel, body in (files or {}).items():
        full = ws / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
    return ws


# ── Sibling index ────────────────────────────────────────────────────────────


def test_sibling_index_tracks_declared(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    manifests = [
        {"id": "i-a", "permissions": {"exec": ["scripts/foo.py"]}},
        {"id": "i-b", "permissions": {"exec": ["scripts/foo.py"]}},
        {"id": "i-c", "permissions": {"exec": ["scripts/bar.py"]}},
    ]
    idx = build_sibling_index(manifests, ws)
    declared = idx.declared.get(("exec", "scripts/foo.py"))
    assert sorted(declared or []) == ["i-a", "i-b"]
    assert idx.declared.get(("exec", "scripts/bar.py")) == ["i-c"]


def test_sibling_index_tracks_used_exec_from_files(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/foo.py": "# foo",
        "scripts/bar.py": "# bar",
    })
    manifests = [
        {"id": "i-a", "files": [{"path": "scripts/foo.py", "layer": "script"}],
         "permissions": {}},  # uses foo.py, doesn't declare it
        {"id": "i-b", "files": [{"path": "scripts/bar.py", "layer": "script"}],
         "permissions": {}},
    ]
    idx = build_sibling_index(manifests, ws)
    assert idx.used.get(("exec", "scripts/foo.py")) == ["i-a"]
    assert idx.used.get(("exec", "scripts/bar.py")) == ["i-b"]


def test_sibling_index_tracks_used_egress_from_grep(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/foo.py": "url = 'https://api.example.com/v1'",
    })
    manifests = [
        {"id": "i-a", "files": [{"path": "scripts/foo.py", "layer": "script"}],
         "permissions": {}},
    ]
    idx = build_sibling_index(manifests, ws)
    assert idx.used.get(("network_egress", "api.example.com")) == ["i-a"]


def test_siblings_declaring_excludes_self(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    manifests = [
        {"id": "i-a", "permissions": {"exec": ["scripts/foo.py"]}},
        {"id": "i-b", "permissions": {"exec": ["scripts/foo.py"]}},
    ]
    idx = build_sibling_index(manifests, ws)
    assert idx.siblings_declaring("exec", "scripts/foo.py", exclude_app_id="i-a") == ["i-b"]
    assert idx.siblings_declaring("exec", "scripts/foo.py", exclude_app_id="i-b") == ["i-a"]


def test_siblings_using_undeclared(tmp_path: Path):
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifests = [
        {"id": "i-a", "permissions": {"exec": ["scripts/foo.py"]}},  # declares
        {"id": "i-b", "files": [{"path": "scripts/foo.py", "layer": "script"}],
         "permissions": {}},  # uses without declaring
    ]
    idx = build_sibling_index(manifests, ws)
    # From i-a's perspective: sibling i-b uses foo.py but doesn't declare
    using_undeclared = idx.siblings_using_undeclared(
        "exec", "scripts/foo.py", exclude_app_id="i-a",
    )
    assert using_undeclared == ["i-b"]


# ── Outcome: AS_IS (no sibling reference) ────────────────────────────────────


def test_unused_finding_with_no_siblings_passes_through(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    manifests = [
        {"id": "i-a", "permissions": {"exec": ["scripts/ghost.py"]}},
        # No sibling declares or uses ghost.py
        {"id": "i-b", "permissions": {"exec": ["scripts/other.py"]}},
    ]
    finding = _make_finding(
        KIND_EXEC_UNUSED, app_id="i-a", entry_value="scripts/ghost.py",
    )
    result = consolidate([finding], manifests, ws)
    assert len(result) == 1
    assert result[0].outcome == OUTCOME_AS_IS
    assert result[0].sibling_apps == []


# ── Outcome: SIBLING_DECLARES ────────────────────────────────────────────────


def test_unused_finding_with_sibling_declaring_annotated(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    manifests = [
        {"id": "i-a", "permissions": {"exec": ["scripts/foo.py"]}},
        {"id": "i-b", "permissions": {"exec": ["scripts/foo.py"]}},
    ]
    finding = _make_finding(
        KIND_EXEC_UNUSED, app_id="i-a", entry_value="scripts/foo.py",
    )
    result = consolidate([finding], manifests, ws)
    assert len(result) == 1
    assert result[0].outcome == OUTCOME_SIBLING_DECLARES
    assert result[0].sibling_apps == ["i-b"]


def test_unused_finding_with_multiple_sibling_declarations(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    manifests = [
        {"id": "i-a", "permissions": {"exec": ["scripts/foo.py"]}},
        {"id": "i-b", "permissions": {"exec": ["scripts/foo.py"]}},
        {"id": "i-c", "permissions": {"exec": ["scripts/foo.py"]}},
    ]
    finding = _make_finding(
        KIND_EXEC_UNUSED, app_id="i-a", entry_value="scripts/foo.py",
    )
    result = consolidate([finding], manifests, ws)
    assert result[0].outcome == OUTCOME_SIBLING_DECLARES
    assert sorted(result[0].sibling_apps) == ["i-b", "i-c"]


# ── Outcome: MOVE_TO_SIBLING ─────────────────────────────────────────────────


def test_unused_finding_with_sibling_using_undeclared_converts_to_move(tmp_path: Path):
    """app A declares foo.py but doesn't use it; sibling B uses it but
    doesn't declare. The consolidator should emit a MOVE proposal."""
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifests = [
        # i-a declares but isn't the one using it
        {"id": "i-a", "permissions": {"exec": ["scripts/foo.py"]}},
        # i-b uses it (via files[]) without declaring
        {"id": "i-b", "files": [{"path": "scripts/foo.py", "layer": "script"}],
         "permissions": {}},  # block exists but no exec declarations
    ]
    finding = _make_finding(
        KIND_EXEC_UNUSED, app_id="i-a", entry_value="scripts/foo.py",
    )
    result = consolidate([finding], manifests, ws)
    assert result[0].outcome == OUTCOME_MOVE_TO_SIBLING
    assert result[0].sibling_apps == ["i-b"]


def test_unused_finding_prefers_declares_over_move(tmp_path: Path):
    """If a sibling DECLARES the entry AND another sibling uses-without-declaring,
    we should emit the DECLARES outcome (not the move). Declaration is the
    cleaner state — moving would create a duplicate."""
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifests = [
        {"id": "i-a", "permissions": {"exec": ["scripts/foo.py"]}},
        {"id": "i-b", "permissions": {"exec": ["scripts/foo.py"]}},  # also declares
        {"id": "i-c", "files": [{"path": "scripts/foo.py", "layer": "script"}],
         "permissions": {}},  # uses without declaring
    ]
    finding = _make_finding(
        KIND_EXEC_UNUSED, app_id="i-a", entry_value="scripts/foo.py",
    )
    result = consolidate([finding], manifests, ws)
    assert result[0].outcome == OUTCOME_SIBLING_DECLARES
    assert result[0].sibling_apps == ["i-b"]


# ── Outcome: SIBLING_ALREADY_HAS for missing_declaration findings ────────────


def test_missing_declaration_with_sibling_already_declaring(tmp_path: Path):
    """If app A is missing a declaration but sibling B has it, annotate."""
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifests = [
        # i-a has a permissions block but doesn't declare foo.py
        {"id": "i-a", "files": [{"path": "scripts/foo.py", "layer": "script"}],
         "permissions": {"fs_read": ["/Users/Shared/x"]}},
        # i-b also has the file AND declares it
        {"id": "i-b", "files": [{"path": "scripts/foo.py", "layer": "script"}],
         "permissions": {"exec": ["scripts/foo.py"]}},
    ]
    finding = _make_finding(
        KIND_EXEC_MISSING_DECLARATION, app_id="i-a",
        entry_value="scripts/foo.py",
    )
    result = consolidate([finding], manifests, ws)
    assert result[0].outcome == OUTCOME_SIBLING_ALREADY_HAS
    assert result[0].sibling_apps == ["i-b"]


def test_missing_declaration_no_sibling_passes_through(tmp_path: Path):
    """No sibling declares the resource → emit as-is."""
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifests = [
        {"id": "i-a", "files": [{"path": "scripts/foo.py", "layer": "script"}],
         "permissions": {"fs_read": ["/x"]}},
        {"id": "i-b", "permissions": {"exec": ["something/else.py"]}},
    ]
    finding = _make_finding(
        KIND_EXEC_MISSING_DECLARATION, app_id="i-a",
        entry_value="scripts/foo.py",
    )
    result = consolidate([finding], manifests, ws)
    assert result[0].outcome == OUTCOME_AS_IS


# ── Overkill: always pass-through ────────────────────────────────────────────


def test_overkill_findings_pass_through_unchanged(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    manifests = [
        {"id": "i-a", "permissions": {"exec": ["scripts/*.py"]}},
        {"id": "i-b", "permissions": {"exec": ["scripts/*.py"]}},
    ]
    finding = _make_finding(
        KIND_EXEC_OVERKILL_WILDCARD, app_id="i-a", entry_value="scripts/*.py",
    )
    result = consolidate([finding], manifests, ws)
    assert result[0].outcome == OUTCOME_AS_IS
    assert result[0].sibling_apps == []


# ── Mixed outcomes in one consolidation run ──────────────────────────────────


def test_consolidate_handles_mixed_kinds(tmp_path: Path):
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifests = [
        {"id": "i-a", "permissions": {"exec": ["scripts/ghost.py"]}},
        {"id": "i-b", "permissions": {"exec": ["scripts/ghost.py"]}},  # sibling declares ghost
        {"id": "i-c", "permissions": {"exec": ["scripts/orphan.py"]}},
    ]
    findings = [
        _make_finding(KIND_EXEC_UNUSED, app_id="i-a", entry_value="scripts/ghost.py"),
        _make_finding(KIND_EXEC_UNUSED, app_id="i-c", entry_value="scripts/orphan.py"),
        _make_finding(KIND_EXEC_OVERKILL_WILDCARD, app_id="i-a", entry_value="scripts/*.py"),
    ]
    result = consolidate(findings, manifests, ws)
    assert len(result) == 3
    # ghost.py - sibling declares
    assert result[0].outcome == OUTCOME_SIBLING_DECLARES
    # orphan.py - nothing
    assert result[1].outcome == OUTCOME_AS_IS
    # overkill - pass through
    assert result[2].outcome == OUTCOME_AS_IS


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_empty_candidates_returns_empty(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    assert consolidate([], [], ws) == []


def test_empty_manifest_set_all_pass_through(tmp_path: Path):
    """No siblings to cross-reference → every candidate is as-is."""
    ws = _make_workspace(tmp_path)
    findings = [
        _make_finding(KIND_EXEC_UNUSED, entry_value="x.py"),
        _make_finding(KIND_EGRESS_MISSING_DECLARATION,
                     entry_kind="network_egress", entry_value="api.example.com"),
    ]
    result = consolidate(findings, [], ws)
    assert all(r.outcome == OUTCOME_AS_IS for r in result)


def test_unknown_finding_kind_passes_through_as_is(tmp_path: Path):
    """Defensive: unknown kinds shouldn't be dropped."""
    ws = _make_workspace(tmp_path)
    finding = _make_finding("unknown_future_kind", entry_value="x")
    result = consolidate([finding], [], ws)
    assert len(result) == 1
    assert result[0].outcome == OUTCOME_AS_IS


# ── Network-egress consolidation works the same way ─────────────────────────


def test_egress_unused_with_sibling_using(tmp_path: Path):
    """Same outcome shape applies to network_egress findings."""
    ws = _make_workspace(tmp_path, {
        "scripts/a.py": "# no host references",
        "scripts/b.py": "url = 'https://api.example.com/v1'",
    })
    manifests = [
        # i-a declares api.example.com but doesn't grep-match it
        {"id": "i-a", "files": [{"path": "scripts/a.py", "layer": "script"}],
         "permissions": {"network_egress": ["api.example.com"]}},
        # i-b uses api.example.com but doesn't declare it
        {"id": "i-b", "files": [{"path": "scripts/b.py", "layer": "script"}],
         "permissions": {}},
    ]
    finding = _make_finding(
        KIND_NETWORK_EGRESS_UNUSED, app_id="i-a",
        entry_kind="network_egress", entry_value="api.example.com",
        severity="info",
    )
    result = consolidate([finding], manifests, ws)
    assert result[0].outcome == OUTCOME_MOVE_TO_SIBLING
    assert result[0].sibling_apps == ["i-b"]
