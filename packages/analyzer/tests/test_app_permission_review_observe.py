"""End-to-end tests for app_permission_review.observe()."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from generators.app_permission_review.observe import (
    AppPermissionReviewContext,
    observe,
)
from generators.app_permission_review.review import (
    KIND_EXEC_MISSING_DECLARATION,
    KIND_EXEC_UNUSED,
)
from schema.proposal import Investigation


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_bot(
    tmp_path: Path,
    bot_id: str,
    *,
    manifests: dict[str, dict] | None = None,
    workspace_files: dict[str, str] | None = None,
) -> Path:
    home = tmp_path / bot_id
    (home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)
    for app_id, m in (manifests or {}).items():
        m.setdefault("id", app_id)
        (home / ".openclaw" / "workspace" / "manifests" / f"{app_id}.json").write_text(
            json.dumps(m)
        )
    for rel, body in (workspace_files or {}).items():
        full = home / ".openclaw" / "workspace" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
    return home


def _run(home: Path, bot_id: str):
    return observe(AppPermissionReviewContext(
        bot_id=bot_id,
        shared_dir=Path("/tmp/unused"),
        home_override=home,
    ))


# ── Skip-when-no-permissions-blocks ──────────────────────────────────────────


def test_observe_returns_empty_when_no_apps_have_permissions(tmp_path: Path):
    """Bots in their fresh state (post-Phase-A, pre-bootstrap) have no
    permissions blocks. Review should silently produce 0 proposals."""
    home = _make_bot(tmp_path, "team_bot_a", manifests={
        "i-app": {
            "name": "App",
            "files": [{"path": "scripts/foo.py", "layer": "script"}],
            # no permissions block
        },
    })
    assert _run(home, "team_bot_a") == []


def test_observe_returns_empty_when_manifests_dir_missing(tmp_path: Path):
    home = tmp_path / "team_bot_a"
    (home / ".openclaw").mkdir(parents=True)
    proposals = observe(AppPermissionReviewContext(
        bot_id="team_bot_a",
        shared_dir=Path("/tmp/unused"),
        home_override=home,
    ))
    assert proposals == []


# ── Per-finding-kind end-to-end ──────────────────────────────────────────────


def test_observe_emits_exec_unused_investigation(tmp_path: Path):
    """A permissions block with a stale exec entry → Investigation proposal."""
    home = _make_bot(
        tmp_path, "team_bot_a",
        manifests={
            "i-task": {
                "name": "Task App",
                "files": [{"path": "scripts/real.py", "layer": "script"}],
                "permissions": {
                    "exec": ["scripts/real.py", "scripts/ghost.py"],
                },
            },
        },
        workspace_files={"scripts/real.py": "# real"},
    )
    proposals = _run(home, "team_bot_a")
    assert len(proposals) >= 1
    # Find the ghost proposal
    ghost_props = [p for p in proposals if "ghost.py" in p.action.context]
    assert len(ghost_props) == 1
    p = ghost_props[0]
    assert isinstance(p.action, Investigation)
    assert "Task App" in p.action.context
    assert "ghost.py" in p.action.context


def test_observe_emits_missing_declaration(tmp_path: Path):
    """Script in files[] not in permissions.exec → missing declaration."""
    home = _make_bot(
        tmp_path, "team_bot_a",
        manifests={
            "i-task": {
                "name": "Task App",
                "files": [
                    {"path": "scripts/foo.py", "layer": "script"},
                    {"path": "scripts/bar.py", "layer": "script"},
                ],
                "permissions": {
                    "exec": ["scripts/foo.py"],
                    # bar.py is missing
                },
            },
        },
        workspace_files={
            "scripts/foo.py": "# foo",
            "scripts/bar.py": "# bar",
        },
    )
    proposals = _run(home, "team_bot_a")
    missing = [p for p in proposals
               if "bar.py" in p.action.context
               and "doesn't cover" in p.action.context]
    assert len(missing) == 1


# ── Pod-aware consolidation routing through observe ──────────────────────────


def test_observe_emits_sibling_declares_annotation(tmp_path: Path):
    """End-to-end: app A declares an unused-by-A network host (no grep
    match in A's scripts), but app B also declares it → observe emits a
    proposal annotated with sibling-declares.

    network_egress is the cleanest test case because the necessity check
    is purely grep-based — file existence isn't part of the question
    (unlike exec). That makes the test premise robust against subtle
    file-presence semantics.
    """
    home = _make_bot(
        tmp_path, "team_bot_a",
        manifests={
            "i-a": {
                "name": "App A",
                "files": [{"path": "scripts/a-script.py", "layer": "script"}],
                "permissions": {
                    "network_egress": ["api.unused-by-a.example"],
                },
            },
            "i-b": {
                "name": "App B",
                "files": [{"path": "scripts/b-script.py", "layer": "script"}],
                "permissions": {
                    "network_egress": ["api.unused-by-a.example"],  # also declares
                },
            },
        },
        workspace_files={
            "scripts/a-script.py": "# no host references",
            "scripts/b-script.py": "# no host references either",
        },
    )
    proposals = _run(home, "team_bot_a")
    # A's unused finding should be annotated as sibling-declares
    a_unused = [p for p in proposals
                if "api.unused-by-a.example" in p.action.context
                and "App A" in p.action.context]
    assert len(a_unused) >= 1
    assert any("remains in effect" in p.action.context for p in a_unused), (
        f"expected SIBLING_DECLARES annotation; got contexts: "
        f"{[p.action.context[:300] for p in a_unused]}"
    )


def test_observe_emits_move_when_sibling_uses_undeclared(tmp_path: Path):
    """A declares a network host but doesn't use it; B uses it but doesn't
    declare → consolidator emits a MOVE proposal."""
    home = _make_bot(
        tmp_path, "team_bot_a",
        manifests={
            "i-a": {
                "name": "App A",
                "files": [{"path": "scripts/a.py", "layer": "script"}],
                "permissions": {"network_egress": ["api.shared.example"]},  # unused by A
            },
            "i-b": {
                "name": "App B",
                "files": [{"path": "scripts/b.py", "layer": "script"}],
                "permissions": {},  # uses but doesn't declare
            },
        },
        workspace_files={
            "scripts/a.py": "# no hosts",
            "scripts/b.py": "url = 'https://api.shared.example/v1'",
        },
    )
    proposals = _run(home, "team_bot_a")
    move_props = [p for p in proposals
                  if "MOVE PROPOSAL" in p.action.context
                  and "App A" in p.action.context]
    assert len(move_props) >= 1, (
        f"expected MOVE proposal; got contexts: "
        f"{[p.action.context[:200] for p in proposals]}"
    )
    # The move target should mention App B (by id or name)
    move_ctx = move_props[0].action.context
    assert "i-b" in move_ctx or "App B" in move_ctx


# ── Affirmed entries skipped ─────────────────────────────────────────────────


def test_observe_skips_affirmed_entries(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        manifests={
            "i-app": {
                "name": "App",
                "files": [],
                "permissions": {
                    "exec": ["scripts/ghost.py"],
                    "_affirmed": [
                        "permission_exec_unused:exec:scripts/ghost.py",
                    ],
                },
            },
        },
    )
    proposals = _run(home, "team_bot_a")
    # ghost.py would normally fire exec_unused, but it's affirmed
    ghost_props = [p for p in proposals if "ghost.py" in p.action.context]
    assert ghost_props == []


# ── Hidden/deprecated apps skipped ───────────────────────────────────────────


def test_observe_skips_hidden_manifests(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        manifests={
            "i-active": {
                "name": "Active",
                "status": "active",
                "files": [],
                "permissions": {"exec": ["scripts/active-ghost.py"]},
            },
            "i-hidden": {
                "name": "Hidden",
                "status": "hidden",
                "files": [],
                "permissions": {"exec": ["scripts/hidden-ghost.py"]},
            },
        },
    )
    proposals = _run(home, "team_bot_a")
    contexts = " | ".join(p.action.context for p in proposals)
    assert "active-ghost.py" in contexts
    assert "hidden-ghost.py" not in contexts


# ── Malformed manifest doesn't abort ─────────────────────────────────────────


def test_observe_malformed_manifest_skipped(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        manifests={
            "i-good": {
                "name": "Good",
                "files": [],
                "permissions": {"exec": ["scripts/orphan.py"]},
            },
        },
    )
    # Drop a malformed manifest alongside
    (home / ".openclaw" / "workspace" / "manifests" / "i-bad.json").write_text(
        "{ not valid json"
    )
    proposals = _run(home, "team_bot_a")
    # Good manifest still produces findings
    assert any("scripts/orphan.py" in p.action.context for p in proposals)


# ── Proposal shape sanity ────────────────────────────────────────────────────


def test_proposal_has_expected_fields(tmp_path: Path):
    home = _make_bot(
        tmp_path, "team_bot_a",
        manifests={
            "i-app": {
                "name": "App",
                "files": [],
                "permissions": {"exec": ["scripts/orphan.py"]},
            },
        },
    )
    proposals = _run(home, "team_bot_a")
    assert len(proposals) == 1
    p = proposals[0]
    assert p.bot_id == "team_bot_a"
    assert p.generator_id == "app_permission_review"
    assert p.dimension == "safety"
    assert isinstance(p.action, Investigation)
    assert p.risk_tag.blast_radius == "bot"
    assert "manifest" in p.risk_tag.touches
    assert p.provenance.technique.startswith("app_permission_review.")
    # The trigger observation should be unique per (bot, app, kind, entry)
    assert len(p.trigger_observations) == 1


def test_observe_emits_zero_proposals_for_clean_bot(tmp_path: Path):
    """Bot with permissions blocks that match reality → no findings."""
    home = _make_bot(
        tmp_path, "team_bot_a",
        manifests={
            "i-app": {
                "name": "App",
                "files": [{"path": "scripts/real.py", "layer": "script"}],
                "permissions": {"exec": ["scripts/real.py"]},
            },
        },
        workspace_files={"scripts/real.py": "# real"},
    )
    proposals = _run(home, "team_bot_a")
    assert proposals == []
