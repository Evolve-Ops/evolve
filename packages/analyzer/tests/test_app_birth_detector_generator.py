"""tests/test_app_birth_detector_generator.py — partial-management awareness.

Regression coverage for the 2026-05-25 triage where the detector fired
"promote to a managed app" on `team_bot_a:ops/tools/` even though six of the
files there were already realised by existing apps (their provenance
markers were ignored). After the fix the detector:

  - never proposes against a directory that's fully under management
  - keeps emitting the existing BuildApp pitch for fully-orphan dirs
  - switches to a ManifestUpdate(add_files) pitch targeting the
    existing app when a directory is mixed under a single owning app
  - stays silent on mixed directories that span multiple owning apps
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.app_birth_detector.observe import (  # noqa: E402
    DetectorContext,
    _cluster_files,
    _load_manifests,
    _marker_app_ids,
    observe,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _write_manifest(
    shared_dir: Path,
    bot_id: str,
    *,
    app_id: str,
    files: list[str],
    name: str | None = None,
) -> None:
    d = shared_dir / "applications" / bot_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": app_id,
        "name": name or app_id,
        "display_name": name or app_id,
        "files": [{"path": p} for p in files],
    }
    (d / f"{app_id}.json").write_text(json.dumps(payload))


def _write_orphan_py(path: Path, lines: int = 12) -> None:
    """Substantial-enough script with no provenance marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"x_{i} = {i}" for i in range(lines))
    path.write_text(f"#!/usr/bin/env python3\n{body}\n")


