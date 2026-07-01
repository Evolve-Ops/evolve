"""Change detector tests — PR 6 of the coherence + reconciliation framework.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §7.6.

The detector decides whether a scan is warranted by diffing the current
workspace state against a stored snapshot. Tests pin each rule from
§7.6.2 (warrant triggers) plus the §7.6.7 failure modes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.change_detector import (  # noqa: E402
    DEFAULT_DISCOVERY_THRESHOLD,
    ScanDecision,
    detect,
    load_snapshot,
    read_decision,
    save_snapshot,
    take_snapshot,
    write_decision,
)


# ── Snapshot capture ──────────────────────────────────────────────────────

def test_take_snapshot_records_every_file_with_sha(tmp_path: Path) -> None:
    """Every regular file in the workspace gets a path + sha256 entry."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "main.py").write_text("hello")
    (tmp_path / "scripts" / "lib.py").write_text("world")
    snap = take_snapshot(tmp_path)
    assert "scripts/main.py" in snap["files"]
    assert "scripts/lib.py" in snap["files"]
    # SHAs are different — content differs.
    assert snap["files"]["scripts/main.py"] != snap["files"]["scripts/lib.py"]


def test_take_snapshot_records_cron_labels(tmp_path: Path) -> None:
    snap = take_snapshot(tmp_path, cron_labels=["x", "y", "x"])  # dedupe
    assert snap["cron_labels"] == ["x", "y"]


def test_take_snapshot_records_heartbeat_sha(tmp_path: Path) -> None:
    snap = take_snapshot(tmp_path, heartbeat_text="some heartbeat content")
    assert snap["heartbeat_sha"]   # 64-char hex string
    assert len(snap["heartbeat_sha"]) == 64


def test_take_snapshot_respects_exclude_globs(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "main.py").write_text("x")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "audit.log").write_text("y")
    snap = take_snapshot(tmp_path, exclude_globs=["logs/*"])
    assert "scripts/main.py" in snap["files"]
    assert "logs/audit.log" not in snap["files"]


def test_take_snapshot_caps_at_max_files(tmp_path: Path) -> None:
    """Pathological-case guard: don't snapshot 100k files."""
    (tmp_path / "data").mkdir()
    for i in range(20):
        (tmp_path / "data" / f"f{i}").write_text(str(i))
    snap = take_snapshot(tmp_path, max_files=5)
    assert len(snap["files"]) == 5


def test_take_snapshot_empty_workspace(tmp_path: Path) -> None:
    snap = take_snapshot(tmp_path)
    assert snap["files"] == {}
    assert snap["captured_at"]


# ── Snapshot persistence ──────────────────────────────────────────────────

def test_save_and_load_snapshot_round_trip(tmp_path: Path) -> None:
    """save_snapshot followed by load_snapshot returns the same dict."""
    snap = {"files": {"a.py": "abc"}, "cron_labels": ["x"], "heartbeat_sha": "h"}
    target = tmp_path / ".last_scan_snapshot.json"
    save_snapshot(snap, target)
    assert target.exists()
    loaded = load_snapshot(target)
    assert loaded == snap


def test_load_snapshot_returns_none_when_missing(tmp_path: Path) -> None:
    """First-run detection: no snapshot means None, which the detector
    treats as 'first run, force full discovery'."""
    assert load_snapshot(tmp_path / "nonexistent.json") is None


def test_load_snapshot_returns_none_on_corrupt_file(tmp_path: Path) -> None:
    """Snapshot file write got truncated → don't crash the detector;
    return None and the next pass treats it as first run."""
    p = tmp_path / "corrupt.json"
    p.write_text("not valid json {")
    assert load_snapshot(p) is None


def test_save_snapshot_atomic_via_rename(tmp_path: Path) -> None:
    """The save uses atomic rename — confirm the tmp file is gone after."""
    snap = {"files": {"a.py": "abc"}}
    target = tmp_path / "snap.json"
    save_snapshot(snap, target)
    assert target.exists()
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_save_snapshot_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "shared" / "applications" / "personal-bot" / ".snapshot.json"
    save_snapshot({}, target)
    assert target.exists()


# ── First-run case ────────────────────────────────────────────────────────

