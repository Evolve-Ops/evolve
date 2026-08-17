"""Tests for evolve_admin.app_permissions.reconciler.

Phase A scope (docs/spec-app-derived-permissions-2026-05-24.md):
  - Reconciler reads manifests, infers exec entries from files/crons,
    merges optional permissions: block, tags each entry with provenance.
  - In tracking mode (full / allowlist), writes the would-be allowlist
    to exec-approvals.preview.json — never to exec-approvals.json.
  - In deny mode (operator-pinned via ``execPolicy: "deny"``), writes nothing.
  - Per-app malformed manifest → recorded in per_app_errors, doesn't abort.
  - All-manifests-unreadable → skipped=True, no write.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.app_permissions import (
    PREVIEW_FILENAME,
    PermissionEntry,
    ReconcileResult,
    reconcile_bot_permissions,
)
from evolve_admin.app_permissions import reconciler as rc


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_bot_home(
    tmp_path: Path,
    bot_id: str,
    *,
    openclaw: dict | None = None,
    exec_approvals: dict | None = None,
    manifests: dict[str, dict] | None = None,
) -> Path:
    """Build a synthetic /Users/<bot> tree with .openclaw/ contents.

    Returns the bot's home dir. The caller passes this as ``home_override``
    to the reconciler so all file I/O bypasses sudo.
    """
    home = tmp_path / bot_id
    (home / ".openclaw").mkdir(parents=True, exist_ok=True)
    (home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True, exist_ok=True)

    if openclaw is not None:
        (home / ".openclaw" / "openclaw.json").write_text(json.dumps(openclaw))
    if exec_approvals is not None:
        (home / ".openclaw" / "exec-approvals.json").write_text(json.dumps(exec_approvals))
    for app_id, manifest in (manifests or {}).items():
        (home / ".openclaw" / "workspace" / "manifests" / f"{app_id}.json").write_text(
            json.dumps(manifest)
        )
    return home


def _network(bot_id: str, *, role: str = "member", user: str | None = None) -> dict:
    return {
        "networkId": "test-net",
        "bots": {
            bot_id: {
                "role": role,
                "user": user or bot_id,
            }
        },
    }


# ── Inference: files / crons / explicit ──────────────────────────────────────


def test_infer_entries_picks_up_script_files_only():
    manifest = {
        "id": "i-test",
        "name": "Test App",
        "files": [
            {"path": "scripts/foo.py", "layer": "script"},
            {"path": "data/bar.json", "layer": "data"},
            {"path": "README.md", "layer": "state"},
        ],
    }
    entries = rc._entries_for_app(manifest)
    exec_entries = [e for e in entries if e.kind == "exec"]
    assert {e.pattern for e in exec_entries} == {"scripts/foo.py"}
    assert all(e.source == rc.SOURCE_APP_DERIVED for e in exec_entries)
    assert all(e.origin == rc.ORIGIN_INFERRED for e in exec_entries)
    assert all(e.app_id == "i-test" for e in exec_entries)


def test_infer_entries_handles_v4_string_files():
    """v4 manifests store files as bare strings — extension drives layer."""
    manifest = {
        "id": "i-v4",
        "name": "Legacy",
        "files": ["legacy/run.sh", "notes.md", "data/x.json"],
    }
    entries = rc._entries_for_app(manifest)
    patterns = {e.pattern for e in entries if e.kind == "exec"}
    assert patterns == {"legacy/run.sh"}


def test_infer_entries_reads_v7_arc_realized_files():
    """v7-arc instances have files=[] and realized_files=[{path, ...}].

    Verified pod-wide on the mini 2026-05-25 (Q6 audit, see spec
    §"Open question 6"): 100% of member-bot manifests use this shape.
    Without realized_files support the reconciler would emit an empty
    preview file pod-wide, which is the load-bearing bug this test
    pins against.
    """
    manifest = {
        "instance_id": "i-personal_bot-journal",
        "manifest_shape": "v7-arc",
        "schema_version": 14,
        "files": [],  # v7-arc instances leave this empty on disk
        "realized_files": [
            {
                "logical_name": "journal",
                "path": "scripts/journal.py",
                "file_id": "f-a3c7e291@2026.05.20-1.0",
                "marker_state": "OWNED",
                "created_in_session": "",
            },
            {
                "logical_name": "config",
                "path": "config/journal.json",  # data, not script
                "file_id": "f-deadbeef@2026.05.20-1.0",
                "marker_state": "OWNED",
                "created_in_session": "",
            },
        ],
    }
    entries = rc._entries_for_app(manifest)
    exec_entries = [e for e in entries if e.kind == "exec"]
    assert {e.pattern for e in exec_entries} == {"scripts/journal.py"}
    # app_id falls back to instance_id when ``id`` is absent (v7-arc shape).
    assert all(e.app_id == "i-personal_bot-journal" for e in exec_entries)


def test_infer_entries_v7_arc_with_only_documents_yields_no_exec():
    """A v7-arc app whose realized_files are all .md/.docx/.json must
    NOT produce any exec entries. Confirms the extension classifier
    rejects non-script paths even when the layer tag is absent."""
    manifest = {
        "instance_id": "i-game-design",
        "manifest_shape": "v7-arc",
        "schema_version": 14,
        "files": [],
        "realized_files": [
            {"path": "project_x/game-design/GAME_DESIGN_REVIEW.docx",
             "file_id": "f-1", "marker_state": "OWNED"},
            {"path": "project_x/game-design/GAME_DESIGN_REVIEW.md",
             "file_id": "f-2", "marker_state": "OWNED"},
            {"path": "project_x/game-design/data.json",
             "file_id": "f-3", "marker_state": "OWNED"},
        ],
    }
    entries = rc._entries_for_app(manifest)
    exec_entries = [e for e in entries if e.kind == "exec"]
    assert exec_entries == []


def test_infer_entries_emits_cron_and_script_for_each_cron():
    manifest = {
        "id": "i-cron",
        "name": "Cron App",
        "files": [],
        "crons": [
            {"schedule": "0 18 * * *", "script": "tools/tally.py"},
        ],
    }
    entries = rc._entries_for_app(manifest)
    kinds = {(e.kind, e.pattern) for e in entries}
    assert ("exec", "tools/tally.py") in kinds
    assert ("cron", "0 18 * * * tools/tally.py") in kinds


def test_explicit_permissions_block_adds_entries():
    manifest = {
        "id": "i-explicit",
        "name": "Explicit App",
        "files": [],
        "permissions": {
            "exec": ["sudo /usr/sbin/launchctl kickstart"],
            "fs_read": ["/Users/Shared/evolve/proposals/"],
            "network_egress": ["*.anthropic.com"],
            "env": ["ANTHROPIC_API_KEY"],
        },
    }
    entries = rc._entries_for_app(manifest)
    by_kind: dict[str, list[PermissionEntry]] = {}
    for e in entries:
        by_kind.setdefault(e.kind, []).append(e)

    assert {e.pattern for e in by_kind["exec"]} == {"sudo /usr/sbin/launchctl kickstart"}
    assert all(e.origin == rc.ORIGIN_EXPLICIT for e in by_kind["exec"])
    # fs / network / env are advisory in Phase A
    assert by_kind["fs_read"][0].advisory is True
    assert by_kind["network_egress"][0].advisory is True
    assert by_kind["env"][0].advisory is True
    # exec entries from a permissions block are NOT advisory (OC enforces exec)
    assert all(not e.advisory for e in by_kind["exec"])


def test_explicit_overrides_inferred_when_pattern_collides():
    """If files[] and permissions.exec both contain the same pattern, the
    explicit record wins (operator-authored rationale beats auto-inference)."""
    manifest = {
        "id": "i-collide",
        "name": "Collision",
        "files": [{"path": "scripts/foo.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/foo.py"]},
    }
    entries = rc._entries_for_app(manifest)
    exec_entries = [e for e in entries if e.kind == "exec"]
    assert len(exec_entries) == 1
    assert exec_entries[0].origin == rc.ORIGIN_EXPLICIT


def test_unknown_permissions_subkey_is_ignored():
    """Typo'd or future keys don't break inference."""
    manifest = {
        "id": "i-typo",
        "name": "Typo App",
        "files": [],
        "permissions": {
            "exec": ["python3 ok.py"],
            "_note": "manually edited 2026-05-24",
            "unknown_kind": ["something"],  # ignored
        },
    }
    entries = rc._entries_for_app(manifest)
    assert {e.pattern for e in entries if e.kind == "exec"} == {"python3 ok.py"}


# ── End-to-end: reconcile_bot_permissions ────────────────────────────────────


def test_reconcile_full_mode_writes_preview_for_member_bot(tmp_path):
    home = _make_bot_home(
        tmp_path, "team_bot_a",
        openclaw={"tools": {"exec": {"security": "full"}}},
        exec_approvals={"agents": {"main": {}}},
        manifests={
            "i-app1": {
                "id": "i-app1",
                "name": "App One",
                "files": [{"path": "scripts/run.py", "layer": "script"}],
            },
        },
    )
    result = reconcile_bot_permissions(
        "team_bot_a", network=_network("team_bot_a"), home_override=home,
    )
    assert result.skipped is False
    assert result.mode == "full"
    assert result.preview_written is True

    preview_path = home / ".openclaw" / PREVIEW_FILENAME
    assert preview_path.exists()
    payload = json.loads(preview_path.read_text())
    assert payload["mode"] == "full"
    assert payload["phase"] == "tracking"
    patterns = {e["pattern"] for e in payload["entries"]}
    assert "scripts/run.py" in patterns

    # Critical Phase A invariant: live exec-approvals.json untouched.
    ea_after = json.loads((home / ".openclaw" / "exec-approvals.json").read_text())
    assert ea_after == {"agents": {"main": {}}}


def test_reconcile_deny_mode_skips_preview_when_operator_pinned(tmp_path):
    """Post-Phase-E.4 the only way to land in ``deny`` mode is an explicit
    operator override via ``execPolicy: "deny"`` in network.json. When
    that's set, the reconciler should still skip the preview write.
    """
    home = _make_bot_home(
        tmp_path, "evolve",
        openclaw={"tools": {"exec": {"security": "deny"}}},
        exec_approvals={},
    )
    # Operator-pinned deny via execPolicy override.
    net = _network("evolve", role="primary")
    net["bots"]["evolve"]["execPolicy"] = "deny"
    result = reconcile_bot_permissions(
        "evolve", network=net, home_override=home,
    )
    assert result.mode == "deny"
    assert result.preview_written is False
    assert not (home / ".openclaw" / PREVIEW_FILENAME).exists()


def test_reconcile_primary_bot_no_longer_short_circuits_to_deny(tmp_path):
    """Phase E.4 regression test: an evo bot with role=primary and no
    explicit override should land at ``full`` (the default), NOT
    ``deny`` (the removed carve-out).
    """
    home = _make_bot_home(
        tmp_path, "evolve",
        openclaw={"tools": {"exec": {"security": "full"}}},
        exec_approvals={},
    )
    result = reconcile_bot_permissions(
        "evolve",
        network=_network("evolve", role="primary"),
        home_override=home,
    )
    assert result.mode == "full"


def test_reconcile_per_app_error_does_not_abort(tmp_path):
    home = _make_bot_home(
        tmp_path, "team_bot_a",
        openclaw={"tools": {"exec": {"security": "full"}}},
        exec_approvals={},
        manifests={
            "i-good": {
                "id": "i-good",
                "name": "Good",
                "files": [{"path": "tools/good.py", "layer": "script"}],
            },
        },
    )
    # Drop a malformed manifest alongside the good one.
    (home / ".openclaw" / "workspace" / "manifests" / "i-bad.json").write_text(
        "{ not valid json"
    )

    result = reconcile_bot_permissions(
        "team_bot_a", network=_network("team_bot_a"), home_override=home,
    )
    assert result.skipped is False
    assert result.preview_written is True
    assert len(result.per_app_errors) == 1
    assert result.per_app_errors[0]["manifest_file"] == "i-bad.json"

    payload = json.loads((home / ".openclaw" / PREVIEW_FILENAME).read_text())
    patterns = {e["pattern"] for e in payload["entries"]}
    assert "tools/good.py" in patterns


def test_reconcile_catastrophic_all_unreadable_skips_write(tmp_path):
    home = _make_bot_home(
        tmp_path, "team_bot_a",
        openclaw={"tools": {"exec": {"security": "full"}}},
        exec_approvals={},
    )
    # Two malformed manifests, no readable ones.
    (home / ".openclaw" / "workspace" / "manifests" / "i-a.json").write_text("not json")
    (home / ".openclaw" / "workspace" / "manifests" / "i-b.json").write_text("{[}")

    result = reconcile_bot_permissions(
        "team_bot_a", network=_network("team_bot_a"), home_override=home,
    )
    assert result.skipped is True
    assert "failed to read" in result.skip_reason.lower()
    assert not (home / ".openclaw" / PREVIEW_FILENAME).exists()


def test_reconcile_no_manifests_writes_empty_preview(tmp_path):
    """Brand-new bot with no scan yet — preview file is empty but written."""
    home = _make_bot_home(
        tmp_path, "newbie",
        openclaw={"tools": {"exec": {"security": "full"}}},
        exec_approvals={},
    )
    result = reconcile_bot_permissions(
        "newbie", network=_network("newbie"), home_override=home,
    )
    assert result.skipped is False
    assert result.preview_written is True
    payload = json.loads((home / ".openclaw" / PREVIEW_FILENAME).read_text())
    assert payload["entries"] == []


def test_reconcile_dry_run_does_not_write(tmp_path):
    home = _make_bot_home(
        tmp_path, "team_bot_a",
        openclaw={"tools": {"exec": {"security": "full"}}},
        exec_approvals={},
        manifests={
            "i-app": {
                "id": "i-app",
                "name": "App",
                "files": [{"path": "tools/x.py", "layer": "script"}],
            },
        },
    )
    result = reconcile_bot_permissions(
        "team_bot_a", network=_network("team_bot_a"), home_override=home, dry_run=True,
    )
    assert result.preview_written is False
    assert not (home / ".openclaw" / PREVIEW_FILENAME).exists()
    # But the in-memory entries are still populated for callers that want them.
    assert any(e.pattern == "tools/x.py" for e in result.entries)


def test_reconcile_provenance_tags_on_every_entry(tmp_path):
    home = _make_bot_home(
        tmp_path, "team_bot_a",
        openclaw={"tools": {"exec": {"security": "full"}}},
        exec_approvals={},
        manifests={
            "i-mix": {
                "id": "i-mix",
                "name": "Mixed",
                "files": [{"path": "tools/inferred.py", "layer": "script"}],
                "permissions": {"exec": ["explicit/cmd"]},
            },
        },
    )
    reconcile_bot_permissions("team_bot_a", network=_network("team_bot_a"), home_override=home)
    payload = json.loads((home / ".openclaw" / PREVIEW_FILENAME).read_text())
    by_pattern = {e["pattern"]: e for e in payload["entries"]}
    assert by_pattern["tools/inferred.py"]["origin"] == rc.ORIGIN_INFERRED
    assert by_pattern["tools/inferred.py"]["source"] == rc.SOURCE_APP_DERIVED
    assert by_pattern["tools/inferred.py"]["app_id"] == "i-mix"
    assert by_pattern["explicit/cmd"]["origin"] == rc.ORIGIN_EXPLICIT


def test_reconcile_skips_hidden_apps(tmp_path):
    home = _make_bot_home(
        tmp_path, "team_bot_a",
        openclaw={"tools": {"exec": {"security": "full"}}},
        exec_approvals={},
        manifests={
            "i-active": {
                "id": "i-active",
                "name": "Active",
                "status": "active",
                "files": [{"path": "tools/active.py", "layer": "script"}],
            },
            "i-hidden": {
                "id": "i-hidden",
                "name": "Hidden",
                "status": "hidden",
                "files": [{"path": "tools/hidden.py", "layer": "script"}],
            },
        },
    )
    reconcile_bot_permissions("team_bot_a", network=_network("team_bot_a"), home_override=home)
    payload = json.loads((home / ".openclaw" / PREVIEW_FILENAME).read_text())
    patterns = {e["pattern"] for e in payload["entries"]}
    assert "tools/active.py" in patterns
    assert "tools/hidden.py" not in patterns


def test_reconcile_allowlist_mode_phase_a_still_only_writes_preview(tmp_path):
    """Security_bot-shape: already in allowlist mode. Phase A never touches the
    live exec-approvals.json. Only the preview file is written."""
    home = _make_bot_home(
        tmp_path, "security_bot",
        openclaw={"tools": {"exec": {"security": "allowlist"}}},
        exec_approvals={"agents": {"main": {"allowlist": [{"pattern": "/usr/bin/curl"}]}}},
        manifests={
            "i-monitor": {
                "id": "i-monitor",
                "name": "Monitor",
                "files": [{"path": "scripts/probe.py", "layer": "script"}],
            },
        },
    )
    result = reconcile_bot_permissions(
        "security_bot", network=_network("security_bot"), home_override=home,
    )
    assert result.mode == "allowlist"
    assert result.enforced_write is False  # Phase A — never enforce
    assert result.preview_written is True

    # Live exec-approvals.json untouched.
    ea_after = json.loads((home / ".openclaw" / "exec-approvals.json").read_text())
    assert ea_after == {
        "agents": {"main": {"allowlist": [{"pattern": "/usr/bin/curl"}]}}
    }