def _write_orphan_json(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hello": "world"}))


def _write_managed_py(path: Path, app_id: str) -> None:
    """Script carrying a v7 spec= provenance marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"y_{i} = {i}" for i in range(12))
    path.write_text(
        "#!/usr/bin/env python3\n"
        f"# evolve: spec={app_id}@2026.05.20-1.0 file=f-deadbeef@2026.05.20-1.0\n"
        f"{body}\n"
    )


def _write_managed_json(path: Path, app_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_evolve": {
            "spec": f"{app_id}@2026.05.20-1.0",
            "file": "f-cafef00d@2026.05.20-1.0",
        },
        "data": True,
    }
    path.write_text(json.dumps(payload, indent=2))


# ── _load_manifests ──────────────────────────────────────────────────────────


def test_load_manifests_indexes_paths_by_app_id(tmp_path):
    _write_manifest(
        tmp_path, "team_bot_a",
        app_id="p-527134e8",
        files=["ops/tools/assign_task_owners.py", "ops/tools/agent_registry.json"],
        name="Task Routing",
    )
    _write_manifest(
        tmp_path, "team_bot_a",
        app_id="p-c9e3ceb9",
        files=["ops/tools/auto_startup_system.py"],
        name="Startup",
    )
    idx = _load_manifests(tmp_path, "team_bot_a")

    assert idx.claims["ops/tools/assign_task_owners.py"] == {"p-527134e8"}
    assert idx.claims["ops/tools/auto_startup_system.py"] == {"p-c9e3ceb9"}
    assert idx.summaries["p-527134e8"]["display_name"] == "Task Routing"
    assert idx.summaries["p-c9e3ceb9"]["display_name"] == "Startup"


def test_load_manifests_missing_dir_returns_empty(tmp_path):
    idx = _load_manifests(tmp_path, "no-such-bot")
    assert idx.claims == {}
    assert idx.summaries == {}


# ── _marker_app_ids ──────────────────────────────────────────────────────────


def test_marker_app_ids_reads_python_spec_marker(tmp_path):
    fp = tmp_path / "script.py"
    _write_managed_py(fp, "p-527134e8")
    assert _marker_app_ids(fp) == {"p-527134e8"}


def test_marker_app_ids_reads_json_evolve_key(tmp_path):
    fp = tmp_path / "state.json"
    _write_managed_json(fp, "p-c9e3ceb9")
    assert _marker_app_ids(fp) == {"p-c9e3ceb9"}


def test_marker_app_ids_returns_empty_for_unmarked_file(tmp_path):
    fp = tmp_path / "plain.py"
    _write_orphan_py(fp)
    assert _marker_app_ids(fp) == set()


# ── _cluster_files ───────────────────────────────────────────────────────────


def test_cluster_files_skips_workspace_root_files(tmp_path):
    # File at workspace root (no parent subdir) must never form a cluster.
    fp = tmp_path / "loose.py"
    _write_orphan_py(fp)
    clusters = _cluster_files([(fp, set())], tmp_path)
    assert clusters == []


def test_cluster_files_requires_script_and_data(tmp_path):
    py = tmp_path / "ops" / "lonely.py"
    _write_orphan_py(py)
    # No co-located data file → no cluster.
    clusters = _cluster_files([(py, set())], tmp_path)
    assert clusters == []


def test_cluster_files_partitions_managed_vs_orphan(tmp_path):
    a = tmp_path / "ops" / "tools" / "orphan_one.py"
    b = tmp_path / "ops" / "tools" / "orphan_two.json"
    c = tmp_path / "ops" / "tools" / "managed.py"
    _write_orphan_py(a)
    _write_orphan_json(b)
    _write_managed_py(c, "p-527134e8")

    clusters = _cluster_files(
        [(a, set()), (b, set()), (c, {"p-527134e8"})],
        tmp_path,
    )
    assert len(clusters) == 1
    cl = clusters[0]
    assert cl.directory == "ops/tools"
    assert cl.scripts == [a]
    assert cl.data_files == [b]
    assert cl.managed_files == [c]
    assert cl.managed_app_ids == {"p-527134e8"}


# ── observe() — the three cluster shapes ─────────────────────────────────────


def _ctx(shared: Path, workspace: Path, bot_id: str = "team_bot_a") -> DetectorContext:
    return DetectorContext(
        bot_id=bot_id,
        shared_dir=shared,
        workspace_root=workspace,
        now=_NOW,
    )


def test_observe_silent_when_directory_fully_managed(tmp_path):
    """0 unmanaged files → no proposal, even though >1 file is present."""
    shared = tmp_path / "shared"
    workspace = tmp_path / "ws"
    _write_managed_py(workspace / "ops" / "tools" / "a.py", "p-527134e8")
    _write_managed_json(workspace / "ops" / "tools" / "b.json", "p-527134e8")
    # Manifest also claims them — both ownership channels agree.
    _write_manifest(
        shared, "team_bot_a",
        app_id="p-527134e8",
        files=["ops/tools/a.py", "ops/tools/b.json"],
    )

    assert observe(_ctx(shared, workspace)) == []


def test_observe_silent_when_directory_managed_via_markers_only(tmp_path):
    """Realised-side ownership alone is enough — no manifest claim needed.

    Regression for the 2026-05-25 bug: the bot writes the marker on every
    forge output, but the manifest's ``files[]`` listing can drift (path
    normalisation, post-deploy edits, etc.). The marker is the truth.
    """
    shared = tmp_path / "shared"
    workspace = tmp_path / "ws"
    _write_managed_py(workspace / "ops" / "tools" / "a.py", "p-527134e8")
    _write_managed_json(workspace / "ops" / "tools" / "b.json", "p-527134e8")
    # No manifest written — only the on-disk markers establish ownership.

    assert observe(_ctx(shared, workspace)) == []


def test_observe_build_app_when_directory_fully_orphan(tmp_path):
    """All unmanaged → existing BuildApp pitch."""
    shared = tmp_path / "shared"
    workspace = tmp_path / "ws"
    _write_orphan_py(workspace / "ops" / "tools" / "a.py")
    _write_orphan_json(workspace / "ops" / "tools" / "b.json")

    proposals = observe(_ctx(shared, workspace))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action.kind == "BuildApp"
    # Phase C-9 humanized title: "promote to a managed app" now lives
    # in the summary/explanation/action_label; the problem line is the
    # operator-facing title.
    assert (
        "promote to a managed app" in p.problem.lower()
        or "promote" in p.problem.lower()
        or "promote" in (p.action_label or "").lower()
    )
    assert p.trigger_observations[0].startswith("orphan_cluster:team_bot_a:")


def test_observe_finish_migration_when_directory_mixed_single_app(tmp_path):
    """Mixed cluster owned by a single existing app → ManifestUpdate."""
    shared = tmp_path / "shared"
    workspace = tmp_path / "ws"
    # Two orphans alongside two files realised by the existing app.
    _write_orphan_py(workspace / "ops" / "tools" / "continuous_improvement.py")
    _write_orphan_json(workspace / "ops" / "tools" / "database_schema.json")
    _write_managed_py(workspace / "ops" / "tools" / "assign_task_owners.py", "p-527134e8")
    _write_managed_json(workspace / "ops" / "tools" / "agent_registry.json", "p-527134e8")
    _write_manifest(
        shared, "team_bot_a",
        app_id="p-527134e8",
        files=["ops/tools/assign_task_owners.py", "ops/tools/agent_registry.json"],
        name="Task Routing",
    )

    proposals = observe(_ctx(shared, workspace))
    assert len(proposals) == 1
    p = proposals[0]

    assert p.action.kind == "ManifestUpdate"
    assert p.action.app_id == "p-527134e8"
    assert p.action.operation == "add_files"
    folded = sorted(p.action.fields["files"])
    assert folded == [
        "ops/tools/continuous_improvement.py",
        "ops/tools/database_schema.json",
    ]

    # Pitch must cite the existing app so the operator sees the actionable
    # framing rather than the misleading "promote to a managed app".
    # Phase C-9 humanized title: target app id + "fold" framing now in
    # action_label / summary / explanation; problem is short.
    assert (
        "p-527134e8" in p.problem
        or "Task Routing" in p.problem
        or "p-527134e8" in (p.summary or "")
        or "p-527134e8" in (p.explanation or "")
    )
    assert (
        "finish migration" in p.problem.lower()
        or "fold" in p.problem.lower()
        or "fold" in (p.action_label or "").lower()
    )
    assert "Task Routing" in p.conversational_pitch
    assert p.trigger_observations[0].startswith("partial_app:team_bot_a:p-527134e8:")
    # Manifest-only edit is safely reversible — distinct from BuildApp's
    # manual reversibility (forge replaces file content on disk).
    assert p.risk_tag.reversibility == "auto"
    assert p.risk_tag.touches == ["app_manifest"]


def test_observe_silent_on_mixed_cluster_with_multiple_owners(tmp_path):
    """Two distinct owning apps in the same dir → no auto-proposal.

    The operator must decide which app the orphans belong to (or
    whether they belong to neither). Auto-folding into one would
    silently misattribute scope.
    """
    shared = tmp_path / "shared"
    workspace = tmp_path / "ws"
    _write_orphan_py(workspace / "ops" / "tools" / "a.py")
    _write_orphan_json(workspace / "ops" / "tools" / "b.json")
    _write_managed_py(workspace / "ops" / "tools" / "by_app_one.py", "p-527134e8")
    _write_managed_json(workspace / "ops" / "tools" / "by_app_two.json", "p-c9e3ceb9")

    assert observe(_ctx(shared, workspace)) == []