def test_detect_first_run_forces_full_discovery(tmp_path: Path) -> None:
    """Spec §7.6.7: 'Snapshot file missing or corrupted → detector
    treats as first run; recommends full discovery.'"""
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path,
        current_snapshot=current,
        prior_snapshot=None,
        manifests=[],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "full_discovery"
    assert "first_run_no_snapshot" in decision.reasons


# ── No changes case ───────────────────────────────────────────────────────

def test_detect_no_changes_returns_no_scan(tmp_path: Path) -> None:
    """Repeated snapshots with nothing changed → no scan warranted."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "main.py").write_text("x")
    prior = take_snapshot(tmp_path)
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path,
        current_snapshot=current,
        prior_snapshot=prior,
        manifests=[{"id": "app", "files": [{"path": "scripts/main.py", "layer": "code"}]}],
    )
    assert decision.scan_warranted is False
    assert decision.scope == "none"


# ── Rule 1: new file in trigger layer ─────────────────────────────────────

def test_detect_new_code_file_triggers_discovery(tmp_path: Path) -> None:
    """A new .py file that isn't in any manifest is a trigger-layer
    unattributable file → full_discovery."""
    prior = take_snapshot(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "new.py").write_text("x")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path,
        current_snapshot=current,
        prior_snapshot=prior,
        manifests=[],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "full_discovery"
    assert any("scripts/new.py" in r for r in decision.reasons)


def test_detect_new_config_file_triggers_discovery(tmp_path: Path) -> None:
    """config-layer unattributable file → discovery."""
    prior = take_snapshot(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "options.yaml").write_text("x")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path,
        current_snapshot=current,
        prior_snapshot=prior,
        manifests=[],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "full_discovery"


def test_detect_new_behavior_doc_triggers_discovery(tmp_path: Path) -> None:
    """A new HEARTBEAT.md, AGENTS.md, etc. is behavior_doc → discovery."""
    prior = take_snapshot(tmp_path)
    (tmp_path / "AGENTS.md").write_text("x")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path,
        current_snapshot=current,
        prior_snapshot=prior,
        manifests=[],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "full_discovery"


def test_detect_new_data_file_does_not_trigger_alone(tmp_path: Path) -> None:
    """A SINGLE new data-layer file does NOT trigger by rule 1
    (data isn't a trigger layer). Rule 2 needs ≥3 of them."""
    prior = take_snapshot(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "2026-06-06.md").write_text("today's journal")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path,
        current_snapshot=current,
        prior_snapshot=prior,
        manifests=[],
    )
    assert decision.scan_warranted is False


def test_detect_new_file_in_volatile_path_glob_skipped(tmp_path: Path) -> None:
    """Files matching a manifest's volatile_paths glob aren't
    unattributable — they're expected. Spec §7.6.2 non-trigger."""
    prior = take_snapshot(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "journal.md").write_text("x")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path,
        current_snapshot=current,
        prior_snapshot=prior,
        manifests=[{
            "id": "journal-app",
            "volatile_paths": [{"glob": "data/*.md", "layer": "data"}],
        }],
    )
    assert decision.scan_warranted is False


# ── Rule 2: ≥3 unattributable files → discovery ────────────────────────────

def test_detect_three_unattributable_files_triggers_discovery(tmp_path: Path) -> None:
    """The ≥3 threshold catches new-app-cluster cases even when no
    single file is in a trigger layer."""
    prior = take_snapshot(tmp_path)
    (tmp_path / "newapp").mkdir()
    for i in range(3):
        (tmp_path / "newapp" / f"file{i}.txt").write_text(str(i))
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path,
        current_snapshot=current,
        prior_snapshot=prior,
        manifests=[],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "full_discovery"
    # Reason cites the unattributable-files count.
    assert any("unattributable" in r for r in decision.reasons)


def test_detect_two_unattributable_files_below_threshold(tmp_path: Path) -> None:
    """Two un-claimed files in a non-trigger layer is below threshold."""
    prior = take_snapshot(tmp_path)
    (tmp_path / "newapp").mkdir()
    (tmp_path / "newapp" / "a.txt").write_text("a")
    (tmp_path / "newapp" / "b.txt").write_text("b")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path,
        current_snapshot=current,
        prior_snapshot=prior,
        manifests=[],
    )
    assert decision.scan_warranted is False


