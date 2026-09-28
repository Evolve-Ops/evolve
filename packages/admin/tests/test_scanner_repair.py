"""Tests for the Phase 4.5 scanner_repair module.

Each per-repair test focuses on one assertion the verifier (`packages/
analyzer/app_audit_structural.py::check_discoverability` and friends)
would otherwise fire, then verifies:

  1. The repair fires when the gap is present.
  2. The repair stays its hand when the field is already populated.
  3. Re-running on a repaired manifest produces no further diff (idempotency).

The end-to-end tests also confirm that the scanner_repair module
stamps `bot_authored` provenance on the fields it fills so the
operator can tell auto-fixes apart from hand-edits in
`provenance.field_origins`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.scanner_repair import (  # noqa: E402
    RepairResult,
    audit_stale_owned_by,
    repair_installed_apps_md,
    repair_manifest,
    repair_stale_owned_by_in_dir,
)


# ── RepairResult helpers ──────────────────────────────────────────────────────


def test_repair_result_is_noop_when_only_skipped() -> None:
    r = RepairResult(skipped=["because"])
    assert r.is_noop()
    assert r.summary() == "no-op"


def test_repair_result_summary_counts() -> None:
    r = RepairResult(applied=["a", "b"], skipped=["s"], errors=["e"])
    s = r.summary()
    assert "applied=2" in s and "skipped=1" in s and "errors=1" in s


# ── Repair 2: CLI backfill ────────────────────────────────────────────────────


def _base_user_routed() -> dict:
    return {
        "id": "my-app",
        "name": "My App",
        "description": "Does the thing.",
        "usage": {"model": "user-initiated"},
        "evidence_files": ["ops/my_app.py", "ops/data.json"],
        "interface_contract": {},
    }


def test_cli_backfill_adds_stub_for_python_evidence() -> None:
    m = _base_user_routed()
    repaired, result = repair_manifest(m)
    cli = repaired["interface_contract"]["cli"]
    assert len(cli) == 1
    assert cli[0]["command"] == "python3 ops/my_app.py"
    assert cli[0]["source"] == "scanner_repair"
    assert any("cli_backfill" in a and "stubbed" in a for a in result.applied)


def test_cli_backfill_chooses_shell_when_first_evidence_is_shell() -> None:
    m = _base_user_routed()
    m["evidence_files"] = ["scripts/run.sh", "ops/data.json"]
    repaired, _ = repair_manifest(m)
    assert repaired["interface_contract"]["cli"][0]["command"] == "bash scripts/run.sh"


def test_cli_backfill_skips_scheduled_app() -> None:
    m = _base_user_routed()
    m["usage"] = {"model": "scheduled"}
    repaired, result = repair_manifest(m)
    assert repaired["interface_contract"] == {}
    assert any("not user-routed" in s for s in result.skipped)


def test_cli_backfill_skips_when_cli_already_populated() -> None:
    m = _base_user_routed()
    m["interface_contract"] = {
        "cli": [{"command": "python3 ops/custom.py", "description": "x"}]
    }
    repaired, result = repair_manifest(m)
    assert repaired["interface_contract"]["cli"][0]["command"] == "python3 ops/custom.py"
    assert any("already populated" in s for s in result.skipped)


def test_cli_backfill_skips_when_no_executable_evidence() -> None:
    m = _base_user_routed()
    m["evidence_files"] = ["data/notes.md", "config/settings.json"]
    repaired, result = repair_manifest(m)
    assert repaired["interface_contract"] == {}
    assert any("no executable" in s for s in result.skipped)


def test_cli_backfill_is_idempotent() -> None:
    m = _base_user_routed()
    repaired1, _ = repair_manifest(m)
    cli1 = list(repaired1["interface_contract"]["cli"])
    repaired2, result2 = repair_manifest(repaired1)
    assert repaired2["interface_contract"]["cli"] == cli1
    assert any("already populated" in s for s in result2.skipped)


# ── Repair 3: Hint-words floor ────────────────────────────────────────────────


def test_hint_words_pads_below_floor() -> None:
    m = {
        "id": "notes",
        "name": "Notes",
        "description": "Capture quick memos and personal observations daily.",
        "usage": {"model": "user-initiated"},
        "capability_tags": ["Notes"],
        "session_keywords": ["notes"],
        "evidence_files": [],
    }
    repaired, result = repair_manifest(m)
    # Original: capability_tags + session_keywords had 1 distinct case-
    # sensitive entry... wait, "Notes" and "notes" are distinct strings
    # after .strip() comparison, so the verifier count is 2. Floor is 3.
    # The repair should add at least 1 token.
    assert len(repaired["capability_tags"]) >= 2
    assert any("hint_words" in a and "added" in a for a in result.applied)


def test_hint_words_skips_when_at_floor() -> None:
    m = {
        "id": "x",
        "name": "Atlas Knowledge",
        "description": "stores articles",
        "capability_tags": ["Atlas Knowledge", "atlas", "knowledge"],
        "session_keywords": [],
    }
    repaired, result = repair_manifest(m)
    assert repaired["capability_tags"] == ["Atlas Knowledge", "atlas", "knowledge"]
    assert any("hint_words" in s and "already" in s for s in result.skipped)


def test_hint_words_caps_at_six() -> None:
    m = {
        "id": "x",
        "name": "Brilliant Atlas Knowledge System",
        "description": "Manages curated reference articles across diverse research domains.",
        "capability_tags": [],
        "session_keywords": [],
    }
    repaired, _ = repair_manifest(m)
    # Repair should add tokens but cap the total at 6 to avoid noise.
    assert len(repaired["capability_tags"]) <= 6


def test_hint_words_is_idempotent() -> None:
    m = {
        "id": "notes",
        "name": "Notes",
        "description": "Capture memos.",
        "capability_tags": ["Notes"],
        "session_keywords": [],
    }
    r1, _ = repair_manifest(m)
    tags_after_first = list(r1["capability_tags"])
    r2, result2 = repair_manifest(r1)
    assert r2["capability_tags"] == tags_after_first
    assert any("hint_words" in s for s in result2.skipped)


# ── Repair 4: Test exemption backfill ─────────────────────────────────────────


def test_test_exemption_scheduled_app() -> None:
    m = {
        "id": "cron-app",
        "name": "Cron App",
        "usage": {"model": "scheduled"},
        "scheduled_actions": [{"trigger": {"kind": "cron"}}],
        "evidence_files": ["ops/run.py"],
    }
    repaired, result = repair_manifest(m)
    assert "scheduled app" in repaired["test_exemption_reason"]
    assert any("test_exemption" in a for a in result.applied)


def test_test_exemption_content_store_app() -> None:
    m = {
        "id": "kb",
        "name": "Knowledge Base",
        "usage": {"model": "user-initiated"},
        "evidence_files": ["kb/articles.md"],
        # No scheduled_actions, no crons, no event_triggers.
    }
    repaired, _ = repair_manifest(m)
    assert "content-store" in repaired["test_exemption_reason"]


def test_test_exemption_skips_when_test_command_set() -> None:
    m = {
        "id": "cron-app",
        "name": "Cron App",
        "usage": {"model": "scheduled"},
        "scheduled_actions": [{"trigger": {"kind": "cron"}}],
        "test_command": "pytest tests/",
    }
    repaired, result = repair_manifest(m)
    assert "test_exemption_reason" not in repaired
    assert any("already set" in s for s in result.skipped)


def test_test_exemption_skips_user_routed_with_no_signal() -> None:
    m = {
        "id": "app",
        "name": "App",
        "usage": {"model": "user-initiated"},
        # No evidence_files → not a content store either.
        "evidence_files": [],
        "scheduled_actions": [],
    }
    repaired, result = repair_manifest(m)
    assert repaired.get("test_exemption_reason", "") == ""
    assert any("operator-authored tests" in s for s in result.skipped)


def test_test_exemption_is_idempotent() -> None:
    m = {
        "id": "cron-app",
        "name": "Cron App",
        "usage": {"model": "scheduled"},
        "scheduled_actions": [{"trigger": {"kind": "cron"}}],
        "evidence_files": ["ops/run.py"],
    }
    r1, _ = repair_manifest(m)
    reason_after_first = r1["test_exemption_reason"]
    r2, result2 = repair_manifest(r1)
    assert r2["test_exemption_reason"] == reason_after_first
    assert any("test_exemption" in s and "already set" in s for s in result2.skipped)


# ── Provenance stamping ───────────────────────────────────────────────────────


def test_repair_stamps_bot_authored_provenance() -> None:
    """Every field the repair fills should land in provenance.field_origins
    with source=bot_authored and by=scanner_repair so the operator can
    tell auto-fixes apart from hand edits."""
    m = _base_user_routed()
    repaired, _ = repair_manifest(m)
    fo = (repaired.get("provenance") or {}).get("field_origins") or {}
    assert fo.get("interface_contract", {}).get("source") == "bot_authored"
    assert fo.get("interface_contract", {}).get("by") == "scanner_repair"


def test_repair_does_not_stamp_unchanged_fields() -> None:
    """Fields that the repair skipped (already populated) shouldn't get
    a bot_authored stamp clobbering whatever they had before."""
    m = _base_user_routed()
    m["interface_contract"] = {
        "cli": [{"command": "python3 ops/custom.py", "description": "x"}]
    }
    repaired, _ = repair_manifest(m)
    fo = (repaired.get("provenance") or {}).get("field_origins") or {}
    # Either absent (never touched) or some non-bot_authored source — but
    # NOT bot_authored, because the repair didn't write the field.
    src = fo.get("interface_contract", {}).get("source", "")
    assert src != "bot_authored"


# ── INSTALLED_APPS.md registration ────────────────────────────────────────────


def _make_manifest_dict(*, app_id: str, name: str) -> dict:
    return {
        "id": app_id,
        "name": name,
        "bot_id": "test-bot",
        "description": f"{name} description.",
        "status": "active",
        "schema_version": 13,
        "manifest_type": "evolve_application",
        "identity": {
            "purpose": f"This app does {name}.",
            "scope_includes": [],
            "scope_excludes": [],
            "user": "",
        },
        "success_criteria": {},
        "constraints": {},
        "evidence_files": [],
        "capability_tags": [name, name.lower()],
        "session_keywords": [name.lower()],
        "interface_contract": {"cli": [{"command": "do-thing", "description": "x"}]},
    }


def test_installed_apps_creates_file_when_missing(tmp_path: Path) -> None:
    workspace = tmp_path
    manifests = [_make_manifest_dict(app_id="a", name="Alpha App")]
    result = repair_installed_apps_md(manifests, workspace, bot_id="test-bot")
    out = workspace / "INSTALLED_APPS.md"
    assert out.exists()
    assert "Alpha App" in out.read_text()
    assert any("installed_apps_md" in a for a in result.applied)


def test_installed_apps_skips_when_all_registered(tmp_path: Path) -> None:
    workspace = tmp_path
    manifests = [_make_manifest_dict(app_id="a", name="Alpha App")]
    # First call creates the file.
    repair_installed_apps_md(manifests, workspace, bot_id="test-bot")
    # Second call should be a no-op since renderer is deterministic.
    result = repair_installed_apps_md(manifests, workspace, bot_id="test-bot")
    assert not result.applied
    assert any("already registered" in s or "matches existing" in s
               for s in result.skipped)


def test_installed_apps_adds_missing_app(tmp_path: Path) -> None:
    workspace = tmp_path
    m1 = [_make_manifest_dict(app_id="a", name="Alpha App")]
    repair_installed_apps_md(m1, workspace, bot_id="test-bot")
    # Add a second manifest, re-run.
    m2 = m1 + [_make_manifest_dict(app_id="b", name="Beta App")]
    result = repair_installed_apps_md(m2, workspace, bot_id="test-bot")
    text = (workspace / "INSTALLED_APPS.md").read_text()
    assert "Alpha App" in text and "Beta App" in text
    assert any("Beta App" in a for a in result.applied)


def test_installed_apps_idempotent_after_addition(tmp_path: Path) -> None:
    workspace = tmp_path
    manifests = [
        _make_manifest_dict(app_id="a", name="Alpha App"),
        _make_manifest_dict(app_id="b", name="Beta App"),
    ]
    repair_installed_apps_md(manifests, workspace, bot_id="test-bot")
    result = repair_installed_apps_md(manifests, workspace, bot_id="test-bot")
    assert not result.applied


def test_installed_apps_missing_workspace_skips(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = repair_installed_apps_md([], missing, bot_id="test-bot")
    assert not result.applied
    assert any("does not exist" in s for s in result.skipped)


# ── Stale owned_by cleanup ────────────────────────────────────────────────────


import json  # noqa: E402


def _write_manifest(
    caps_dir: Path, app_id: str, pkg_id: str, files: list[dict],
    *, status: str = "active",
) -> Path:
    """Helper: drop a v5-shaped manifest JSON into caps_dir."""
    caps_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": app_id,
        "name": app_id.replace("-", " ").title(),
        "bot_id": "test-bot",
        "pkg_id": pkg_id,
        "status": status,
        "schema_version": 13,
        "manifest_type": "evolve_application",
        "files": files,
    }
    path = caps_dir / f"{app_id}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _file_entry(file_id: str, path: str, owned_by: str) -> dict:
    return {
        "file_id": file_id,
        "path": path,
        "layer": "data",
        "owned_by": owned_by,
        "shared_with": [],
    }


def test_audit_finds_only_stale_tags() -> None:
    manifest = {
        "pkg_id": "p-A",
        "files": [
            _file_entry("f-1", "ops/a.py", "p-A"),   # self-owned, keep
            _file_entry("f-2", "ops/b.py", "p-B"),   # live cross-app, keep
            _file_entry("f-3", "ops/c.py", "p-C"),   # dead, surface
            _file_entry("f-4", "ops/d.py", ""),       # unowned, ignore
            _file_entry("f-5", "ops/e.py", "admin"),  # carve-out, ignore
        ],
    }
    stale = audit_stale_owned_by(manifest, live_pkg_ids={"p-A", "p-B"})
    assert stale == [("ops/c.py", "p-C")]


def test_cleanup_clears_dead_pkg_keeps_self_and_live(tmp_path: Path) -> None:
    """Fixture pod: A (Biometric-like), B (Memory-Continuity-like), C deleted.

    A claims files attributed to itself, to live B, to dead C, plus a
    carve-out tag. After cleanup only the C-tag is cleared.
    """
    caps = tmp_path / "manifests"
    a_files = [
        _file_entry("f-self", "memory/health/own.md", "p-A"),
        _file_entry("f-shared", "memory/shared.md", "p-B"),
        _file_entry("f-dead", "memory/health/dead.md", "p-C"),
        _file_entry("f-admin", "scripts/from-admin.py", "admin"),
    ]
    a_path = _write_manifest(caps, "biometric", "p-A", a_files)
    _write_manifest(
        caps, "memory-continuity", "p-B",
        [_file_entry("f-b1", "memory/shared.md", "p-B")],
    )
    # Note: NO manifest with pkg_id=p-C — that's the deleted app.

    result, diag = repair_stale_owned_by_in_dir(caps, write=True)

    # Pre-mutation diagnostic captured exactly one dead pkg.
    assert diag == {"p-C": 1}

    # The biometric manifest on disk: own + live + carve-out untouched,
    # dead C tag cleared to empty string.
    reread = json.loads(a_path.read_text())
    by_path = {f["path"]: f for f in reread["files"]}
    assert by_path["memory/health/own.md"]["owned_by"] == "p-A"
    assert by_path["memory/shared.md"]["owned_by"] == "p-B"
    assert by_path["memory/health/dead.md"]["owned_by"] == ""
    assert by_path["scripts/from-admin.py"]["owned_by"] == "admin"

    # Operator-facing log mentions the dead pkg and an example path.
    assert any("p-C" in line and "dead.md" in line
               for line in result.applied)


def test_cleanup_is_noop_when_all_owners_live(tmp_path: Path) -> None:
    caps = tmp_path / "manifests"
    _write_manifest(
        caps, "alpha", "p-A",
        [_file_entry("f-1", "a.py", "p-A")],
    )
    _write_manifest(
        caps, "beta", "p-B",
        [_file_entry("f-2", "b.py", "p-A")],   # cross-app to live p-A
    )
    result, diag = repair_stale_owned_by_in_dir(caps, write=True)
    assert diag == {}
    assert not result.applied
    assert not result.errors


def test_cleanup_skips_dormant_pkg_from_live_set(tmp_path: Path) -> None:
    """A dormant or hidden manifest's pkg_id should NOT count as live —
    those manifests are vestigial state and references to their pkg_id
    are stale."""
    caps = tmp_path / "manifests"
    _write_manifest(
        caps, "alive", "p-LIVE",
        [_file_entry("f-1", "scripts/lives.py", "p-DORMANT")],
    )
    _write_manifest(
        caps, "dormant-old", "p-DORMANT",
        [], status="dormant",
    )
    _, diag = repair_stale_owned_by_in_dir(caps, write=True)
    assert diag == {"p-DORMANT": 1}


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    caps = tmp_path / "manifests"
    a_path = _write_manifest(
        caps, "biometric", "p-A",
        [_file_entry("f-dead", "memory/dead.md", "p-DEAD")],
    )
    r1, _ = repair_stale_owned_by_in_dir(caps, write=True)
    assert r1.applied
    # Second pass — manifest now has owned_by="" for the dead entry.
    r2, diag2 = repair_stale_owned_by_in_dir(caps, write=True)
    assert not r2.applied
    assert diag2 == {}
    # Sanity check the persisted state.
    again = json.loads(a_path.read_text())
    assert again["files"][0]["owned_by"] == ""


def test_cleanup_handles_v4_string_entries(tmp_path: Path) -> None:
    """Mixed v4 (string) + v5 (dict) file lists must not crash the pass;
    v4 strings carry no provenance to audit and should be skipped."""
    caps = tmp_path / "manifests"
    caps.mkdir(parents=True, exist_ok=True)
    data = {
        "id": "legacy",
        "name": "Legacy",
        "bot_id": "test-bot",
        "pkg_id": "p-L",
        "status": "active",
        "schema_version": 13,
        "manifest_type": "evolve_application",
        "files": [
            "scripts/old.py",   # v4 path-only
            _file_entry("f-1", "scripts/new.py", "p-DEAD"),
        ],
    }
    path = caps / "legacy.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    result, diag = repair_stale_owned_by_in_dir(caps, write=True)
    assert diag == {"p-DEAD": 1}
    reread = json.loads(path.read_text())
    assert reread["files"][0] == "scripts/old.py"  # v4 untouched
    assert reread["files"][1]["owned_by"] == ""
    assert any("p-DEAD" in line for line in result.applied)


def test_cleanup_diagnostic_groups_by_dead_pkg(tmp_path: Path) -> None:
    """Diagnostic dict counts file entries per dead pkg_id across
    every manifest in the directory."""
    caps = tmp_path / "manifests"
    _write_manifest(
        caps, "app-a", "p-A",
        [
            _file_entry("f-1", "a/1.md", "p-DEAD1"),
            _file_entry("f-2", "a/2.md", "p-DEAD1"),
            _file_entry("f-3", "a/3.md", "p-DEAD2"),
        ],
    )
    _write_manifest(
        caps, "app-b", "p-B",
        [
            _file_entry("f-4", "b/1.md", "p-DEAD1"),
        ],
    )
    _, diag = repair_stale_owned_by_in_dir(caps, write=False)
    assert diag == {"p-DEAD1": 3, "p-DEAD2": 1}


def test_extra_live_pkg_ids_widens_live_set(tmp_path: Path) -> None:
    """extra_live_pkg_ids is the cross-bot extension hook — pkg_ids
    passed in this way are treated as live and their references
    survive cleanup."""
    caps = tmp_path / "manifests"
    a_path = _write_manifest(
        caps, "app-a", "p-A",
        [_file_entry("f-1", "shared.md", "p-FROM-OTHER-BOT")],
    )
    _, diag = repair_stale_owned_by_in_dir(
        caps, write=True,
        extra_live_pkg_ids={"p-FROM-OTHER-BOT"},
    )
    assert diag == {}
    reread = json.loads(a_path.read_text())
    assert reread["files"][0]["owned_by"] == "p-FROM-OTHER-BOT"


def test_cleanup_missing_dir_skips(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result, diag = repair_stale_owned_by_in_dir(missing, write=True)
    assert not result.applied
    assert diag == {}
    assert any("does not exist" in s for s in result.skipped)


def test_cleanup_stamps_bot_authored_on_files(tmp_path: Path) -> None:
    """When the cleanup changes files[], the field gets stamped
    bot_authored so the operator can tell auto-cleanups from
    hand edits."""
    caps = tmp_path / "manifests"
    a_path = _write_manifest(
        caps, "biometric", "p-A",
        [_file_entry("f-1", "memory/dead.md", "p-DEAD")],
    )
    repair_stale_owned_by_in_dir(caps, write=True)
    reread = json.loads(a_path.read_text())
    fo = (reread.get("provenance") or {}).get("field_origins") or {}
    files_origin = fo.get("files") or {}
    assert files_origin.get("source") == "bot_authored"
    assert files_origin.get("by") == "scanner_repair"