def test_detect_threshold_configurable(tmp_path: Path) -> None:
    """The discovery_threshold arg lets ops tune sensitivity per spec
    open question Q31."""
    prior = take_snapshot(tmp_path)
    (tmp_path / "newapp").mkdir()
    (tmp_path / "newapp" / "a.txt").write_text("a")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior, manifests=[],
        discovery_threshold=1,   # any unattributable file triggers
    )
    assert decision.scan_warranted is True


# ── Rule 3: sha drift on trigger-layer claimed file → targeted ────────────

def test_detect_sha_drift_on_code_file_triggers_targeted(tmp_path: Path) -> None:
    """Spec §7.6.2 rule 3: sha drift on code/config/contract file → scan."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "main.py").write_text("v1")
    prior = take_snapshot(tmp_path)
    # Change content → sha changes.
    (tmp_path / "scripts" / "main.py").write_text("v2 — changed")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior,
        manifests=[{"id": "myapp", "files": [
            {"path": "scripts/main.py", "layer": "code"}
        ]}],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "targeted"
    assert decision.targeted_apps == ["myapp"]
    assert any("sha_drift" in r for r in decision.reasons)


def test_detect_sha_drift_on_data_file_no_trigger(tmp_path: Path) -> None:
    """Sha drift on data layer is benign (data evolves by design)."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "journal.md").write_text("v1")
    prior = take_snapshot(tmp_path)
    (tmp_path / "data" / "journal.md").write_text("v2")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior,
        manifests=[{"id": "j", "files": [
            {"path": "data/journal.md", "layer": "data"}
        ]}],
    )
    assert decision.scan_warranted is False


def test_detect_sha_drift_on_contract_file_triggers_targeted(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{}")
    prior = take_snapshot(tmp_path)
    (tmp_path / "manifest.json").write_text("{\"changed\": true}")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior,
        manifests=[{"id": "myapp", "files": [
            {"path": "manifest.json", "layer": "contract"}
        ]}],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "targeted"


# ── Rule 4: new cron not claimed → discovery ──────────────────────────────

def test_detect_new_unclaimed_cron_triggers_discovery(tmp_path: Path) -> None:
    """Operator added a new cron outside the app framework → likely
    new app or operator wants it tracked."""
    prior = take_snapshot(tmp_path, cron_labels=["existing-cron"])
    current = take_snapshot(tmp_path, cron_labels=["existing-cron", "new-cron"])
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior,
        manifests=[{"id": "myapp", "crons": [
            {"label": "existing-cron", "schedule": "0 0 * * *"}
        ]}],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "full_discovery"
    assert any("new-cron" in r for r in decision.reasons)


def test_detect_new_cron_claimed_by_manifest_no_trigger(tmp_path: Path) -> None:
    """A new cron that IS in a manifest's crons[] isn't unattributable."""
    prior = take_snapshot(tmp_path, cron_labels=[])
    current = take_snapshot(tmp_path, cron_labels=["backup"])
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior,
        manifests=[{"id": "backupapp", "crons": [
            {"label": "backup", "schedule": "0 3 * * *"}
        ]}],
    )
    assert decision.scan_warranted is False


# ── Rule 5: heartbeat changed ────────────────────────────────────────────

def test_detect_heartbeat_change_triggers_discovery(tmp_path: Path) -> None:
    prior = take_snapshot(tmp_path, heartbeat_text="version 1")
    current = take_snapshot(tmp_path, heartbeat_text="version 2 - new section")
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior, manifests=[],
    )
    assert decision.scan_warranted is True
    assert "heartbeat_content_changed" in decision.reasons


def test_detect_heartbeat_unchanged_no_trigger(tmp_path: Path) -> None:
    prior = take_snapshot(tmp_path, heartbeat_text="same")
    current = take_snapshot(tmp_path, heartbeat_text="same")
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior, manifests=[],
    )
    assert decision.scan_warranted is False


# ── Rule 6: claimed code/config/contract file removed → targeted ──────────

def test_detect_claimed_code_file_removed_triggers_targeted(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "main.py").write_text("x")
    prior = take_snapshot(tmp_path)
    (tmp_path / "scripts" / "main.py").unlink()
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior,
        manifests=[{"id": "myapp", "files": [
            {"path": "scripts/main.py", "layer": "code"}
        ]}],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "targeted"
    assert decision.targeted_apps == ["myapp"]


def test_detect_unowned_file_removed_no_trigger(tmp_path: Path) -> None:
    """A removed file not claimed by any manifest → no finding (was
    probably never tracked)."""
    (tmp_path / "tmpfile.py").write_text("x")
    prior = take_snapshot(tmp_path)
    (tmp_path / "tmpfile.py").unlink()
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior, manifests=[],
    )
    assert decision.scan_warranted is False


# ── Decision file write/read ──────────────────────────────────────────────

def test_write_decision_creates_canonical_path(tmp_path: Path) -> None:
    """Spec §7.6.1: path is
    {shared_dir}/applications/{bot_id}/.detector_decision.json"""
    decision = ScanDecision(
        scan_warranted=True, scope="targeted",
        targeted_apps=["myapp"], reasons=["sha_drift"],
    )
    write_decision(decision, "personal-bot", tmp_path)
    target = tmp_path / "applications" / "personal-bot" / ".detector_decision.json"
    assert target.exists()


def test_read_decision_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_decision("personal-bot", tmp_path) is None


def test_decision_write_read_round_trip(tmp_path: Path) -> None:
    """save / load / save / load gives back the same decision."""
    decision = ScanDecision(
        scan_warranted=True, scope="full_discovery",
        targeted_apps=["a", "b"], reasons=["r1", "r2"],
        evidence={"x": 1},
    )
    write_decision(decision, "personal-bot", tmp_path)
    loaded = read_decision("personal-bot", tmp_path)
    assert loaded is not None
    assert loaded.scan_warranted is True
    assert loaded.scope == "full_discovery"
    assert loaded.targeted_apps == ["a", "b"]
    assert loaded.reasons == ["r1", "r2"]
    assert loaded.evidence == {"x": 1}


# ── Evidence shape ────────────────────────────────────────────────────────

def test_evidence_includes_change_counts(tmp_path: Path) -> None:
    """Evidence dict carries the diff counts so the UI/CLI can display
    "5 new files, 2 sha drifts" without re-running the detector."""
    (tmp_path / "old.py").write_text("x")
    prior = take_snapshot(tmp_path)
    (tmp_path / "old.py").write_text("y")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "new.py").write_text("z")
    current = take_snapshot(tmp_path)
    decision = detect(
        workspace_path=tmp_path, current_snapshot=current,
        prior_snapshot=prior, manifests=[],
    )
    assert decision.evidence["new_files"] == 1
    assert decision.evidence["drifted_files"] == 1


# ── Idempotency ────────────────────────────────────────────────────────────

def test_detect_idempotent_for_same_inputs(tmp_path: Path) -> None:
    """Same prior + same current + same manifests → same decision (modulo
    the decided_at timestamp)."""
    (tmp_path / "a.py").write_text("x")
    prior = take_snapshot(tmp_path)
    current = take_snapshot(tmp_path)
    d1 = detect(workspace_path=tmp_path, current_snapshot=current,
                prior_snapshot=prior, manifests=[])
    d2 = detect(workspace_path=tmp_path, current_snapshot=current,
                prior_snapshot=prior, manifests=[])
    assert d1.scan_warranted == d2.scan_warranted
    assert d1.scope == d2.scope
    assert d1.reasons == d2.reasons
    assert d1.targeted_apps == d2.targeted_apps


# ── Spec §7.6.7 failure modes ─────────────────────────────────────────────

def test_corrupted_snapshot_falls_to_first_run() -> None:
    """Spec §7.6.7: snapshot corrupted → treat as first run; recommend
    full_discovery. load_snapshot returning None triggers this."""
    decision = detect(
        workspace_path=Path("/nonexistent"),
        current_snapshot={"files": {}, "cron_labels": [],
                          "heartbeat_sha": ""},
        prior_snapshot=None,   # corrupted load returns None
        manifests=[],
    )
    assert decision.scan_warranted is True
    assert decision.scope == "full_discovery"
    assert "first_run_no_snapshot" in decision.reasons
